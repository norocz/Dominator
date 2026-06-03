"""Autentizace přes AD (LDAP bind jako uživatel).

V demo režimu (DM_DEMO=1) se přijmou jakékoliv přihlašovací údaje
a sezení se nastaví na uživatele "demo".

Oprávnění: přihlásit se mohou pouze uživatelé v AD skupině
'Domain Admins' nebo 'DM-Admins'. Ostatní platní AD uživatelé
dostanou chybu 'unauthorized'.

Auth pořadí (fallback chain):
  1) LDAP NTLM bind  (domain\\user)
  2) LDAP SIMPLE bind (user@realm — UPN, funguje na starších ldap3/Samba)
  3) config password fallback (pouze pro Administrator — rychlý bootstrap)
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
log = logging.getLogger("dm.auth")

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


def _try_ldap_bind(dc_ip: str, user: str, password: str, auth_type) -> object | None:
    """Pokusí se o LDAP bind. Vrátí Connection nebo None."""
    try:
        from ldap3 import Connection, Server
        server = Server(dc_ip, connect_timeout=5)
        conn = Connection(server, user=user, password=password, authentication=auth_type, auto_bind=True)
        return conn
    except Exception as e:
        log.debug("LDAP bind selhání (%s %s): %s", auth_type, user, e)
        return None


def _get_member_of(dc_ip: str, admin_user: str, admin_pass: str, username: str, base_dn: str) -> list[str]:
    """Načte memberOf pro uživatele pomocí admin bind. Vrátí seznam DN."""
    try:
        from ldap3 import Connection, Server, NTLM, SIMPLE, ALL_ATTRIBUTES
        for auth, usr in [
            ("NTLM", admin_user),
            ("SIMPLE", admin_user.split("\\")[-1] + "@" + base_dn.replace(",DC=", ".").replace("DC=", "")),
        ]:
            try:
                from ldap3 import NTLM as _NTLM, SIMPLE as _SIMPLE
                a = _NTLM if auth == "NTLM" else _SIMPLE
                conn = Connection(Server(dc_ip, connect_timeout=5), user=(admin_user if auth == "NTLM" else usr),
                                  password=admin_pass, authentication=a, auto_bind=True)
                conn.search(base_dn, f"(sAMAccountName={_ldap_escape(username)})", attributes=["memberOf"])
                if conn.entries:
                    raw = conn.entries[0].memberOf
                    result = [str(v) for v in raw] if raw else []
                    conn.unbind()
                    return result
                conn.unbind()
            except Exception:
                continue
    except Exception as e:
        log.warning("memberOf lookup selhal: %s", e)
    return []


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if _DEMO_MODE:
        request.session["user"] = username or "demo"
        return RedirectResponse("/", status_code=303)

    from ...config import load_config
    from ldap3 import NTLM, SIMPLE
    cfg = load_config()

    dc_ip = str(cfg.servers.dc1.ip)
    realm_lower = cfg.domain.realm.lower()
    realm_parts = realm_lower.split(".")
    base_dn = ",".join(f"DC={p}" for p in realm_parts)
    netbios = cfg.domain.netbios

    conn = None
    auth_method = None

    # Metoda 1: NTLM bind (domain\user)
    conn = _try_ldap_bind(dc_ip, f"{netbios}\\{username}", password, NTLM)
    if conn:
        auth_method = "ntlm"

    # Metoda 2: SIMPLE bind s UPN (user@domain)
    if not conn:
        conn = _try_ldap_bind(dc_ip, f"{username}@{realm_lower}", password, SIMPLE)
        if conn:
            auth_method = "simple_upn"

    # Metoda 3: config-password fallback (jen pro Administrator, bootstrap)
    if not conn and username.lower() == "administrator" and password == cfg.domain.admin_password:
        log.info("Login: config-password fallback pro Administrator")
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)

    if not conn:
        log.warning("Login: všechny metody selhaly pro uživatele '%s'", username)
        return RedirectResponse("/?error=invalid", status_code=303)

    if auth_method == "ntlm":
        conn.unbind()

    # Ověření skupiny přes admin bind (nezávisí na auth metodě uživatele)
    member_of = _get_member_of(dc_ip, f"{netbios}\\Administrator", cfg.domain.admin_password, username, base_dn)

    if not _is_in_allowed_group(member_of):
        log.warning("Login: '%s' není v povolené skupině (memberOf=%s)", username, member_of[:2])
        return RedirectResponse("/?error=unauthorized", status_code=303)

    request.session["user"] = username
    log.info("Login: '%s' přihlášen (%s)", username, auth_method)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
