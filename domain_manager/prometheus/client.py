"""Prometheus HTTP API klient.

Dotazuje Prometheus pro HW metriky klientů (node_exporter).
Výsledky se ukládají do DB (Computer.last_cpu_pct, is_online, ...).

Prometheus API:
  GET /api/v1/query?query=<promql>           instant vector
  GET /api/v1/query_range?query=<promql>&... range vector

Instance v metrikách mají tvar "ip:port" (např. "192.168.10.20:9100").
Matchujeme s Computer.ip_reserved nebo hostname (přes DNS).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger("dm.prometheus")


class PrometheusError(RuntimeError):
    pass


@dataclass
class NodeMetrics:
    instance: str          # "192.168.10.20" (bez portu)
    is_online: bool = False
    cpu_pct: int | None = None
    ram_pct: int | None = None
    disk_pct: int | None = None
    last_seen: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "instance": self.instance,
            "is_online": self.is_online,
            "cpu_pct": self.cpu_pct,
            "ram_pct": self.ram_pct,
            "disk_pct": self.disk_pct,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


class PrometheusClient:
    """Synchronní Prometheus HTTP API klient."""

    def __init__(self, host: str, port: int = 9090):
        self.base = f"http://{host}:{port}/api/v1"
        self._http = httpx.Client(timeout=10.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PrometheusClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # --- nízkoúrovňový dotaz -----------------------------------------------

    def query(self, promql: str) -> list[dict]:
        """Instant query. Vrátí seznam {metric: {}, value: [ts, "value"]}."""
        try:
            r = self._http.get(f"{self.base}/query", params={"query": promql})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise PrometheusError(f"Prometheus nedostupný ({self.base}): {e}") from e
        data = r.json()
        if data.get("status") != "success":
            raise PrometheusError(f"Prometheus chyba: {data.get('error', '?')}")
        return data["data"]["result"]

    def _fval(self, result_item: dict) -> float | None:
        try:
            return float(result_item["value"][1])
        except (KeyError, IndexError, ValueError):
            return None

    def _instance_ip(self, result_item: dict) -> str:
        """Extrahuje IP ze instance labelu "host:port"."""
        return result_item["metric"].get("instance", "").split(":")[0]

    # --- agregované HW metriky --------------------------------------------

    def node_metrics(self) -> dict[str, NodeMetrics]:
        """Vrátí {ip_str: NodeMetrics} pro všechny scrapované nody."""
        nodes: dict[str, NodeMetrics] = {}

        def _get(ip: str) -> NodeMetrics:
            if ip not in nodes:
                nodes[ip] = NodeMetrics(instance=ip)
            return nodes[ip]

        # Online status + čas poslední odpovědi
        try:
            for m in self.query('up{job="node"}'):
                ip = self._instance_ip(m)
                if not ip:
                    continue
                nm = _get(ip)
                v = self._fval(m)
                nm.is_online = v == 1.0
                ts = m["value"][0]
                nm.last_seen = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except PrometheusError as e:
            log.warning("Prometheus up query selhala: %s", e)
            return nodes

        # CPU %
        try:
            for m in self.query(
                '100 - (avg by (instance) '
                '(rate(node_cpu_seconds_total{mode="idle",job="node"}[5m])) * 100)'
            ):
                ip = self._instance_ip(m)
                v = self._fval(m)
                if ip and v is not None:
                    _get(ip).cpu_pct = max(0, min(100, int(v)))
        except PrometheusError as e:
            log.warning("Prometheus CPU query selhala: %s", e)

        # RAM %
        try:
            for m in self.query(
                '(1 - node_memory_MemAvailable_bytes{job="node"}'
                ' / node_memory_MemTotal_bytes{job="node"}) * 100'
            ):
                ip = self._instance_ip(m)
                v = self._fval(m)
                if ip and v is not None:
                    _get(ip).ram_pct = max(0, min(100, int(v)))
        except PrometheusError as e:
            log.warning("Prometheus RAM query selhala: %s", e)

        # Disk % (root filesystem)
        try:
            for m in self.query(
                '(1 - node_filesystem_avail_bytes{mountpoint="/",job="node"}'
                ' / node_filesystem_size_bytes{mountpoint="/",job="node"}) * 100'
            ):
                ip = self._instance_ip(m)
                v = self._fval(m)
                if ip and v is not None:
                    _get(ip).disk_pct = max(0, min(100, int(v)))
        except PrometheusError as e:
            log.warning("Prometheus Disk query selhala: %s", e)

        return nodes

    # --- jednoduchý test ---------------------------------------------------

    def is_reachable(self) -> bool:
        try:
            r = self._http.get(f"{self.base.replace('/api/v1', '')}/-/healthy", timeout=3)
            return r.status_code == 200
        except Exception:
            return False


def make_client(cfg) -> PrometheusClient:
    """Prometheus běží na DC2."""
    return PrometheusClient(
        host=str(cfg.servers.dc2.ip),
        port=cfg.monitoring.prometheus.port,
    )


def sync_to_db(cfg, session) -> dict:
    """Dotáže Prometheus a aktualizuje metriky všech počítačů v DB.

    Vrátí souhrn: {"updated": n, "online": n, "errors": [...]}.
    """
    from ..db.models import Computer

    errors: list[str] = []
    updated = 0
    online = 0

    with PrometheusClient(
        host=str(cfg.servers.dc2.ip),
        port=cfg.monitoring.prometheus.port,
    ) as prom:
        try:
            metrics = prom.node_metrics()
        except PrometheusError as e:
            return {"updated": 0, "online": 0, "errors": [str(e)]}

    # Matchování: ip_reserved nebo hostname (přes řetězec)
    computers = session.query(Computer).all()
    for comp in computers:
        nm = metrics.get(comp.ip_reserved or "")
        if nm is None:
            # Zkus hostname (Prometheus může mít hostname jako instance)
            for ip, m in metrics.items():
                if comp.hostname and comp.hostname.lower() in ip.lower():
                    nm = m
                    break
        if nm is None:
            continue

        comp.is_online = nm.is_online
        if nm.last_seen:
            comp.last_seen = nm.last_seen
        if nm.cpu_pct is not None:
            comp.last_cpu_pct = nm.cpu_pct
        if nm.ram_pct is not None:
            comp.last_ram_used_pct = nm.ram_pct
        if nm.disk_pct is not None:
            comp.last_disk_used_pct = nm.disk_pct

        updated += 1
        if nm.is_online:
            online += 1

    try:
        session.commit()
    except Exception as e:
        errors.append(f"DB commit chyba: {e}")

    return {"updated": updated, "online": online, "errors": errors, "total_nodes": len(metrics)}
