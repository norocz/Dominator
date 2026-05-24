"""Pomocné funkce pro zápis do audit logu.

Volat po každé write operaci — nesmí vyhodit výjimku (audit log nesmí
zabít hlavní akci).
"""
from __future__ import annotations

import logging

log = logging.getLogger("dm.audit")


def log_action(
    actor: str,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    details: dict | None = None,
) -> None:
    """Zapíše řádek do audit_log tabulky. Bezpečné - zachytí všechny výjimky."""
    try:
        from ..db.models import AuditEntry, get_session
        with get_session() as session:
            entry = AuditEntry(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details or {},
            )
            session.add(entry)
            session.commit()
    except Exception as exc:
        log.warning("Nepodařilo se zapsat audit log: %s", exc)
