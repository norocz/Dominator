"""Stránka /computers — filtrovatelná tabulka inventáře.

Architektura:
  - GET /computers              celá stránka (layout + tabulka)
  - GET /computers/table        jen tabulka (HTMX vrátí fragment)
  - GET /computers/{id}         detail jednoho počítače
  - POST /computers/{id}        update polí (z formuláře v detailu)

Filtry jdou přes URL query params, takže odkazy jsou bookmarkovatelné a
HTMX je umí push-stat (?os_family=windows&department=Účetní).
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Computer, get_session
from ...db.queries import ComputerQuery
from ...ansible.runner import AnsibleRunner
from .._audit import log_action

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

router = APIRouter(prefix="/computers")
from .._templates import templates


def _require_user(request: Request):
    if not request.session.get("user"):
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return request.session["user"]


# Sloupce, které tabulka standardně zobrazuje. Uživatel si je v UI může
# přepínat (drawer "Sloupce"). Hodnota = klíč ve fields ComputerQuery.
DEFAULT_COLUMNS = [
    "hostname", "is_online", "os_family", "ram_mb", "department",
    "location", "primary_user", "last_seen",
]

# Všechny zobrazitelné sloupce + lidský popis (pro hlavičku).
ALL_COLUMNS = {
    "hostname":          "Hostname",
    "fqdn":              "FQDN",
    "asset_tag":         "Inv. číslo",
    "mac":               "MAC",
    "ip_reserved":       "IP",
    "manufacturer":      "Výrobce",
    "model":             "Model",
    "serial_number":     "S/N",
    "form_factor":       "Typ",
    "cpu_cores":         "Jádra",
    "ram_mb":            "RAM (MB)",
    "storage_total_gb":  "Disk (GB)",
    "storage_type":      "Disk typ",
    "os_family":         "OS rodina",
    "os_name":           "OS",
    "os_version":        "OS verze",
    "is_domain_joined":  "V doméně",
    "location":          "Lokace",
    "building":          "Budova",
    "floor":             "Patro",
    "room":              "Místnost",
    "department":        "Oddělení",
    "primary_user":      "Hlavní uživatel",
    "status":            "Stav",
    "purchase_date":     "Pořízeno",
    "warranty_until":    "Záruka do",
    "supplier":          "Dodavatel",
    "is_online":         "Online",
    "last_seen":         "Naposledy",
    "last_cpu_pct":      "CPU %",
    "last_ram_used_pct": "RAM %",
    "last_disk_used_pct":"Disk %",
}


@router.get("", response_class=HTMLResponse)
async def computers_page(request: Request, user: str = Depends(_require_user)):
    """Celá stránka — layout, filtrový panel, slot pro tabulku."""
    columns = _columns_from_params(request)
    return templates.TemplateResponse("computers.html", {
        "request": request,
        "user": user,
        "all_columns": ALL_COLUMNS,
        "active_columns": columns,
        "facets": _facets(),
    })


@router.get("/table", response_class=HTMLResponse)
async def computers_table(request: Request, user: str = Depends(_require_user)):
    """Pouze tabulkový fragment — HTMX cíl pro filtry, řazení a stránkování."""
    page = int(request.query_params.get("page", 1))
    per_page = int(request.query_params.get("per_page", 50))
    columns = _columns_from_params(request)

    with get_session() as session:
        result = (
            ComputerQuery(session)
            .filter_from_params(request.query_params)
            .paginate(page=page, per_page=per_page)
        )
        rows_data = [_row_dict(r) for r in result.rows]

    return templates.TemplateResponse("computers_table.html", {
        "request": request,
        "rows": rows_data,
        "columns": columns,
        "column_labels": ALL_COLUMNS,
        "total": result.total,
        "page": result.page,
        "per_page": result.per_page,
        "total_pages": result.total_pages,
        "current_sort": request.query_params.get("sort", ""),
    })


@router.get("/{computer_id}", response_class=HTMLResponse)
async def computer_detail(computer_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        comp = session.get(Computer, computer_id)
        if not comp:
            raise HTTPException(404, "Počítač nenalezen")
        data = _row_dict(comp, full=True)
    return templates.TemplateResponse("computer_detail.html", {
        "request": request,
        "user": user,
        "c": data,
        "labels": ALL_COLUMNS,
    })


@router.post("/{computer_id}")
async def computer_update(
    computer_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    """Update polí z formuláře. Akceptuje jen pole z ALL_COLUMNS + notes."""
    form = await request.form()
    allowed = set(ALL_COLUMNS.keys()) | {"description", "notes"}
    with get_session() as session:
        comp = session.get(Computer, computer_id)
        if not comp:
            raise HTTPException(404)
        for key, value in form.items():
            if key not in allowed:
                continue
            if hasattr(comp, key):
                setattr(comp, key, value or None)
        comp.updated_by = user
        session.commit()
    return RedirectResponse(f"/computers/{computer_id}", status_code=303)


@router.post("/{computer_id}/internet-block", response_class=HTMLResponse)
async def internet_block(
    computer_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    """Zablokuje přístup k internetu přes Ansible + Pi-hole."""
    import os
    demo = os.environ.get("DM_DEMO", "0") == "1"

    with get_session() as session:
        comp = session.get(Computer, computer_id)
        if not comp:
            raise HTTPException(404)

        job_id: str | None = None
        source = "demo" if demo else "ansible"

        if not demo and comp.hostname:
            runner = AnsibleRunner(request.app.state.config)
            job_id = runner.start(
                "block_internet.yml",
                limit=comp.hostname,
                extra_vars={"target_host": comp.hostname},
            )
            source = "ansible"

        comp.internet_blocked = True
        comp.internet_block_source = source
        comp.internet_block_job_id = job_id
        comp.updated_by = user
        session.commit()
        data = _row_dict(comp)

    return templates.TemplateResponse("computer_internet_fragment.html", {
        "request": request,
        "c": data,
        "job_id": job_id,
        "message": f"Internet zablokován {'(demo)' if demo else ''}",
    })


@router.post("/{computer_id}/internet-unblock", response_class=HTMLResponse)
async def internet_unblock(
    computer_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    """Odblokuje přístup k internetu."""
    import os
    demo = os.environ.get("DM_DEMO", "0") == "1"

    with get_session() as session:
        comp = session.get(Computer, computer_id)
        if not comp:
            raise HTTPException(404)

        job_id: str | None = None

        if not demo and comp.hostname:
            runner = AnsibleRunner(request.app.state.config)
            job_id = runner.start(
                "unblock_internet.yml",
                limit=comp.hostname,
                extra_vars={"target_host": comp.hostname},
            )

        comp.internet_blocked = False
        comp.internet_block_source = None
        comp.internet_block_job_id = job_id
        comp.updated_by = user
        session.commit()
        data = _row_dict(comp)

    return templates.TemplateResponse("computer_internet_fragment.html", {
        "request": request,
        "c": data,
        "job_id": job_id,
        "message": f"Internet odblokován {'(demo)' if demo else ''}",
    })


# --- CSV import ------------------------------------------------------------

# Mapování CSV záhlaví → pole modelu Computer
_CSV_FIELD_MAP = {
    "hostname": "hostname", "host": "hostname", "name": "hostname",
    "mac": "mac", "mac_address": "mac", "mac adresa": "mac",
    "ip": "ip_reserved", "ip_reserved": "ip_reserved", "ip adresa": "ip_reserved",
    "ip_address": "ip_reserved",
    "description": "description", "popis": "description",
    "notes": "notes", "poznámky": "notes", "poznamky": "notes",
    "asset_tag": "asset_tag", "inventarni_cislo": "asset_tag",
    "inventární číslo": "asset_tag", "inv": "asset_tag",
    "serial_number": "serial_number", "sn": "serial_number",
    "serial": "serial_number", "výrobní číslo": "serial_number",
    "manufacturer": "manufacturer", "vyrobce": "manufacturer", "výrobce": "manufacturer",
    "model": "model",
    "form_factor": "form_factor", "typ": "form_factor",
    "os_family": "os_family", "os": "os_family",
    "os_name": "os_name",
    "department": "department", "oddeleni": "department", "oddělení": "department",
    "location": "location", "lokace": "location",
    "building": "building", "budova": "building",
    "floor": "floor", "patro": "floor",
    "room": "room", "mistnost": "room", "místnost": "room",
    "primary_user": "primary_user", "uzivatel": "primary_user",
    "uživatel": "primary_user", "user": "primary_user",
    "status": "status", "stav": "status",
    "supplier": "supplier", "dodavatel": "supplier",
    "warranty_until": "warranty_until", "zaruka_do": "warranty_until",
    "záruka do": "warranty_until",
    "purchase_date": "purchase_date", "datum_koupě": "purchase_date",
    "datum koupu": "purchase_date",
}

_DATE_FIELDS = {"warranty_until", "purchase_date"}


def _parse_date(val: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            pass
    return None


@router.post("/import-csv", response_class=HTMLResponse)
async def computers_import_csv(
    request: Request,
    user: str = Depends(_require_user),
    file: UploadFile = File(...),
    sync_ad: str = Form("0"),
):
    """
    Importuje počítače z CSV. Povinný sloupec: hostname.
    Ostatní sloupce jsou volitelné a namapují se na pole modelu.
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1250", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return HTMLResponse('<div id="csv-result" style="color:var(--danger)">CSV je prázdné nebo chybí záhlaví.</div>')

    # Normalizuj záhlaví (lowercase, bez mezer na okrajích)
    normalized = {k.strip().lower(): k for k in reader.fieldnames}

    created, updated, skipped, errors = [], [], [], []

    ad_client = None
    if sync_ad == "1" and not _DEMO_MODE:
        try:
            from ...ad.client import ADClient
            ad_client = ADClient(request.app.state.config)
        except Exception:
            pass

    with get_session() as session:
        for row in reader:
            # Přemapuj záhlaví
            data: dict = {}
            for csv_col, orig_col in normalized.items():
                field = _CSV_FIELD_MAP.get(csv_col)
                if not field:
                    continue
                val = (row.get(orig_col) or "").strip()
                if not val:
                    continue
                if field in _DATE_FIELDS:
                    parsed = _parse_date(val)
                    if parsed:
                        data[field] = parsed
                else:
                    data[field] = val

            hostname = data.get("hostname")
            if not hostname:
                skipped.append("(bez hostname)")
                continue

            # MAC normalizace
            if "mac" in data:
                data["mac"] = data["mac"].lower().replace("-", ":").replace(".", ":")

            existing = session.query(Computer).filter(Computer.hostname == hostname).first()
            if existing:
                # Update existujícího záznamu (pouze neprázdné hodnoty)
                for k, v in data.items():
                    if k != "hostname" and v:
                        setattr(existing, k, v)
                existing.updated_by = user
                updated.append(hostname)
            else:
                c = Computer(created_by=user, updated_by=user, **data)
                session.add(c)
                created.append(hostname)

        try:
            session.commit()
        except Exception as e:
            session.rollback()
            errors.append(str(e))

    if ad_client:
        from ...ad.client import ADComputer
        for h in created:
            try:
                ad_client.create_computer(ADComputer(hostname=h))
            except Exception as e:
                errors.append(f"{h}: {e}")

    log_action(user, "import_computers_csv", "computer", None,
               {"created": len(created), "updated": len(updated), "skipped": len(skipped)})

    parts = []
    if created:
        parts.append(f'<strong>{len(created)}</strong> vytvořeno')
    if updated:
        parts.append(f'<strong>{len(updated)}</strong> aktualizováno')
    if skipped:
        parts.append(f'{len(skipped)} přeskočeno')
    if errors:
        parts.append(f'<span style="color:var(--warning)">{len(errors)} chyb</span>')

    color = "var(--danger)" if errors and not created and not updated else "var(--success)"
    msg = ", ".join(parts) or "Žádná data"
    return HTMLResponse(
        f'<div id="csv-result" style="color:{color};padding:8px 0">{msg}.</div>'
        + ('<script>setTimeout(()=>location.reload(),1000)</script>' if created or updated else '')
    )


