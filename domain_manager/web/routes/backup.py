"""Zálohy — /backup

Spouští Ansible playbooky pro zálohu:
  - PostgreSQL: pg_dump
  - Samba AD: samba-tool domain backup
  - Pi-hole: teleporter export API

Záznamy o zálohách ukládá do DB (BackupRecord).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.models import BackupRecord, get_session
from ...ansible.runner import AnsibleRunner
from .._audit import log_action

router = APIRouter(prefix="/backup")
from .._templates import templates

_DEMO_MODE = os.environ.get("DM_DEMO", "0") == "1"

BACKUP_TYPES = {
    "db":     ("Databáze (pg_dump)",        "backup_db.yml"),
    "samba":  ("Samba AD domain",            "backup_samba.yml"),
    "pihole": ("Pi-hole konfigurace",        "backup_pihole.yml"),
    "full":   ("Kompletní záloha (vše)",     "backup_full.yml"),
}


def _require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
def backup_page(request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        records = (
            session.query(BackupRecord)
            .order_by(BackupRecord.started_at.desc())
            .limit(50)
            .all()
        )
        data = [
            {
                "id": r.id,
                "backup_type": r.backup_type,
                "type_label": BACKUP_TYPES.get(r.backup_type, (r.backup_type, ""))[0],
                "status": r.status,
                "file_path": r.file_path or "—",
                "file_size": _fmt_size(r.file_size_bytes),
                "started_at": r.started_at.strftime("%d.%m.%Y %H:%M") if r.started_at else "—",
                "finished_at": r.finished_at.strftime("%H:%M:%S") if r.finished_at else "—",
                "ansible_job_id": r.ansible_job_id,
                "error_message": r.error_message or "",
            }
            for r in records
        ]

    cfg = request.app.state.config
    backup_dir = str(cfg.backup.backup_dir) if hasattr(cfg, "backup") else "/var/lib/domain-manager/backups"

    return templates.TemplateResponse(request, "backup.html", {
        "user": user,
        "records": data,
        "backup_types": BACKUP_TYPES,
        "backup_dir": backup_dir,
    })


@router.post("/start/{backup_type}")
def backup_start(backup_type: str, request: Request, user: str = Depends(_require_user)):
    if backup_type not in BACKUP_TYPES:
        raise HTTPException(400, f"Neznámý typ zálohy: {backup_type}")

    _, playbook = BACKUP_TYPES[backup_type]
    cfg = request.app.state.config

    with get_session() as session:
        record = BackupRecord(
            backup_type=backup_type,
            status="running",
            created_by=user,
        )
        session.add(record)
        session.flush()
        record_id = record.id

        if _DEMO_MODE:
            runner = AnsibleRunner(cfg)
            job_id = runner.start(playbook, demo=True)
            record.status = "success"
            record.ansible_job_id = job_id
            record.file_path = f"/var/lib/domain-manager/backups/{backup_type}-demo.tar.gz"
            record.file_size_bytes = 1024 * 1024 * 5  # 5 MB demo
        else:
            try:
                runner = AnsibleRunner(cfg)
                job_id = runner.start(playbook, extra_vars={"backup_type": backup_type})
                record.ansible_job_id = job_id
            except Exception as e:
                record.status = "failed"
                record.error_message = str(e)

        session.commit()

    log_action(user, f"backup_{backup_type}", "backup", record_id)
    return RedirectResponse("/backup", status_code=303)


@router.post("/{record_id}/delete")
def backup_delete_record(record_id: int, request: Request, user: str = Depends(_require_user)):
    with get_session() as session:
        r = session.get(BackupRecord, record_id)
        if r:
            session.delete(r)
            session.commit()
    return RedirectResponse("/backup", status_code=303)


def _fmt_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024**2:.1f} MB"
    return f"{size_bytes / 1024**3:.2f} GB"
