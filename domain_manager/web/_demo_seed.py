"""Demo seed data — plní in-memory SQLite ukázkovými daty pro dm web start --demo."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db.models import (
    Certificate, Computer, ComputerGroupMembership, DevicePosition, FloorPlan, Group,
    NetworkDevice, User, UserGroupMembership, get_session,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def seed_demo_data() -> None:
    with get_session() as session:
        if session.query(Computer).count() > 0:
            return  # Již naseedováno

        # --- Skupiny počítačů -----------------------------------------------
        g_teachers = Group(name="Učitelé-PC", kind="computer", description="Počítače v učebnách")
        g_office   = Group(name="Kanceláře",  kind="computer", description="Kancelářské PC")
        g_servers  = Group(name="Servery",    kind="computer", description="Serverová infrastruktura")

        # --- Skupiny uživatelů -----------------------------------------------
        g_it      = Group(name="IT",          kind="user", description="IT oddělení")
        g_staff   = Group(name="Pedagogové",  kind="user", description="Vyučující personál")
        g_admin   = Group(name="Administrátoři", kind="user", description="Správci domény")

        for g in (g_teachers, g_office, g_servers, g_it, g_staff, g_admin):
            session.add(g)
        session.flush()

        # --- Počítače -------------------------------------------------------
        computers = [
            Computer(
                hostname="pc-ucebna-01", fqdn="pc-ucebna-01.demo.local",
                manufacturer="Dell", model="OptiPlex 7010",
                os_family="windows", os_name="Windows 11 Pro", os_version="23H2",
                mac="00:1A:2B:3C:4D:01", ip_reserved="192.168.10.101",
                ram_mb=8192, cpu_cores=4, storage_total_gb=256, storage_type="SSD",
                form_factor="desktop", department="Učebny", building="Hlavní budova",
                floor="1", room="Učebna A1", primary_user="novakj",
                is_domain_joined=True, is_online=True,
                last_seen=_now(), last_cpu_pct=12, last_ram_used_pct=45, last_disk_used_pct=33,
                status="active",
            ),
            Computer(
                hostname="pc-ucebna-02", fqdn="pc-ucebna-02.demo.local",
                manufacturer="HP", model="EliteDesk 800 G5",
                os_family="windows", os_name="Windows 11 Pro", os_version="23H2",
                mac="00:1A:2B:3C:4D:02", ip_reserved="192.168.10.102",
                ram_mb=16384, cpu_cores=6, storage_total_gb=512, storage_type="SSD",
                form_factor="desktop", department="Učebny", building="Hlavní budova",
                floor="1", room="Učebna A2", primary_user="dvorakp",
                is_domain_joined=True, is_online=True,
                last_seen=_now(), last_cpu_pct=34, last_ram_used_pct=62, last_disk_used_pct=48,
                status="active", internet_blocked=True, internet_block_source="ansible",
            ),
            Computer(
                hostname="pc-kancelar-01", fqdn="pc-kancelar-01.demo.local",
                manufacturer="Lenovo", model="ThinkCentre M720q",
                os_family="linux", os_name="Ubuntu 24.04 LTS", os_version="24.04",
                mac="00:1A:2B:3C:4D:03", ip_reserved="192.168.10.110",
                ram_mb=16384, cpu_cores=8, storage_total_gb=1024, storage_type="NVMe",
                form_factor="desktop", department="IT", building="Hlavní budova",
                floor="2", room="Kancelář IT", primary_user="admin",
                is_domain_joined=True, is_online=True,
                last_seen=_now(), last_cpu_pct=8, last_ram_used_pct=38, last_disk_used_pct=22,
                status="active",
            ),
            Computer(
                hostname="dc1", fqdn="dc1.demo.local",
                manufacturer="HP", model="ProLiant DL20 Gen10",
                os_family="linux", os_name="Debian 12", os_version="12",
                mac="00:1A:2B:3C:4D:10", ip_reserved="192.168.10.10",
                ram_mb=32768, cpu_cores=8, storage_total_gb=2048, storage_type="SSD",
                form_factor="server", department="IT", building="Serverovna",
                floor="0", room="Rack A1",
                is_domain_joined=True, is_online=True,
                last_seen=_now(), last_cpu_pct=22, last_ram_used_pct=55, last_disk_used_pct=40,
                status="active",
            ),
            Computer(
                hostname="pc-reditels-01", fqdn="pc-reditels-01.demo.local",
                manufacturer="Apple", model="Mac mini M2",
                os_family="macos", os_name="macOS Sequoia", os_version="15.0",
                mac="00:1A:2B:3C:4D:20", ip_reserved="192.168.10.120",
                ram_mb=16384, cpu_cores=8, storage_total_gb=512, storage_type="NVMe",
                form_factor="desktop", department="Vedení", building="Hlavní budova",
                floor="3", room="Ředitelna", primary_user="reditel",
                is_domain_joined=False, is_online=False,
                last_seen=None, status="active",
            ),
            Computer(
                hostname="laptop-servis-01",
                manufacturer="Lenovo", model="ThinkPad T14s Gen4",
                os_family="linux", os_name="Ubuntu 22.04 LTS", os_version="22.04",
                mac="00:1A:2B:3C:4D:30", ip_reserved="192.168.10.130",
                ram_mb=32768, cpu_cores=12, storage_total_gb=1024, storage_type="NVMe",
                form_factor="laptop", department="IT",
                primary_user="admin",
                is_domain_joined=True, is_online=False,
                last_seen=None, status="active",
            ),
        ]

        for c in computers:
            session.add(c)
        session.flush()

        # Přiřazení do skupin
        session.add(ComputerGroupMembership(computer_id=computers[0].id, group_id=g_teachers.id))
        session.add(ComputerGroupMembership(computer_id=computers[1].id, group_id=g_teachers.id))
        session.add(ComputerGroupMembership(computer_id=computers[2].id, group_id=g_office.id))
        session.add(ComputerGroupMembership(computer_id=computers[3].id, group_id=g_servers.id))

        # --- Uživatelé -------------------------------------------------------
        users = [
            User(
                username="admin", first_name="Jan", last_name="Správce",
                display_name="Jan Správce", title="",
                email="admin@demo.local", phone="+420 777 001 001",
                department="IT", job_title="Správce sítě", employee_id="EMP-001",
                office="Kancelář IT, 2. patro", enabled=True,
                last_logon=_now(),
            ),
            User(
                username="novakj", first_name="Jan", last_name="Novák",
                display_name="Jan Novák", title="Mgr.",
                email="novakj@demo.local", phone="+420 777 002 001",
                department="Učebny", job_title="Vyučující", employee_id="EMP-002",
                office="Sbor, 1. patro", enabled=True,
                last_logon=_now(),
            ),
            User(
                username="dvorakp", first_name="Petra", last_name="Dvořák",
                display_name="Petra Dvořák", title="Bc.",
                email="dvorakp@demo.local", phone="+420 777 002 002",
                department="Učebny", job_title="Vyučující", employee_id="EMP-003",
                office="Sbor, 1. patro", enabled=True,
                last_logon=_now(),
            ),
            User(
                username="reditel", first_name="Karel", last_name="Ředitel",
                display_name="Karel Ředitel", title="Ing.",
                email="reditel@demo.local", phone="+420 777 001 100",
                department="Vedení", job_title="Ředitel", employee_id="EMP-100",
                office="Ředitelna, 3. patro", enabled=True,
                last_logon=_now(),
            ),
            User(
                username="novotnal", first_name="Lenka", last_name="Novotná",
                display_name="Lenka Novotná",
                email="novotnal@demo.local",
                department="Sekretariát", job_title="Sekretářka", employee_id="EMP-004",
                enabled=True, last_logon=_now(),
            ),
            User(
                username="stara.ucitelka", first_name="Marie", last_name="Stará",
                display_name="Marie Stará", title="RNDr.",
                email="stara@demo.local",
                department="Učebny", job_title="Vyučující — na mateřské", employee_id="EMP-005",
                enabled=False,
            ),
        ]

        for u in users:
            session.add(u)
        session.flush()

        # Přiřazení uživatelů do skupin
        session.add(UserGroupMembership(user_id=users[0].id, group_id=g_it.id))
        session.add(UserGroupMembership(user_id=users[0].id, group_id=g_admin.id))
        session.add(UserGroupMembership(user_id=users[1].id, group_id=g_staff.id))
        session.add(UserGroupMembership(user_id=users[2].id, group_id=g_staff.id))
        session.add(UserGroupMembership(user_id=users[3].id, group_id=g_admin.id))

        # --- Síťová zařízení (SNMP) -----------------------------------------
        now = _now()
        devices = [
            NetworkDevice(
                hostname="switch-main", ip="192.168.10.2",
                community="public", snmp_version="2c",
                device_type="switch", manufacturer="MikroTik",
                model="CRS326-24G-2S+", location="Serverovna, rack A1",
                sys_name="switch-main", sys_description="RouterOS v7.12",
                sys_uptime_seconds=86400 * 45,
                port_stats={
                    "1": {"name": "ether1", "alias": "Uplink ISP", "oper_status": "up", "speed_mbps": 1000, "in_bytes": 52428800, "out_bytes": 10485760, "in_errors": 0, "out_errors": 0},
                    "2": {"name": "ether2", "alias": "DC1", "oper_status": "up", "speed_mbps": 1000, "in_bytes": 20971520, "out_bytes": 5242880, "in_errors": 0, "out_errors": 0},
                    "3": {"name": "ether3", "alias": "DC2", "oper_status": "up", "speed_mbps": 1000, "in_bytes": 15728640, "out_bytes": 4194304, "in_errors": 0, "out_errors": 0},
                    "24": {"name": "ether24", "alias": "Switch PC učebna A", "oper_status": "up", "speed_mbps": 1000, "in_bytes": 8388608, "out_bytes": 2097152, "in_errors": 0, "out_errors": 0},
                },
                connected_macs={
                    "00:1a:2b:3c:4d:01": 24,
                    "00:1a:2b:3c:4d:02": 24,
                    "00:1a:2b:3c:4d:10": 2,
                    "00:1a:2b:3c:4d:11": 3,
                },
                last_sync=now, created_by="system",
            ),
            NetworkDevice(
                hostname="switch-ucebna-a", ip="192.168.10.3",
                community="public", snmp_version="2c",
                device_type="switch", manufacturer="TP-Link",
                model="TL-SG1024D", location="Učebna A, pod tabulí",
                sys_name="switch-ucebna-a",
                sys_description="TP-Link TL-SG1024D",
                sys_uptime_seconds=86400 * 12,
                port_stats={
                    "1": {"name": "port1", "alias": "Uplink", "oper_status": "up", "speed_mbps": 1000, "in_bytes": 4194304, "out_bytes": 1048576, "in_errors": 0, "out_errors": 0},
                    "2": {"name": "port2", "alias": "PC-ucebna-01", "oper_status": "up", "speed_mbps": 100, "in_bytes": 2097152, "out_bytes": 524288, "in_errors": 0, "out_errors": 0},
                    "3": {"name": "port3", "alias": "PC-ucebna-02", "oper_status": "up", "speed_mbps": 100, "in_bytes": 1572864, "out_bytes": 393216, "in_errors": 0, "out_errors": 0},
                },
                connected_macs={
                    "00:1a:2b:3c:4d:01": 2,
                    "00:1a:2b:3c:4d:02": 3,
                },
                last_sync=now, created_by="system",
            ),
        ]
        for d in devices:
            session.add(d)

        # --- Certifikáty ----------------------------------------------------
        certs = [
            Certificate(
                hostname="dc1.demo.local", port=443,
                subject_cn="dc1.demo.local",
                issuer="Dominator Internal CA",
                not_before=now - timedelta(days=90),
                not_after=now + timedelta(days=127),
                serial="0x1A2B3C",
                fingerprint_sha256="aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
                last_checked=now, notes="Interní CA certifikát DC1",
                created_by="system",
            ),
            Certificate(
                hostname="dc2.demo.local", port=443,
                subject_cn="dc2.demo.local",
                issuer="Dominator Internal CA",
                not_before=now - timedelta(days=90),
                not_after=now + timedelta(days=8),   # Expiruje brzy!
                serial="0x1A2B3D",
                fingerprint_sha256="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                last_checked=now, notes="POZOR: expiruje za 8 dní",
                created_by="system",
            ),
            Certificate(
                hostname="pihole.demo.local", port=80,
                subject_cn="pihole.demo.local",
                issuer="Let's Encrypt Authority X3",
                not_before=now - timedelta(days=60),
                not_after=now + timedelta(days=30),
                serial="0xDEADBEEF",
                last_checked=now, notes="Pi-hole webový certifikát",
                created_by="system",
            ),
        ]
        for c in certs:
            session.add(c)

        # --- Plánky budovy --------------------------------------------------
        # Cesta relativní k static/uploads/ — app.py je servíruje pod /static/uploads/
        plan_building = FloorPlan(
            name="Hlavní budova",
            image_path="demo_floor_1.svg",
            width_px=1000, height_px=680,
            parent_id=None,
        )
        session.add(plan_building)
        session.flush()

        plan_floor2 = FloorPlan(
            name="2. patro",
            image_path="demo_floor_2.svg",
            width_px=1000, height_px=680,
            parent_id=plan_building.id,
        )
        session.add(plan_floor2)
        session.flush()

        # Umísti počítače na 1. patro (plan_building)
        # Souřadnice jsou v pixelech vzhledem k SVG 1000×680
        positions = [
            # Učebna A1
            DevicePosition(computer_id=computers[0].id, plan_id=plan_building.id, x=110, y=180, icon="desktop"),
            DevicePosition(computer_id=computers[1].id, plan_id=plan_building.id, x=200, y=180, icon="desktop"),
            # IT kancelář
            DevicePosition(computer_id=computers[2].id, plan_id=plan_building.id, x=800, y=160, icon="desktop"),
            DevicePosition(computer_id=computers[5].id, plan_id=plan_building.id, x=870, y=200, icon="laptop"),
            # Serverovna
            DevicePosition(computer_id=computers[3].id, plan_id=plan_building.id, x=800, y=500, icon="server"),
            # Ředitelna
            DevicePosition(computer_id=computers[4].id, plan_id=plan_building.id, x=450, y=500, icon="desktop"),
        ]
        for pos in positions:
            session.add(pos)

        session.commit()