# --- helpers ---------------------------------------------------------------


def _columns_from_params(request: Request) -> list[str]:
    raw = request.query_params.get("cols")
    if not raw:
        return DEFAULT_COLUMNS
    cols = [c for c in raw.split(",") if c in ALL_COLUMNS]
    return cols or DEFAULT_COLUMNS


def _row_dict(c: Computer, *, full: bool = False) -> dict:
    """Computer → dict s formátovanými hodnotami pro šablonu."""
    base = {
        "id": c.id,
        "hostname": c.hostname,
        "fqdn": c.fqdn,
        "asset_tag": c.asset_tag,
        "mac": c.mac,
        "ip_reserved": c.ip_reserved,
        "manufacturer": c.manufacturer,
        "model": c.model,
        "serial_number": c.serial_number,
        "form_factor": c.form_factor,
        "cpu_cores": c.cpu_cores,
        "ram_mb": c.ram_mb,
        "storage_total_gb": c.storage_total_gb,
        "storage_type": c.storage_type,
        "os_family": c.os_family,
        "os_name": c.os_name,
        "os_version": c.os_version,
        "is_domain_joined": c.is_domain_joined,
        "location": c.location,
        "building": c.building,
        "floor": c.floor,
        "room": c.room,
        "department": c.department,
        "primary_user": c.primary_user,
        "status": c.status,
        "purchase_date": c.purchase_date.strftime("%d.%m.%Y") if c.purchase_date else None,
        "warranty_until": c.warranty_until.strftime("%d.%m.%Y") if c.warranty_until else None,
        "supplier": c.supplier,
        "is_online": c.is_online,
        "last_seen": _fmt_relative(c.last_seen),
        "last_cpu_pct": c.last_cpu_pct,
        "last_ram_used_pct": c.last_ram_used_pct,
        "last_disk_used_pct": c.last_disk_used_pct,
        "tags": c.tags or [],
        "internet_blocked": c.internet_blocked,
        "internet_block_source": c.internet_block_source,
        "internet_block_job_id": c.internet_block_job_id,
    }
    if full:
        base.update({
            "cpu_model": c.cpu_model,
            "cpu_threads": c.cpu_threads,
            "gpu": c.gpu,
            "os_build": c.os_build,
            "os_arch": c.os_arch,
            "kernel_version": c.kernel_version,
            "description": c.description,
            "notes": c.notes,
            "additional_macs": c.additional_macs or [],
            "network_interfaces": c.network_interfaces or [],
            "disks": c.disks or [],
            "installed_software": c.installed_software or [],
            "custom_fields": c.custom_fields or {},
            "extra_metrics": c.extra_metrics or {},
            "purchase_price": c.purchase_price,
            "purchase_currency": c.purchase_currency,
            "invoice_number": c.invoice_number,
            "last_boot": c.last_boot,
            "uptime_seconds": c.uptime_seconds,
        })
    return base


def _fmt_relative(dt) -> str | None:
    if not dt:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    diff = now - dt
    s = diff.total_seconds()
    if s < 60:
        return "právě teď"
    if s < 3600:
        return f"před {int(s // 60)} min"
    if s < 86400:
        return f"před {int(s // 3600)} h"
    if s < 86400 * 7:
        return f"před {int(s // 86400)} dny"
    return dt.strftime("%d.%m.%Y")


def _facets() -> dict[str, list[str]]:
    """Distinct hodnoty z DB pro rozbalovací filtry."""
    with get_session() as session:
        def distinct(col):
            rows = session.query(col).distinct().filter(col.isnot(None)).all()
            return sorted([r[0] for r in rows if r[0]])
        return {
            "os_family":    distinct(Computer.os_family),
            "department":   distinct(Computer.department),
            "building":     distinct(Computer.building),
            "form_factor":  distinct(Computer.form_factor),
            "status":       distinct(Computer.status),
            "manufacturer": distinct(Computer.manufacturer),
        }
