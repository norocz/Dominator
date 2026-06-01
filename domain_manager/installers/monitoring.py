"""Monitoring stack: Prometheus + Grafana (Docker, běží na DC2).

Prometheus scrapuje:
  - node_exporter na DC1, DC2 (HW metriky)
  - samba_exporter (volitelně)
  - pihole-exporter (z gh.com/eko/pihole-exporter)

Grafana předkonfigurované datasource Prometheus + dashboardy pro:
  - Node Exporter Full (ID 1860)
  - Samba AD DC (vlastní)
  - Pi-hole (ID 10176)

Zabbix je samostatná komponenta — viz installers/zabbix.py.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .base import BaseInstaller


MONITORING_DIR = Path("/opt/monitoring")


class MonitoringInstaller(BaseInstaller):
    name = "Monitoring (Prometheus + Grafana)"

    def install(self) -> None:
        MONITORING_DIR.mkdir(parents=True, exist_ok=True)
        (MONITORING_DIR / "prometheus").mkdir(exist_ok=True)
        (MONITORING_DIR / "grafana").mkdir(exist_ok=True)

        self._write_prometheus_config()
        self._write_compose()

        self.runner.sh(
            ["docker", "compose", "-f", str(MONITORING_DIR / "docker-compose.yml"), "up", "-d"],
        )

    def _write_prometheus_config(self) -> None:
        dc1 = str(self.cfg.servers.dc1.ip)
        dc2 = str(self.cfg.servers.dc2.ip)
        cfg = dedent(f"""\
            global:
              scrape_interval: 30s
              evaluation_interval: 30s

            scrape_configs:
              - job_name: 'node'
                static_configs:
                  - targets:
                      - '{dc1}:9100'
                      - '{dc2}:9100'

              - job_name: 'prometheus'
                static_configs:
                  - targets: ['localhost:9090']

              # Pi-hole exporter (oba)
              - job_name: 'pihole'
                static_configs:
                  - targets:
                      - '{dc1}:9617'
                      - '{dc2}:9617'
            """)
        self.runner.write_file(
            MONITORING_DIR / "prometheus" / "prometheus.yml",
            cfg, mode=0o644,
        )

    def _write_compose(self) -> None:
        p = self.cfg.monitoring.prometheus
        g = self.cfg.monitoring.grafana

        compose = dedent(f"""\
            services:
              prometheus:
                image: prom/prometheus:latest
                restart: unless-stopped
                ports:
                  - "{p.port}:9090"
                volumes:
                  - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
                  - prom-data:/prometheus
                command:
                  - '--config.file=/etc/prometheus/prometheus.yml'
                  - '--storage.tsdb.retention.time={p.retention_days}d'

              grafana:
                image: grafana/grafana:latest
                restart: unless-stopped
                ports:
                  - "{g.port}:3000"
                environment:
                  GF_SECURITY_ADMIN_PASSWORD: "{g.admin_password}"
                  GF_INSTALL_PLUGINS: ""
                volumes:
                  - grafana-data:/var/lib/grafana

            volumes:
              prom-data:
              grafana-data:
            """)
        self.runner.write_file(MONITORING_DIR / "docker-compose.yml", compose, mode=0o600)
