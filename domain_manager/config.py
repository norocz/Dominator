"""Načítání a validace config.yaml pomocí Pydantic.

Důvod, proč mít silné modely: instalace zasahuje hluboko do systému (Samba,
síť, firewall). Špatný překlep v IP adrese nebo chybějící heslo znamená
hodiny ladění. Pydantic chytne většinu hloupých chyb ještě než se cokoli
spustí.
"""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, IPvAnyAddress, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("/etc/domain-manager/config.yaml")


# --- jednotlivé sekce ------------------------------------------------------


class DomainCfg(BaseModel):
    realm: str = Field(..., description="FQDN domény, např. FIRMA.LOCAL")
    netbios: str = Field(..., max_length=15)
    admin_password: str = Field(..., min_length=8)
    dns_forwarder: IPvAnyAddress

    @field_validator("realm")
    @classmethod
    def realm_uppercase_dotted(cls, v: str) -> str:
        if "." not in v:
            raise ValueError("realm musí obsahovat tečku (např. FIRMA.LOCAL)")
        if v != v.upper():
            # Není to fatal, Samba si poradí, ale je to konvence.
            # Tichounce normalizuju.
            v = v.upper()
        if not re.match(r"^[A-Z0-9.\-]+$", v):
            raise ValueError("realm obsahuje nepovolené znaky")
        return v

    @field_validator("netbios")
    @classmethod
    def netbios_uppercase(cls, v: str) -> str:
        if not re.match(r"^[A-Z0-9\-]+$", v.upper()):
            raise ValueError("netbios obsahuje nepovolené znaky")
        return v.upper()

    @field_validator("admin_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        # Samba defaultně vyžaduje "complex" heslo: 3 ze 4 (upper/lower/digit/special)
        groups = sum(
            bool(re.search(p, v))
            for p in (r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]")
        )
        if groups < 3:
            raise ValueError(
                "admin_password musí splňovat 3 ze 4: velké/malé písmeno, číslo, speciální znak"
            )
        return v


class NetworkCfg(BaseModel):
    subnet: str
    gateway: IPvAnyAddress
    dns_servers: list[IPvAnyAddress]

    @field_validator("subnet")
    @classmethod
    def valid_cidr(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as e:
            raise ValueError(f"subnet musí být platná CIDR notace: {e}") from e
        return v

    @model_validator(mode="after")
    def gateway_in_subnet(self) -> "NetworkCfg":
        net = ipaddress.ip_network(self.subnet, strict=False)
        if ipaddress.ip_address(str(self.gateway)) not in net:
            raise ValueError(f"gateway {self.gateway} není v podsíti {self.subnet}")
        return self


class ServerCfg(BaseModel):
    hostname: str
    ip: IPvAnyAddress
    interface: str
    role: Literal["primary", "secondary"]

    @field_validator("hostname")
    @classmethod
    def valid_hostname(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9\-]+$", v):
            raise ValueError("hostname musí být lowercase alfanum + pomlčky")
        if len(v) > 15:
            raise ValueError("hostname > 15 znaků - NetBIOS to neunese")
        return v


class ServersCfg(BaseModel):
    dc1: ServerCfg
    dc2: ServerCfg

    @model_validator(mode="after")
    def roles_complementary(self) -> "ServersCfg":
        if self.dc1.role != "primary" or self.dc2.role != "secondary":
            raise ValueError("dc1 musí být primary, dc2 secondary")
        if self.dc1.ip == self.dc2.ip:
            raise ValueError("dc1 a dc2 nemůžou mít stejnou IP")
        if self.dc1.hostname == self.dc2.hostname:
            raise ValueError("dc1 a dc2 nemůžou mít stejný hostname")
        return self


class DhcpReservation(BaseModel):
    mac: str
    ip: IPvAnyAddress
    hostname: str

    @field_validator("mac")
    @classmethod
    def valid_mac(cls, v: str) -> str:
        v = v.lower().replace("-", ":")
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", v):
            raise ValueError(f"neplatná MAC adresa: {v}")
        return v


class DhcpCfg(BaseModel):
    enabled: bool = True
    pool_start: IPvAnyAddress
    pool_end: IPvAnyAddress
    lease_time_seconds: int = 86400
    reservations: list[DhcpReservation] = Field(default_factory=list)
    ctrl_port: int = 8001   # Kea Control Agent HTTP port


class PiholeCfg(BaseModel):
    enabled: bool = True
    webpassword: str = Field(..., min_length=8)
    upstream_dns: list[IPvAnyAddress]
    dns_port: int = 5353
    web_port: int = 8081


class PrometheusCfg(BaseModel):
    port: int = 9090
    retention_days: int = 30


class GrafanaCfg(BaseModel):
    port: int = 3000
    admin_password: str = Field(..., min_length=8)


class ZabbixCfg(BaseModel):
    port: int = 8082
    db_password: str = Field(..., min_length=8)


class MonitoringCfg(BaseModel):
    enabled: bool = True
    prometheus: PrometheusCfg
    grafana: GrafanaCfg
    zabbix: ZabbixCfg


class PostgresCfg(BaseModel):
    enabled: bool = True
    db_name: str = "domainmgr"
    db_user: str = "domainmgr"
    db_password: str = Field(..., min_length=8)
    replication_password: str = Field(..., min_length=8)


class ManagerCfg(BaseModel):
    enabled: bool = True
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    secret_key: str = Field(..., min_length=32)
    session_timeout_minutes: int = 120
    uploads_dir: Path = Path("/var/lib/domain-manager/uploads")


class EgressRule(BaseModel):
    proto: Literal["tcp", "udp"]
    port: int


class FirewallCfg(BaseModel):
    enabled: bool = True
    default_policy: Literal["drop", "accept"] = "drop"
    trusted_networks: list[str]
    egress_always: list[EgressRule]


class AnsibleCfg(BaseModel):
    enabled: bool = True
    inventory_path: Path = Path("/var/lib/domain-manager/ansible/inventory")
    playbooks_path: Path = Path("/var/lib/domain-manager/ansible/playbooks")


class NotificationCfg(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = Field(default_factory=list)
    webhook_url: str = ""
    # Prahy pro alerty
    disk_alert_pct: int = 90       # disk >= X% → alert
    offline_alert_hours: int = 24   # počítač offline >= X hodin → alert
    cert_expiry_days: int = 30      # certifikát expiruje za <= X dní → alert


class BackupCfg(BaseModel):
    enabled: bool = True
    backup_dir: Path = Path("/var/lib/domain-manager/backups")
    keep_days: int = 30             # jak dlouho uchovávat zálohy


# --- hlavní config ---------------------------------------------------------


class Config(BaseModel):
    domain: DomainCfg
    network: NetworkCfg
    servers: ServersCfg
    dhcp: DhcpCfg
    pihole: PiholeCfg
    monitoring: MonitoringCfg
    postgres: PostgresCfg
    manager: ManagerCfg
    firewall: FirewallCfg
    ansible: AnsibleCfg
    notifications: NotificationCfg = Field(default_factory=NotificationCfg)
    backup: BackupCfg = Field(default_factory=BackupCfg)

    @model_validator(mode="after")
    def cross_section_checks(self) -> "Config":
        # Kontrola: DC IP adresy musí být v subnetu sítě
        net = ipaddress.ip_network(self.network.subnet, strict=False)
        for name, srv in [("dc1", self.servers.dc1), ("dc2", self.servers.dc2)]:
            if ipaddress.ip_address(str(srv.ip)) not in net:
                raise ValueError(f"{name}.ip ({srv.ip}) není v podsíti {self.network.subnet}")

        # Kontrola: DNS servery v configu by měly odpovídat DC IPs
        # (klienti budou rezolvovat doménu přes DC)
        dns_ips = {str(d) for d in self.network.dns_servers}
        dc_ips = {str(self.servers.dc1.ip), str(self.servers.dc2.ip)}
        if not dc_ips.issubset(dns_ips):
            # Není to fatal, ale dost pravděpodobně chyba
            import warnings
            warnings.warn(
                f"network.dns_servers ({dns_ips}) neobsahuje IP řadičů domény ({dc_ips}). "
                "Klienti se nemusí přihlásit do domény.",
                stacklevel=2,
            )

        # DHCP rozsah v subnetu
        if self.dhcp.enabled:
            for ip in (self.dhcp.pool_start, self.dhcp.pool_end):
                if ipaddress.ip_address(str(ip)) not in net:
                    raise ValueError(f"DHCP pool {ip} není v podsíti {self.network.subnet}")

        return self


# --- načítání --------------------------------------------------------------


def load_config(path: Path | None = None) -> Config:
    """Načte a zvaliduje config.yaml. Vyhodí ValueError s detaily při chybě."""
    path = path or _resolved_config_path()
    if not path.exists():
        if os.environ.get("DM_DEMO"):
            return _demo_config()
        raise FileNotFoundError(
            f"Konfigurace {path} neexistuje. Spusťte bootstrap.sh nebo "
            f"zkopírujte config.yaml.example a upravte."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)


def _demo_config() -> Config:
    """Ukázková konfigurace pro demo/testovací režim (dm web start --demo)."""
    return Config.model_validate({
        "domain": {
            "realm": "DEMO.LOCAL",
            "netbios": "DEMO",
            "admin_password": "Demo.Heslo123!",
            "dns_forwarder": "1.1.1.1",
        },
        "network": {
            "subnet": "192.168.10.0/24",
            "gateway": "192.168.10.1",
            "dns_servers": ["192.168.10.10", "192.168.10.11"],
        },
        "servers": {
            "dc1": {"hostname": "dc1", "ip": "192.168.10.10", "interface": "eth0", "role": "primary"},
            "dc2": {"hostname": "dc2", "ip": "192.168.10.11", "interface": "eth0", "role": "secondary"},
        },
        "dhcp": {"enabled": True, "pool_start": "192.168.10.100", "pool_end": "192.168.10.200"},
        "pihole": {"enabled": True, "webpassword": "Demo.Pihole123!", "upstream_dns": ["1.1.1.1"]},
        "monitoring": {
            "enabled": True,
            "prometheus": {"port": 9090, "retention_days": 30},
            "grafana": {"port": 3000, "admin_password": "Demo.Grafana123!"},
            "zabbix": {"port": 8082, "db_password": "Demo.Zabbix123!"},
        },
        "postgres": {
            "enabled": True,
            "db_password": "Demo.Postgres123!",
            "replication_password": "Demo.Repl123!",
        },
        "manager": {
            "enabled": True,
            "secret_key": "demo-secret-key-placeholder-32chrhr",
        },
        "firewall": {
            "enabled": False,
            "trusted_networks": ["192.168.10.0/24"],
            "egress_always": [{"proto": "tcp", "port": 443}],
        },
        "ansible": {"enabled": True},
    })


def _resolved_config_path() -> Path:
    """Najde config: env > /etc/domain-manager > current dir."""
    env = os.environ.get("DM_CONFIG")
    if env:
        return Path(env)
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return Path("config.yaml")
