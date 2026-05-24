"""Autentizace přes AD (LDAP bind jako uživatel).

V demo režimu (DM_DEMO=1) se přijmou jakékoliv přihlašovací údaje
a sezení se nastaví na uživatele "demo".
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if _DEMO_MODE:
        request.session["user"] = username or "demo"
        return RedirectResponse("/", status_code=303)

    from ...config import load_config
    from ldap3 import Connection, Server, NTLM
    cfg = load_config()
    try:
        server = Server(str(cfg.servers.dc1.ip))
        conn = Connection(
            server,
            user=f"{cfg.domain.netbios}\\{username}",
            password=password,
            authentication=NTLM,
            auto_bind=True,
        )
        # TODO: ověřit členství v 'Domain Admins' nebo vyhrazené 'DM-Admins'
        conn.unbind()
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    except Exception:
        return RedirectResponse("/?error=invalid", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
