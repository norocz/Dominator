"""Správa SSL/TLS certifikátů — /certs"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Certificate, get_session
from .._audit import log_action

router = APIRouter(prefix="/certs")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def certs_list(request: Request, user: str = Depends(_require_user)):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        certs = session.query(Certificate).order_by(Certificate.hostname).all()
        data = [_cert_dict(c, now) for c in certs]

    return templates.TemplateResponse(request, "certs.html", {
        "user": user,
        "certs": data,
        "now": now.strftime("%d.%m.%Y %H:%M"),
    })


@router.post("")
async def cert_add(request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    hostname = (form.get("hostname") or "").strip()
    if not hostname:
        raise HTTPException(400, "Hostname je povinný")
    port = int(form.get("port") or 443)

    with get_session() as session:
        existing = session.query(Certificate).filter_by(hostname=hostname, port=port).first()
        if existing:
            raise HTTPException(400, f"{hostname}:{port} již sledován")
        c = Certificate(
            hostname=hostname, port=port,
            notes=form.get("notes") or None,
            created_by=user,
        )
        session.add(c)
        session.flush()
        new_id = c.id
        session.commit()

    log_action(user, "add_cert", "certificate", new_id, {"hostname": hostname, "port": port})
    # Hned zkontroluj
    _refresh_cert(new_id)
    return RedirectResponse("/certs", status_code=303)


@router.post("/{cert_id}/refresh", response_class=HTMLResponse)
def cert_refresh(cert_id: int, request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse('<span style="color:var(--warning);">Demo: kontrola certifikátu simulována</span>')
    _refresh_cert(cert_id)
    return RedirectResponse("/certs", status_code=303)


@router.post("/refresh-all", response_class=HTMLResponse)
def cert_refresh_all(request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse('<span style="color:var(--warning);">Demo: kontrola všech certifikátů simulována</span>')
    from ...certs.checker import check_all_certs
    results = check_all_certs()
    ok = sum(1 for r in results if not r.error)
    errors = sum(1 for r in results if r.error)
    color = "var(--success)" if not errors else "var(--warning)"
    return HTMLResponse(f'<span style="color:{color};">Zkontrolováno {ok} certifikátů, {errors} chyb</span>')


@router.post("/{cert_id}/delete")
def cert_delete(cert_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        c = session.get(Certificate, cert_id)
        if c:
            session.delete(c)
            session.commit()
    return RedirectResponse("/certs", status_code=303)


def _refresh_cert(cert_id: int) -> None:
    from ...certs.checker import fetch
    with get_session() as session:
        c = session.get(Certificate, cert_id)
        if not c:
            return
        info = fetch(c.hostname, c.port)
        c.subject_cn = info.subject_cn
        c.subject_san = info.subject_san
        c.issuer = info.issuer
        c.not_before = info.not_before
        c.not_after = info.not_after
        c.serial = info.serial
        c.fingerprint_sha256 = info.fingerprint_sha256
        c.check_error = info.error
        c.last_checked = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()


def _cert_dict(c: Certificate, now: datetime) -> dict:
    days = None
    status = "unknown"
    if c.not_after:
        days = (c.not_after - now).days
        if days < 0:
            status = "expired"
        elif days <= 14:
            status = "critical"
        elif days <= 30:
            status = "warning"
        else:
            status = "ok"
    return {
        "id": c.id,
        "hostname": c.hostname,
        "port": c.port,
        "subject_cn": c.subject_cn or "—",
        "issuer": c.issuer or "—",
        "not_before": c.not_before.strftime("%d.%m.%Y") if c.not_before else "—",
        "not_after": c.not_after.strftime("%d.%m.%Y") if c.not_after else "—",
        "days_until_expiry": days,
        "status": status,
        "fingerprint": (c.fingerprint_sha256 or "")[:23] + "…" if c.fingerprint_sha256 else "—",
        "last_checked": c.last_checked.strftime("%d.%m. %H:%M") if c.last_checked else "Nekontrolováno",
        "check_error": c.check_error,
        "notes": c.notes,
    }
