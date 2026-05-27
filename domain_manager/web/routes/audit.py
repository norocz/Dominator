"""Audit log UI — /audit"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...db.models import AuditEntry, get_session

router = APIRouter(prefix="/audit")
from .._templates import templates


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def audit_log(
    request: Request,
    user: str = Depends(_require_user),
    page: int = 1,
    per_page: int = 100,
    actor: str = "",
    action: str = "",
    target_type: str = "",
):
    with get_session() as session:
        q = session.query(AuditEntry).order_by(AuditEntry.ts.desc())
        if actor:
            q = q.filter(AuditEntry.actor == actor)
        if action:
            q = q.filter(AuditEntry.action.ilike(f"%{action}%"))
        if target_type:
            q = q.filter(AuditEntry.target_type == target_type)

        total = q.count()
        per_page = max(10, min(500, per_page))
        page = max(1, page)
        entries = q.offset((page - 1) * per_page).limit(per_page).all()

        actors = [r[0] for r in session.query(AuditEntry.actor).distinct().all() if r[0]]
        target_types = [r[0] for r in session.query(AuditEntry.target_type).distinct().filter(AuditEntry.target_type.isnot(None)).all()]

        data = [
            {
                "id": e.id,
                "ts": e.ts.strftime("%d.%m.%Y %H:%M:%S") if e.ts else "—",
                "actor": e.actor,
                "action": e.action,
                "target_type": e.target_type or "—",
                "target_id": e.target_id,
                "details": e.details or {},
            }
            for e in entries
        ]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse("audit.html", {
        "request": request,
        "user": user,
        "entries": data,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "actors": sorted(actors),
        "target_types": sorted(target_types),
        "filter_actor": actor,
        "filter_action": action,
        "filter_target_type": target_type,
    })
