"""Zdraví domény — /health

Kontroluje:
  - Samba AD: dbcheck, replikace DC1↔DC2, čas (NTP drift)
  - PostgreSQL: primární/standby status, replikace lag
  - Pi-hole: dostupnost API
  - Prometheus/Grafana: HTTP ping
  - Certifikáty: dny do expirace
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/health")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


# --- Kontroly --------------------------------------------------------------

def _check_http(url: str, timeout: int = 5) -> dict:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return {"ok": r.status_code < 500, "status": r.status_code, "error": None}
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)[:80]}


def _run_cmd(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (r.stdout + r.stderr).strip()
        return r.returncode == 0, output[:500]
    except FileNotFoundError:
        return False, f"Příkaz nenalezen: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def _samba_checks() -> list[dict]:
    checks = []

    ok, out = _run_cmd(["samba-tool", "dbcheck", "--cross-ncs"])
    checks.append({
        "name": "Samba DB check",
        "ok": ok and "errors" not in out.lower(),
        "detail": out[:200] if out else "—",
    })

    ok, out = _run_cmd(["samba-tool", "drs", "showrepl"])
    checks.append({
        "name": "AD replikace",
        "ok": ok and "Failure" not in out and "error" not in out.lower(),
        "detail": _parse_repl_summary(out),
    })

    ok, out = _run_cmd(["timedatectl", "status"])
    synced = "synchronized: yes" in out.lower() or "sync'd" in out.lower()
    checks.append({
        "name": "NTP synchronizace",
        "ok": synced,
        "detail": _parse_ntp_drift(out),
    })

    return checks


def _parse_repl_summary(out: str) -> str:
    lines = [l for l in out.splitlines() if "Failure" in l or "DsReplicaNeighbor" in l]
    if not lines:
        return "OK — žádné chyby replikace"
    return "; ".join(lines[:3])


def _parse_ntp_drift(out: str) -> str:
    for line in out.splitlines():
        if "synchronized" in line.lower() or "offset" in line.lower() or "drift" in line.lower():
            return line.strip()
    return out[:100] if out else "—"


def _pg_checks(cfg) -> list[dict]:
    checks = []
    try:
        import psycopg
        pg = cfg.postgres
        dsn = f"postgresql://{pg.db_user}:{pg.db_password}@localhost/{pg.db_name}"
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            # Primární nebo standby?
            row = conn.execute("SELECT pg_is_in_recovery()").fetchone()
            is_standby = row[0] if row else None
            role = "Standby (hot standby)" if is_standby else "Primary"
            checks.append({"name": "PostgreSQL role", "ok": True, "detail": role})

            # Replikace lag (jen na primary)
            if not is_standby:
                row2 = conn.execute(
                    "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn, "
                    "pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes "
                    "FROM pg_stat_replication"
                ).fetchall()
                if row2:
                    replicas = [f"{r[0]}: lag {r[6]} B" for r in row2]
                    checks.append({
                        "name": "PostgreSQL replikace",
                        "ok": True,
                        "detail": "; ".join(replicas),
                    })
                else:
                    checks.append({
                        "name": "PostgreSQL replikace",
                        "ok": False,
                        "detail": "Žádný standby nepřipojen",
                    })
    except Exception as e:
        checks.append({"name": "PostgreSQL", "ok": False, "detail": str(e)[:120]})
    return checks


def _cert_checks() -> list[dict]:
    checks = []
    try:
        from ...db.models import Certificate, get_session
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_session() as session:
            certs = session.query(Certificate).filter(Certificate.not_after.isnot(None)).all()
            for c in certs:
                days = (c.not_after - now).days if c.not_after else None
                checks.append({
                    "name": f"Cert {c.hostname}:{c.port}",
                    "ok": days is not None and days > 14,
                    "detail": f"Expiruje za {days} dní ({c.not_after.strftime('%d.%m.%Y') if c.not_after else '?'})" if days is not None else "Nekontrolováno",
                })
    except Exception as e:
        checks.append({"name": "Certifikáty", "ok": False, "detail": str(e)[:100]})
    return checks


def _demo_checks(cfg) -> list[dict]:
    return [
        {"category": "Samba AD",     "name": "Samba DB check",       "ok": True,  "detail": "0 chyb"},
        {"category": "Samba AD",     "name": "AD replikace",         "ok": True,  "detail": "dc1 → dc2: OK, 0 selhání"},
        {"category": "Samba AD",     "name": "NTP synchronizace",    "ok": True,  "detail": "synchronized: yes, offset: +0.012s"},
        {"category": "PostgreSQL",   "name": "PostgreSQL role",      "ok": True,  "detail": "Primary"},
        {"category": "PostgreSQL",   "name": "PostgreSQL replikace", "ok": False, "detail": "Žádný standby nepřipojen (demo)"},
        {"category": "Pi-hole",      "name": "Pi-hole API",          "ok": True,  "detail": "HTTP 200, version: v6.0"},
        {"category": "Monitoring",   "name": "Prometheus",           "ok": True,  "detail": "HTTP 200"},
        {"category": "Monitoring",   "name": "Grafana",              "ok": True,  "detail": "HTTP 200"},
        {"category": "Certifikáty",  "name": "Cert dc1.demo.local",  "ok": True,  "detail": "Expiruje za 127 dní"},
    ]


@router.get("", response_class=HTMLResponse)
def health_dashboard(request: Request, user: str = Depends(_require_user)):
    cfg = request.app.state.config

    if _DEMO_MODE:
        checks = _demo_checks(cfg)
        return templates.TemplateResponse("health.html", {
            "request": request, "user": user, "checks": checks,
            "checked_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S"),
        })

    checks = []

    # Samba
    for c in _samba_checks():
        checks.append({"category": "Samba AD", **c})

    # HTTP services
    for name, url in [
        ("Pi-hole API", f"http://{cfg.servers.dc1.ip}:{cfg.pihole.web_port}/api/version"),
        ("Prometheus",  f"http://{cfg.servers.dc2.ip}:{cfg.monitoring.prometheus.port}/-/healthy"),
        ("Grafana",     f"http://{cfg.servers.dc2.ip}:{cfg.monitoring.grafana.port}/api/health"),
    ]:
        r = _check_http(url)
        cat = "Pi-hole" if "Pi-hole" in name else "Monitoring"
        checks.append({"category": cat, "name": name, "ok": r["ok"],
                        "detail": f"HTTP {r['status']}" if r["ok"] else r["error"]})

    # PostgreSQL
    for c in _pg_checks(cfg):
        checks.append({"category": "PostgreSQL", **c})

    # Certifikáty
    for c in _cert_checks():
        checks.append({"category": "Certifikáty", **c})

    return templates.TemplateResponse("health.html", {
        "request": request, "user": user, "checks": checks,
        "checked_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S"),
    })


@router.get("/refresh", response_class=HTMLResponse)
def health_refresh(request: Request, user: str = Depends(_require_user)):
    """HTMX fragment — jen tabulka kontrol bez celé stránky."""
    return health_dashboard(request, user)
