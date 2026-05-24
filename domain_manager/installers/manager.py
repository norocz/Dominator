"""Systemd unit pro management web."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .base import BaseInstaller


class ManagerServiceInstaller(BaseInstaller):
    name = "Management web (systemd unit)"

    def install(self) -> None:
        unit = dedent(f"""\
            [Unit]
            Description=Domain Manager web UI
            After=network-online.target postgresql.service
            Wants=network-online.target

            [Service]
            Type=simple
            User=root
            ExecStart=/opt/domain-manager/.venv/bin/dm web start
            Restart=on-failure
            RestartSec=5
            Environment=DM_CONFIG=/etc/domain-manager/config.yaml

            [Install]
            WantedBy=multi-user.target
            """)
        self.runner.write_file(
            Path("/etc/systemd/system/domain-manager.service"),
            unit, mode=0o644,
        )
        self.runner.sh(["systemctl", "daemon-reload"])
        self.runner.systemd_enable_now("domain-manager")
