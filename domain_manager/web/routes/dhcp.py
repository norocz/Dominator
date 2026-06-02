"""DHCP správa — přehled rezervací a aktivních lease z Kea Control Agent.

GET  /dhcp                   přehled rezervací + aktivních lease
POST /dhcp/reserve           přidá rezervaci (+ aktualizuje DB počítače)
POST /dhcp/reserve/{mac}/delete  smaže rezervaci
POST /dhcp/sync-to-db        načte rezervace z Kea a doplní DB počítačů
"""
from __future__ import annotations

import os
from pathlib import Path

from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Computer, get_session
from ...dhcp.client import KeaClient, KeaError, make_client

router = APIRouter(prefix="/dhcp")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


def _kea(request: Request) -> KeaClient:
    return make_client(request.app.state.config)


# --- přehled ---------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def dhcp_page(request: Request, user: str = Depends(_require_user)):
    cfg = request.app.state.config
    reservations: list[dict] = []
    leases: list[dict] = []
    stats: dict = {}
    error: str | None = None

    if _DEMO_MODE:
        reservations = [
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.10.50", "hostname": "pc-ucetni-01"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.10.51", "hostname": "pc-ucetni-02"},
            {"mac": "aa:bb:cc:dd:ee:03", "ip": "192.168.10.52", "hostname": "pc-it-01"},
        ]
        leases = [
            {"ip": "192.168.10.50", "mac": "aa:bb:cc:dd:ee:01",
             "hostname": "pc-ucetni-01", "state": "aktivní", "expires_at": "23.05.2026 08:00"},
            {"ip": "192.168.10.101", "mac": "ff:ee:dd:cc:bb:aa",
             "hostname": "notebook-hosty", "state": "aktivní", "expires_at": "22.05.2026 18:30"},
        ]
        stats = {"total": 100, "assigned": 12, "declined": 0}
    else:
        with _kea(request) as kea:
            try:
                reservations = [r.as_dict() for r in kea.list_reservations()]
                leases = [l.as_dict() for l in kea.list_leases()]
                stats = kea.stats()
            except KeaError as e:
                error = str(e)

    return templates.TemplateResponse(request, "dhcp.html", {
        "user": user,
        "reservations": reservations,
        "leases": leases,
        "stats": stats,
        "error": error,
        "pool_start": str(cfg.dhcp.pool_start),
        "pool_end": str(cfg.dhcp.pool_end),
        "subnet": cfg.network.subnet,
    })


# --- add rezervace ---------------------------------------------------------


@router.post("/reserve")
def dhcp_add_reservation(
    request: Request,
    user: str = Depends(_require_user),
    mac: str = Form(...),
    ip: str = Form(...),
    hostname: str = Form(...),
):
    if _DEMO_MODE:
        return RedirectResponse("/dhcp?demo=1", status_code=303)

    with _kea(request) as kea:
        try:
            kea.add_reservation(mac, ip, hostname)
        except KeaError as e:
            return RedirectResponse(f"/dhcp?error={_url_quote(str(e)[:120])}", status_code=303)

    # Pokud v DB existuje počítač s tímto hostname nebo MAC, doplníme ip_reserved
    with get_session() as session:
        comp = (
            session.query(Computer)
            .filter((Computer.hostname == hostname) | (Computer.mac == mac.lower()))
            .first()
        )
        if comp:
            comp.ip_reserved = ip
            if not comp.mac:
                comp.mac = mac.lower()
            comp.updated_by = user
            session.commit()

    return RedirectResponse("/dhcp", status_code=303)


# --- delete rezervace ------------------------------------------------------


@router.post("/reserve/{mac}/delete")
def dhcp_delete_reservation(
    mac: str,
    request: Request,
    user: str = Depends(_require_user),
):
    if _DEMO_MODE:
        return RedirectResponse("/dhcp?demo=1", status_code=303)

    with _kea(request) as kea:
        try:
            kea.delete_reservation(mac)
        except KeaError as e:
            return RedirectResponse(f"/dhcp?error={_url_quote(str(e)[:120])}", status_code=303)

    return RedirectResponse("/dhcp", status_code=303)


# --- sync Kea → DB ---------------------------------------------------------


@router.post("/sync-to-db", response_class=HTMLResponse)
def dhcp_sync(request: Request, user: str = Depends(_require_user)):
    """Přečte rezervace z Kea a doplní ip_reserved do DB počítačů."""
    if _DEMO_MODE:
        return HTMLResponse(
            '<div style="color: var(--warning); padding: 8px;">Demo: synchronizace simulována.</div>'
        )

    updated = 0
    errors: list[str] = []

    with _kea(request) as kea:
        try:
            reservations = kea.list_reservations()
        except KeaError as e:
            return HTMLResponse(
                f'<div style="color:var(--danger);padding:8px;">Kea nedostupná: {e}</div>'
            )

    with get_session() as session:
        for r in reservations:
            comp = (
                session.query(Computer)
                .filter(
                    (Computer.mac == r.mac.lower())
                    | (Computer.hostname == r.hostname)
                )
                .first()
            )
            if comp:
                comp.ip_reserved = r.ip
                if not comp.mac:
                    comp.mac = r.mac
                updated += 1
        try:
            session.commit()
        except Exception as e:
            errors.append(str(e))

    msg = f"Synchronizováno {updated} počítačů z {len(reservations)} rezervací."
    color = "var(--success)" if not errors else "var(--warning)"
    return HTMLResponse(
        f'<div style="color:{color};padding:8px;">{msg}'
        + ("".join(f"<br>⚠ {e}" for e in errors))
        + "</div>"
    )
