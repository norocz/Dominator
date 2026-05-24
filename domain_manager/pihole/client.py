"""Pi-hole FTL REST API klient (kompatibilní s Pi-hole v6+).

Pi-hole v6 povoluje per-client skupinové blokování přes skupiny adlistů.
POZOR na DNS topologii: pokud klienti dotazují DC (Samba port 53) a Samba
přeposílá do Pi-hole, Pi-hole vidí dotazy od DC, ne od klientů.
Per-client blokování funguje jen pokud klienti dotazují Pi-hole přímo.

Pro garantované blokování použijte Ansible modul (lokální firewall na klientovi).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx

log = logging.getLogger("dm.pihole")

# Skupina, do které přeřadíme blokované klienty.
# Musí existovat v Pi-hole + mít přiřazen agresivní adlist nebo regex blok.
_BLOCKED_GROUP = "dm-blokováno"
_DEFAULT_GROUP = "default"


class PiholeError(RuntimeError):
    pass


class PiholeClient:
    """Asynchronní klient pro Pi-hole FTL REST API.

    Použití:
        async with PiholeClient("192.168.10.10", 8081, "heslo") as client:
            await client.block_client("192.168.10.50")
    """

    def __init__(self, host: str, port: int, password: str):
        self.base_url = f"http://{host}:{port}/api"
        self.password = password
        self._sid: str | None = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PiholeClient":
        self._http = httpx.AsyncClient(timeout=10.0)
        await self._login()
        return self

    async def __aexit__(self, *_) -> None:
        await self._logout()
        if self._http:
            await self._http.aclose()

    # --- auth --------------------------------------------------------------

    async def _login(self) -> None:
        r = await self._http.post(f"{self.base_url}/auth", json={"password": self.password})
        self._check(r)
        self._sid = r.json()["session"]["sid"]
        log.debug("Pi-hole auth OK, sid=%s", self._sid[:8])

    async def _logout(self) -> None:
        if self._sid and self._http:
            try:
                await self._http.delete(
                    f"{self.base_url}/auth",
                    headers=self._headers(),
                )
            except Exception:
                pass
        self._sid = None

    def _headers(self) -> dict:
        return {"X-FTL-SID": self._sid} if self._sid else {}

    def _check(self, r: httpx.Response, context: str = "") -> dict:
        if r.status_code >= 400:
            raise PiholeError(
                f"Pi-hole API chyba {r.status_code}{' [' + context + ']' if context else ''}: {r.text[:200]}"
            )
        return r.json()

    # --- skupiny (groups) --------------------------------------------------

    async def list_groups(self) -> list[dict]:
        r = await self._http.get(f"{self.base_url}/groups", headers=self._headers())
        return self._check(r, "list_groups").get("groups", [])

    async def ensure_blocked_group(self) -> int:
        """Zajistí existenci skupiny 'dm-blokováno', vrátí její id."""
        groups = await self.list_groups()
        for g in groups:
            if g["name"] == _BLOCKED_GROUP:
                return g["id"]
        r = await self._http.post(
            f"{self.base_url}/groups",
            headers=self._headers(),
            json={"name": _BLOCKED_GROUP, "enabled": True,
                  "comment": "Blokováno Domain Managerem"},
        )
        data = self._check(r, "create_blocked_group")
        group_id = data["group"]["id"]
        log.info("Vytvořena Pi-hole skupina '%s' (id=%d)", _BLOCKED_GROUP, group_id)
        return group_id

    async def _get_group_ids(self, names: list[str]) -> list[int]:
        groups = await self.list_groups()
        name_to_id = {g["name"]: g["id"] for g in groups}
        return [name_to_id[n] for n in names if n in name_to_id]

    # --- klienti -----------------------------------------------------------

    async def get_client(self, ip: str) -> dict | None:
        r = await self._http.get(f"{self.base_url}/clients/{ip}", headers=self._headers())
        if r.status_code == 404:
            return None
        return self._check(r, f"get_client {ip}").get("client")

    async def block_client(self, ip: str) -> None:
        """Přiřadí klienta do skupiny 'dm-blokováno'. Odebere ho z 'default'."""
        blocked_id = await self.ensure_blocked_group()
        existing = await self.get_client(ip)
        if existing:
            r = await self._http.put(
                f"{self.base_url}/clients/{ip}",
                headers=self._headers(),
                json={"groups": [blocked_id], "comment": "internet blocked"},
            )
        else:
            r = await self._http.post(
                f"{self.base_url}/clients",
                headers=self._headers(),
                json={"client": ip, "groups": [blocked_id], "comment": "internet blocked"},
            )
        self._check(r, f"block_client {ip}")
        log.info("Pi-hole: %s přiřazen do skupiny '%s'", ip, _BLOCKED_GROUP)

    async def unblock_client(self, ip: str) -> None:
        """Přesune klienta zpět do skupiny 'default'."""
        default_ids = await self._get_group_ids([_DEFAULT_GROUP])
        existing = await self.get_client(ip)
        if existing:
            r = await self._http.put(
                f"{self.base_url}/clients/{ip}",
                headers=self._headers(),
                json={"groups": default_ids, "comment": ""},
            )
            self._check(r, f"unblock_client {ip}")
        log.info("Pi-hole: %s přesunut zpět do '%s'", ip, _DEFAULT_GROUP)

    async def is_blocked(self, ip: str) -> bool:
        client = await self.get_client(ip)
        if not client:
            return False
        groups = await self.list_groups()
        id_to_name = {g["id"]: g["name"] for g in groups}
        client_groups = [id_to_name.get(gid, "") for gid in (client.get("groups") or [])]
        return _BLOCKED_GROUP in client_groups


    # --- adlisty (gravity) -------------------------------------------------

    async def list_adlists(self) -> list[dict]:
        """Blokující adlisty (type=block)."""
        r = await self._http.get(f"{self.base_url}/lists?type=block", headers=self._headers())
        return self._check(r, "list_adlists").get("lists", [])

    async def add_adlist(self, url: str, comment: str = "") -> dict:
        r = await self._http.post(
            f"{self.base_url}/lists",
            headers=self._headers(),
            json={"address": url, "type": "block", "comment": comment, "enabled": True},
        )
        return self._check(r, "add_adlist")

    async def delete_adlist(self, adlist_id: int) -> None:
        r = await self._http.delete(
            f"{self.base_url}/lists/{adlist_id}",
            headers=self._headers(),
        )
        self._check(r, f"delete_adlist {adlist_id}")

    async def toggle_adlist(self, adlist_id: int, enabled: bool) -> None:
        r = await self._http.put(
            f"{self.base_url}/lists/{adlist_id}",
            headers=self._headers(),
            json={"enabled": enabled},
        )
        self._check(r, f"toggle_adlist {adlist_id}")

    async def update_gravity(self) -> str:
        """Spustí aktualizaci gravity databáze. Vrátí zprávu o výsledku."""
        r = await self._http.post(
            f"{self.base_url}/action/gravity",
            headers=self._headers(),
            timeout=120.0,
        )
        data = self._check(r, "update_gravity")
        return data.get("message", "Gravity aktualizována")


def make_client(cfg) -> PiholeClient:
    """Vytvoří klienta pro Pi-hole #1 (na DC1)."""
    return PiholeClient(
        host=str(cfg.servers.dc1.ip),
        port=cfg.pihole.web_port,
        password=cfg.pihole.webpassword,
    )
