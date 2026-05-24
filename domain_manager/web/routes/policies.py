"""Editor politik — /policies.

Policy typy a jejich spec schema:
  firewall  : {rules: [{proto, port, action}], default_policy: drop|accept}
  pihole    : {blocklists: [url], allowlists: [url], regex_blocks: [pattern]}
  software  : {packages: [{name, state: present|absent}], pip: [...]}
  settings  : {key: value, ...}  (libovolné key/value)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...db.models import Group, Policy, PolicyAssignment, get_session
from .._audit import log_action

router = APIRouter(prefix="/policies")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

POLICY_KINDS = {
    "firewall": "Firewall pravidla",
    "pihole":   "Pi-hole filtrování",
    "software": "Softwarové balíčky",
    "settings": "Nastavení (key/value)",
}

# Výchozí spec pro každý typ politiky
_DEFAULT_SPECS = {
    "firewall": {"default_policy": "drop", "rules": []},
    "pihole":   {"blocklists": [], "allowlists": [], "regex_blocks": []},
    "software": {"packages": [], "pip": []},
    "settings": {},
}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def list_policies(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        policies = session.query(Policy).order_by(Policy.kind, Policy.name).all()
        # Počet přiřazení pro každou politiku
        assignment_counts = {}
        for p in policies:
            assignment_counts[p.id] = session.query(PolicyAssignment).filter(
                PolicyAssignment.policy_id == p.id
            ).count()

        data = [
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "kind_label": POLICY_KINDS.get(p.kind, p.kind),
                "description": p.description or "",
                "assignments": assignment_counts.get(p.id, 0),
                "created_at": p.created_at.strftime("%d.%m.%Y") if p.created_at else "",
            }
            for p in policies
        ]

    return templates.TemplateResponse("policies.html", {
        "request": request, "user": user,
        "policies": data,
        "kinds": POLICY_KINDS,
    })


@router.get("/new", response_class=HTMLResponse)
def policy_new(request: Request, user: str = Depends(_require_user)):
    kind = request.query_params.get("kind", "settings")
    default_spec = _DEFAULT_SPECS.get(kind, {})
    return templates.TemplateResponse("policy_detail.html", {
        "request": request, "user": user,
        "p": None,
        "kinds": POLICY_KINDS,
        "default_kind": kind,
        "spec_json": json.dumps(default_spec, indent=2, ensure_ascii=False),
        "groups": _groups_for_assignment(),
        "assignments": [],
    })


@router.post("", response_class=HTMLResponse)
async def policy_create(request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    kind = (form.get("kind") or "settings").strip()
    if not name:
        raise HTTPException(400, "Jméno politiky je povinné")
    if kind not in POLICY_KINDS:
        raise HTTPException(400, f"Neznámý typ: {kind}")

    spec_raw = form.get("spec_json") or "{}"
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Neplatný JSON ve spec: {e}")

    with get_session() as session:
        p = Policy(
            name=name,
            kind=kind,
            spec=spec,
            description=form.get("description") or None,
        )
        session.add(p)
        session.flush()
        new_id = p.id
        session.commit()

    log_action(user, "create_policy", "policy", new_id, {"name": name, "kind": kind})
    return RedirectResponse(f"/policies/{new_id}", status_code=303)


@router.get("/{policy_id}", response_class=HTMLResponse)
def policy_detail(policy_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        p = session.get(Policy, policy_id)
        if not p:
            raise HTTPException(404, "Politika nenalezena")

        assignments = (
            session.query(PolicyAssignment)
            .filter(PolicyAssignment.policy_id == policy_id)
            .all()
        )
        assignments_data = [
            {"id": a.id, "target_type": a.target_type, "target_id": a.target_id, "priority": a.priority}
            for a in assignments
        ]

        data = {
            "id": p.id, "name": p.name, "kind": p.kind,
            "kind_label": POLICY_KINDS.get(p.kind, p.kind),
            "description": p.description or "",
            "spec": p.spec,
            "created_at": p.created_at.strftime("%d.%m.%Y %H:%M") if p.created_at else "",
        }

    return templates.TemplateResponse("policy_detail.html", {
        "request": request, "user": user,
        "p": data,
        "kinds": POLICY_KINDS,
        "default_kind": p.kind,
        "spec_json": json.dumps(p.spec, indent=2, ensure_ascii=False),
        "groups": _groups_for_assignment(),
        "assignments": assignments_data,
    })


@router.post("/{policy_id}")
async def policy_update(policy_id: int, request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    spec_raw = form.get("spec_json") or "{}"
    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Neplatný JSON: {e}")

    with get_session() as session:
        p = session.get(Policy, policy_id)
        if not p:
            raise HTTPException(404)
        p.name = (form.get("name") or p.name).strip()
        p.description = form.get("description") or None
        p.spec = spec
        session.commit()

    log_action(user, "update_policy", "policy", policy_id, {"name": p.name})
    return RedirectResponse(f"/policies/{policy_id}", status_code=303)


@router.post("/{policy_id}/assign")
async def policy_assign(policy_id: int, request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    target_type = form.get("target_type")
    target_id = int(form.get("target_id") or 0)
    priority = int(form.get("priority") or 100)

    with get_session() as session:
        existing = (
            session.query(PolicyAssignment)
            .filter_by(policy_id=policy_id, target_type=target_type, target_id=target_id)
            .first()
        )
        if not existing:
            session.add(PolicyAssignment(
                policy_id=policy_id,
                target_type=target_type,
                target_id=target_id,
                priority=priority,
            ))
            session.commit()
    return RedirectResponse(f"/policies/{policy_id}", status_code=303)


@router.post("/{policy_id}/unassign/{assignment_id}")
def policy_unassign(policy_id: int, assignment_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        a = session.get(PolicyAssignment, assignment_id)
        if a:
            session.delete(a)
            session.commit()
    return RedirectResponse(f"/policies/{policy_id}", status_code=303)


@router.post("/{policy_id}/delete")
def policy_delete(policy_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        p = session.get(Policy, policy_id)
        if not p:
            raise HTTPException(404)
        session.query(PolicyAssignment).filter_by(policy_id=policy_id).delete()
        session.delete(p)
        session.commit()
    log_action(user, "delete_policy", "policy", policy_id)
    return RedirectResponse("/policies", status_code=303)


def _groups_for_assignment() -> dict:
    with get_session() as session:
        from ...db.models import Group
        groups = session.query(Group).order_by(Group.kind, Group.name).all()
        return {
            "computer": [{"id": g.id, "name": g.name} for g in groups if g.kind == "computer"],
            "user":     [{"id": g.id, "name": g.name} for g in groups if g.kind == "user"],
        }
