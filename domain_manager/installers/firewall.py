"""Firewall - nftables.

Pravidla pro DC1/DC2 jsou statická (AD porty + SSH + manager). Per-počítač
pravidla se generují z DB management engine a aplikují buď na centrálním FW
(pokud děláte routovaný setup) nebo přes Ansible na klientech (Windows
Defender Firewall přes PowerShell).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .base import BaseInstaller


class FirewallInstaller(BaseInstaller):
    name = "Firewall (nftables)"

    PACKAGES = ["nftables"]

    # Porty potřebné pro Samba AD DC
    AD_PORTS_TCP = [53, 88, 135, 139, 389, 445, 464, 636, 3268, 3269, 49152, 49153, 49154, 49155]
    AD_PORTS_UDP = [53, 88, 123, 137, 138, 389, 464]

    def install(self) -> None:
        self.runner.apt_install(self.PACKAGES)
        self._write_ruleset()
        self.runner.sh(["nft", "-f", "/etc/nftables.conf"])
        self.runner.systemd_enable_now("nftables")

    def _write_ruleset(self) -> None:
        trusted = " , ".join(self.cfg.firewall.trusted_networks)
        ad_tcp = ", ".join(str(p) for p in self.AD_PORTS_TCP)
        ad_udp = ", ".join(str(p) for p in self.AD_PORTS_UDP)
        mgr_port = self.cfg.manager.bind_port

        # Pi-hole/Grafana/Zabbix porty z trusted sítí
        extra_tcp = []
        if self.cfg.pihole.enabled:
            extra_tcp += [self.cfg.pihole.web_port, self.cfg.pihole.dns_port]
        if self.cfg.monitoring.enabled:
            extra_tcp += [
                self.cfg.monitoring.prometheus.port,
                self.cfg.monitoring.grafana.port,
                self.cfg.monitoring.zabbix.port,
                9100,  # node_exporter
            ]
        extra_tcp.append(mgr_port)
        extra_tcp_str = ", ".join(str(p) for p in extra_tcp)

        ruleset = dedent(f"""\
            #!/usr/sbin/nft -f
            # Domain Manager - nftables ruleset
            flush ruleset

            table inet filter {{
                chain input {{
                    type filter hook input priority 0; policy {self.cfg.firewall.default_policy};

                    # base
                    iif lo accept
                    ct state established,related accept
                    ct state invalid drop

                    # ICMP
                    icmp type {{ echo-request }} accept
                    icmpv6 type {{ echo-request, nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert }} accept

                    # SSH ze všech trusted sítí
                    ip saddr {{ {trusted} }} tcp dport 22 accept

                    # AD porty z trusted
                    ip saddr {{ {trusted} }} tcp dport {{ {ad_tcp} }} accept
                    ip saddr {{ {trusted} }} udp dport {{ {ad_udp} }} accept

                    # Management + Pi-hole + monitoring porty
                    ip saddr {{ {trusted} }} tcp dport {{ {extra_tcp_str} }} accept

                    # DHCP server (kromě DC2 - jen na DC1; ale necháme oba pro failover)
                    udp dport {{ 67, 68 }} accept

                    counter
                }}

                chain forward {{
                    type filter hook forward priority 0; policy drop;
                }}

                chain output {{
                    type filter hook output priority 0; policy accept;
                }}
            }}
            """)
        self.runner.write_file(Path("/etc/nftables.conf"), ruleset, mode=0o644)
