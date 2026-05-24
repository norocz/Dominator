"""SSL/TLS certificate checker.

Načte certifikát ze zadaného hostu:portu přes ssl.get_server_certificate(),
pak ho parsuje buď přes cryptography library (preferováno) nebo stdlib ssl.
"""
from __future__ import annotations

import hashlib
import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("dm.certs")

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtensionOID, NameOID
    _CRYPTO = True
except ImportError:
    _CRYPTO = False
    log.info("cryptography library chybí — nainstalujte: pip install cryptography")


class CertError(RuntimeError):
    pass


@dataclass
class CertInfo:
    hostname: str
    port: int
    subject_cn: Optional[str] = None
    subject_san: list[str] = None
    issuer: Optional[str] = None
    not_before: Optional[datetime] = None
    not_after: Optional[datetime] = None
    serial: Optional[str] = None
    fingerprint_sha256: Optional[str] = None
    days_until_expiry: Optional[int] = None
    is_expired: bool = False
    is_self_signed: bool = False
    error: Optional[str] = None

    def __post_init__(self):
        if self.subject_san is None:
            self.subject_san = []
        if self.not_after and self.not_before:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            delta = self.not_after - now
            self.days_until_expiry = delta.days
            self.is_expired = delta.days < 0


def fetch(hostname: str, port: int = 443, timeout: int = 10) -> CertInfo:
    """Stáhne a zparsuje TLS certifikát. Nikdy nevyhazuje výjimku."""
    info = CertInfo(hostname=hostname, port=port)

    try:
        raw_pem = ssl.get_server_certificate((hostname, port), timeout=timeout)
    except Exception as e:
        info.error = str(e)
        return info

    if _CRYPTO:
        _parse_with_cryptography(info, raw_pem)
    else:
        _parse_with_stdlib(info, raw_pem, hostname, port, timeout)

    # Fingerprint ze surového PEM
    try:
        der = ssl.PEM_cert_to_DER_cert(raw_pem)
        fp = hashlib.sha256(der).hexdigest()
        info.fingerprint_sha256 = ":".join(fp[i:i+2] for i in range(0, len(fp), 2))
    except Exception:
        pass

    return info


def _parse_with_cryptography(info: CertInfo, pem: str) -> None:
    try:
        cert = x509.load_pem_x509_certificate(pem.encode())
        # Subject CN
        try:
            info.subject_cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except Exception:
            pass
        # SAN
        try:
            san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            info.subject_san = [str(n.value) for n in san_ext.value]
        except Exception:
            pass
        # Issuer
        try:
            cn_list = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
            info.issuer = cn_list[0].value if cn_list else str(cert.issuer)
        except Exception:
            pass
        # Validity
        info.not_before = cert.not_valid_before_utc.replace(tzinfo=None)
        info.not_after  = cert.not_valid_after_utc.replace(tzinfo=None)
        # Serial
        info.serial = hex(cert.serial_number)
        # Self-signed
        info.is_self_signed = cert.issuer == cert.subject
    except Exception as e:
        info.error = f"Parse error: {e}"


def _parse_with_stdlib(info: CertInfo, pem: str, hostname: str, port: int, timeout: int) -> None:
    """Fallback bez cryptography — použije ssl.SSLContext."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
        # Subject CN
        for part in cert.get("subject", []):
            for k, v in part:
                if k == "commonName":
                    info.subject_cn = v
        # SAN
        san_raw = cert.get("subjectAltName", [])
        info.subject_san = [f"{t}:{v}" for t, v in san_raw]
        # Issuer
        for part in cert.get("issuer", []):
            for k, v in part:
                if k == "commonName":
                    info.issuer = v
        # Dates
        def _parse_dt(s: str) -> datetime:
            from email.utils import parsedate
            import time
            return datetime(*parsedate(s)[:6])
        try:
            info.not_before = _parse_dt(cert["notBefore"])
            info.not_after  = _parse_dt(cert["notAfter"])
        except Exception:
            pass
        info.serial = cert.get("serialNumber")
    except Exception as e:
        info.error = f"stdlib fallback error: {e}"


def check_all_certs(alert_days: int = 30) -> list[CertInfo]:
    """Projde všechny certifikáty v DB, stáhne je a aktualizuje záznamy."""
    from ..db.models import Certificate, get_session
    results = []
    with get_session() as session:
        certs = session.query(Certificate).all()
        for c in certs:
            info = fetch(c.hostname, c.port)
            c.subject_cn = info.subject_cn
            c.subject_san = info.subject_san
            c.issuer = info.issuer
            c.not_before = info.not_before
            c.not_after = info.not_after
            c.serial = info.serial
            c.fingerprint_sha256 = info.fingerprint_sha256
            c.check_error = info.error
            from datetime import datetime, timezone
            c.last_checked = datetime.now(timezone.utc).replace(tzinfo=None)
            # Reset alert flag pokud se cert obnovil
            if info.days_until_expiry and info.days_until_expiry > alert_days:
                c.alert_sent = False
            results.append(info)
        session.commit()
    return results
