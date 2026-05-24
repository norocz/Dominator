"""Instalace sekundárního Samba AD DC.

Postup:
  1) Předpoklad: DC1 už běží a je dostupný (ping, port 88/389)
  2) Hostname, /etc/hosts, netplan
  3) Stop konfliktních služeb, instalace samba-ad-dc
  4) /etc/krb5.conf -> realm DC1 (potřebujeme kinit)
  5) kinit Administrator (přihlášení do AD)
  6) samba-tool domain join <realm> DC
  7) Spustit samba-ad-dc, ověřit replikaci
"""
from __future__ import annotations

import socket
import time
from pathlib import Path
from textwrap import dedent

from rich.console import Console

from .base import BaseInstaller

console = Console()


class SambaDC2Installer(BaseInstaller):
    name = "Samba AD DC (sekundární)"

    PACKAGES = [
        "samba-ad-dc",
        "samba",
        "krb5-user",
        "krb5-config",
        "smbclient",
        "ldb-tools",
        "winbind",
        "libnss-winbind",
        "libpam-winbind",
        "chrony",
        "dnsutils",
    ]

    def preflight(self) -> None:
        # DC1 musí odpovídat
        dc1_ip = str(self.cfg.servers.dc1.ip)
        console.print(f"[blue]→[/] kontroluji dostupnost DC1 ({dc1_ip})...")
        r = self.runner.sh(["ping", "-c", "2", "-W", "2", dc1_ip], check=False)
        if not r.ok:
            raise RuntimeError(f"DC1 ({dc1_ip}) neodpovídá. Zkontrolujte síť a že DC1 běží.")

        # Existující Samba data
        if Path("/var/lib/samba/private/sam.ldb").exists():
            raise RuntimeError(
                "/var/lib/samba/private/sam.ldb existuje. "
                "Pro re-join smažte /var/lib/samba a /etc/samba/smb.conf."
            )

    def install(self, *, skip_join: bool = False) -> None:
        self._setup_hostname()
        self._setup_network()
        self._stop_conflicting_services()
        self._install_packages()
        self._setup_chrony()
        self._setup_krb5_for_join()
        if not skip_join:
            self._kinit_admin()
            self._join_domain()
        self._enable_samba_ad_dc()

    def verify(self) -> None:
        time.sleep(3)
        if not self.runner.is_active("samba-ad-dc"):
            console.print("[red]✗ samba-ad-dc neběží[/]")
            return
        # Replikace
        r = self.runner.sh(
            ["samba-tool", "drs", "showrepl"],
            check=False,
        )
        if r.ok:
            console.print(f"[green]✓ DRS replikace:[/]\n{r.stdout[:1000]}")
        else:
            console.print(f"[yellow]Pozor: samba-tool drs showrepl selhal[/]")

    # --- kroky -----------------------------------------------------------

    def _setup_hostname(self) -> None:
        dc = self.cfg.servers.dc2
        realm_lower = self.cfg.domain.realm.lower()
        fqdn = f"{dc.hostname}.{realm_lower}"

        self.runner.sh(["hostnamectl", "set-hostname", dc.hostname])
        hosts = dedent(f"""\
            127.0.0.1   localhost
            {dc.ip}   {fqdn} {dc.hostname}
            {self.cfg.servers.dc1.ip}   {self.cfg.servers.dc1.hostname}.{realm_lower} {self.cfg.servers.dc1.hostname}

            ::1         ip6-localhost ip6-loopback
            fe00::0     ip6-localnet
            ff00::0     ip6-mcastprefix
            ff02::1     ip6-allnodes
            ff02::2     ip6-allrouters
            """)
        self.runner.write_file(Path("/etc/hosts"), hosts, mode=0o644)

    def _setup_network(self) -> None:
        """Klient na DC2 musí dotazovat DC1 jako primární DNS aby uměl rezolvovat doménu při joinu."""
        import ipaddress
        dc = self.cfg.servers.dc2
        prefix = ipaddress.ip_network(self.cfg.network.subnet, strict=False).prefixlen
        netplan = dedent(f"""\
            network:
              version: 2
              ethernets:
                {dc.interface}:
                  dhcp4: false
                  addresses:
                    - {dc.ip}/{prefix}
                  routes:
                    - to: default
                      via: {self.cfg.network.gateway}
                  nameservers:
                    addresses:
                      - {self.cfg.servers.dc1.ip}
                      - {dc.ip}
                    search:
                      - {self.cfg.domain.realm.lower()}
            """)
        changed = self.runner.write_file(
            Path("/etc/netplan/99-domain-manager.yaml"),
            netplan,
            mode=0o600,
        )
        if changed:
            self.runner.sh(["netplan", "apply"])

    def _stop_conflicting_services(self) -> None:
        for unit in ("smbd", "nmbd", "winbind"):
            self.runner.sh(["systemctl", "stop", unit], check=False)
            self.runner.sh(["systemctl", "disable", unit], check=False)
            self.runner.sh(["systemctl", "mask", unit], check=False)

    def _install_packages(self) -> None:
        self.runner.apt_update()
        self.runner.apt_install(self.PACKAGES)

    def _setup_chrony(self) -> None:
        self.runner.systemd_enable_now("chrony")

    def _setup_krb5_for_join(self) -> None:
        """Před joinem ještě nemáme samba-vygenerovaný krb5.conf. Vytvoříme minimální."""
        realm = self.cfg.domain.realm
        realm_lower = realm.lower()
        content = dedent(f"""\
            [libdefaults]
                default_realm = {realm}
                dns_lookup_realm = false
                dns_lookup_kdc = true

            [realms]
                {realm} = {{
                    default_domain = {realm_lower}
                }}

            [domain_realm]
                .{realm_lower} = {realm}
                {realm_lower} = {realm}
            """)
        self.runner.write_file(Path("/etc/krb5.conf"), content, mode=0o644)

    def _kinit_admin(self) -> None:
        """Přihlásit se k AD jako Administrator (potřebné pro join)."""
        # echo password | kinit -- klasický anti-pattern, ale tady to není citlivé,
        # protože heslo je v config.yaml ke kterému má přístup jen root
        console.print("[blue]→[/] kinit Administrator...")
        self.runner.sh(
            "kinit Administrator",
            input_data=self.cfg.domain.admin_password + "\n",
            sensitive=True,
        )
        # Ověření
        self.runner.sh(["klist"])

    def _join_domain(self) -> None:
        cmd = [
            "samba-tool", "domain", "join",
            self.cfg.domain.realm,
            "DC",
            f"-U{self.cfg.domain.netbios}\\Administrator",
            f"--password={self.cfg.domain.admin_password}",
            f"--option=interfaces=lo {self.cfg.servers.dc2.interface}",
            "--option=bind interfaces only=yes",
        ]
        console.print("[blue]→[/] samba-tool domain join (může trvat několik minut)...")
        self.runner.sh(cmd, sensitive=True)

        # Přepneme krb5.conf na ten od Samby
        src = Path("/var/lib/samba/private/krb5.conf")
        dst = Path("/etc/krb5.conf")
        if src.exists():
            if dst.exists() and not dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src)

    def _enable_samba_ad_dc(self) -> None:
        self.runner.sh(["systemctl", "unmask", "samba-ad-dc"])
        self.runner.systemd_enable_now("samba-ad-dc")
