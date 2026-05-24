"""Kea DHCPv4 Control Agent API klient.

Kea Control Agent přijímá POST požadavky na http://host:8001/ s JSON tělem:
  {"command": "<cmd>", "service": ["dhcp4"], "arguments": {...}}

Odpovídá:
  [{"result": 0, "text": "...", "arguments": {...}}]
  result=0 je úspěch, ostatní jsou chyby.

Používá synchronní httpx — vhodné pro management UI (nízký provoz).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger("dm.dhcp")

_SUBNET_ID = 1  # zatím vždy subnet 1


class KeaError(RuntimeError):
    pass


@dataclass
class Reservation:
    mac: str
    ip: str
    hostname: str
    subnet_id: int = _SUBNET_ID

    def as_dict(self) -> dict:
        return {"mac": self.mac, "ip": self.ip, "hostname": self.hostname}


@dataclass
class Lease:
    ip: str
    mac: str
    hostname: str
    state: int          # 0=active, 1=declined, 2=expired-reclaimed
    expire: int | None  # unix timestamp

    def state_label(self) -> str:
        return {0: "aktivní", 1: "odmítnutá", 2: "prošlá"}.get(self.state, str(self.state))

    def expires_at(self) -> str | None:
        if not self.expire:
            return None
        try:
            return datetime.fromtimestamp(self.expire).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return str(self.expire)

    def as_dict(self) -> dict:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "hostname": self.hostname or "—",
            "state": self.state_label(),
            "expires_at": self.expires_at(),
        }


class KeaClient:
    """Synchronní klient pro Kea Control Agent REST API."""

    def __init__(self, host: str, port: int = 8001):
        self.url = f"http://{host}:{port}/"
        self._http = httpx.Client(timeout=8.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KeaClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # --- nízkoúrovňový transport ------------------------------------------

    def _cmd(self, command: str, arguments: dict | None = None) -> dict:
        payload: dict = {"command": command, "service": ["dhcp4"]}
        if arguments is not None:
            payload["arguments"] = arguments
        try:
            r = self._http.post(self.url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise KeaError(f"Kea Control Agent nedostupný ({self.url}): {e}") from e

        resp = r.json()
        if isinstance(resp, list):
            resp = resp[0]
        result = resp.get("result", -1)
        if result != 0:
            raise KeaError(f"Kea [{command}] chyba result={result}: {resp.get('text', '?')}")
        return resp.get("arguments") or {}

    # --- rezervace ---------------------------------------------------------

    def list_reservations(self) -> list[Reservation]:
        try:
            data = self._cmd("reservation-get-all", {"subnet-id": _SUBNET_ID})
        except KeaError:
            return []
        return [
            Reservation(
                mac=h.get("hw-address", ""),
                ip=h.get("ip-address", ""),
                hostname=h.get("hostname", ""),
            )
            for h in data.get("hosts", [])
        ]

    def add_reservation(self, mac: str, ip: str, hostname: str) -> None:
        self._cmd("reservation-add", {
            "reservation": {
                "subnet-id": _SUBNET_ID,
                "hw-address": mac.lower(),
                "ip-address": ip,
                "hostname": hostname,
            }
        })
        log.info("Kea: rezervace přidána %s → %s (%s)", mac, ip, hostname)

    def update_reservation(self, mac: str, ip: str, hostname: str) -> None:
        """Kea nemá update — smaž a znovu přidej."""
        try:
            self.delete_reservation(mac)
        except KeaError:
            pass
        self.add_reservation(mac, ip, hostname)

    def delete_reservation(self, mac: str) -> None:
        self._cmd("reservation-del", {
            "subnet-id": _SUBNET_ID,
            "identifier-type": "hw-address",
            "identifier": mac.lower(),
        })
        log.info("Kea: rezervace smazána %s", mac)

    # --- leases ------------------------------------------------------------

    def list_leases(self) -> list[Lease]:
        try:
            data = self._cmd("lease4-get-all")
        except KeaError:
            return []
        return [
            Lease(
                ip=l.get("ip-address", ""),
                mac=l.get("hw-address", ""),
                hostname=l.get("hostname", ""),
                state=l.get("state", 0),
                expire=l.get("expire"),
            )
            for l in data.get("leases", [])
        ]

    def get_lease(self, ip: str) -> Lease | None:
        try:
            data = self._cmd("lease4-get", {"ip-address": ip})
        except KeaError:
            return None
        if not data:
            return None
        return Lease(
            ip=data.get("ip-address", ip),
            mac=data.get("hw-address", ""),
            hostname=data.get("hostname", ""),
            state=data.get("state", 0),
            expire=data.get("expire"),
        )

    # --- statistiky --------------------------------------------------------

    def stats(self) -> dict:
        """Základní statistiky DHCP poolu."""
        try:
            data = self._cmd("stat-lease4-get")
            rows = data.get("result-set", {}).get("rows", [])
            if rows:
                cols = data["result-set"]["columns"]
                row = dict(zip(cols, rows[0]))
                return {
                    "total": row.get("total-addresses", 0),
                    "assigned": row.get("assigned-addresses", 0),
                    "declined": row.get("declined-addresses", 0),
                }
        except KeaError:
            pass
        return {"total": 0, "assigned": 0, "declined": 0}


def make_client(cfg) -> KeaClient:
    return KeaClient(host=str(cfg.servers.dc1.ip), port=cfg.dhcp.ctrl_port)
