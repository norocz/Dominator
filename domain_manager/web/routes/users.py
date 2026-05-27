"""Stránka /users — filtrovatelná tabulka a CRUD uživatelů.

Architektura:
  - GET /users              celá stránka
  - GET /users/table        jen tabulka (HTMX fragment)
  - GET /users/new          formulář pro nového uživatele
  - POST /users             vytvoření
  - GET /users/{id}         detail + editační formulář
  - POST /users/{id}        update polí
  - POST /users/{id}/delete smazání
"""
from __future__ import annotations

import os
from pathlib import Path

import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import Group, User, UserGroupMembership, get_session
from ...db.queries import UserQuery
from .._audit import log_action

router = APIRouter(prefix="/users")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

DEFAULT_COLUMNS = [
    "username", "display_name", "department", "job_title",
    "email", "phone", "enabled", "last_logon",
]

ALL_COLUMNS = {
    "username":          "Přihlašovací jméno",
    "first_name":        "Jméno",
    "last_name":         "Příjmení",
    "display_name":      "Zobrazované jméno",
    "title":             "Titul",
    "email":             "E-mail",
    "phone":             "Telefon",
    "mobile":            "Mobil",
    "department":        "Oddělení",
    "job_title":         "Pracovní pozice",
    "manager_username":  "Nadřízený",
    "employee_id":       "ID zaměstnance",
    "office":            "Kancelář",
    "enabled":           "Aktivní",
    "last_logon":        "Poslední přihlášení",
    "account_expires":   "Platnost do",
}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def list_users(request: Request, user: str = Depends(_require_user)):
    columns = _columns_from_params(request)
    return templates.TemplateResponse("users.html", {
        "request": request,
        "user": user,
        "all_columns": ALL_COLUMNS,
        "active_columns": columns,
        "facets": _facets(),
    })


