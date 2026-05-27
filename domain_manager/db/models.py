"""SQLAlchemy modely.

Databáze je SSOT (single source of truth) pro management metadata:
  - HW info, MAC adresy, IP rezervace
  - Pozice na plánku budovy
  - Vlastní atributy a tagy
  - Politiky a jejich aplikace na entity
  - Audit log změn

AD je SSOT pro identity (uživatelé, počítače jako objekty AD, skupiny).
Synchronizace: management engine → AD (přes ADClient), data o HW jdou
z Prometheus → management DB (cache).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _now_utc() -> datetime:
    """Naive UTC pro DB - náhrada za deprecovaný datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


# --- skupiny ---------------------------------------------------------------

class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # 'user' | 'computer'
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


# --- počítače --------------------------------------------------------------
#
# Plnohodnotný inventář, ne jen "hostname+MAC". Sloupce jsou navržené pro
# rychlé filtrování a třídění (indexy, ne JSON). Pole, která jsou opravdu
# variabilní (Win-only registry hodnoty apod.), jdou do `custom_fields` JSON.

class Computer(Base):
    __tablename__ = "computers"

    # --- identita ---------------------------------------------------------
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fqdn: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    asset_tag: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    # Interní inventární číslo (např. "PC-2024-0042")

    # --- síť --------------------------------------------------------------
    mac: Mapped[Optional[str]] = mapped_column(String(17), unique=True, index=True)
    ip_reserved: Mapped[Optional[str]] = mapped_column(String(45), index=True)
    # Další síťové rozhraní (WiFi MAC, sekundární NIC apod.)
    additional_macs: Mapped[Optional[list]] = mapped_column(JSON)
    # Plné rozhraní (eth0, wlan0, IP, gw, speed) - načítáno z node_exporter
    network_interfaces: Mapped[Optional[list]] = mapped_column(JSON)

    # --- hardware ---------------------------------------------------------
    manufacturer: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # "Dell", "Lenovo", "HP", "Apple", "vlastní sestava"
    model: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    form_factor: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # "desktop" | "laptop" | "server" | "vm" | "thin-client" | "all-in-one"

    cpu_model: Mapped[Optional[str]] = mapped_column(String(128))
    cpu_cores: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    cpu_threads: Mapped[Optional[int]] = mapped_column(Integer)
    ram_mb: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    storage_total_gb: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    storage_type: Mapped[Optional[str]] = mapped_column(String(16))
    # "HDD" | "SSD" | "NVMe" | "mixed"
    disks: Mapped[Optional[list]] = mapped_column(JSON)
    # [{"device": "/dev/sda", "size_gb": 512, "type": "SSD", "model": "..."}]
    gpu: Mapped[Optional[str]] = mapped_column(String(128))

    # --- operační systém --------------------------------------------------
    os_family: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    # "windows" | "linux" | "macos" | "other"
    os_name: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # "Windows 11 Pro", "Ubuntu 24.04 LTS", "macOS Sequoia"
    os_version: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    os_build: Mapped[Optional[str]] = mapped_column(String(64))
    os_arch: Mapped[Optional[str]] = mapped_column(String(16))
    # "x86_64" | "arm64" | "x86"
    kernel_version: Mapped[Optional[str]] = mapped_column(String(64))

    # --- doména -----------------------------------------------------------
    is_domain_joined: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ad_dn: Mapped[Optional[str]] = mapped_column(String(512))
    # Distinguished name v AD - link k AD objektu

    # --- lokace a vlastnictví --------------------------------------------
    location: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    # "Praha - kancelář 2.05" nebo strukturovaně přes plánek
    building: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    floor: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    room: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    department: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # "Účetní", "IT", "Marketing"
    primary_user: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # username hlavního uživatele (volný text, neplést s ACL)

    # --- životní cyklus ---------------------------------------------------
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # "active" | "inactive" | "decommissioned" | "stock" | "repair" | "lost"
    purchase_date: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    purchase_price: Mapped[Optional[int]] = mapped_column(Integer)
    # v haléřích nebo centech (integer = bezpečnější než float)
    purchase_currency: Mapped[Optional[str]] = mapped_column(String(3))
    warranty_until: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    supplier: Mapped[Optional[str]] = mapped_column(String(128))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(64))

    # --- stav z monitoringu (cache z Prometheus) -------------------------
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_boot: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    uptime_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # Naposledy odečtené metriky (refresh nightly z Prometheus)
    last_cpu_pct: Mapped[Optional[int]] = mapped_column(Integer)
    last_ram_used_pct: Mapped[Optional[int]] = mapped_column(Integer)
    last_disk_used_pct: Mapped[Optional[int]] = mapped_column(Integer)

    # Cokoli dalšího ze sběru (per OS), neindexované - jen pro zobrazení
    extra_metrics: Mapped[Optional[dict]] = mapped_column(JSON)

    # --- software (cache) -------------------------------------------------
    installed_software: Mapped[Optional[list]] = mapped_column(JSON)
    # [{"name": "Firefox", "version": "126.0", "publisher": "Mozilla"}]
    last_software_scan: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # --- tagy a vlastní pole ---------------------------------------------
    tags: Mapped[Optional[list]] = mapped_column(JSON)
    # ["ucetni-pc", "prizemi", "ucitelna-2"] - pro rychlé filtrování
    custom_fields: Mapped[Optional[dict]] = mapped_column(JSON)
    # Libovolné key/value pro to, co tabulkový model nepokrývá

    # --- řízení přístupu k internetu --------------------------------------
    internet_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Mechanismus, kterým bylo blokování aplikováno: 'ansible' | 'pihole' | None
    internet_block_source: Mapped[Optional[str]] = mapped_column(String(16))
    # ID posledního Ansible jobu pro blokování/odblokování
    internet_block_job_id: Mapped[Optional[str]] = mapped_column(String(32))

    # --- audit ------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now_utc, onupdate=_now_utc
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))

    # --- relace -----------------------------------------------------------
    memberships: Mapped[list["ComputerGroupMembership"]] = relationship(back_populates="computer")
    position: Mapped[Optional["DevicePosition"]] = relationship(back_populates="computer")


