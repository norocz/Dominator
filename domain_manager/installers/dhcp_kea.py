"""ISC Kea DHCP server s host reservations (MAC -> IP).

Kea má Control Agent s REST API - management engine přes ni přidává/odebírá
reservace bez restartu (commit do paměti + perzistence do JSON souboru).

Vazba MAC -> IP přesně to, co uživatel chce: nový Win PC se připojí, DHCP
mu nedá nic (nebo dá z 'guest' poolu), admin v UI přidá MAC, dostane
přidělenou IP a může jít na join do domény.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import BaseInstaller


class KeaDhcpInstaller(BaseInstaller):
    name = "ISC Kea DHCPv4"

    PACKAGES = ["kea-dhcp4-server", "kea-ctrl-agent", "kea-admin"]

    def install(self) -> None:
        self.runner.apt_install(self.PACKAGES)
        self._write_dhcp4_config()
        self._write_ctrl_agent_config()
        self.runner.systemd_enable_now("kea-dhcp4-server")
        self.runner.systemd_enable_now("kea-ctrl-agent")

    def _write_dhcp4_config(self) -> None:
        net = self.cfg.network
        dhcp = self.cfg.dhcp
        subnet = net.subnet

        reservations = [
            {
                "hw-address": r.mac,
                "ip-address": str(r.ip),
                "hostname": r.hostname,
            }
            for r in dhcp.reservations
        ]

        config = {
            "Dhcp4": {
                "interfaces-config": {
                    "interfaces": [self.cfg.servers.dc1.interface],
                },
                "control-socket": {
                    "socket-type": "unix",
                    "socket-name": "/run/kea/kea4-ctrl-socket",
                },
                "lease-database": {
                    "type": "memfile",
                    "persist": True,
                    "name": "/var/lib/kea/kea-leases4.csv",
                },
                "valid-lifetime": dhcp.lease_time_seconds,
                "option-data": [
                    {"name": "routers", "data": str(net.gateway)},
                    {"name": "domain-name-servers",
                     "data": ", ".join(str(d) for d in net.dns_servers)},
                    {"name": "domain-name", "data": self.cfg.domain.realm.lower()},
                    {"name": "domain-search", "data": self.cfg.domain.realm.lower()},
                ],
                "subnet4": [{
                    "id": 1,
                    "subnet": subnet,
                    "pools": [{
                        "pool": f"{dhcp.pool_start} - {dhcp.pool_end}",
                    }],
                    "reservations": reservations,
                }],
                "loggers": [{
                    "name": "kea-dhcp4",
                    "output_options": [{"output": "/var/log/kea/kea-dhcp4.log"}],
                    "severity": "INFO",
                }],
            }
        }

        self.runner.write_file(
            Path("/etc/kea/kea-dhcp4.conf"),
            json.dumps(config, indent=2),
            mode=0o640,
            owner="root:_kea" if self._kea_user_exists() else None,
        )

    def _write_ctrl_agent_config(self) -> None:
        config = {
            "Control-agent": {
                "http-host": "127.0.0.1",
                "http-port": 8000,  # POZOR: koliduje s manager.bind_port=8000
                # Pro produkci přepnout na 8001
                "control-sockets": {
                    "dhcp4": {
                        "socket-type": "unix",
                        "socket-name": "/run/kea/kea4-ctrl-socket",
                    },
                },
                "loggers": [{
                    "name": "kea-ctrl-agent",
                    "output_options": [{"output": "/var/log/kea/kea-ctrl-agent.log"}],
                    "severity": "INFO",
                }],
            }
        }
        # FIXME: port konflikt s manager web! Přesunout na 8001
        config["Control-agent"]["http-port"] = 8001
        self.runner.write_file(
            Path("/etc/kea/kea-ctrl-agent.conf"),
            json.dumps(config, indent=2),
            mode=0o640,
        )

    def _kea_user_exists(self) -> bool:
        import subprocess
        return subprocess.run(["id", "_kea"], capture_output=True).returncode == 0
