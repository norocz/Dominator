"""Zabbix monitoring stack (Docker, běží na DC2).

Zabbix agent 2 umí nativně monitorovat Windows klienty — proto je
oddělen od Prometheus/Grafana stacku a lze instalovat samostatně.

Stack: PostgreSQL backend + zabbix-server + zabbix-web (nginx).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .base import BaseInstaller


ZABBIX_DIR = Path("/opt/zabbix")


class ZabbixInstaller(BaseInstaller):
    name = "Zabbix (agent-based monitoring, Windows klienti)"

    def install(self) -> None:
        if not self.cfg.zabbix.enabled:
            raise RuntimeError(
                "Zabbix není povolen v config.yaml (zabbix.enabled: false). "
                "Nastavte enabled: true a zadejte db_password."
            )
        ZABBIX_DIR.mkdir(parents=True, exist_ok=True)
        self._write_compose()
        self.runner.sh(
            ["docker", "compose", "-f", str(ZABBIX_DIR / "docker-compose.yml"), "up", "-d"],
        )

    def _write_compose(self) -> None:
        z = self.cfg.zabbix

        compose = dedent(f"""\
            services:
              zabbix-db:
                image: postgres:16
                restart: unless-stopped
                environment:
                  POSTGRES_USER: zabbix
                  POSTGRES_PASSWORD: "{z.db_password}"
                  POSTGRES_DB: zabbix
                volumes:
                  - zabbix-db:/var/lib/postgresql/data

              zabbix-server:
                image: zabbix/zabbix-server-pgsql:latest
                restart: unless-stopped
                environment:
                  DB_SERVER_HOST: zabbix-db
                  POSTGRES_USER: zabbix
                  POSTGRES_PASSWORD: "{z.db_password}"
                  POSTGRES_DB: zabbix
                depends_on:
                  - zabbix-db
                ports:
                  - "10051:10051"

              zabbix-web:
                image: zabbix/zabbix-web-nginx-pgsql:latest
                restart: unless-stopped
                environment:
                  DB_SERVER_HOST: zabbix-db
                  POSTGRES_USER: zabbix
                  POSTGRES_PASSWORD: "{z.db_password}"
                  POSTGRES_DB: zabbix
                  ZBX_SERVER_HOST: zabbix-server
                  PHP_TZ: Europe/Prague
                ports:
                  - "{z.port}:8080"
                depends_on:
                  - zabbix-server

            volumes:
              zabbix-db:
            """)
        self.runner.write_file(ZABBIX_DIR / "docker-compose.yml", compose, mode=0o600)
