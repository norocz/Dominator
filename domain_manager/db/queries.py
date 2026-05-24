"""Dotazovací vrstva pro filtrovatelné tabulky.

Návrh: filtr je seznam podmínek `(pole, operátor, hodnota)`. Engine je
přeloží na SQLAlchemy `Query` s WHERE klauzulemi. Podporujeme:

    eq, ne          - rovnost
    lt, le, gt, ge  - porovnání (čísla, datumy)
    in, notin       - členství v seznamu
    contains        - LIKE %x% (case-insensitive)
    starts, ends    - LIKE x%, %x
    is_null, not_null

Speciální operátory pro JSON pole:
    tag_has         - tag je v seznamu `tags`
    custom_eq       - custom_fields[key] == value

Filtr se serializuje do URL query params (`?os_family=windows&ram_mb__ge=8192`)
takže UI HTMX prostě postupně přidává filtry a klikací na sloupce řadí.

Volání:
    q = ComputerQuery(session).filter_from_params(request.query_params)
    rows, total = q.paginate(page=1, per_page=50)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, asc, cast, desc, func, or_
from sqlalchemy.orm import Query, Session

from .models import Computer, User


# Mapování suffixu v URL -> SQL operátor.
# `os_family__contains=Win` -> Computer.os_family ILIKE '%Win%'
OPERATORS = {
    "eq": lambda col, v: col == v,
    "ne": lambda col, v: col != v,
    "lt": lambda col, v: col < v,
    "le": lambda col, v: col <= v,
    "gt": lambda col, v: col > v,
    "ge": lambda col, v: col >= v,
    "in": lambda col, v: col.in_(v if isinstance(v, list) else [s.strip() for s in str(v).split(",")]),
    "notin": lambda col, v: col.notin_(v if isinstance(v, list) else [s.strip() for s in str(v).split(",")]),
    "contains": lambda col, v: col.ilike(f"%{v}%"),
    "starts": lambda col, v: col.ilike(f"{v}%"),
    "ends": lambda col, v: col.ilike(f"%{v}"),
    "is_null": lambda col, v: col.is_(None) if _truthy(v) else col.isnot(None),
    "not_null": lambda col, v: col.isnot(None) if _truthy(v) else col.is_(None),
}


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes", "ano")


def _coerce(value: str, col_type: type) -> Any:
    """Z URL přijde vždycky string, ale my potřebujeme int/datetime/bool."""
    if value == "":
        return None
    if col_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if col_type is bool:
        return _truthy(value)
    if col_type is datetime:
        # ISO 8601 nebo "YYYY-MM-DD"
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


@dataclass
class PageResult:
    rows: list[Any]
    total: int
    page: int
    per_page: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)


class BaseQuery:
    """Bázová třída pro filtrovatelné dotazy."""

    model: type  # přepíše potomek
    searchable_text: list[str] = []
    # Sloupce, ve kterých hledá fulltext (`?q=text`)

    # Mapa: name -> (column, python_type). Potomek vyplní.
    fields: dict[str, tuple[Any, type]] = {}

    def __init__(self, session: Session):
        self.session = session
        self.q: Query = session.query(self.model)
        self._has_filters = False

    # --- filtry z dict / query params ------------------------------------

    def filter_from_params(self, params) -> "BaseQuery":
        """Přijme dict nebo MultiDict (Starlette QueryParams)."""
        # Fulltext - hledá ve sloupcích z `searchable_text`. Atributy bereme
        # přímo z modelu, ne ze `fields` (fulltext sloupce nemusí být filtrovatelné).
        text = params.get("q") if hasattr(params, "get") else None
        if text and self.searchable_text:
            clauses = []
            for fname in self.searchable_text:
                col = getattr(self.model, fname, None)
                if col is not None:
                    clauses.append(col.ilike(f"%{text}%"))
            if clauses:
                self.q = self.q.filter(or_(*clauses))
                self._has_filters = True

        # Strukturované: <field>__<op>=val nebo <field>=val (=eq)
        for key, value in (params.multi_items() if hasattr(params, "multi_items") else params.items()):
            if key in ("q", "sort", "page", "per_page"):
                continue
            if "__" in key:
                fname, op = key.split("__", 1)
            else:
                fname, op = key, "eq"
            if fname not in self.fields:
                continue
            if op not in OPERATORS:
                continue
            col, py_type = self.fields[fname]
            coerced = _coerce(value, py_type)
            if coerced is None and op not in ("is_null", "not_null"):
                continue
            self.q = self.q.filter(OPERATORS[op](col, coerced))
            self._has_filters = True

        # Řazení: ?sort=hostname nebo ?sort=-ram_mb (mínus = desc)
        sort = params.get("sort") if hasattr(params, "get") else None
        if sort:
            direction = desc if sort.startswith("-") else asc
            fname = sort.lstrip("-")
            if fname in self.fields:
                col, _ = self.fields[fname]
                self.q = self.q.order_by(direction(col))

        return self

    # --- stránkování -----------------------------------------------------

    def paginate(self, *, page: int = 1, per_page: int = 50) -> PageResult:
        page = max(1, page)
        per_page = max(1, min(500, per_page))
        total = self.q.with_entities(func.count()).order_by(None).scalar() or 0
        rows = self.q.offset((page - 1) * per_page).limit(per_page).all()
        return PageResult(rows=rows, total=total, page=page, per_page=per_page)


# --- konkrétní queries ------------------------------------------------------


class ComputerQuery(BaseQuery):
    model = Computer
    searchable_text = [
        "hostname", "fqdn", "asset_tag", "serial_number",
        "primary_user", "model", "manufacturer", "notes", "description",
    ]

    fields = {
        # identita
        "hostname":        (Computer.hostname, str),
        "fqdn":            (Computer.fqdn, str),
        "asset_tag":       (Computer.asset_tag, str),
        # síť
        "mac":             (Computer.mac, str),
        "ip_reserved":     (Computer.ip_reserved, str),
        # hardware
        "manufacturer":    (Computer.manufacturer, str),
        "model":           (Computer.model, str),
        "serial_number":   (Computer.serial_number, str),
        "form_factor":     (Computer.form_factor, str),
        "cpu_cores":       (Computer.cpu_cores, int),
        "ram_mb":          (Computer.ram_mb, int),
        "storage_total_gb":(Computer.storage_total_gb, int),
        "storage_type":    (Computer.storage_type, str),
        # OS
        "os_family":       (Computer.os_family, str),
        "os_name":         (Computer.os_name, str),
        "os_version":      (Computer.os_version, str),
        "os_arch":         (Computer.os_arch, str),
        # doména
        "is_domain_joined":(Computer.is_domain_joined, bool),
        # lokace
        "location":        (Computer.location, str),
        "building":        (Computer.building, str),
        "floor":           (Computer.floor, str),
        "room":            (Computer.room, str),
        "department":      (Computer.department, str),
        "primary_user":    (Computer.primary_user, str),
        # životní cyklus
        "status":          (Computer.status, str),
        "purchase_date":   (Computer.purchase_date, datetime),
        "warranty_until":  (Computer.warranty_until, datetime),
        "supplier":        (Computer.supplier, str),
        # monitoring
        "last_seen":       (Computer.last_seen, datetime),
        "is_online":       (Computer.is_online, bool),
        "last_cpu_pct":    (Computer.last_cpu_pct, int),
        "last_ram_used_pct":(Computer.last_ram_used_pct, int),
        "last_disk_used_pct":(Computer.last_disk_used_pct, int),
    }


class UserQuery(BaseQuery):
    model = User
    searchable_text = [
        "username", "first_name", "last_name", "display_name",
        "email", "phone", "department", "job_title", "notes",
    ]

    fields = {
        "username":        (User.username, str),
        "first_name":      (User.first_name, str),
        "last_name":       (User.last_name, str),
        "display_name":    (User.display_name, str),
        "title":           (User.title, str),
        "email":           (User.email, str),
        "phone":           (User.phone, str),
        "mobile":          (User.mobile, str),
        "department":      (User.department, str),
        "job_title":       (User.job_title, str),
        "manager_username":(User.manager_username, str),
        "employee_id":     (User.employee_id, str),
        "office":          (User.office, str),
        "enabled":         (User.enabled, bool),
        "must_change_password": (User.must_change_password, bool),
        "last_logon":      (User.last_logon, datetime),
        "account_expires": (User.account_expires, datetime),
    }
