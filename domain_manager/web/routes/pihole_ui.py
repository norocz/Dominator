"""Pi-hole správa — /pihole"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/pihole")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

# Kurátorský výběr populárních blokujících seznamů
CURATED_LISTS = [
    {
        "category": "Reklamy",
        "lists": [
            {"name": "Steven Black (Unified Hosts)",
             "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
             "desc": "6M+ domén — nejoblíbenější, vyvážený"},
            {"name": "Hagezi Multi Light",
             "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/light.txt",
             "desc": "Rychlý, málo false-positive"},
            {"name": "Hagezi Multi Normal",
             "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/multi.txt",
             "desc": "Komplexní pokrytí reklam"},
            {"name": "OISD Basic",
             "url": "https://basic.oisd.nl/",
             "desc": "Konzervativní, bezpečný"},
            {"name": "OISD Full",
             "url": "https://full.oisd.nl/",
             "desc": "Agresivnější varianta OISD"},
            {"name": "AdGuard DNS filter",
             "url": "https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt",
             "desc": "AdGuard vlastní databáze"},
        ],
    },
    {
        "category": "Soukromí & sledování",
        "lists": [
            {"name": "EasyPrivacy",
             "url": "https://v.firebog.net/hosts/Easyprivacy.txt",
             "desc": "Tracking pixels, analytika"},
            {"name": "Disconnect.me Tracking",
             "url": "https://s3.amazonaws.com/lists.disconnect.me/simple_tracking.txt",
             "desc": "Sledovací sítě třetích stran"},
            {"name": "Hagezi Tracking Heavy",
             "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/trackers.txt",
             "desc": "Fingerprinty, telemetrie"},
        ],
    },
    {
        "category": "Malware & phishing",
        "lists": [
            {"name": "Phishing Army",
             "url": "https://phishing.army/download/phishing_army_blocklist.txt",
             "desc": "Phishingové a podvodné domény"},
            {"name": "Hagezi Threat Intelligence",
             "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/tif.txt",
             "desc": "Malware, C2, ransomware"},
            {"name": "URLhaus Malware",
             "url": "https://urlhaus-filter.pages.dev/urlhaus-filter-hosts.txt",
             "desc": "Abuse.ch URLhaus databáze"},
        ],
    },
    {
        "category": "CZ/SK lokální",
        "lists": [
            {"name": "EasyList Czech & Slovak",
             "url": "https://raw.githubusercontent.com/tomasko126/easylistczechandslovak/master/filters.txt",
             "desc": "Česko-slovenské reklamy a trackery"},
        ],
    },
]


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


def _demo_adlists() -> list[dict]:
    return [
        {"id": 1, "address": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
         "comment": "Steven Black Unified", "enabled": True, "domains": 6825177, "last_updated": "23.05.2026 08:12"},
        {"id": 2, "address": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/light.txt",
         "comment": "Hagezi Multi Light", "enabled": True, "domains": 382491, "last_updated": "23.05.2026 08:12"},
        {"id": 3, "address": "https://basic.oisd.nl/",
         "comment": "OISD Basic", "enabled": True, "domains": 198034, "last_updated": "23.05.2026 08:12"},
        {"id": 4, "address": "https://phishing.army/download/phishing_army_blocklist.txt",
         "comment": "Phishing Army", "enabled": False, "domains": 94211, "last_updated": "21.05.2026 14:30"},
    ]


def _demo_data() -> dict:
    return {
        "groups": [
            {"id": 0, "name": "default",       "enabled": True,  "clients": 18, "description": "Výchozí skupina"},
            {"id": 1, "name": "dm-blokováno",  "enabled": True,  "clients": 2,  "description": "Blokováno Dominatorem"},
            {"id": 2, "name": "pedagogové",    "enabled": True,  "clients": 12, "description": "Učitelský síťový segment"},
            {"id": 3, "name": "hosté",         "enabled": True,  "clients": 4,  "description": "Hostovská WiFi"},
        ],
        "clients": [
            {"ip": "192.168.10.101", "name": "pc-ucebna-01", "groups": ["dm-blokováno"], "blocked": True},
            {"ip": "192.168.10.102", "name": "pc-ucebna-02", "groups": ["dm-blokováno"], "blocked": True},
            {"ip": "192.168.10.110", "name": "pc-kancelar-01", "groups": ["default"], "blocked": False},
            {"ip": "192.168.10.10",  "name": "dc1",           "groups": ["default"], "blocked": False},
        ],
        "stats": {
            "total_queries": 14823,
            "blocked_queries": 2341,
            "blocked_pct": 15.8,
            "domains_blocked": 186432,
            "unique_clients": 18,
        },
        "adlists": _demo_adlists(),
    }


@router.get("", response_class=HTMLResponse)
async def pihole_overview(request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        data = _demo_data()
        return templates.TemplateResponse("pihole.html", {
            "request": request, "user": user, **data,
            "curated_lists": CURATED_LISTS, "error": None, "demo": True,
        })

    cfg = request.app.state.config
    from ...pihole.client import PiholeClient
    error = None
    groups, clients, stats, adlists = [], [], {}, []
    try:
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            raw_groups = await ph.list_groups()
            groups = [
                {
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "enabled": g.get("enabled", True),
                    "clients": 0,
                    "description": g.get("comment", ""),
                }
                for g in raw_groups
            ]

            # Klienti
            r = await ph._http.get(f"{ph.base_url}/clients", headers=ph._headers())
            raw_clients = r.json().get("clients", []) if r.status_code < 400 else []
            id_to_name = {g["id"]: g["name"] for g in raw_groups}
            for c in raw_clients:
                c_groups = [id_to_name.get(gid, str(gid)) for gid in (c.get("groups") or [])]
                clients.append({
                    "ip": c.get("client", ""),
                    "name": c.get("comment", ""),
                    "groups": c_groups,
                    "blocked": "dm-blokováno" in c_groups,
                })

            # Statistiky
            r2 = await ph._http.get(f"{ph.base_url}/stats/summary", headers=ph._headers())
            if r2.status_code < 400:
                s = r2.json()
                stats = {
                    "total_queries": s.get("queries", {}).get("total", 0),
                    "blocked_queries": s.get("queries", {}).get("blocked", 0),
                    "blocked_pct": s.get("queries", {}).get("percent_blocked", 0),
                    "domains_blocked": s.get("gravity", {}).get("domains_being_blocked", 0),
                    "unique_clients": s.get("clients", {}).get("total", 0),
                }

            # Adlisty
            raw_lists = await ph.list_adlists()
            import datetime as _dt
            for al in raw_lists:
                ts = al.get("date_updated") or al.get("date_added")
                try:
                    dt = _dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"
                except Exception:
                    dt = "—"
                adlists.append({
                    "id": al.get("id"),
                    "address": al.get("address", ""),
                    "comment": al.get("comment", ""),
                    "enabled": al.get("enabled", True),
                    "domains": al.get("number", 0),
                    "last_updated": dt,
                })
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse("pihole.html", {
        "request": request, "user": user,
        "groups": groups, "clients": clients, "stats": stats,
        "adlists": adlists, "curated_lists": CURATED_LISTS,
        "error": error, "demo": False,
    })


@router.post("/adlists")
async def adlist_add(
    request: Request,
    user: str = Depends(_require_user),
    url: str = Form(...),
    comment: str = Form(""),
):
    if not _DEMO_MODE:
        cfg = request.app.state.config
        from ...pihole.client import PiholeClient
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            await ph.add_adlist(url, comment)
    return RedirectResponse("/pihole", status_code=303)


@router.post("/adlists/bulk")
async def adlist_add_bulk(request: Request, user: str = Depends(_require_user)):
    """Přidá více předvolených adlistů najednou (checkboxy z formuláře)."""
    form = await request.form()
    urls = form.getlist("urls")
    if urls and not _DEMO_MODE:
        cfg = request.app.state.config
        from ...pihole.client import PiholeClient
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            existing = {al["address"] for al in await ph.list_adlists()}
            for url in urls:
                if url not in existing:
                    try:
                        await ph.add_adlist(url)
                    except Exception:
                        pass
    return RedirectResponse("/pihole", status_code=303)


@router.post("/adlists/{adlist_id}/delete", response_class=HTMLResponse)
async def adlist_delete(adlist_id: int, request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse('<tr style="display:none"></tr>')
    cfg = request.app.state.config
    from ...pihole.client import PiholeClient
    try:
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            await ph.delete_adlist(adlist_id)
    except Exception as e:
        return HTMLResponse(
            f'<tr><td colspan="5" style="color:var(--danger);padding:8px">Chyba: {e}</td></tr>'
        )
    return HTMLResponse('<tr style="display:none"></tr>')


@router.post("/adlists/{adlist_id}/toggle")
async def adlist_toggle(
    adlist_id: int,
    request: Request,
    user: str = Depends(_require_user),
    enabled: str = Form(...),
):
    new_state = enabled == "1"
    if not _DEMO_MODE:
        cfg = request.app.state.config
        from ...pihole.client import PiholeClient
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            await ph.toggle_adlist(adlist_id, new_state)
    return RedirectResponse("/pihole", status_code=303)


@router.post("/gravity", response_class=HTMLResponse)
async def gravity_update(request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse(
            '<div id="gravity-result" style="color:var(--warning);padding:8px 0">'
            'Demo: gravity aktualizace by byla spuštěna (trvá 1–5 min na produkci).</div>'
        )
    cfg = request.app.state.config
    from ...pihole.client import PiholeClient
    try:
        async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
            msg = await ph.update_gravity()
        return HTMLResponse(
            f'<div id="gravity-result" style="color:var(--success);padding:8px 0">{msg}</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div id="gravity-result" style="color:var(--danger);padding:8px 0">Chyba: {e}</div>'
        )


@router.post("/clients/{ip}/block", response_class=HTMLResponse)
async def block_client(ip: str, request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse(f'<span style="color:var(--warning);">Demo: {ip} by byl přidán do dm-blokováno</span>')
    from ...pihole.client import PiholeClient
    cfg = request.app.state.config
    async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
        await ph.block_client(ip)
    return RedirectResponse("/pihole", status_code=303)


@router.post("/clients/{ip}/unblock", response_class=HTMLResponse)
async def unblock_client(ip: str, request: Request, user: str = Depends(_require_user)):
    if _DEMO_MODE:
        return HTMLResponse(f'<span style="color:var(--success);">Demo: {ip} by byl odblokován</span>')
    from ...pihole.client import PiholeClient
    cfg = request.app.state.config
    async with PiholeClient(str(cfg.servers.dc1.ip), cfg.pihole.web_port, cfg.pihole.webpassword) as ph:
        await ph.unblock_client(ip)
    return RedirectResponse("/pihole", status_code=303)
