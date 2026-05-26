"""Autentizace přes AD (LDAP bind jako uživatel).

V demo režimu (DM_DEMO=1) se přijmou jakékoliv přihlašovací údaje
a sezení se nastaví na uživatele "demo".

Oprávnění: přihlásit se mohou pouze uživatelé v AD skupině
'Domain Admins' nebo 'DM-Admins'. Ostatní platní AD uživatelé
dostanou chybu 'unauthorized'.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

# AD skupiny, jejichž členové mají přístup do webového rozhraní
_ALLOWED_GROUPS = frozenset({"domain admins", "dm-admins"})


def _ldap_escape(val: str) -> str:
    """Escapuje speciální znaky v hodnotě LDAP filtru (RFC 4515)."""
    return (
        val.replace("\\", "\\5c")
           .replace("*",  "\\2a")
           .replace("(",  "\\28")
           .replace(")",  "\\29")
           .replace("\x00", "\\00")
    )


def _is_in_allowed_group(member_of: list[str]) -> bool:
    """Vrátí True pokud alespoň jedno DN ze seznamu memberOf odpovídá povolené skupině."""
    for dn_str in member_of:
        for part in dn_str.split(","):
            part = part.strip()
            if part.lower().startswith("cn=") and part[3:].lower() in _ALLOWED_GROUPS:
                return True
    return False


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if _DEMO_MODE:
        request.session["user"] = username or "demo"
        return RedirectResponse("/", status_code=303)

    from ...config import load_config
    from ldap3 import Connection, Server, NTLM
    cfg = load_config()

    # Krok 1: ověření hesla přes NTLM bind
    try:
        server = Server(str(cfg.servers.dc1.ip))
        conn = Connection(
            server,
            user=f"{cfg.domain.netbios}\\{username}",
            password=password,
            authentication=NTLM,
            auto_bind=True,
        )
    except Exception:
        return RedirectResponse("/?error=invalid", status_code=303)

    # Krok 2: ověření členství ve skupině
    try:
        realm_parts = cfg.domain.realm.lower().split(".")
        base_dn = ",".join(f"DC={p}" for p in realm_parts)
        conn.search(base_dn, f"(sAMAccountName={_ldap_escape(username)})", attributes=["memberOf"])
        member_of: list[str] = []
        if conn.entries:
            raw = conn.entries[0].memberOf
            member_of = [str(v) for v in raw] if raw else []
    finally:
        conn.unbind()

    if not _is_in_allowed_group(member_of):
        return RedirectResponse("/?error=unauthorized", status_code=303)

    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
