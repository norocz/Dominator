"""SNMP zařízení — /snmp

Správa síťových zařízení (switche, routery, AP) přes SNMP.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import NetworkDevice, get_session
from .._audit import log_action

router = APIRouter(prefix="/snmp")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

DEVICE_TYPES = {
    "switch":   "Switch",
    "router":   "Router",
    "ap":       "Access Point",
    "firewall": "Firewall",
    "other":    "Jiné",
}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def snmp_list(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        devices = session.query(NetworkDevice).order_by(NetworkDevice.device_type, NetworkDevice.hostname).all()
        data = [_device_dict(d) for d in devices]

    return templates.TemplateResponse(request, "snmp.html", {
        "user": user,
        "devices": data,
        "device_types": DEVICE_TYPES,
    })


@router.get("/new", response_class=HTMLResponse)
def snmp_new(request: Request, user: str = Depends(_require_user)):
    return templates.TemplateResponse(request, "snmp_detail.html", {
        "user": user,
        "d": None,
        "device_types": DEVICE_TYPES,
        "port_stats": {},
        "mac_table": {},
    })


@router.post("")
async def snmp_create(request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    hostname = (form.get("hostname") or "").strip()
    if not hostname:
        raise HTTPException(400, "Hostname je povinný")

    with get_session() as session:
        d = NetworkDevice(
            hostname=hostname,
            ip=(form.get("ip") or "").strip(),
            community=form.get("community") or "public",
            snmp_version=form.get("snmp_version") or "2c",
            device_type=form.get("device_type") or "switch",
            manufacturer=form.get("manufacturer") or None,
            model=form.get("model") or None,
            location=form.get("location") or None,
            notes=form.get("notes") or None,
            created_by=user,
        )
        session.add(d)
        session.flush()
        new_id = d.id
        session.commit()

    log_action(user, "create_network_device", "network_device", new_id, {"hostname": hostname})
    return RedirectResponse(f"/snmp/{new_id}", status_code=303)


@router.get("/{device_id}", response_class=HTMLResponse)
def snmp_detail(device_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        d = session.get(NetworkDevice, device_id)
        if not d:
            raise HTTPException(404)
        data = _device_dict(d, full=True)

    return templates.TemplateResponse(request, "snmp_detail.html", {
        "user": user,
        "d": data,
        "device_types": DEVICE_TYPES,
        "port_stats": data.get("port_stats") or {},
        "mac_table": data.get("connected_macs") or {},
    })


@router.post("/{device_id}")
async def snmp_update(device_id: int, request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    with get_session() as session:
        d = session.get(NetworkDevice, device_id)
        if not d:
            raise HTTPException(404)
        for field in ("ip", "community", "snmp_version", "device_type", "manufacturer", "model", "location", "notes"):
            if field in form:
                setattr(d, field, form[field] or None)
        d.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
    return RedirectResponse(f"/snmp/{device_id}", status_code=303)


@router.post("/{device_id}/sync", response_class=HTMLResponse)
def snmp_sync(device_id: int, request: Request, user: str = Depends(_require_user)):
    """Synchronizuje data ze zařízení přes SNMP."""
    if _DEMO_MODE:
        return HTMLResponse('<span style="color:var(--warning);">Demo: SNMP sync simulován</span>')

    with get_session() as session:
        d = session.get(NetworkDevice, device_id)
        if not d:
            raise HTTPException(404)

        from ...snmp.client import sync_device
        info = sync_device(d.ip, d.community, d.snmp_version)

        d.sys_name = info.sys_name
        d.sys_description = info.sys_description
        d.sys_uptime_seconds = info.sys_uptime_seconds
        d.port_stats = {str(k): vars(v) for k, v in info.ports.items()}
        d.connected_macs = info.mac_table
        d.last_sync = datetime.now(timezone.utc).replace(tzinfo=None)
        d.sync_error = info.sync_error
        session.commit()

    if info.sync_error:
        return HTMLResponse(f'<span style="color:var(--danger);">Chyba: {info.sync_error}</span>')
    return HTMLResponse(f'<span style="color:var(--success);">Synchronizováno: {len(info.ports)} portů, {len(info.mac_table)} MAC adres</span>')


@router.post("/{device_id}/delete")
def snmp_delete(device_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        d = session.get(NetworkDevice, device_id)
        if d:
            session.delete(d)
            session.commit()
    return RedirectResponse("/snmp", status_code=303)


def _device_dict(d: NetworkDevice, *, full: bool = False) -> dict:
    base = {
        "id": d.id,
        "hostname": d.hostname,
        "ip": d.ip,
        "device_type": d.device_type,
        "type_label": DEVICE_TYPES.get(d.device_type or "other", "Jiné"),
        "manufacturer": d.manufacturer,
        "model": d.model,
        "location": d.location,
        "sys_name": d.sys_name,
        "sys_uptime_seconds": d.sys_uptime_seconds,
        "port_count": len(d.port_stats or {}),
        "mac_count": len(d.connected_macs or {}),
        "last_sync": d.last_sync.strftime("%d.%m.%Y %H:%M") if d.last_sync else None,
        "sync_error": d.sync_error,
    }
    if full:
        base.update({
            "community": d.community,
            "snmp_version": d.snmp_version,
            "sys_description": d.sys_description,
            "port_stats": d.port_stats,
            "connected_macs": d.connected_macs,
            "vlans": d.vlans,
            "notes": d.notes,
            "created_at": d.created_at.strftime("%d.%m.%Y") if d.created_at else None,
        })
    return base
