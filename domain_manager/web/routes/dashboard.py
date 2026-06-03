"""Dashboard — přehledová stránka s živými daty."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Computer, Group, User, get_session

router = APIRouter()
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(_require_user)):
    cfg = request.app.state.config

    db_error: str | None = None
    computer_count = online_count = user_count = group_count = blocked_count = 0
    recent_data: list[dict] = []

    try:
        with get_session() as session:
            computer_count = session.query(Computer).count()
            online_count = session.query(Computer).filter(Computer.is_online == True).count()  # noqa: E712
            user_count = session.query(User).count()
            group_count = session.query(Group).count()
            blocked_count = session.query(Computer).filter(Computer.internet_blocked == True).count()  # noqa: E712
            recent_online = (
                session.query(Computer)
                .filter(Computer.is_online == True)  # noqa: E712
                .order_by(Computer.last_seen.desc())
                .limit(5)
                .all()
            )
            recent_data = [
                {
                    "id": c.id,
                    "hostname": c.hostname,
                    "ip": c.ip_reserved or "—",
                    "cpu_pct": c.last_cpu_pct,
                    "ram_pct": c.last_ram_used_pct,
                    "disk_pct": c.last_disk_used_pct,
                    "last_seen": c.last_seen.strftime("%d.%m. %H:%M") if c.last_seen else "—",
                }
                for c in recent_online
            ]
    except Exception as exc:
        db_error = str(exc).split("\n")[0][:120]

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "db_error": db_error,
        "computer_count": computer_count,
        "online_count": online_count,
        "user_count": user_count,
        "group_count": group_count,
        "blocked_count": blocked_count,
        "recent_online": recent_data,
        "grafana_url": f"http://{cfg.servers.dc2.ip}:{cfg.monitoring.grafana.port}",
        "prometheus_url": f"http://{cfg.servers.dc2.ip}:{cfg.monitoring.prometheus.port}",
    })


@router.post("/api/prometheus/sync", response_class=HTMLResponse)
def prometheus_sync(request: Request, user: str = Depends(_require_user)):
    """Synchronizuje HW metriky z Prometheus do DB."""
    if _DEMO_MODE:
        return HTMLResponse(
            '<div style="color:var(--success);padding:8px;">'
            'Demo: synchronizace simulována (0 skutečných dat).</div>'
        )

    from ...prometheus.client import sync_to_db, PrometheusError
    with get_session() as session:
        result = sync_to_db(request.app.state.config, session)

    if result["errors"]:
        color = "var(--warning)"
        msg = f"Prometheus nedostupný nebo částečně: {'; '.join(result['errors'])}"
    else:
        color = "var(--success)"
        msg = (
            f"Synchronizováno {result['updated']} počítačů "
            f"({result['online']} online) "
            f"z {result.get('total_nodes', 0)} nodů."
        )

    return HTMLResponse(f'<div style="color:{color};padding:8px;">{msg}</div>')
