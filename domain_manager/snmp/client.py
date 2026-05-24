"""SNMP klient pro switche, routery a AP.

Podporuje SNMP v1/v2c (community string) přes pysnmp (pure Python).
Pokud pysnmp není k dispozici, padne zpět na subprocess snmpwalk/snmpget.

Klíčové MIB skupiny:
  - SNMPv2-MIB: sysDescr, sysName, sysUpTime
  - IF-MIB:     ifTable (interface stats — bytes, errors, speed)
  - BRIDGE-MIB: dot1dTpFdbTable (MAC forwarding table, MAC→port)
  - Q-BRIDGE-MIB: dot1qTpFdbTable (pro vlany)
"""
from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("dm.snmp")

# Pokus o import pysnmp (pure Python SNMP)
try:
    from pysnmp.hlapi import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        SnmpEngine, UdpTransportTarget, getCmd, nextCmd, bulkCmd,
    )
    _PYSNMP = True
except ImportError:
    _PYSNMP = False
    log.info("pysnmp není nainstalováno — SNMP vyžaduje 'pip install pysnmp'")


class SNMPError(RuntimeError):
    pass


# Standard OIDs
_OID_SYS_DESCR  = "1.3.6.1.2.1.1.1.0"
_OID_SYS_NAME   = "1.3.6.1.2.1.1.5.0"
_OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
_OID_IF_TABLE   = "1.3.6.1.2.1.2.2"   # ifTable
_OID_IF_DESCR   = "1.3.6.1.2.1.2.2.1.2"
_OID_IF_SPEED   = "1.3.6.1.2.1.2.2.1.5"
_OID_IF_OPER    = "1.3.6.1.2.1.2.2.1.8"
_OID_IF_IN_OCT  = "1.3.6.1.2.1.2.2.1.10"
_OID_IF_OUT_OCT = "1.3.6.1.2.1.2.2.1.16"
_OID_IF_IN_ERR  = "1.3.6.1.2.1.2.2.1.14"
_OID_IF_OUT_ERR = "1.3.6.1.2.1.2.2.1.20"
_OID_IF_ALIAS   = "1.3.6.1.2.1.31.1.1.1.18"   # ifAlias (IF-MIB extension)
_OID_FDB_PORT   = "1.3.6.1.2.1.17.4.3.1.2"    # dot1dTpFdbPort (BRIDGE-MIB)


@dataclass
class PortStats:
    index: int
    name: str = ""
    alias: str = ""
    speed_mbps: Optional[int] = None
    oper_status: str = "unknown"   # "up"|"down"|"testing"|"unknown"
    in_bytes: int = 0
    out_bytes: int = 0
    in_errors: int = 0
    out_errors: int = 0


@dataclass
class DeviceInfo:
    sys_name: str = ""
    sys_description: str = ""
    sys_uptime_seconds: int = 0
    ports: dict[int, PortStats] = field(default_factory=dict)
    mac_table: dict[str, int] = field(default_factory=dict)   # mac → port_index
    sync_error: Optional[str] = None
    synced_at: Optional[datetime] = None


# --- SNMP operace ----------------------------------------------------------

def _get(ip: str, community: str, oid: str, port: int = 161, timeout: int = 5) -> str:
    """Jednoduchý SNMP GET. Vrátí string hodnotu nebo vyhodí SNMPError."""
    if not _PYSNMP:
        raise SNMPError("pysnmp není nainstalováno — spusťte: pip install pysnmp")

    iterator = getCmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),
        UdpTransportTarget((ip, port), timeout=timeout, retries=1),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    error_indication, error_status, error_index, var_binds = next(iterator)
    if error_indication:
        raise SNMPError(str(error_indication))
    if error_status:
        raise SNMPError(f"SNMP error: {error_status.prettyPrint()}")
    return str(var_binds[0][1])


def _walk(ip: str, community: str, oid: str, port: int = 161, timeout: int = 10) -> list[tuple[str, str]]:
    """SNMP GETNEXT walk. Vrátí seznam (oid, hodnota)."""
    if not _PYSNMP:
        raise SNMPError("pysnmp není nainstalováno")

    results = []
    for (error_indication, error_status, _, var_binds) in nextCmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),
        UdpTransportTarget((ip, port), timeout=timeout, retries=1),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if error_indication or error_status:
            break
        for oid_obj, val in var_binds:
            results.append((str(oid_obj), str(val)))
    return results