@router.get("/table", response_class=HTMLResponse)
def users_table_fragment(request: Request, user: str = Depends(_require_user)):
    page = int(request.query_params.get("page", 1))
    per_page = int(request.query_params.get("per_page", 50))
    columns = _columns_from_params(request)

    with get_session() as session:
        result = (
            UserQuery(session)
            .filter_from_params(request.query_params)
            .paginate(page=page, per_page=per_page)
        )
        rows_data = [_row_dict(r) for r in result.rows]

    return templates.TemplateResponse("users_table.html", {
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


@router.get("/new", response_class=HTMLResponse)
def user_new_form(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        groups = session.query(Group).filter(Group.kind == "user").order_by(Group.name).all()
        groups_data = [{"id": g.id, "name": g.name} for g in groups]
    return templates.TemplateResponse("user_detail.html", {
        "request": request,
        "user": user,
        "u": None,
        "labels": ALL_COLUMNS,
        "groups": groups_data,
        "user_groups": [],
    })


@router.post("", response_class=HTMLResponse)
async def user_create(request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "username je povinné")

    with get_session() as session:
        exists = session.query(User).filter(User.username == username).first()
        if exists:
            raise HTTPException(400, f"Uživatel '{username}' již existuje")

        u = User(
            username=username,
            first_name=(form.get("first_name") or "").strip() or "—",
            last_name=(form.get("last_name") or "").strip() or "—",
            display_name=form.get("display_name") or None,
            title=form.get("title") or None,
            email=form.get("email") or None,
            phone=form.get("phone") or None,
            mobile=form.get("mobile") or None,
            department=form.get("department") or None,
            job_title=form.get("job_title") or None,
            manager_username=form.get("manager_username") or None,
            employee_id=form.get("employee_id") or None,
            office=form.get("office") or None,
            enabled=True,
            notes=form.get("notes") or None,
            created_by=user,
            updated_by=user,
        )
        session.add(u)
        session.flush()
        new_id = u.id

        group_ids = [int(v) for v in form.getlist("group_ids") if v]
        for gid in group_ids:
            session.add(UserGroupMembership(user_id=new_id, group_id=gid))

        session.commit()

    log_action(user, "create_user", "user", new_id, {"username": username})
    return RedirectResponse(f"/users/{new_id}", status_code=303)


@router.get("/{user_id}", response_class=HTMLResponse)
def user_detail(user_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            raise HTTPException(404, "Uživatel nenalezen")
        data = _row_dict(u, full=True)

        memberships = (
            session.query(UserGroupMembership)
            .filter(UserGroupMembership.user_id == user_id)
            .all()
        )
        user_group_ids = {m.group_id for m in memberships}

        all_groups = session.query(Group).filter(Group.kind == "user").order_by(Group.name).all()
        groups_data = [{"id": g.id, "name": g.name, "member": g.id in user_group_ids} for g in all_groups]

    return templates.TemplateResponse("user_detail.html", {
        "request": request,
        "user": user,
        "u": data,
        "labels": ALL_COLUMNS,
        "groups": groups_data,
        "user_groups": [g for g in groups_data if g["member"]],
    })


@router.post("/{user_id}")
async def user_update(
    user_id: int,
    request: Request,
    user: str = Depends(_require_user),
):
    form = await request.form()
    allowed_str = {
        "first_name", "last_name", "display_name", "title", "email",
        "phone", "mobile", "department", "job_title", "manager_username",
        "employee_id", "office", "notes",
    }
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            raise HTTPException(404)

        for key in allowed_str:
            if key in form:
                setattr(u, key, form[key] or None)

        if "enabled" in form:
            u.enabled = form["enabled"].lower() in ("1", "true", "on", "yes")

        # Update skupin
        new_group_ids = {int(v) for v in form.getlist("group_ids") if v}
        existing = session.query(UserGroupMembership).filter(UserGroupMembership.user_id == user_id).all()
        existing_ids = {m.group_id for m in existing}

        for gid in new_group_ids - existing_ids:
            session.add(UserGroupMembership(user_id=user_id, group_id=gid))
        for m in existing:
            if m.group_id not in new_group_ids:
                session.delete(m)

        u.updated_by = user
        session.commit()

    log_action(user, "update_user", "user", user_id)
    return RedirectResponse(f"/users/{user_id}", status_code=303)


@router.post("/{user_id}/delete")
def user_delete(user_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            raise HTTPException(404)
        session.query(UserGroupMembership).filter(UserGroupMembership.user_id == user_id).delete()
        session.delete(u)
        session.commit()
    log_action(user, "delete_user", "user", user_id)
    return RedirectResponse("/users", status_code=303)


# --- CSV import ----------------------------------------------------------------

_USER_CSV_MAP = {
    "username": "username", "login": "username", "prihlasovaci_jmeno": "username",
    "přihlašovací jméno": "username", "sam": "username",
    "first_name": "first_name", "jmeno": "first_name", "jméno": "first_name",
    "krestni_jmeno": "first_name", "křestní jméno": "first_name",
    "last_name": "last_name", "prijmeni": "last_name", "příjmení": "last_name",
    "surname": "last_name",
    "email": "email", "mail": "email", "e-mail": "email",
    "phone": "phone", "telefon": "phone",
    "mobile": "mobile", "mobil": "mobile",
    "department": "department", "oddeleni": "department", "oddělení": "department",
    "job_title": "job_title", "pozice": "job_title", "pracovni_pozice": "job_title",
    "pracovní pozice": "job_title",
    "title": "title", "titul": "title",
    "manager": "manager_username", "nadrizeny": "manager_username",
    "manager_username": "manager_username", "nadřízený": "manager_username",
    "employee_id": "employee_id", "id_zamestnance": "employee_id",
    "office": "office", "kancelar": "office", "kancelář": "office",
    "display_name": "display_name", "zobrazovane_jmeno": "display_name",
}


@router.post("/import-csv", response_class=HTMLResponse)
async def users_import_csv(
    request: Request,
    user: str = Depends(_require_user),
    file: UploadFile = File(...),
    sync_ad: str = Form("0"),
    ad_password: str = Form(""),
):
    """
    Importuje uživatele z CSV. Povinné sloupce: username, first_name, last_name.
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1250", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return HTMLResponse('<div id="csv-result" style="color:var(--danger)">CSV je prázdné nebo chybí záhlaví.</div>')

    normalized = {k.strip().lower(): k for k in reader.fieldnames}
    created, updated, skipped, errors = [], [], [], []

    ad_client = None
    if sync_ad == "1" and not _DEMO_MODE:
        try:
            from ...ad.client import ADClient
            ad_client = ADClient(request.app.state.config)
        except Exception:
            pass

    # Mapování username → data pro pozdější AD sync (původní kód měl prázdný seznam)
    rows_by_username: dict[str, dict] = {}

    with get_session() as session:
        for row in reader:
            data: dict = {}
            for csv_col, orig_col in normalized.items():
                field = _USER_CSV_MAP.get(csv_col)
                if not field:
                    continue
                val = (row.get(orig_col) or "").strip()
                if val:
                    data[field] = val

            username = data.get("username")
            first_name = data.get("first_name", "")
            last_name = data.get("last_name", "")

            if not username:
                skipped.append("(bez username)")
                continue
            if not first_name or not last_name:
                skipped.append(username)
                continue

            rows_by_username[username] = data

            existing = session.query(User).filter(User.username == username).first()
            if existing:
                for k, v in data.items():
                    if k not in ("username",) and v:
                        setattr(existing, k, v)
                updated.append(username)
            else:
                u_obj = User(created_by=user, updated_by=user, **data)
                session.add(u_obj)
                created.append(username)

        try:
            session.commit()
        except Exception as e:
            session.rollback()
            errors.append(str(e))

    if ad_client:
        from ...ad.client import ADUser
        for uname in created:
            try:
                row_data = rows_by_username.get(uname, {})
                ad_client.create_user(ADUser(
                    username=uname,
                    first_name=row_data.get("first_name", uname),
                    last_name=row_data.get("last_name", ""),
                    email=row_data.get("email"),
                    password=ad_password or None,
                ))
            except Exception as e:
                errors.append(f"{uname}: {e}")

    log_action(user, "import_users_csv", "user", None,
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


# --- helpers -------------------------------------------------------------------


def _columns_from_params(request: Request) -> list[str]:
    raw = request.query_params.get("cols")
    if not raw:
        return DEFAULT_COLUMNS
    cols = [c for c in raw.split(",") if c in ALL_COLUMNS]
    return cols or DEFAULT_COLUMNS


def _row_dict(u: User, *, full: bool = False) -> dict:
    base = {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "display_name": u.display_name or f"{u.first_name} {u.last_name}",
        "title": u.title,
        "email": u.email,
        "phone": u.phone,
        "mobile": u.mobile,
        "department": u.department,
        "job_title": u.job_title,
        "manager_username": u.manager_username,
        "employee_id": u.employee_id,
        "office": u.office,
        "enabled": u.enabled,
        "last_logon": u.last_logon.strftime("%d.%m.%Y %H:%M") if u.last_logon else None,
        "account_expires": u.account_expires.strftime("%d.%m.%Y") if u.account_expires else None,
    }
    if full:
        base.update({
            "notes": u.notes,
            "ad_dn": u.ad_dn,
            "must_change_password": u.must_change_password,
            "password_last_set": u.password_last_set.strftime("%d.%m.%Y") if u.password_last_set else None,
            "tags": u.tags or [],
            "custom_fields": u.custom_fields or {},
            "created_at": u.created_at.strftime("%d.%m.%Y %H:%M") if u.created_at else None,
            "updated_at": u.updated_at.strftime("%d.%m.%Y %H:%M") if u.updated_at else None,
            "created_by": u.created_by,
            "updated_by": u.updated_by,
        })
    return base


def _facets() -> dict[str, list[str]]:
    with get_session() as session:
        def distinct(col):
            rows = session.query(col).distinct().filter(col.isnot(None)).all()
            return sorted([r[0] for r in rows if r[0]])
        return {
            "department": distinct(User.department),
            "job_title":  distinct(User.job_title),
            "office":     distinct(User.office),
        }
