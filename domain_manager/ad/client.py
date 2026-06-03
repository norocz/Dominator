"""AD klient.

Dvě cesty komunikace s Samba AD:
  1) `samba-tool` - CLI nástroj, jednodušší pro CRUD operace na DC
  2) LDAP3 (Python) - rychlejší pro hromadné dotazy a vzdálené úpravy

Použijeme oba podle situace. samba-tool pro vytváření uživatelů (umí
nastavit heslo přes Kerberos), LDAP3 pro čtení a hromadné úpravy atributů.

Auth: connect() zkouší NTLM → SIMPLE/UPN fallback (NTLM může selhat
      u některých konfigurací ldap3/Samba na novějším Pythonu).
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from ldap3 import ALL, MODIFY_REPLACE, NTLM, SIMPLE, SUBTREE, Connection, Server

from ..config import Config

log = logging.getLogger("dm.ad")


# AD timestamp = 100-ns intervaly od 1601-01-01
_AD_EPOCH_DIFF = 116444736000000000  # 100-ns intervals between 1601 and 1970


def _ad_ts(val) -> str | None:
    """Převede AD integer timestamp na ISO date string. Vrátí None pro 0 / chybu."""
    try:
        raw = int(str(val))
        if raw <= 0:
            return None
        unix_ns = raw - _AD_EPOCH_DIFF
        dt = datetime.fromtimestamp(unix_ns / 1e7, tz=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return None


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
    """Klient pro Samba AD. Použije samba-tool (lokálně) i LDAP3 (čtení/zápis)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._conn: Connection | None = None

    # --- LDAP connection -------------------------------------------------

    @property
    def base_dn(self) -> str:
        return ",".join(f"DC={p}" for p in self.cfg.domain.realm.split("."))

    def connect(self) -> Connection:
        if self._conn and self._conn.bound:
            return self._conn
        dc_ip = str(self.cfg.servers.dc1.ip)
        admin_pass = self.cfg.domain.admin_password
        realm = self.cfg.domain.realm.lower()
        netbios = self.cfg.domain.netbios

        for auth_type, user_str in [
            (NTLM,   f"{netbios}\\Administrator"),
            (SIMPLE, f"Administrator@{realm}"),
        ]:
            try:
                server = Server(dc_ip, get_info=ALL, connect_timeout=10)
                conn = Connection(server, user=user_str, password=admin_pass,
                                  authentication=auth_type, auto_bind=True)
                self._conn = conn
                log.debug("LDAP připojen (%s)", auth_type)
                return self._conn
            except Exception as e:
                log.debug("LDAP bind selhání (%s): %s", auth_type, e)

        raise RuntimeError(
            f"Nelze se připojit k AD na {dc_ip} — zkontrolujte samba-ad-dc a admin_password v configu"
        )

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.unbind()
            except Exception:
                pass
            self._conn = None

    # --- samba-tool ------------------------------------------------------

    def _samba_tool(self, *args: str, sensitive: bool = False) -> subprocess.CompletedProcess:
        cmd = ["samba-tool", *args]
        display = " ".join(cmd) if not sensitive else "samba-tool *** (sensitive)"
        log.debug("RUN: %s", display)
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    # =========================================================================
    # UŽIVATELÉ
    # =========================================================================

    def list_users_full(self) -> list[dict]:
        """Načte AD uživatele se všemi běžnými atributy."""
        conn = self.connect()
        conn.search(
            self.base_dn,
            "(&(objectClass=user)(!(objectClass=computer)))",
            search_scope=SUBTREE,
            attributes=[
                "sAMAccountName", "givenName", "sn", "displayName",
                "mail", "telephoneNumber", "mobile", "department", "title",
                "memberOf", "userAccountControl", "lastLogon", "pwdLastSet",
                "distinguishedName", "description", "manager", "company",
            ],
        )
        result = []
        for e in conn.entries:
            uac = int(str(e.userAccountControl)) if e.userAccountControl else 0
            enabled = not bool(uac & 0x2)
            result.append({
                "username":     str(e.sAMAccountName),
                "dn":           str(e.distinguishedName),
                "first_name":   str(e.givenName)    if e.givenName    else "",
                "last_name":    str(e.sn)            if e.sn           else "",
                "display_name": str(e.displayName)   if e.displayName  else "",
                "email":        str(e.mail)           if e.mail         else "",
                "phone":        str(e.telephoneNumber) if e.telephoneNumber else "",
                "mobile":       str(e.mobile)         if e.mobile       else "",
                "department":   str(e.department)     if e.department   else "",
                "title":        str(e.title)          if e.title        else "",
                "description":  str(e.description)    if e.description  else "",
                "enabled":      enabled,
                "groups":       [str(g) for g in e.memberOf] if e.memberOf else [],
                "last_logon":   _ad_ts(e.lastLogon),
                "pwd_last_set": _ad_ts(e.pwdLastSet),
            })
        return sorted(result, key=lambda x: x["username"].lower())

    def get_user(self, username: str) -> dict | None:
        users = [u for u in self.list_users_full() if u["username"].lower() == username.lower()]
        return users[0] if users else None

    def user_exists(self, username: str) -> bool:
        conn = self.connect()
        conn.search(self.base_dn, f"(&(objectClass=user)(sAMAccountName={username}))",
                    search_scope=SUBTREE, attributes=[])
        return len(conn.entries) > 0

    def create_user(self, user: ADUser) -> tuple[bool, str]:
        if self.user_exists(user.username):
            return False, f"Uživatel '{user.username}' už existuje"
        args = [
            "user", "create", user.username,
            user.password or self._random_password(),
            f"--given-name={user.first_name}",
            f"--surname={user.last_name}",
        ]
        if user.email:
            args.append(f"--mail-address={user.email}")
        r = self._samba_tool(*args, sensitive=True)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        for group in (user.groups or []):
            self.add_user_to_group(user.username, group)
        return True, ""

    def delete_user(self, username: str) -> tuple[bool, str]:
        r = self._samba_tool("user", "delete", username)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def enable_user(self, username: str) -> tuple[bool, str]:
        r = self._samba_tool("user", "enable", username)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def disable_user(self, username: str) -> tuple[bool, str]:
        r = self._samba_tool("user", "disable", username)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def set_password(self, username: str, new_password: str) -> tuple[bool, str]:
        r = self._samba_tool("user", "setpassword", username,
                             f"--newpassword={new_password}", sensitive=True)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def update_user_attrs(self, username: str, attrs: dict) -> tuple[bool, str]:
        """Aktualizuje LDAP atributy uživatele. attrs = {friendly_name: value}."""
        _map = {
            "email":        "mail",
            "phone":        "telephoneNumber",
            "mobile":       "mobile",
            "department":   "department",
            "title":        "title",
            "display_name": "displayName",
            "description":  "description",
            "company":      "company",
        }
        conn = self.connect()
        conn.search(self.base_dn, f"(sAMAccountName={username})",
                    search_scope=SUBTREE, attributes=["distinguishedName"])
        if not conn.entries:
            return False, f"Uživatel '{username}' nenalezen v AD"
        dn = str(conn.entries[0].distinguishedName)

        changes: dict = {}
        for key, val in attrs.items():
            ldap_attr = _map.get(key)
            if not ldap_attr:
                continue
            changes[ldap_attr] = [(MODIFY_REPLACE, [val] if val else [])]

        if not changes:
            return True, ""
        ok = conn.modify(dn, changes)
        if not ok:
            return False, str(conn.result.get("description", "LDAP modify selhal"))
        return True, ""

    def list_users(self) -> list[dict]:
        """Jednodušší verze pro zpětnou kompatibilitu s importéry."""
        conn = self.connect()
        conn.search(self.base_dn, "(&(objectClass=user)(!(objectClass=computer)))",
                    search_scope=SUBTREE,
                    attributes=["sAMAccountName", "givenName", "sn", "mail", "memberOf"])
        return [
            {
                "username":   str(e.sAMAccountName),
                "first_name": str(e.givenName) if e.givenName else "",
                "last_name":  str(e.sn)        if e.sn        else "",
                "email":      str(e.mail)       if e.mail      else "",
                "groups":     [str(g) for g in e.memberOf] if e.memberOf else [],
            }
            for e in conn.entries
        ]

    # =========================================================================
    # SKUPINY
    # =========================================================================

    def list_groups(self) -> list[dict]:
        conn = self.connect()
        conn.search(self.base_dn, "(objectClass=group)", search_scope=SUBTREE,
                    attributes=["cn", "description", "member", "distinguishedName"])
        result = []
        for e in conn.entries:
            members = e.member.values if e.member else []
            result.append({
                "name":         str(e.cn),
                "dn":           str(e.distinguishedName),
                "description":  str(e.description) if e.description else "",
                "member_count": len(members),
            })
        return sorted(result, key=lambda x: x["name"].lower())

    def list_group_members(self, group_name: str) -> list[dict]:
        """Vrátí členy skupiny jako seznam {name, dn, is_computer}."""
        conn = self.connect()
        conn.search(self.base_dn, f"(&(objectClass=group)(cn={group_name}))",
                    search_scope=SUBTREE, attributes=["member"])
        if not conn.entries:
            return []
        member_dns = [str(m) for m in conn.entries[0].member] if conn.entries[0].member else []
        result = []
        for dn in member_dns:
            # Načti sAMAccountName a typ z DN
            try:
                conn.search(dn, "(objectClass=*)",
                            attributes=["sAMAccountName", "objectClass"])
                if conn.entries:
                    sam = str(conn.entries[0].sAMAccountName) if conn.entries[0].sAMAccountName else ""
                    classes = [str(c).lower() for c in conn.entries[0].objectClass]
                    is_computer = "computer" in classes
                    result.append({"name": sam.rstrip("$"), "dn": dn, "is_computer": is_computer})
                else:
                    cn = dn.split(",")[0].replace("CN=", "").replace("cn=", "")
                    result.append({"name": cn, "dn": dn, "is_computer": False})
            except Exception:
                cn = dn.split(",")[0].replace("CN=", "").replace("cn=", "")
                result.append({"name": cn, "dn": dn, "is_computer": False})
        return sorted(result, key=lambda x: x["name"].lower())

    def create_group(self, name: str, description: str = "") -> tuple[bool, str]:
        args = ["group", "add", name]
        if description:
            args.append(f"--description={description}")
        r = self._samba_tool(*args)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        return True, ""

    def delete_group(self, name: str) -> tuple[bool, str]:
        r = self._samba_tool("group", "delete", name)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def add_user_to_group(self, username: str, group: str) -> tuple[bool, str]:
        r = self._samba_tool("group", "addmembers", group, username)
        if r.returncode != 0 and "already a member" not in (r.stderr or "").lower():
            return False, (r.stderr or r.stdout).strip()
        return True, ""

    def remove_user_from_group(self, username: str, group: str) -> tuple[bool, str]:
        r = self._samba_tool("group", "removemembers", group, username)
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    def add_computer_to_group(self, hostname: str, group: str) -> tuple[bool, str]:
        r = self._samba_tool("group", "addmembers", group, f"{hostname}$")
        return r.returncode == 0, (r.stderr or r.stdout).strip()

    # =========================================================================
    # POČÍTAČE
    # =========================================================================

    def list_computers_full(self) -> list[dict]:
        conn = self.connect()
        conn.search(
            self.base_dn,
            "(objectClass=computer)",
            search_scope=SUBTREE,
            attributes=[
                "sAMAccountName", "dNSHostName", "operatingSystem",
                "operatingSystemVersion", "lastLogon", "userAccountControl",
                "distinguishedName", "description",
            ],
        )
        result = []
        for e in conn.entries:
            uac = int(str(e.userAccountControl)) if e.userAccountControl else 0
            enabled = not bool(uac & 0x2)
            hostname = str(e.sAMAccountName).rstrip("$") if e.sAMAccountName else ""
            result.append({
                "hostname":   hostname,
                "fqdn":       str(e.dNSHostName) if e.dNSHostName else "",
                "os":         str(e.operatingSystem) if e.operatingSystem else "",
                "os_version": str(e.operatingSystemVersion) if e.operatingSystemVersion else "",
                "enabled":    enabled,
                "last_logon": _ad_ts(e.lastLogon),
                "description": str(e.description) if e.description else "",
                "dn":         str(e.distinguishedName),
            })
        return sorted(result, key=lambda x: x["hostname"].lower())

    def computer_exists(self, hostname: str) -> bool:
        conn = self.connect()
        conn.search(self.base_dn, f"(&(objectClass=computer)(sAMAccountName={hostname}$))",
                    search_scope=SUBTREE, attributes=[])
        return len(conn.entries) > 0

    def create_computer(self, computer: ADComputer) -> tuple[bool, str]:
        if self.computer_exists(computer.hostname):
            return False, f"Počítač '{computer.hostname}' už existuje"
        args = ["computer", "create", computer.hostname]
        if computer.description:
            args.append(f"--description={computer.description}")
        r = self._samba_tool(*args)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        for group in (computer.groups or []):
            self.add_computer_to_group(computer.hostname, group)
        return True, ""

    # =========================================================================
    # DOMÉNOVÉ INFO
    # =========================================================================

    def get_domain_info(self) -> dict:
        """Základní info o doméně — spustí samba-tool domain info."""
        r = self._samba_tool("domain", "info", "127.0.0.1")
        info: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                info[key.strip().lower().replace(" ", "_")] = val.strip()
        if r.returncode != 0:
            info["_error"] = (r.stderr or r.stdout).strip()[:200]
        return info

    def get_password_policy(self) -> dict:
        r = self._samba_tool("domain", "passwordsettings", "show")
        settings: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                settings[key.strip().lower().replace(" ", "_")] = val.strip()
        if r.returncode != 0:
            settings["_error"] = (r.stderr or r.stdout).strip()[:200]
        return settings

    def get_dc_status(self) -> list[dict]:
        """Replikační status DCs."""
        r = self._samba_tool("drs", "showrepl")
        lines = r.stdout.splitlines()
        checks = []
        for line in lines:
            if "Failure" in line or "failed" in line.lower():
                checks.append({"line": line.strip(), "ok": False})
            elif "==== INBOUND" in line or "==== OUTBOUND" in line:
                checks.append({"line": line.strip(), "ok": True})
        return checks

    # =========================================================================
    # UTILITY
    # =========================================================================

    @staticmethod
    def _random_password() -> str:
        import secrets, string
        chars = string.ascii_letters + string.digits + "!@#$%"
        pwd = (
            secrets.choice(string.ascii_uppercase)
            + secrets.choice(string.ascii_lowercase)
            + secrets.choice(string.digits)
            + secrets.choice("!@#$%")
            + "".join(secrets.choice(chars) for _ in range(8))
        )
        return pwd