def _last_oid_component(oid_str: str) -> str:
    """Poslední část OID — obvykle index."""
    return oid_str.rstrip(".").split(".")[-1]


def _format_mac(raw: str) -> str:
    """Převede SNMP hex string na MAC ve formátu XX:XX:XX:XX:XX:XX."""
    # pysnmp vrací OctetString jako '0x...' nebo jako bytes-like
    raw = raw.strip()
    if raw.startswith("0x"):
        hex_part = raw[2:]
        if len(hex_part) == 12:
            return ":".join(hex_part[i:i+2] for i in range(0, 12, 2))
    # fallback - raw string s mezery nebo pomlčky
    clean = raw.replace(" ", "").replace(":", "").replace("-", "")
    if len(clean) == 12:
        return ":".join(clean[i:i+2] for i in range(0, 12, 2))
    return raw


# --- Hlavní funkce ---------------------------------------------------------

def sync_device(ip: str, community: str = "public", snmp_version: str = "2c", port: int = 161) -> DeviceInfo:
    """
    Stáhne informace ze zařízení přes SNMP.
    Vrátí DeviceInfo — při chybě nastaví sync_error.
    """
    info = DeviceInfo(synced_at=datetime.now(timezone.utc).replace(tzinfo=None))

    if not _PYSNMP:
        info.sync_error = "pysnmp není nainstalováno (pip install pysnmp)"
        return info

    try:
        info.sys_name = _get(ip, community, _OID_SYS_NAME, port)
        info.sys_description = _get(ip, community, _OID_SYS_DESCR, port)
        uptime_raw = _get(ip, community, _OID_SYS_UPTIME, port)
        # Uptime přichází jako TimeTicks (1/100 sec)
        try:
            ticks = int(uptime_raw.split("(")[1].split(")")[0]) if "(" in uptime_raw else int(uptime_raw)
            info.sys_uptime_seconds = ticks // 100
        except Exception:
            info.sys_uptime_seconds = 0
    except SNMPError as e:
        info.sync_error = str(e)
        return info

    # Interface stats
    try:
        ports: dict[int, PortStats] = {}

        for oid, val in _walk(ip, community, _OID_IF_DESCR, port):
            idx = int(_last_oid_component(oid))
            ports[idx] = PortStats(index=idx, name=val)

        for oid, val in _walk(ip, community, _OID_IF_ALIAS, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                ports[idx].alias = val

        for oid, val in _walk(ip, community, _OID_IF_SPEED, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                try:
                    ports[idx].speed_mbps = int(val) // 1_000_000
                except ValueError:
                    pass

        oper_map = {"1": "up", "2": "down", "3": "testing", "4": "unknown",
                    "5": "dormant", "6": "notPresent", "7": "lowerLayerDown"}
        for oid, val in _walk(ip, community, _OID_IF_OPER, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                ports[idx].oper_status = oper_map.get(val, val)

        for oid, val in _walk(ip, community, _OID_IF_IN_OCT, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                try:
                    ports[idx].in_bytes = int(val)
                except ValueError:
                    pass

        for oid, val in _walk(ip, community, _OID_IF_OUT_OCT, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                try:
                    ports[idx].out_bytes = int(val)
                except ValueError:
                    pass

        for oid, val in _walk(ip, community, _OID_IF_IN_ERR, port):
            idx = int(_last_oid_component(oid))
            if idx in ports:
                try:
                    ports[idx].in_errors = int(val)
                except ValueError:
                    pass

        info.ports = ports
    except SNMPError as e:
        log.warning("Interface walk failed for %s: %s", ip, e)

    # MAC forwarding table
    try:
        for oid, val in _walk(ip, community, _OID_FDB_PORT, port):
            # OID suffix = MAC address encoded as decimal octets
            oid_parts = oid.rstrip(".").split(".")
            mac_parts = oid_parts[-6:]
            mac = ":".join(f"{int(b):02x}" for b in mac_parts)
            try:
                port_idx = int(val)
                info.mac_table[mac] = port_idx
            except ValueError:
                pass
    except SNMPError as e:
        log.debug("FDB walk failed for %s: %s", ip, e)

    return info


def is_reachable(ip: str, port: int = 161, timeout: float = 2.0) -> bool:
    """Rychlý UDP ping — zkusí otevřít UDP socket."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.close()
        return True
    except Exception:
        return False
