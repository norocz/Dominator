"""Administrace Ansible — seznam playbooků, editor, spuštění, real-time výstup.

Architektura:
  GET  /ansible                        přehled: playbook list + historie jobů
  GET  /ansible/playbooks/new          editor — nový playbook
  GET  /ansible/playbooks/{name}/edit  editor — existující playbook
  POST /ansible/playbooks/save         uložit playbook na disk
  POST /ansible/playbooks/{name}/delete smazat playbook
  POST /ansible/run                    spustí job (redirect na /ansible/jobs/{id})
  GET  /ansible/jobs/{id}              detail jobu (full stránka)
  GET  /ansible/jobs/{id}/output       HTMX fragment - výstup + status (polling)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...ansible.runner import AnsibleRunner
from .._audit import log_action

router = APIRouter(prefix="/ansible")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

_STARTER_TEMPLATE = """\
---
# Popis: Co tento playbook dělá
- name: Název playbooku
  hosts: all
  become: yes

  vars:
    # Definujte proměnné zde
    example_var: hodnota

  tasks:
    - name: Hello world
      ansible.builtin.debug:
        msg: "Spouštím na {{ inventory_hostname }}"
"""

_ALLOWED_SUFFIXES = {".yml", ".yaml"}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


def _runner(request: Request) -> AnsibleRunner:
    return AnsibleRunner(request.app.state.config)


@router.get("", response_class=HTMLResponse)
async def ansible_page(request: Request, user: str = Depends(_require_user)):
    runner = _runner(request)
    return templates.TemplateResponse("ansible.html", {
        "request": request,
        "user": user,
        "playbooks": runner.list_playbooks(),
        "groups": runner.list_groups(),
        "jobs": [j.as_dict() for j in runner.list_jobs()],
    })


@router.post("/run")
async def ansible_run(
    request: Request,
    user: str = Depends(_require_user),
    playbook: str = Form(...),
    limit: str = Form("all"),
):
    runner = _runner(request)
    job_id = runner.start(playbook, limit=limit, demo=_DEMO_MODE)
    return RedirectResponse(f"/ansible/jobs/{job_id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def ansible_job(
    job_id: str, request: Request, user: str = Depends(_require_user)
):
    runner = _runner(request)
    job = runner.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job nenalezen")
    return templates.TemplateResponse("ansible_job.html", {
        "request": request,
        "user": user,
        "job": job.as_dict(),
        "output": job.output_lines,
    })


@router.get("/playbooks/new", response_class=HTMLResponse)
def playbook_new(request: Request, user: str = Depends(_require_user)):
    return templates.TemplateResponse("ansible_editor.html", {
        "request": request, "user": user,
        "filename": "",
        "content": _STARTER_TEMPLATE,
        "is_new": True,
        "saved": False,
    })


@router.get("/playbooks/{name}/edit", response_class=HTMLResponse)
def playbook_edit(
    name: str, request: Request,
    user: str = Depends(_require_user),
    saved: int = 0,
):
    safe = Path(name).name
    if Path(safe).suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(400, "Neplatný typ souboru")
    runner = _runner(request)
    path = runner.playbooks_path / safe
    if not path.exists():
        raise HTTPException(404, f"Playbook {safe!r} nenalezen")
    return templates.TemplateResponse("ansible_editor.html", {
        "request": request, "user": user,
        "filename": safe,
        "content": path.read_text(encoding="utf-8"),
        "is_new": False,
        "saved": bool(saved),
    })


@router.post("/playbooks/save")
async def playbook_save(
    request: Request,
    user: str = Depends(_require_user),
    name: str = Form(...),
    content: str = Form(...),
):
    safe = Path(name.strip()).name
    if not safe:
        raise HTTPException(400, "Název playbooku nesmí být prázdný")
    if Path(safe).suffix not in _ALLOWED_SUFFIXES:
        safe += ".yml"
    runner = _runner(request)
    runner.playbooks_path.mkdir(parents=True, exist_ok=True)
    (runner.playbooks_path / safe).write_text(content, encoding="utf-8")
    log_action(user, "save_playbook", "playbook", None, {"name": safe})
    return RedirectResponse(f"/ansible/playbooks/{safe}/edit?saved=1", status_code=303)


@router.post("/playbooks/{name}/delete")
def playbook_delete(
    name: str, request: Request,
    user: str = Depends(_require_user),
):
    safe = Path(name).name
    runner = _runner(request)
    path = runner.playbooks_path / safe
    if path.exists():
        path.unlink()
    log_action(user, "delete_playbook", "playbook", None, {"name": safe})
    return RedirectResponse("/ansible", status_code=303)


@router.get("/jobs/{job_id}/output", response_class=HTMLResponse)
async def ansible_job_output(
    job_id: str, request: Request, user: str = Depends(_require_user)
):
    """HTMX fragment — výstup + status badge. Polling se zastaví sám když job skončí."""
    runner = _runner(request)
    job = runner.get_job(job_id)
    if not job:
        return HTMLResponse("<div>Job nenalezen</div>")
    return templates.TemplateResponse("ansible_output_fragment.html", {
        "request": request,
        "job": job.as_dict(),
        "output": job.output_lines,
    })
