"""Ansible playbook runner.

Spouští ansible-playbook jako subprocess, zachycuje výstup řádek po řádku
a ukládá ho do in-memory jobu. Web UI si výstup stahuje přes HTMX polling.

Joby jsou in-memory (ztratí se při restartu serveru). Pro produkci by
šly ukládat do AuditLog tabulky v DB, ale pro management tool to stačí.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class AnsibleJob:
    id: str
    playbook: str
    limit: str
    started_at: datetime
    status: str = "running"   # running | success | failed
    output_lines: list[str] = field(default_factory=list)
    return_code: int | None = None
    finished_at: datetime | None = None

    def is_done(self) -> bool:
        return self.status in ("success", "failed")

    def duration(self) -> str | None:
        if not self.finished_at:
            return None
        s = int((self.finished_at - self.started_at).total_seconds())
        return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "playbook": self.playbook,
            "limit": self.limit,
            "status": self.status,
            "is_done": self.is_done(),
            "started_at": self.started_at.strftime("%d.%m.%Y %H:%M:%S"),
            "finished_at": self.finished_at.strftime("%d.%m.%Y %H:%M:%S") if self.finished_at else None,
            "duration": self.duration(),
            "return_code": self.return_code,
            "line_count": len(self.output_lines),
        }


# Globální sklad jobů (modul-level singleton, thread-safe přes lock).
_jobs: dict[str, AnsibleJob] = {}
_jobs_lock = threading.Lock()


class AnsibleRunner:
    """Fasáda nad ansible-playbook. Instanci si vytváří každý request."""

    def __init__(self, cfg):
        self.inventory_path = Path(cfg.ansible.inventory_path)
        self.playbooks_path = Path(cfg.ansible.playbooks_path)

    # --- dotazy ----------------------------------------------------------

    def list_playbooks(self) -> list[str]:
        """Vrátí seznam .yml/.yaml souborů z playbooks_path."""
        if not self.playbooks_path.exists():
            return []
        plays: list[str] = []
        for ext in ("*.yml", "*.yaml"):
            plays.extend(p.name for p in sorted(self.playbooks_path.glob(ext)))
        return plays

    def list_groups(self) -> list[str]:
        """Skupiny z adresářové inventory (podadresáře = skupiny)."""
        groups = ["all"]
        if self.inventory_path.is_dir():
            groups.extend(
                d.name for d in sorted(self.inventory_path.iterdir()) if d.is_dir()
            )
        return groups

    def get_job(self, job_id: str) -> AnsibleJob | None:
        return _jobs.get(job_id)

    def list_jobs(self, limit: int = 30) -> list[AnsibleJob]:
        with _jobs_lock:
            jobs = list(_jobs.values())
        return sorted(jobs, key=lambda j: j.started_at, reverse=True)[:limit]

    # --- spuštění --------------------------------------------------------

    def start(
        self,
        playbook: str,
        limit: str = "all",
        extra_vars: dict | None = None,
        demo: bool = False,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        job = AnsibleJob(
            id=job_id,
            playbook=playbook,
            limit=limit,
            started_at=datetime.now(),
        )
        with _jobs_lock:
            _jobs[job_id] = job

        if demo:
            thread = threading.Thread(target=self._run_demo, args=(job,), daemon=True)
        else:
            thread = threading.Thread(
                target=self._run_real, args=(job, extra_vars), daemon=True
            )
        thread.start()
        return job_id

    def _run_demo(self, job: AnsibleJob) -> None:
        """Simulovaný běh pro demo/testovací režim."""
        import time
        lines = [
            f"PLAY [{job.limit}] *{'*' * 50}",
            "",
            "TASK [Gathering Facts] *" + "*" * 48,
            f"ok: [demo-host-01]",
            f"ok: [demo-host-02]",
            "",
            f"TASK [Demo: {job.playbook}] *" + "*" * 40,
            "changed: [demo-host-01] => {}",
            "changed: [demo-host-02] => {}",
            "",
            "PLAY RECAP *" + "*" * 59,
            "demo-host-01   : ok=2  changed=1  unreachable=0  failed=0",
            "demo-host-02   : ok=2  changed=1  unreachable=0  failed=0",
            "",
            "[DEMO REŽIM] Ansible playbook nebyl skutečně spuštěn.",
        ]
        for line in lines:
            time.sleep(0.3)
            job.output_lines.append(line)
        job.return_code = 0
        job.status = "success"
        job.finished_at = datetime.now()

    def _run_real(self, job: AnsibleJob, extra_vars: dict | None) -> None:
        """Skutečné spuštění přes ansible-playbook subprocess."""
        ansible_pb = shutil.which("ansible-playbook")
        if not ansible_pb:
            job.output_lines.append("CHYBA: ansible-playbook nenalezeno v PATH.")
            job.output_lines.append("Nainstalujte: pip install ansible")
            job.status = "failed"
            job.finished_at = datetime.now()
            return

        cmd = [ansible_pb, str(self.playbooks_path / job.playbook)]
        if self.inventory_path.exists():
            cmd.extend(["-i", str(self.inventory_path)])
        if job.limit and job.limit != "all":
            cmd.extend(["--limit", job.limit])
        if extra_vars:
            import json
            cmd.extend(["-e", json.dumps(extra_vars)])

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "ANSIBLE_FORCE_COLOR": "0"},
            )
            for line in proc.stdout:
                job.output_lines.append(line.rstrip("\n"))
            proc.wait()
            job.return_code = proc.returncode
            job.status = "success" if proc.returncode == 0 else "failed"
        except Exception as exc:
            job.output_lines.append(f"CHYBA: {exc}")
            job.status = "failed"
        finally:
            job.finished_at = datetime.now()
