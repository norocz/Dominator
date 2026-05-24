"""Bázová třída pro všechny instalátory."""
from __future__ import annotations

from abc import ABC, abstractmethod

from rich.console import Console
from rich.panel import Panel

from ..config import Config
from ..runner import Runner

console = Console()


class BaseInstaller(ABC):
    """Společný protokol pro instalátory.

    Každý instalátor:
      1) Ohlásí, co bude dělat (banner)
      2) Provede preflight kontroly (preflight)
      3) Provede instalaci (install)
      4) Ověří výsledek (verify)
    """

    name: str = "base"

    def __init__(self, cfg: Config, runner: Runner):
        self.cfg = cfg
        self.runner = runner

    @abstractmethod
    def install(self) -> None: ...

    def preflight(self) -> None:
        """Volitelné kontroly před instalací. Default: nic."""
        return

    def verify(self) -> None:
        """Volitelné post-checks. Default: nic."""
        return

    def run(self, **kwargs) -> None:
        console.print(Panel.fit(
            f"[bold cyan]{self.name}[/]",
            border_style="cyan",
        ))
        self.preflight()
        self.install(**kwargs) if kwargs else self.install()
        self.verify()
        console.print(f"[green]✓ {self.name} hotovo[/]\n")

    # --- helpers ----------------------------------------------------------

    def is_dc1(self) -> bool:
        """Vrátí True pokud běžíme na DC1 (podle hostname)."""
        import socket
        return socket.gethostname() == self.cfg.servers.dc1.hostname

    def is_dc2(self) -> bool:
        import socket
        return socket.gethostname() == self.cfg.servers.dc2.hostname
