"""Plánky budov — /plans

Architektura:
  GET  /plans                  seznam (strom budova→patra)
  GET  /plans/new              formulář pro nový plánek
  POST /plans                  nahrání obrázku + vytvoření záznamu
  GET  /plans/{id}             zobrazení + drag&drop editor
  POST /plans/{id}             update metadat
  POST /plans/{id}/delete      smazání
  POST /plans/{id}/positions   uložení pozice počítače (HTMX, volá JS)
  POST /plans/{id}/positions/{cid}/delete  odebrání počítače z plánku
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...db.models import Computer, DevicePosition, FloorPlan, get_session
from .._audit import log_action

router = APIRouter(prefix="/plans")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

ICON_LABELS = {
    "desktop":  "Počítač",
    "laptop":   "Laptop",
    "server":   "Server",
    "printer":  "Tiskárna",
    "ap":       "Access Point",
    "camera":   "Kamera",
    "other":    "Jiné",
}

ICON_EMOJI = {
    "desktop":  "🖥",
    "laptop":   "💻",
    "server":   "🖧",
    "printer":  "🖨",
    "ap":       "📡",
    "camera":   "📷",
    "other":    "📦",
}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plan_dict(p: FloorPlan) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "image_path": p.image_path,
        "image_url": f"/uploads/{p.image_path}" if p.image_path else None,
        "width_px": p.width_px or 1000,
        "height_px": p.height_px or 680,
        "parent_id": p.parent_id,
        "created_at": p.created_at.strftime("%d.%m.%Y") if p.created_at else "",
    }


def _build_tree(plans: list[FloorPlan]) -> list[dict]:
    """Sestaví strom: kořeny (parent_id=None) + jejich děti."""
    by_id = {p.id: {**_plan_dict(p), "children": []} for p in plans}
    roots = []
    for p in plans:
        node = by_id[p.id]
        if p.parent_id and p.parent_id in by_id:
            by_id[p.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


def _save_upload(file: UploadFile, uploads_dir: Path) -> tuple[str, int, int]:
    """Uloží nahraný soubor, vrátí (filename, width, height)."""
    suffix = Path(file.filename).suffix.lower() if file.filename else ".png"
    if suffix not in (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"):
        raise HTTPException(400, "Nepodporovaný formát. Použijte PNG, JPG nebo SVG.")
    fname = f"plan_{uuid.uuid4().hex[:12]}{suffix}"
    dest = uploads_dir / fname
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Zjistit rozměry (PNG/JPG pomocí Pillow, SVG parsovat)
    w, h = _detect_dimensions(dest, suffix)
    return fname, w, h


def _detect_dimensions(path: Path, suffix: str) -> tuple[int, int]:
    if suffix == ".svg":
        try:
            import re
            text = path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'<svg[^>]+width=["\'](\d+)', text)
            n = re.search(r'<svg[^>]+height=["\'](\d+)', text)
            if m and n:
                return int(m.group(1)), int(n.group(1))
            # viewBox fallback
            vb = re.search(r'viewBox=["\'][\d.]+ [\d.]+ ([\d.]+) ([\d.]+)', text)
            if vb:
                return int(float(vb.group(1))), int(float(vb.group(2)))
        except Exception:
            pass
        return 1000, 680
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return 1000, 680


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def list_plans(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        plans = session.query(FloorPlan).order_by(FloorPlan.parent_id.asc().nullsfirst(), FloorPlan.name).all()
        tree = _build_tree(plans)
        plan_count = len(plans)

    return templates.TemplateResponse("plans.html", {
        "request": request, "user": user,
        "tree": tree,
        "plan_count": plan_count,
    })


@router.get("/new", response_class=HTMLResponse)
def plan_new(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        parents = session.query(FloorPlan).order_by(FloorPlan.name).all()
        parents_data = [_plan_dict(p) for p in parents]
    return templates.TemplateResponse("plan_form.html", {
        "request": request, "user": user,
        "p": None,
        "parents": parents_data,
    })


@router.get("/{plan_id}/edit", response_class=HTMLResponse)
def plan_edit(plan_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        p = session.get(FloorPlan, plan_id)
        if not p:
            raise HTTPException(404)
        parents = session.query(FloorPlan).order_by(FloorPlan.name).all()
        parents_data = [_plan_dict(p2) for p2 in parents]
        plan_data = _plan_dict(p)
    return templates.TemplateResponse("plan_form.html", {
        "request": request, "user": user,
        "p": plan_data,
        "parents": parents_data,
    })


@router.post("", response_class=HTMLResponse)
async def plan_create(
    request: Request,
    user: str = Depends(_require_user),
    name: str = Form(...),
    parent_id: str = Form(""),
    image: UploadFile = File(None),
):
    uploads_dir: Path = request.app.state.uploads_dir
    pid = int(parent_id) if parent_id.strip() else None
    image_path = None
    width_px, height_px = 1000, 680

    if image and image.filename:
        image_path, width_px, height_px = _save_upload(image, uploads_dir)

    with get_session() as session:
        p = FloorPlan(
            name=name.strip(),
            image_path=image_path or "",
            width_px=width_px,
            height_px=height_px,
            parent_id=pid,
        )
        session.add(p)
        session.flush()
        new_id = p.id
        session.commit()

    log_action(user, "create_plan", "floor_plan", new_id, {"name": name})
    return RedirectResponse(f"/plans/{new_id}", status_code=303)


@router.get("/{plan_id}", response_class=HTMLResponse)
def plan_detail(plan_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        p = session.get(FloorPlan, plan_id)
        if not p:
            raise HTTPException(404, "Plánek nenalezen")

        # Pozice na tomto plánku
        positions = session.query(DevicePosition).filter(DevicePosition.plan_id == plan_id).all()
        placed_ids = {pos.computer_id for pos in positions}

        placed = []
        for pos in positions:
            c = session.get(Computer, pos.computer_id)
            if c:
                placed.append({
                    "pos_id": pos.id,
                    "computer_id": c.id,
                    "hostname": c.hostname,
                    "icon": pos.icon or "desktop",
                    "icon_emoji": ICON_EMOJI.get(pos.icon or "desktop", "🖥"),
                    "x": pos.x,
                    "y": pos.y,
                    "is_online": c.is_online,
                    "ip": c.ip_reserved or "—",
                    "cpu_pct": c.last_cpu_pct,
                    "ram_pct": c.last_ram_used_pct,
                    "disk_pct": c.last_disk_used_pct,
                    "internet_blocked": c.internet_blocked,
                    "department": c.department or "",
                    "form_factor": c.form_factor or "desktop",
                })

        # Počítače bez pozice na tomto plánku
        unplaced = (
            session.query(Computer)
            .filter(Computer.id.notin_(placed_ids))
            .order_by(Computer.hostname)
            .all()
        )
        unplaced_data = [
            {
                "id": c.id,
                "hostname": c.hostname,
                "icon": _guess_icon(c),
                "icon_emoji": ICON_EMOJI.get(_guess_icon(c), "🖥"),
                "is_online": c.is_online,
                "department": c.department or "",
                "ip": c.ip_reserved or "",
            }
            for c in unplaced
        ]

        # Sourozenci (ostatní plánky se stejným parentem — navigace)
        siblings = session.query(FloorPlan).filter(
            FloorPlan.parent_id == p.parent_id,
            FloorPlan.id != plan_id,
        ).all() if p.parent_id else []

        # Děti (pod-plánky)
        children = session.query(FloorPlan).filter(FloorPlan.parent_id == plan_id).all()

        plan_data = _plan_dict(p)
        parent = session.get(FloorPlan, p.parent_id) if p.parent_id else None

    return templates.TemplateResponse("plan_detail.html", {
        "request": request, "user": user,
        "p": plan_data,
        "placed": placed,
        "unplaced": unplaced_data,
        "icon_labels": ICON_LABELS,
        "icon_emoji": ICON_EMOJI,
        "parent": _plan_dict(parent) if parent else None,
        "siblings": [_plan_dict(s) for s in siblings],
        "children": [_plan_dict(c) for c in children],
    })


@router.post("/{plan_id}/update")
async def plan_update(plan_id: int, request: Request, user: str = Depends(_require_user)):
    form = await request.form()
    uploads_dir: Path = request.app.state.uploads_dir
    with get_session() as session:
        p = session.get(FloorPlan, plan_id)
        if not p:
            raise HTTPException(404)
        p.name = (form.get("name") or p.name).strip()
        pid_raw = form.get("parent_id", "")
        p.parent_id = int(pid_raw) if pid_raw.strip() else None

        image = form.get("image")
        if hasattr(image, "filename") and image.filename:
            fname, w, h = _save_upload(image, uploads_dir)
            p.image_path = fname
            p.width_px = w
            p.height_px = h
        session.commit()
    return RedirectResponse(f"/plans/{plan_id}", status_code=303)


@router.post("/{plan_id}/positions", response_class=HTMLResponse)
async def save_position(plan_id: int, request: Request, user: str = Depends(_require_user)):
    """Uloží nebo aktualizuje pozici počítače. Volá se z JS při přetažení."""
    form = await request.form()
    computer_id = int(form["computer_id"])
    x = int(float(form["x"]))
    y = int(float(form["y"]))
    icon = form.get("icon", "desktop")

    with get_session() as session:
        existing = (
            session.query(DevicePosition)
            .filter_by(plan_id=plan_id, computer_id=computer_id)
            .first()
        )
        if existing:
            existing.x = x
            existing.y = y
            existing.icon = icon
        else:
            session.add(DevicePosition(
                plan_id=plan_id, computer_id=computer_id,
                x=x, y=y, icon=icon,
            ))
        session.commit()

        # Vrátit jen badge "uloženo" - HTMX target je #save-status
        return HTMLResponse(
            '<span style="color:var(--success);font-size:11px;">✓ uloženo</span>'
        )


@router.post("/{plan_id}/positions/{computer_id}/delete", response_class=HTMLResponse)
def remove_position(
    plan_id: int, computer_id: int,
    request: Request, user: str = Depends(_require_user),
):
    """Odebere počítač z plánku."""
    with get_session() as session:
        pos = (
            session.query(DevicePosition)
            .filter_by(plan_id=plan_id, computer_id=computer_id)
            .first()
        )
        if pos:
            session.delete(pos)
            session.commit()
    # HTMX: odstraní ikonu ze stránky
    return HTMLResponse("")


@router.post("/{plan_id}/delete")
def plan_delete(plan_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        p = session.get(FloorPlan, plan_id)
        if not p:
            raise HTTPException(404)
        # Odpojit děti (ne smazat)
        session.query(FloorPlan).filter_by(parent_id=plan_id).update({"parent_id": None})
        session.query(DevicePosition).filter_by(plan_id=plan_id).delete()
        session.delete(p)
        session.commit()
    log_action(user, "delete_plan", "floor_plan", plan_id)
    return RedirectResponse("/plans", status_code=303)


def _guess_icon(c: Computer) -> str:
    ff = (c.form_factor or "").lower()
    if ff == "server" or (c.hostname and "dc" in c.hostname.lower()):
        return "server"
    if ff == "laptop":
        return "laptop"
    return "desktop"
