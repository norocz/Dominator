"""Hromadný import z CSV souborů.

Formáty CSV (UTF-8, středník nebo čárka):

  users.csv:
    username;first_name;last_name;email;password;groups
    jnovak;Jan;Novák;jnovak@firma.cz;TajneHeslo123!;ucetni,vsichni

  computers.csv:
    hostname;mac;ip;description;groups
    pc-ucetni-01;aa:bb:cc:dd:ee:ff;192.168.10.50;PC pro účetní;ucetni-pc

  groups.csv:
    name;description;kind
    ucetni;Účetní oddělení;user
    ucetni-pc;Počítače účetních;computer

Pravidla:
  - Hesla v CSV jsou nouzové řešení; lepší je generovat náhodně a posílat
    uživatelům přes jiný kanál. Pokud `password` chybí, vygeneruje se náhodné.
  - Skupiny v `groups` musí existovat předem (nejdřív import groups.csv).
  - Dry-run režim je default - skutečně provede až s `--apply`.
"""
from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..config import Config
from .client import ADClient, ADComputer, ADUser

console = Console()


def _detect_dialect(path: Path) -> csv.Dialect:
    """Auto-detekce oddělovače (čárka vs středník).

    csv.Sniffer občas selže (krátké soubory, diakritika). Fallback:
    spočítáme výskyty obou znaků v prvním řádku, vítězí ten s vyšším počtem.
    """
    with path.open("r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        first_line = sample.split("\n", 1)[0]
        if first_line.count(";") > first_line.count(","):
            return csv.excel_tab if first_line.count("\t") > first_line.count(";") else _Semicolon
        return csv.excel


class _Semicolon(csv.Dialect):
    """CSV dialect s ';' jako oddělovačem - česká excel-friendly varianta."""
    delimiter = ";"
    quotechar = '"'
    doublequote = True
    skipinitialspace = False
    lineterminator = "\r\n"
    quoting = csv.QUOTE_MINIMAL


class UserImporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ad = ADClient(cfg)

    def import_csv(self, path: Path, *, dry_run: bool = True) -> None:
        users = list(self._parse(path))
        self._preview(users)
        if dry_run:
            console.print("[yellow]Dry-run - nic neprovedeno. Pro reálný import: --apply[/]")
            return
        created = 0
        for u in users:
            if self.ad.create_user(u):
                created += 1
        console.print(f"[green]Hotovo. Vytvořeno {created} z {len(users)} uživatelů.[/]")

    def _parse(self, path: Path):
        dialect = _detect_dialect(path)
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                groups = [g.strip() for g in (row.get("groups") or "").split(",") if g.strip()]
                yield ADUser(
                    username=row["username"].strip(),
                    first_name=row["first_name"].strip(),
                    last_name=row["last_name"].strip(),
                    email=(row.get("email") or "").strip() or None,
                    password=(row.get("password") or "").strip() or None,
                    groups=groups,
                )

    def _preview(self, users: list[ADUser]) -> None:
        table = Table(title=f"Náhled importu uživatelů ({len(users)})")
        for col in ("username", "first_name", "last_name", "email", "groups", "password"):
            table.add_column(col)
        for u in users[:30]:  # max 30 řádků náhledu
            table.add_row(
                u.username, u.first_name, u.last_name,
                u.email or "",
                ", ".join(u.groups or []),
                "***" if u.password else "[random]",
            )
        console.print(table)
        if len(users) > 30:
            console.print(f"[dim]... a dalších {len(users) - 30} řádků[/]")


class ComputerImporter:
    """Import počítačů z CSV.

    Povinný sloupec: `hostname`.
    Volitelné sloupce (cokoli z níže), neuvedené sloupce zůstanou prázdné:

      Síť:      mac, ip, additional_macs (čárka-oddělené)
      Identita: fqdn, asset_tag, description, notes
      Hardware: manufacturer, model, serial_number, form_factor,
                cpu_model, cpu_cores, cpu_threads, ram_mb,
                storage_total_gb, storage_type, gpu
      OS:       os_family, os_name, os_version, os_build, os_arch, kernel_version
      Doména:   is_domain_joined (true/false)
      Lokace:   location, building, floor, room, department, primary_user
      Životní:  status, purchase_date (YYYY-MM-DD), purchase_price,
                purchase_currency, warranty_until, supplier, invoice_number
      Ostatní:  tags (čárka-oddělené), groups (čárka-oddělené)

    Zapisuje do AD (computer account) i do PostgreSQL (rozšířený inventář).
    """

    # Sloupce, které jdou rovnou na atribut DB modelu Computer
    DIRECT_COLUMNS = {
        "fqdn", "asset_tag", "description", "notes", "mac", "ip_reserved",
        "manufacturer", "model", "serial_number", "form_factor",
        "cpu_model", "cpu_cores", "cpu_threads", "ram_mb",
        "storage_total_gb", "storage_type", "gpu",
        "os_family", "os_name", "os_version", "os_build", "os_arch",
        "kernel_version", "is_domain_joined",
        "location", "building", "floor", "room", "department", "primary_user",
        "status", "purchase_price", "purchase_currency",
        "supplier", "invoice_number",
    }
    INT_COLUMNS = {
        "cpu_cores", "cpu_threads", "ram_mb",
        "storage_total_gb", "purchase_price",
    }
    BOOL_COLUMNS = {"is_domain_joined"}
    DATE_COLUMNS = {"purchase_date", "warranty_until"}

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ad = ADClient(cfg)

    def import_csv(self, path: Path, *, dry_run: bool = True) -> None:
        rows = list(self._parse(path))
        self._preview(rows)
        if dry_run:
            console.print("[yellow]Dry-run. Pro reálný import: --apply[/]")
            return

        # Lazy import - DB nemusí existovat při dry-run testech
        from ..db.models import Computer, ComputerGroupMembership, Group, get_session
        from sqlalchemy.exc import IntegrityError

        ad_created = db_upserted = 0
        with get_session(self.cfg) as session:
            for row in rows:
                # 1) AD computer account (idempotentní)
                ad_obj = ADComputer(
                    hostname=row["hostname"],
                    mac=row.get("mac"),
                    ip=row.get("ip_reserved"),
                    description=row.get("description"),
                    groups=row.get("_groups", []),
                )
                if self.ad.create_computer(ad_obj):
                    ad_created += 1

                # 2) DB inventář (upsert podle hostname)
                comp = session.query(Computer).filter_by(hostname=row["hostname"]).one_or_none()
                if comp is None:
                    comp = Computer(hostname=row["hostname"])
                    session.add(comp)

                for col in self.DIRECT_COLUMNS:
                    if col in row and row[col] is not None:
                        setattr(comp, col, row[col])
                for col in self.DATE_COLUMNS:
                    if col in row and row[col] is not None:
                        setattr(comp, col, row[col])
                if row.get("_tags"):
                    comp.tags = row["_tags"]
                if row.get("additional_macs"):
                    comp.additional_macs = row["additional_macs"]

                # Skupiny - upsert členství
                for gname in row.get("_groups", []):
                    grp = session.query(Group).filter_by(name=gname).one_or_none()
                    if not grp:
                        # Skupina musí existovat v DB i v AD; varování:
                        console.print(f"[yellow]  pozor: skupina '{gname}' v DB neexistuje, přeskočeno[/]")
                        continue
                    exists = session.query(ComputerGroupMembership).filter_by(
                        computer_id=comp.id, group_id=grp.id,
                    ).first()
                    if not exists and comp.id:
                        session.add(ComputerGroupMembership(
                            computer_id=comp.id, group_id=grp.id,
                        ))

                try:
                    session.commit()
                    db_upserted += 1
                except IntegrityError as e:
                    session.rollback()
                    console.print(f"[red]✗ DB chyba na {row['hostname']}:[/] {e.orig}")

        console.print(
            f"[green]Hotovo.[/] AD vytvořeno {ad_created}/{len(rows)}, "
            f"DB upserted {db_upserted}/{len(rows)}."
        )

    def _parse(self, path: Path):
        """Vrátí seznam dictů připravených pro DB. Nepovinné sloupce -> None."""
        from datetime import datetime as _dt

        dialect = _detect_dialect(path)
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, dialect=dialect)
            for raw in reader:
                # Normalizace hodnot
                row: dict = {}
                for key, val in raw.items():
                    if key is None:
                        continue
                    key = key.strip().lower().replace(" ", "_")
                    val = (val or "").strip()
                    if val == "":
                        row[key] = None
                        continue

                    # Bool
                    if key in self.BOOL_COLUMNS:
                        row[key] = val.lower() in ("true", "1", "yes", "ano", "ano,")
                        continue
                    # Int
                    if key in self.INT_COLUMNS:
                        try:
                            row[key] = int(val)
                        except ValueError:
                            console.print(f"[yellow]  pozor: '{val}' v {key} není číslo[/]")
                            row[key] = None
                        continue
                    # Date
                    if key in self.DATE_COLUMNS:
                        try:
                            row[key] = _dt.fromisoformat(val)
                        except ValueError:
                            try:
                                row[key] = _dt.strptime(val, "%d.%m.%Y")
                            except ValueError:
                                console.print(f"[yellow]  pozor: '{val}' v {key} není datum[/]")
                                row[key] = None
                        continue
                    # Speciální: ip se mapuje na ip_reserved
                    if key == "ip":
                        row["ip_reserved"] = val
                        continue
                    row[key] = val

                if not row.get("hostname"):
                    console.print(f"[yellow]přeskočen řádek bez hostname: {raw}[/]")
                    continue

                # tags a groups
                row["_tags"] = self._split(row.pop("tags", None))
                row["_groups"] = self._split(row.pop("groups", None))
                row["additional_macs"] = self._split(row.pop("additional_macs", None))
                yield row

    @staticmethod
    def _split(val) -> list[str]:
        if not val:
            return []
        return [x.strip() for x in str(val).split(",") if x.strip()]

    def _preview(self, rows: list[dict]) -> None:
        # Náhled jen klíčových sloupců, ať to není nečitelná zeď
        table = Table(title=f"Náhled importu počítačů ({len(rows)})")
        cols = ["hostname", "mac", "ip_reserved", "manufacturer", "model",
                "os_name", "department", "_groups", "_tags"]
        for c in cols:
            table.add_column(c.lstrip("_"))
        for r in rows[:30]:
            table.add_row(*[
                ", ".join(r.get(c)) if isinstance(r.get(c), list)
                else (str(r.get(c)) if r.get(c) is not None else "")
                for c in cols
            ])
        console.print(table)
        if len(rows) > 30:
            console.print(f"[dim]... a dalších {len(rows) - 30} řádků[/]")


class GroupImporter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ad = ADClient(cfg)

    def import_csv(self, path: Path) -> None:
        dialect = _detect_dialect(path)
        created = 0
        total = 0
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                total += 1
                if self.ad.create_group(
                    row["name"].strip(),
                    description=(row.get("description") or "").strip(),
                ):
                    created += 1
        console.print(f"[green]Skupiny: vytvořeno {created} z {total}.[/]")
