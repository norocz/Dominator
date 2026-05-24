"""Instalace Docker Engine na Ubuntu 26.04.

Pozn.: 26.04 přinesla Docker 29 s containerd 2.x a defaultním image storem
containerd. Pro náš use case (Pi-hole + monitoring stack) je default v pohodě.
"""
from __future__ import annotations

from .base import BaseInstaller


class DockerInstaller(BaseInstaller):
    name = "Docker Engine"

    PACKAGES = [
        "docker.io",        # z Ubuntu repozitáře - jednodušší než upstream Docker repo
        "docker-compose-v2",
        "docker-buildx",
    ]

    def install(self) -> None:
        self.runner.apt_update()
        self.runner.apt_install(self.PACKAGES)
        self.runner.systemd_enable_now("docker")

    def verify(self) -> None:
        r = self.runner.sh(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
        if r.ok:
            from rich.console import Console
            Console().print(f"[green]Docker server:[/] {r.stdout.strip()}")
