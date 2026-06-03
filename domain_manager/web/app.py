"""FastAPI aplikace pro Domain Manager.

Stack:
  - FastAPI - API + server-side rendering
  - Jinja2 - šablony
  - HTMX - interaktivita bez SPA složitosti
  - itsdangerous - signed session cookies

Záměrně NE React/Vue - HTMX pokryje 90% potřeb, je 10x jednodušší
na údržbu a deploy (žádný npm build, žádné node_modules).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from ._templates import templates as _shared_templates
from starlette.middleware.sessions import SessionMiddleware

from ..config import load_config, _demo_config

DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

try:
    cfg = load_config()
except FileNotFoundError:
    if DEMO_MODE:
        cfg = _demo_config()
    else:
        raise

app = FastAPI(title="Domain Manager", version="0.1.0")
app.state.demo_mode = DEMO_MODE
app.state.config = cfg

# V demo režimu inicializujeme DB ihned při startu (ne lazy) a naseedujeme data
if DEMO_MODE:
    from ..db.models import get_engine
    get_engine()  # vytvoří SQLite + create_all()
    from ._demo_seed import seed_demo_data
    seed_demo_data()

    # Demo playbooks: použít zapisovatelný adresář a předplnit příklady
    import shutil as _shutil
    DEMO_PLAYBOOKS_DIR = STATIC_DIR / "demo_playbooks"
    DEMO_PLAYBOOKS_DIR.mkdir(exist_ok=True)
    _EXAMPLES = PACKAGE_DIR.parent.parent / "examples" / "playbooks"
    if _EXAMPLES.exists():
        for _f in _EXAMPLES.glob("*.yml"):
            _dst = DEMO_PLAYBOOKS_DIR / _f.name
            if not _dst.exists():
                _shutil.copy2(_f, _dst)
    cfg.ansible.playbooks_path = DEMO_PLAYBOOKS_DIR

app.add_middleware(
    SessionMiddleware,
    secret_key=cfg.manager.secret_key,
    max_age=cfg.manager.session_timeout_minutes * 60,
    same_site="lax",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Uploads adresář — v demo módu static/uploads, jinak z configu
if DEMO_MODE:
    UPLOADS_DIR = STATIC_DIR / "uploads"
else:
    UPLOADS_DIR = Path(cfg.manager.uploads_dir)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.state.uploads_dir = UPLOADS_DIR
# Slouží pod /uploads/  (odděleně od /static/ aby nešly do VCS)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
templates = _shared_templates
templates.env.globals["demo_mode"] = DEMO_MODE


# --- Routes ----------------------------------------------------------------

from .routes import (  # noqa: E402
    auth, dashboard, computers, users, groups, plans, policies,
    ansible, dhcp, audit, pihole_ui, health, backup, snmp_devices, certs, help,
)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(computers.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(plans.router)
app.include_router(policies.router)
app.include_router(ansible.router)
app.include_router(dhcp.router)
app.include_router(audit.router)
app.include_router(pihole_ui.router)
app.include_router(health.router)
app.include_router(backup.router)
app.include_router(snmp_devices.router)
app.include_router(certs.router)
app.include_router(help.router)

# Spustit background notifikační checker (jen v produkci)
if not DEMO_MODE and cfg.notifications.enabled:
    from ..notifications.sender import start_background_checker
    start_background_checker(cfg)


# --- Globální exception handlery -------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> HTMLResponse:
    """Zachytí neošetřené výjimky (hlavně DB/psycopg chyby) a vrátí čitelnou stránku
    místo prázdné 500 Internal Server Error."""
    import logging
    log = logging.getLogger("dm.app")
    log.error("Neošetřená výjimka v %s: %s", request.url.path, exc, exc_info=exc)

    # Pokud uživatel není přihlášen, přesměruj na login bez ohledu na chybu
    if not request.session.get("user"):
        return RedirectResponse("/", status_code=303)

    # Jinak zobraz chybovou stránku s detailem
    err_short = str(exc).split("\n")[0][:200]
    err_type = type(exc).__name__
    html = f"""
    <html><head><title>Chyba — Domain Manager</title>
    <style>body{{font-family:monospace;background:#1a1a2e;color:#e2e8f0;padding:40px}}
    .box{{max-width:700px;margin:auto;background:#16213e;padding:32px;border-radius:8px;
    border-left:4px solid #e53e3e}}
    h2{{color:#fc8181;margin:0 0 16px}}
    code{{background:#0f3460;padding:8px 16px;border-radius:4px;display:block;
    white-space:pre-wrap;word-break:break-all;font-size:13px;margin:12px 0}}
    a{{color:#63b3ed}}</style></head>
    <body><div class="box">
    <h2>⚠ Chyba serveru ({err_type})</h2>
    <code>{err_short}</code>
    <p>Zkontrolujte stav služeb:<br>
    <code>systemctl status postgresql
systemctl status samba-ad-dc</code></p>
    <a href="/dashboard">← Zpět na dashboard</a> &nbsp;
    <a href="/logout">Odhlásit</a>
    </div></body></html>"""
    return HTMLResponse(content=html, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if DEMO_MODE and not request.session.get("user"):
        request.session["user"] = "demo"
    if not request.session.get("user"):
        return templates.TemplateResponse(request, "login.html")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "demo": DEMO_MODE}