class ComputerGroupMembership(Base):
    __tablename__ = "computer_group_memberships"
    __table_args__ = (UniqueConstraint("computer_id", "group_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    computer_id: Mapped[int] = mapped_column(ForeignKey("computers.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))

    computer: Mapped[Computer] = relationship(back_populates="memberships")
    group: Mapped[Group] = relationship()


# --- uživatelé -------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    # --- identita ---------------------------------------------------------
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(64), index=True)
    last_name: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    title: Mapped[Optional[str]] = mapped_column(String(128))
    # "Ing. CSc.", "MUDr.", "PhDr."

    # --- kontakt ----------------------------------------------------------
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    mobile: Mapped[Optional[str]] = mapped_column(String(32))

    # --- organizace -------------------------------------------------------
    department: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    manager_username: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    employee_id: Mapped[Optional[str]] = mapped_column(String(32), unique=True, index=True)
    office: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    # "Praha - 2. patro, kancelář 205"

    # --- stav -------------------------------------------------------------
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_logon: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    password_last_set: Mapped[Optional[datetime]] = mapped_column(DateTime)
    account_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)

    # --- ostatní ----------------------------------------------------------
    notes: Mapped[Optional[str]] = mapped_column(Text)
    tags: Mapped[Optional[list]] = mapped_column(JSON)
    custom_fields: Mapped[Optional[dict]] = mapped_column(JSON)
    ad_dn: Mapped[Optional[str]] = mapped_column(String(512))

    # --- audit ------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now_utc, onupdate=_now_utc
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))

    memberships: Mapped[list["UserGroupMembership"]] = relationship(back_populates="user")


class UserGroupMembership(Base):
    __tablename__ = "user_group_memberships"
    __table_args__ = (UniqueConstraint("user_id", "group_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))

    user: Mapped[User] = relationship(back_populates="memberships")
    group: Mapped[Group] = relationship()


# --- plánky budov ----------------------------------------------------------

class FloorPlan(Base):
    """Půdorys budovy / patra / místnosti. Obrázek + metadata."""
    __tablename__ = "floor_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    image_path: Mapped[str] = mapped_column(String(512))  # /var/lib/.../uploads/plan1.png
    width_px: Mapped[Optional[int]] = mapped_column(Integer)
    height_px: Mapped[Optional[int]] = mapped_column(Integer)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("floor_plans.id"))  # hierarchie
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)

    positions: Mapped[list["DevicePosition"]] = relationship(back_populates="plan")


class DevicePosition(Base):
    """Pozice počítače na plánku."""
    __tablename__ = "device_positions"
    __table_args__ = (UniqueConstraint("computer_id"),)  # počítač má max 1 pozici
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    computer_id: Mapped[int] = mapped_column(ForeignKey("computers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("floor_plans.id"))
    x: Mapped[int] = mapped_column(Integer)  # px
    y: Mapped[int] = mapped_column(Integer)
    icon: Mapped[Optional[str]] = mapped_column(String(32))  # 'desktop', 'laptop', 'printer'...

    computer: Mapped[Computer] = relationship(back_populates="position")
    plan: Mapped[FloorPlan] = relationship(back_populates="positions")


# --- politiky --------------------------------------------------------------

class Policy(Base):
    """Politika - sada nastavení aplikovatelná na entitu.

    Typy:
      - firewall  : nftables/Windows FW pravidla
      - pihole    : blocklisty, allowlisty
      - software  : balíčky k instalaci (Ansible)
      - settings  : libovolné key/value nastavení

    Dědičnost (od nejnižší k nejvyšší prioritě):
      computer_group → computer → user_group → user
    Vyšší priorita přepisuje (merge per klíč).
    """
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # 'firewall'|'pihole'|'software'|'settings'
    spec: Mapped[dict] = mapped_column(JSON)        # konkrétní obsah politiky
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)


class PolicyAssignment(Base):
    """Přiřazení politiky na entitu."""
    __tablename__ = "policy_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    target_type: Mapped[str] = mapped_column(String(32))  # 'computer'|'computer_group'|'user'|'user_group'
    target_id: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # vyšší vyhrává při konfliktech


# --- certifikáty -----------------------------------------------------------

class Certificate(Base):
    """SSL/TLS certifikát monitorovaného hostu."""
    __tablename__ = "certificates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    port: Mapped[int] = mapped_column(Integer, default=443)
    subject_cn: Mapped[Optional[str]] = mapped_column(String(255))
    subject_san: Mapped[Optional[list]] = mapped_column(JSON)   # [alt names]
    issuer: Mapped[Optional[str]] = mapped_column(String(255))
    not_before: Mapped[Optional[datetime]] = mapped_column(DateTime)
    not_after: Mapped[Optional[datetime]] = mapped_column(DateTime)
    serial: Mapped[Optional[str]] = mapped_column(String(64))
    fingerprint_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    last_checked: Mapped[Optional[datetime]] = mapped_column(DateTime)
    check_error: Mapped[Optional[str]] = mapped_column(String(255))
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))


