"""PostgreSQL pro management engine.

Topologie:
  - DC1 = primary (read-write)
  - DC2 = hot standby (read-only)
  - Streaming replication, async (sync by zpomalovalo, není kritické)

Failover ručně přes pg_ctlcluster promote, případně do budoucna Patroni.
Pro malou doménu (jednotky stovek PC) tohle stačí.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .base import BaseInstaller


class PostgresInstaller(BaseInstaller):
    name = "PostgreSQL"

    PACKAGES = ["postgresql", "postgresql-contrib", "postgresql-client"]

    def install(self, *, role: str = "primary") -> None:
        self.runner.apt_install(self.PACKAGES)
        self.runner.systemd_enable_now("postgresql")

        if role == "primary":
            self._setup_primary()
        else:
            self._setup_standby()

    def _setup_primary(self) -> None:
        # Vytvoření DB + uživatele přes psql
        pg = self.cfg.postgres
        sql = dedent(f"""\
            DO $$ BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{pg.db_user}') THEN
                    CREATE ROLE {pg.db_user} LOGIN PASSWORD '{pg.db_password}';
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN
                    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '{pg.replication_password}';
                END IF;
            END $$;
        """)
        self.runner.sh(
            ["sudo", "-u", "postgres", "psql", "-c", sql],
            sensitive=True,
        )

        # CREATE DATABASE musí běžet samostatně (nelze v DO bloku)
        self.runner.sh(
            ["sudo", "-u", "postgres", "psql", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{pg.db_name}'"],
            check=False,
        )
        # Lépe: createdb pokud neexistuje
        r = self.runner.sh(
            ["sudo", "-u", "postgres", "psql", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{pg.db_name}'"],
            check=False,
        )
        if r.stdout.strip() != "1":
            self.runner.sh(
                ["sudo", "-u", "postgres", "createdb", "-O", pg.db_user, pg.db_name],
            )

        # postgresql.conf úpravy pro replikaci
        # POZOR: cesty se v Ubuntu liší podle verze PG - hledáme dynamicky
        conf_dir = self._pg_conf_dir()
        self._patch_postgresql_conf(conf_dir / "postgresql.conf")
        self._patch_pg_hba(conf_dir / "pg_hba.conf")
        self.runner.sh(["systemctl", "restart", "postgresql"])

    def _setup_standby(self) -> None:
        # pg_basebackup z primary, pak start
        # TODO: implementovat - prozatím vypíšeme manuální postup
        from rich.console import Console
        Console().print(
            "[yellow]TODO:[/] Standby setup vyžaduje pg_basebackup z DC1. "
            "Bude doplněno - viz comments v souboru."
        )
        # Skica:
        #   systemctl stop postgresql
        #   rm -rf /var/lib/postgresql/<ver>/main
        #   sudo -u postgres pg_basebackup -h <dc1_ip> -U replicator -D /var/lib/postgresql/<ver>/main -P -R
        #   systemctl start postgresql

    def _pg_conf_dir(self) -> Path:
        # /etc/postgresql/<version>/main/
        base = Path("/etc/postgresql")
        if not base.exists():
            raise FileNotFoundError("PostgreSQL config dir nenalezen v /etc/postgresql")
        versions = [p for p in base.iterdir() if p.is_dir()]
        if not versions:
            raise FileNotFoundError("Žádná verze PostgreSQL v /etc/postgresql")
        return versions[0] / "main"

    def _patch_postgresql_conf(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        if "# domain-manager" in content:
            return  # už upraveno
        addition = dedent(f"""\

            # domain-manager: replikace
            listen_addresses = '*'
            wal_level = replica
            max_wal_senders = 5
            wal_keep_size = 256MB
            hot_standby = on
            """)
        path.write_text(content + addition, encoding="utf-8")

    def _patch_pg_hba(self, path: Path) -> None:
        net = self.cfg.network.subnet
        content = path.read_text(encoding="utf-8")
        if "# domain-manager" in content:
            return
        addition = dedent(f"""\

            # domain-manager
            host    all             all             {net}            scram-sha-256
            host    replication     replicator      {net}            scram-sha-256
            """)
        path.write_text(content + addition, encoding="utf-8")
