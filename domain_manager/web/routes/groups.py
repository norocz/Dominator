"""Skupiny — přehled, CRUD AD skupin, internet blokování, CSV import."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Computer, ComputerGroupMembership, Group, get_session
from ...ansible.runner import AnsibleRunner
from .._audit import log_action

router = APIRouter(prefix="/groups")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
async def list_groups(request: Request, user: str = Depends(_require_user)):
    runner = AnsibleRunner(request.app.state.config)
    playbooks = runner.list_playbooks()

    with get_session() as session:
        groups = session.query(Group).order_by(Group.kind, Group.name).all()
        # Počet počítačů v každé skupině (jen computer groups)
        group_stats: dict[int, dict] = {}
        for g in groups:
            if g.kind == "computer":
                memberships = (
                    session.query(ComputerGroupMembership)
                    .filter(ComputerGroupMembership.group_id == g.id)
                    .all()
                )
                comp_ids = [m.computer_id for m in memberships]
                computers = (
                    session.query(Computer)
                    .filter(Computer.id.in_(comp_ids))
                    .all() if comp_ids else []
                )
                blocked_count = sum(1 for c in computers if c.internet_blocked)
                group_stats[g.id] = {
                    "computer_count": len(computers),
                    "blocked_count": blocked_count,
                }
        groups_data = [
            {
                "id": g.id,
                "name": g.name,
                "kind": g.kind,
                "description": g.description or "",
                **group_stats.get(g.id, {"computer_count": "—", "blocked_count": 0}),
            }
            for g in groups
        ]
    return templates.TemplateResponse("groups.html", {
        "request": request,
        "user": user,
        "groups": groups_data,
        "playbooks": playbooks,
    })


@router.post("", response_class=HTMLResponse)
async def create_group(
    request: Request,
    user: str = Depends(_require_user),
    name: str = Form(...),
    kind: str = Form(...),
    description: str = Form(""),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Název skupiny nesmí být prázdný")
    if kind not in ("user", "computer"):
        raise HTTPException(400, "Neplatný typ skupiny")

    with get_session() as session:
        existing = session.query(Group).filter(Group.name == name).first()
        if existing:
            return HTMLResponse(
                f'<div id="group-msg" style="color:var(--danger);padding:8px 0">'
                f'Skupina <strong>{name}</strong> už existuje.</div>'
            )
        g = Group(name=name, kind=kind, description=description or None)
        session.add(g)
        session.commit()
        session.refresh(g)
        group_id = g.id

    # Vytvořit skupinu i v AD (pouze v produkci a pokud je AD dostupné)
    ad_error = None
    if not _DEMO_MODE:
        try:
            from ...ad.client import ADClient
            cfg = request.app.state.config
            ad = ADClient(cfg)
            ad.create_group(name, description)
        except Exception as e:
            ad_error = str(e)

    log_action(user, "create_group", "group", group_id, {"name": name, "kind": kind})

    msg = f'Skupina <strong>{name}</strong> byla vytvořena.'
    if ad_error:
        msg += f' <span style="color:var(--warning)">(AD chyba: {ad_error[:80]})</span>'
    elif _DEMO_MODE:
        msg += ' <span style="color:var(--warning)">(Demo: uloženo pouze lokálně)</span>'

    return HTMLResponse(
        f'<div id="group-msg" style="color:var(--success);padding:8px 0">{msg}'
        f'</div><script>setTimeout(()=>location.reload(),800)</script>'
    )


@router.post("/{group_id}/run-playbook", response_class=HTMLResponse)
async def group_run_playbook(
    group_id: int,
    request: Request,
    user: str = Depends(_require_user),
    playbook: str = Form(...),
    extra_vars: str = Form(""),
    limit_override: str = Form(""),
):
    """Spustí zvolený Ansible playbook na všech počítačích ve skupině."""
    with get_session() as session:
        group = session.get(Group, group_id)
        if not group:
            raise HTTPException(404, "Skupina nenalezena")
        group_name = group.name
        if group.kind == "computer":
            memberships = (
                session.query(ComputerGroupMembership)
                .filter(ComputerGroupMembership.group_id == group_id)
                .all()
            )
            comp_ids = [m.computer_id for m in memberships]
            computers = (
                session.query(Computer).filter(Computer.id.in_(comp_ids)).all()
                if comp_ids else []
            )
            hostnames = [c.hostname for c in computers if c.hostname]
        else:
            hostnames = []

    limit = limit_override.strip() or (",".join(hostnames) if hostnames else "all")

    ev: dict | None = None
    if extra_vars.strip():
        import json as _json
        try:
            ev = _json.loads(extra_vars)
        except Exception:
            ev = {}
            for pair in extra_vars.split():
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    ev[k.strip()] = v.strip()
            if not ev:
                ev = None

    runner = AnsibleRunner(request.app.state.config)
    job_id = runner.start(playbook, limit=limit, extra_vars=ev, demo=_DEMO_MODE)
    log_action(user, "run_playbook_group", "group", group_id, {"playbook": playbook, "limit": limit})

    return HTMLResponse(
        f'<div style="padding:6px 0; font-size:13px;">'
        f'<span style="color:var(--success);">&#10003; Job spuštěn</span> — '
        f'<a href="/ansible?job={job_id}" style="color:var(--accent);">#{job_id} ({playbook})</a>'
        f'&nbsp;&nbsp;<a href="/ansible" style="color:var(--text-dim); font-size:11px;">Ansible výstupy →</a>'
        f'</div>'
    )


@router.post("/{group_id}/delete")
async def delete_group(
    group_id: int,
    request: Request,
    user: str = Depends(_require_user),
    delete_from_ad: str = Form("0"),
):
    with get_session() as session:
        g = session.get(Group, group_id)
        if not g:
            raise HTTPException(404, "Skupina nenalezena")
        group_name = g.name
        session.delete(g)
        session.commit()

    ad_error = None
    if delete_from_ad == "1" and not _DEMO_MODE:
        try:
            from ...ad.client import ADClient
            cfg = request.app.state.config
            ad = ADClient(cfg)
            ad.delete_group(group_name)
        except Exception as e:
            ad_error = str(e)

    log_action(user, "delete_group", "group", group_id, {"name": group_name, "from_ad": delete_from_ad == "1"})
    return RedirectResponse("/groups", status_code=303)


@router.post("/import-csv", response_class=HTMLResponse)
async def import_csv(
    request: Request,
    user: str = Depends(_require_user),
    file: UploadFile = File(...),
    sync_ad: str = Form("0"),
):
    """CSV formát: name,kind,description (záhlaví volitelné)."""
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # utf-8-sig odstraní BOM z Excel exportů
    except UnicodeDecodeError:
        text = content.decode("windows-1250", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    # Podpora CSV bez záhlaví: první sloupec = name, druhý = kind, třetí = description
    if reader.fieldnames and reader.fieldnames[0].lower() not in ("name", "název", "jméno"):
        # Záhlaví nerozpoznáno — tratujeme jako data bez záhlaví
        text_reset = io.StringIO(text)
        rows = list(csv.reader(text_reset))
        records = []
        for r in rows:
            if not r or not r[0].strip():
                continue
            records.append({
                "name": r[0].strip(),
                "kind": r[1].strip() if len(r) > 1 else "computer",
                "description": r[2].strip() if len(r) > 2 else "",
            })
    else:
        records = []
        for row in reader:
            name = (row.get("name") or row.get("název") or row.get("jméno") or "").strip()
            if not name:
                continue
            kind_raw = (row.get("kind") or row.get("typ") or "computer").strip().lower()
            kind = "user" if kind_raw in ("user", "uživatelé", "uzivatele", "u") else "computer"
            records.append({
                "name": name,
                "kind": kind,
                "description": (row.get("description") or row.get("popis") or "").strip(),
            })

    if not records:
        return HTMLResponse(
            '<div id="import-result" style="color:var(--danger)">CSV neobsahuje žádná platná data.</div>'
        )

    created, skipped, ad_errors = [], [], []
    ad_client = None
    if sync_ad == "1" and not _DEMO_MODE:
        try:
            from ...ad.client import ADClient
            cfg = request.app.state.config
            ad_client = ADClient(cfg)
        except Exception:
            pass

    with get_session() as session:
        for rec in records:
            existing = session.query(Group).filter(Group.name == rec["name"]).first()
            if existing:
                skipped.append(rec["name"])
                continue
            g = Group(name=rec["name"], kind=rec["kind"], description=rec["description"] or None)
            session.add(g)
            created.append(rec["name"])
        session.commit()

    if ad_client:
        for rec in [r for r in records if r["name"] in created]:
            try:
                ad_client.create_group(rec["name"], rec["description"])
            except Exception as e:
                ad_errors.append(f'{rec["name"]}: {e}')

    log_action(user, "import_groups_csv", "group", None, {"created": len(created), "skipped": len(skipped)})

    parts = [f'<strong>{len(created)}</strong> skupin vytvořeno']
    if skipped:
        parts.append(f'{len(skipped)} přeskočeno (již existují)')
    if ad_errors:
        parts.append(f'<span style="color:var(--warning)">AD chyby: {len(ad_errors)}</span>')
    msg = ", ".join(parts)

    return HTMLResponse(
        f'<div id="import-result" style="color:var(--success);padding:8px 0">{msg}.</div>'
        f'<script>setTimeout(()=>location.reload(),1200)</script>'
    )


@router.post("/{group_id}/internet-block", response_class=HTMLResponse)
async def group_internet_block(
    group_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    """Zablokuje internet pro všechny počítače ve skupině."""
    job_ids: list[str] = []
    blocked_hostnames: list[str] = []

    with get_session() as session:
        group = session.get(Group, group_id)
        if not group or group.kind != "computer":
            raise HTTPException(404, "Skupina nenalezena nebo není počítačová")

        memberships = (
            session.query(ComputerGroupMembership)
            .filter(ComputerGroupMembership.group_id == group_id)
            .all()
        )
        comp_ids = [m.computer_id for m in memberships]
        computers = (
            session.query(Computer).filter(Computer.id.in_(comp_ids)).all()
            if comp_ids else []
        )

        runner = AnsibleRunner(request.app.state.config)
        hostnames = [c.hostname for c in computers if c.hostname]

        if not _DEMO_MODE and hostnames:
            job_id = runner.start(
                "block_internet.yml",
                limit=",".join(hostnames),
                extra_vars={"group_name": group.name},
            )
            job_ids.append(job_id)
        elif _DEMO_MODE and hostnames:
            job_id = runner.start("block_internet.yml", limit=",".join(hostnames), demo=True)
            job_ids.append(job_id)

        for comp in computers:
            comp.internet_blocked = True
            comp.internet_block_source = "demo" if _DEMO_MODE else "ansible"
            comp.internet_block_job_id = job_ids[0] if job_ids else None
            comp.updated_by = user
            blocked_hostnames.append(comp.hostname or str(comp.id))

        session.commit()
        group_name = group.name

    return templates.TemplateResponse("group_internet_result.html", {
        "request": request,
        "group_id": group_id,
        "group_name": group_name,
        "action": "block",
        "hostnames": blocked_hostnames,
        "job_ids": job_ids,
    })


@router.post("/{group_id}/internet-unblock", response_class=HTMLResponse)
async def group_internet_unblock(
    group_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    """Odblokuje internet pro všechny počítače ve skupině."""
    job_ids: list[str] = []
    unblocked_hostnames: list[str] = []

    with get_session() as session:
        group = session.get(Group, group_id)
        if not group or group.kind != "computer":
            raise HTTPException(404)

        memberships = (
            session.query(ComputerGroupMembership)
            .filter(ComputerGroupMembership.group_id == group_id)
            .all()
        )
        comp_ids = [m.computer_id for m in memberships]
        computers = (
            session.query(Computer).filter(Computer.id.in_(comp_ids)).all()
            if comp_ids else []
        )

        runner = AnsibleRunner(request.app.state.config)
        hostnames = [c.hostname for c in computers if c.hostname]

        if not _DEMO_MODE and hostnames:
            job_id = runner.start("unblock_internet.yml", limit=",".join(hostnames))
            job_ids.append(job_id)
        elif _DEMO_MODE and hostnames:
            job_id = runner.start("unblock_internet.yml", limit=",".join(hostnames), demo=True)
            job_ids.append(job_id)

        for comp in computers:
            comp.internet_blocked = False
            comp.internet_block_source = None
            comp.internet_block_job_id = job_ids[0] if job_ids else None
            comp.updated_by = user
            unblocked_hostnames.append(comp.hostname or str(comp.id))

        session.commit()
        group_name = group.name

    return templates.TemplateResponse("group_internet_result.html", {
        "request": request,
        "group_id": group_id,
        "group_name": group_name,
        "action": "unblock",
        "hostnames": unblocked_hostnames,
        "job_ids": job_ids,
    })
