"""AD klient.

Dvě cesty komunikace s Samba AD:
  1) `samba-tool` - CLI nástroj, jednodušší pro CRUD operace na DC
  2) LDAP3 (Python) - rychlejší pro hromadné dotazy a vzdálené úpravy

Použijeme oba podle situace. samba-tool pro vytváření uživatelů (umí
nastavit heslo přes Kerberos), LDAP3 pro čtení a hromadné úpravy atributů.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ldap3 import Connection, Server, ALL, NTLM, SUBTREE
from rich.console import Console

from ..config import Config

console = Console()


@dataclass
class ADUser:
    username: str
    first_name: str
    last_name: str
    email: str | None = None
    password: str | None = None
    groups: list[str] | None = None


@dataclass
class ADComputer:
    hostname: str
    mac: str | None = None
    ip: str | None = None
    description: str | None = None
    groups: list[str] | None = None


class ADClient:
    """Klient pro Samba AD. Použije samba-tool (lokálně) i LDAP3 (čtení)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._conn: Connection | None = None

    # --- LDAP connection -------------------------------------------------

    @property
    def base_dn(self) -> str:
        # FIRMA.LOCAL -> DC=FIRMA,DC=LOCAL
        return ",".join(f"DC={p}" for p in self.cfg.domain.realm.split("."))

    def connect(self) -> Connection:
        if self._conn and self._conn.bound:
            return self._conn
        # Připojení k DC1 přes LDAPS by bylo lepší, ale samba defaultně negeneruje cert
        server = Server(str(self.cfg.servers.dc1.ip), get_info=ALL)
        user = f"{self.cfg.domain.netbios}\\Administrator"
        self._conn = Connection(
            server,
            user=user,
            password=self.cfg.domain.admin_password,
            authentication=NTLM,
            auto_bind=True,
        )
        return self._conn

    # --- samba-tool ------------------------------------------------------

    def _samba_tool(self, *args: str, sensitive: bool = False) -> subprocess.CompletedProcess:
        """Spustí samba-tool jako Administrator. Musí běžet na DC."""
        cmd = ["samba-tool", *args]
        if not sensitive:
            console.print(f"[cyan]$[/] {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    # --- uživatelé -------------------------------------------------------

    def create_user(self, user: ADUser) -> bool:
        """Vytvoří uživatele. Vrátí True pokud byl vytvořen, False pokud existoval."""
        if self.user_exists(user.username):
            console.print(f"[yellow]Uživatel {user.username} už existuje[/]")
            return False

        args = [
            "user", "create", user.username,
            user.password or self._random_password(),
            f"--given-name={user.first_name}",
            f"--surname={user.last_name}",
        ]
        if user.email:
            args.append(f"--mail-address={user.email}")

        result = self._samba_tool(*args, sensitive=True)
        if result.returncode != 0:
            console.print(f"[red]✗ Chyba při vytváření {user.username}:[/] {result.stderr}")
            return False
        console.print(f"[green]✓[/] vytvořen uživatel {user.username}")

        # Skupiny
        for group in (user.groups or []):
            self.add_user_to_group(user.username, group)
        return True

    def user_exists(self, username: str) -> bool:
        conn = self.connect()
        conn.search(
            self.base_dn,
            f"(&(objectClass=user)(sAMAccountName={username}))",
            search_scope=SUBTREE,
            attributes=[],
        )
        return len(conn.entries) > 0

    def list_users(self) -> list[dict]:
        conn = self.connect()
        conn.search(
            self.base_dn,
            "(&(objectClass=user)(!(objectClass=computer)))",
            search_scope=SUBTREE,
            attributes=["sAMAccountName", "givenName", "sn", "mail", "memberOf"],
        )
        return [
            {
                "username": str(e.sAMAccountName),
                "first_name": str(e.givenName) if e.givenName else "",
                "last_name": str(e.sn) if e.sn else "",
                "email": str(e.mail) if e.mail else "",
                "groups": [str(g) for g in e.memberOf] if e.memberOf else [],
            }
            for e in conn.entries
        ]

    # --- počítače --------------------------------------------------------

    def create_computer(self, computer: ADComputer) -> bool:
        """Předem vytvoří computer účet v AD. Užitečné pro 'pre-staging' před joinem."""
        if self.computer_exists(computer.hostname):
            console.print(f"[yellow]Počítač {computer.hostname} už existuje[/]")
            return False

        args = ["computer", "create", computer.hostname]
        if computer.description:
            args.append(f"--description={computer.description}")
        result = self._samba_tool(*args)
        if result.returncode != 0:
            console.print(f"[red]✗ {computer.hostname}:[/] {result.stderr}")
            return False
        console.print(f"[green]✓[/] vytvořen počítač {computer.hostname}")
        for group in (computer.groups or []):
            self.add_computer_to_group(computer.hostname, group)
        return True

    def computer_exists(self, hostname: str) -> bool:
        conn = self.connect()
        # Pozn.: v AD má computer account sAMAccountName s '$' na konci
        conn.search(
            self.base_dn,
            f"(&(objectClass=computer)(sAMAccountName={hostname}$))",
            search_scope=SUBTREE,
            attributes=[],
        )
        return len(conn.entries) > 0

    # --- skupiny ---------------------------------------------------------

    def list_groups(self) -> list[dict]:
        """Načte skupiny z AD přes LDAP."""
        conn = self.connect()
        conn.search(
            self.base_dn,
            "(objectClass=group)",
            search_scope=SUBTREE,
            attributes=["cn", "description", "member"],
        )
        result = []
        for e in conn.entries:
            members = e.member.values if e.member else []
            result.append({
                "name": str(e.cn),
                "description": str(e.description) if e.description else "",
                "member_count": len(members),
            })
        return result

    def create_group(self, name: str, description: str = "") -> bool:
        args = ["group", "add", name]
        if description:
            args.append(f"--description={description}")
        result = self._samba_tool(*args)
        if result.returncode != 0:
            if "already exists" in result.stderr.lower():
                return False
            console.print(f"[red]✗ skupina {name}:[/] {result.stderr}")
            return False
        console.print(f"[green]✓[/] vytvořena skupina {name}")
        return True

    def delete_group(self, name: str) -> bool:
        result = self._samba_tool("group", "delete", name)
        if result.returncode != 0:
            console.print(f"[red]✗ mazání skupiny {name}:[/] {result.stderr}")
            return False
        console.print(f"[green]✓[/] smazána skupina {name}")
        return True

    def add_user_to_group(self, username: str, group: str) -> bool:
        result = self._samba_tool("group", "addmembers", group, username)
        if result.returncode != 0 and "already a member" not in result.stderr.lower():
            console.print(f"[red]✗ {username} -> {group}:[/] {result.stderr}")
            return False
        return True

    def add_computer_to_group(self, hostname: str, group: str) -> bool:
        result = self._samba_tool("group", "addmembers", group, f"{hostname}$")
        if result.returncode != 0 and "already a member" not in result.stderr.lower():
            console.print(f"[red]✗ {hostname} -> {group}:[/] {result.stderr}")
            return False
        return True

    # --- utility ---------------------------------------------------------

    @staticmethod
    def _random_password() -> str:
        import secrets, string
        chars = string.ascii_letters + string.digits + "!@#$%"
        # Garantujeme všechny 4 kategorie
        pwd = (
            secrets.choice(string.ascii_uppercase)
            + secrets.choice(string.ascii_lowercase)
            + secrets.choice(string.digits)
            + secrets.choice("!@#$%")
            + "".join(secrets.choice(chars) for _ in range(8))
        )
        return pwd