# --- síťová zařízení (SNMP) -----------------------------------------------

class NetworkDevice(Base):
    """Switch, router, AP — správa přes SNMP."""
    __tablename__ = "network_devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ip: Mapped[str] = mapped_column(String(45), index=True)
    community: Mapped[str] = mapped_column(String(64), default="public")
    snmp_version: Mapped[str] = mapped_column(String(4), default="2c")
    device_type: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # 'switch' | 'router' | 'ap' | 'firewall' | 'other'
    manufacturer: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # 'MikroTik' | 'Cisco' | 'TP-Link' | 'Ubiquiti'
    model: Mapped[Optional[str]] = mapped_column(String(128))
    location: Mapped[Optional[str]] = mapped_column(String(128))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Cached SNMP data
    sys_name: Mapped[Optional[str]] = mapped_column(String(255))
    sys_description: Mapped[Optional[str]] = mapped_column(Text)
    sys_uptime_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    # {port_id: {name, alias, status, speed_mbps, in_bytes, out_bytes, errors}}
    port_stats: Mapped[Optional[dict]] = mapped_column(JSON)
    # {mac_address: port_id} — forwarding table (MAC→port mapping)
    connected_macs: Mapped[Optional[dict]] = mapped_column(JSON)
    # {vlan_id: name}
    vlans: Mapped[Optional[dict]] = mapped_column(JSON)
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    sync_error: Mapped[Optional[str]] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, onupdate=_now_utc)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))


# --- zálohy ----------------------------------------------------------------

class BackupRecord(Base):
    """Záznam o provedené záloze."""
    __tablename__ = "backup_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backup_type: Mapped[str] = mapped_column(String(32))  # 'db'|'samba'|'pihole'|'full'
    status: Mapped[str] = mapped_column(String(16))       # 'running'|'success'|'failed'
    file_path: Mapped[Optional[str]] = mapped_column(String(512))
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    ansible_job_id: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))


# --- ansible akce ----------------------------------------------------------

class AnsibleAction(Base):
    """Pojmenovaná Ansible akce uložená v DB (nezávislá na souborovém systému)."""
    __tablename__ = "ansible_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="Obecné")
    # Čárkou oddělené typy cílů: computer,computer_group,user,user_group
    targets: Mapped[str] = mapped_column(String(128), default="computer,computer_group")
    playbook: Mapped[str] = mapped_column(Text, nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, onupdate=_now_utc)

    def targets_list(self) -> list[str]:
        return [t.strip() for t in self.targets.split(",") if t.strip()]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "category": self.category,
            "targets": self.targets,
            "targets_list": self.targets_list(),
            "is_builtin": self.is_builtin,
            "playbook": self.playbook,
        }


# --- audit log -------------------------------------------------------------

class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now_utc, index=True)
    actor: Mapped[str] = mapped_column(String(64))  # uživatel nebo 'system'
    action: Mapped[str] = mapped_column(String(64))  # 'create_user', 'change_policy', ...
    target_type: Mapped[Optional[str]] = mapped_column(String(32))
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    details: Mapped[Optional[dict]] = mapped_column(JSON)


# --- engine / session factory ---------------------------------------------

_engine = None
_SessionLocal = None


def get_engine(cfg=None):
    global _engine
    if _engine is None:
        import os
        if os.environ.get("DM_DEMO"):
            # StaticPool = jedna sdílená connection → in-memory data přetrvávají
            _engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(_engine)
        else:
            from ..config import load_config
            cfg = cfg or load_config()
            pg = cfg.postgres
            url = f"postgresql+psycopg://{pg.db_user}:{pg.db_password}@localhost/{pg.db_name}"
            _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def get_session(cfg=None):
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(cfg), expire_on_commit=False)
    return _SessionLocal()


def create_all(cfg=None) -> None:
    """Vytvoří všechny tabulky. Bootstrap a testy."""
    Base.metadata.create_all(get_engine(cfg))
