"""Odesílání notifikací — e-mail a webhook.

Volá se z background checkeru. Nikdy nevyhazuje výjimku — loguje a jde dál.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger("dm.notifications")


class Notifier:
    def __init__(self, cfg: "Config"):
        self.cfg = cfg.notifications

    def send(self, subject: str, body: str, level: str = "warning") -> None:
        """Odešle notifikaci všemi konfigurovanými kanály."""
        if not self.cfg.enabled:
            return
        self._send_email(subject, body)
        self._send_webhook(subject, body, level)

    def _send_email(self, subject: str, body: str) -> None:
        if not self.cfg.smtp_host or not self.cfg.email_to:
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[Dominator] {subject}"
            msg["From"]    = self.cfg.email_from or "dominator@localhost"
            msg["To"]      = ", ".join(self.cfg.email_to)
            msg.attach(MIMEText(body, "plain", "utf-8"))

            context = ssl.create_default_context()
            if self.cfg.smtp_tls:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=10) as smtp:
                    smtp.starttls(context=context)
                    if self.cfg.smtp_user:
                        smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=10) as smtp:
                    if self.cfg.smtp_user:
                        smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    smtp.send_message(msg)
            log.info("E-mail notifikace odeslána: %s", subject)
        except Exception as exc:
            log.warning("Nepodařilo se odeslat e-mail: %s", exc)

    def _send_webhook(self, subject: str, body: str, level: str) -> None:
        if not self.cfg.webhook_url:
            return
        try:
            import httpx
            payload = {"subject": subject, "body": body, "level": level, "source": "Dominator"}
            httpx.post(self.cfg.webhook_url, json=payload, timeout=10)
            log.info("Webhook notifikace odeslána: %s", subject)
        except Exception as exc:
            log.warning("Nepodařilo se odeslat webhook: %s", exc)


# --- Background checker ----------------------------------------------------

def run_checks(cfg: "Config") -> list[str]:
    """Spustí všechny automatické kontroly. Vrátí seznam odeslaných alertů."""
    alerts: list[str] = []
    notifier = Notifier(cfg)

    try:
        alerts += _check_computers_offline(cfg, notifier)
    except Exception as e:
        log.warning("check_computers_offline chyba: %s", e)

    try:
        alerts += _check_disk_usage(cfg, notifier)
    except Exception as e:
        log.warning("check_disk_usage chyba: %s", e)

    try:
        alerts += _check_certs(cfg, notifier)
    except Exception as e:
        log.warning("check_certs chyba: %s", e)

    return alerts


def _check_computers_offline(cfg: "Config", notifier: Notifier) -> list[str]:
    from datetime import datetime, timezone, timedelta
    from ..db.models import Computer, get_session

    threshold_hours = cfg.notifications.offline_alert_hours
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=threshold_hours)
    alerts = []

    with get_session() as session:
        long_offline = (
            session.query(Computer)
            .filter(Computer.is_online == False)  # noqa: E712
            .filter(Computer.last_seen.isnot(None))
            .filter(Computer.last_seen < cutoff)
            .filter(Computer.status == "active")
            .all()
        )
        for c in long_offline:
            msg = f"Počítač {c.hostname} je offline více než {threshold_hours} hodin (naposledy: {c.last_seen})"
            notifier.send(f"Počítač offline: {c.hostname}", msg)
            alerts.append(msg)

    return alerts


def _check_disk_usage(cfg: "Config", notifier: Notifier) -> list[str]:
    from ..db.models import Computer, get_session

    threshold = cfg.notifications.disk_alert_pct
    alerts = []

    with get_session() as session:
        high_disk = (
            session.query(Computer)
            .filter(Computer.last_disk_used_pct >= threshold)
            .filter(Computer.is_online == True)  # noqa: E712
            .all()
        )
        for c in high_disk:
            msg = f"Počítač {c.hostname}: disk {c.last_disk_used_pct}% (práh: {threshold}%)"
            notifier.send(f"Plný disk: {c.hostname}", msg, level="critical")
            alerts.append(msg)

    return alerts


def _check_certs(cfg: "Config", notifier: Notifier) -> list[str]:
    from datetime import datetime, timezone
    from ..db.models import Certificate, get_session

    threshold_days = cfg.notifications.cert_expiry_days
    alerts = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with get_session() as session:
        certs = session.query(Certificate).filter(Certificate.not_after.isnot(None)).all()
        for c in certs:
            if c.not_after and not c.alert_sent:
                days = (c.not_after - now).days
                if days <= threshold_days:
                    msg = f"Certifikát {c.hostname}:{c.port} expiruje za {days} dní ({c.not_after})"
                    notifier.send(f"Expirující certifikát: {c.hostname}", msg, level="warning")
                    c.alert_sent = True
                    alerts.append(msg)
        session.commit()

    return alerts


def start_background_checker(cfg: "Config") -> None:
    """Spustí periodické kontroly v daemon vlákně (každou hodinu)."""
    import threading
    import time

    def _loop():
        time.sleep(300)  # Počkej 5 min po startu
        while True:
            try:
                run_checks(cfg)
            except Exception as e:
                log.error("Background checker chyba: %s", e)
            time.sleep(3600)  # Znovu za hodinu

    t = threading.Thread(target=_loop, daemon=True, name="dm-notif-checker")
    t.start()
    log.info("Background notification checker spuštěn")
