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
        self._disable_systemd_resolved()   # port 53 pro Samba, stejný problém jako na DC1
        self._install_packages()
        self._setup_chrony()
        self._setup_krb5_for_join()
        self._verify_dc1_kerberos()        # DNS/konektivita k DC1 než spustíme kinit
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
        """DC2 musí jako primární DNS používat DC1, aby mohl rezolvovat doménu při joinu."""
        import ipaddress
        dc = self.cfg.servers.dc2
        dc1_ip = str(self.cfg.servers.dc1.ip)
        prefix = ipaddress.ip_network(self.cfg.network.subnet, strict=False).prefixlen
        iface = self._detect_interface(dc.interface)

        netplan = dedent(f"""\
            network:
              version: 2
              ethernets:
                {iface}:
                  dhcp4: false
                  addresses:
                    - {dc.ip}/{prefix}
                  routes:
                    - to: default
                      via: {self.cfg.network.gateway}
                  nameservers:
                    addresses:
                      - {dc1_ip}
                      - {str(self.cfg.domain.dns_forwarder)}
                    search:
                      - {self.cfg.domain.realm.lower()}
            """)
        changed = self.runner.write_file(
            Path("/etc/netplan/99-domain-manager.yaml"),
            netplan,
            mode=0o600,
        )
        if changed:
            self._warn_ip_change(str(dc.ip))
            self.runner.sh(["netplan", "apply"])
            self._verify_gateway()

    def _stop_conflicting_services(self) -> None:
        for unit in ("smbd", "nmbd", "winbind"):
            self.runner.sh(["systemctl", "stop", unit], check=False)
            self.runner.sh(["systemctl", "disable", unit], check=False)
            self.runner.sh(["systemctl", "mask", unit], check=False)

    def _disable_systemd_resolved(self) -> None:
        """Stejný problém jako na DC1 — Samba potřebuje port 53 pro DNS."""
        console.print("[blue]→[/] zakazuji systemd-resolved stub listener...")
        resolved_conf = Path("/etc/systemd/resolved.conf.d/no-stub.conf")
        self.runner.write_file(
            resolved_conf,
            "[Resolve]\nDNSStubListener=no\n",
            mode=0o644,
        )
        self.runner.sh(["systemctl", "restart", "systemd-resolved"], check=False)

        resolv = Path("/etc/resolv.conf")
        if resolv.is_symlink():
            resolv.unlink()
        dc1_ip = str(self.cfg.servers.dc1.ip)
        realm_lower = self.cfg.domain.realm.lower()
        forwarder = str(self.cfg.domain.dns_forwarder)
        self.runner.write_file(
            resolv,
            f"nameserver {dc1_ip}\nnameserver {forwarder}\nsearch {realm_lower}\n",
            mode=0o644,
            backup=False,
        )

    def _install_packages(self) -> None:
        self.runner.apt_update()
        self.runner.apt_install(self.PACKAGES)

    def _setup_chrony(self) -> None:
        self.runner.systemd_enable_now("chrony")

    def _setup_krb5_for_join(self) -> None:
        """Před joinem ještě nemáme samba-vygenerovaný krb5.conf. Vytvoříme minimální.

        Explicitně uvádíme KDC adresu (DC1 FQDN z /etc/hosts) místo spoléhání
        na DNS SRV lookup, který nemusí ještě fungovat.
        """
        realm = self.cfg.domain.realm
        realm_lower = realm.lower()
        dc1_fqdn = f"{self.cfg.servers.dc1.hostname}.{realm_lower}"
        content = dedent(f"""\
            [libdefaults]
                default_realm = {realm}
                dns_lookup_realm = false
                dns_lookup_kdc = false

            [realms]
                {realm} = {{
                    kdc = {dc1_fqdn}
                    admin_server = {dc1_fqdn}
                    default_domain = {realm_lower}
                }}

            [domain_realm]
                .{realm_lower} = {realm}
                {realm_lower} = {realm}
            """)
        self.runner.write_file(Path("/etc/krb5.conf"), content, mode=0o644)

    def _verify_dc1_kerberos(self) -> None:
        """Ověří, že DC1 je dostupný na portu 88 (Kerberos) před kinit."""
        dc1_ip = str(self.cfg.servers.dc1.ip)
        console.print(f"[blue]→[/] ověřuji Kerberos port 88 na DC1 ({dc1_ip})...")
        r = self.runner.sh(
            ["bash", "-c", f"timeout 5 bash -c 'echo > /dev/tcp/{dc1_ip}/88' 2>/dev/null && echo OK || echo FAIL"],
            check=False,
        )
        if "FAIL" in r.stdout or not r.ok:
            raise RuntimeError(
                f"DC1 ({dc1_ip}) neodpovídá na portu 88 (Kerberos).\n"
                f"Zkontrolujte: 1) samba-ad-dc běží na DC1, 2) firewall povoluje port 88, "
                f"3) síťová konektivita mezi DC1 a DC2."
            )
        console.print(f"[green]✓[/] DC1 Kerberos port 88 dostupný")

    def _kinit_admin(self) -> None:
        """Přihlásit se k AD jako Administrator (potřebné pro join)."""
        console.print("[blue]→[/] kinit Administrator...")
        self.runner.sh(
            "kinit Administrator",
            input_data=self.cfg.domain.admin_password + "\n",
            sensitive=True,
        )
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

    def _warn_ip_change(self, new_ip: str) -> None:
        """Upozornění před netplan apply — SSH se odpojí pokud stroj měl jinou IP."""
        import subprocess, time
        current_ips: list[str] = []
        try:
            r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1] != "lo":
                    current_ips.append(parts[3].split("/")[0])
        except Exception:
            pass

        if new_ip in current_ips:
            console.print(f"[green]✓[/] statická IP {new_ip} je už nastavená")
            return

        console.print(f"""
[bold yellow]⚠  UPOZORNĚNÍ — ZMĚNA IP ADRESY[/]

  Aktuální IP:      {', '.join(current_ips) or '(neznámá)'}
  Nová statická IP: [bold]{new_ip}[/]

  Po [bold]netplan apply[/] bude stroj dostupný na [bold]{new_ip}[/].
  Pokud jste připojeni přes SSH na starou adresu, spojení se přeruší.

  Čekám 10 sekund.
""")
        for i in range(10, 0, -1):
            console.print(f"  [dim]{i}...[/]", end="\r")
            time.sleep(1)
        console.print()

    def _detect_interface(self, configured: str) -> str:
        """Ověří, že interface z configu existuje. Pokud ne, vrátí první aktivní a varuje."""
        sys_net = Path("/sys/class/net")
        if (sys_net / configured).exists():
            return configured
        candidates = [
            d.name for d in sys_net.iterdir()
            if d.name not in ("lo",) and (sys_net / d.name / "operstate").exists()
        ]
        if candidates:
            found = candidates[0]
            console.print(
                f"[yellow]Pozor:[/] interface '{configured}' z configu neexistuje. "
                f"Použiji '{found}'. Opravte servers.dc2.interface v config.yaml."
            )
            return found
        return configured

    def _verify_gateway(self) -> None:
        gw = str(self.cfg.network.gateway)
        r = self.runner.sh(["ping", "-c", "2", "-W", "3", gw], check=False)
        if r.ok:
            console.print(f"[green]✓[/] gateway {gw} dostupná")
        else:
            console.print(
                f"[red]✗ Gateway {gw} není dostupná po netplan apply![/]\n"
                f"Zkontrolujte: servers.dc2.interface a network.gateway v configu."
            )

    def _enable_samba_ad_dc(self) -> None:
        self.runner.sh(["systemctl", "unmask", "samba-ad-dc"])
        self.runner.systemd_enable_now("samba-ad-dc")
