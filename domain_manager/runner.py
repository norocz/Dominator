"""Bezpečné spouštění systémových příkazů s logováním a dry-run režimem.

Filosofie: každý instalátor volá jenom `Runner.sh(...)`, `Runner.apt_install(...)`
apod. Nikdy ne přímo subprocess. Důvod:
  - jeden centrální bod pro logování
  - dry-run režim (vypíše co by se stalo, nic nespustí)
  - idempotence (apt_install přeskočí už nainstalované balíčky)
  - jednotné error handling
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

console = Console()
log = logging.getLogger("dm.runner")


@dataclass
class CmdResult:
    cmd: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RunnerError(RuntimeError):
    """Chyba při spuštění příkazu."""

    def __init__(self, result: CmdResult):
        super().__init__(
            f"Příkaz selhal (rc={result.returncode}): {result.cmd}\n"
            f"STDERR: {result.stderr.strip()[:500]}"
        )
        self.result = result


class Runner:
    """Centrální spouštěč systémových příkazů.

    Použití:
        runner = Runner(dry_run=False)
        runner.sh("hostnamectl set-hostname dc1")
        runner.apt_install(["samba", "krb5-user"])
        runner.write_file(Path("/etc/foo.conf"), "obsah", mode=0o600)
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    # --- shell ------------------------------------------------------------

    def sh(
        self,
        cmd: str | list[str],
        *,
        input_data: str | None = None,
        check: bool = True,
        env_extra: dict[str, str] | None = None,
        sensitive: bool = False,
    ) -> CmdResult:
        """Spustí příkaz. Pokud `sensitive`, v logu se neukáže (heslo apod.)."""
        if isinstance(cmd, list):
            cmd_str = " ".join(shlex.quote(p) for p in cmd)
            argv = cmd
            shell = False
        else:
            cmd_str = cmd
            argv = cmd
            shell = True

        display = "***SENSITIVE***" if sensitive else cmd_str
        if self.dry_run:
            console.print(f"[yellow][DRY][/] {display}")
            return CmdResult(cmd=display, returncode=0, stdout="", stderr="")

        console.print(f"[cyan]$[/] {display}")
        log.info("RUN: %s", display)

        import os
        env = None
        if env_extra:
            env = os.environ.copy()
            env.update(env_extra)

        proc = subprocess.run(
            argv,
            shell=shell,
            input=input_data,
            capture_output=True,
            text=True,
            env=env,
        )
        result = CmdResult(
            cmd=display,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if proc.stdout.strip():
            log.debug("stdout: %s", proc.stdout.strip()[:1000])
        if proc.stderr.strip():
            log.debug("stderr: %s", proc.stderr.strip()[:1000])

        if check and not result.ok:
            raise RunnerError(result)
        return result

    # --- apt --------------------------------------------------------------

    def apt_install(self, packages: list[str]) -> None:
        """Idempotentní instalace: přeskočí už nainstalované."""
        if not packages:
            return
        missing = self._missing_packages(packages)
        if not missing:
            console.print(f"[green]✓[/] balíčky už jsou nainstalované: {', '.join(packages)}")
            return
        console.print(f"[blue]→[/] instaluji: {', '.join(missing)}")
        self.sh(
            ["apt-get", "install", "-y", "-q", *missing],
            env_extra={"DEBIAN_FRONTEND": "noninteractive"},
        )

    def apt_update(self) -> None:
        self.sh(["apt-get", "update", "-q"])

    def _missing_packages(self, packages: list[str]) -> list[str]:
        if self.dry_run:
            return packages
        # dpkg-query je rychlejší než apt list
        missing = []
        for pkg in packages:
            r = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True,
            )
            if "install ok installed" not in r.stdout:
                missing.append(pkg)
        return missing

    # --- soubory ----------------------------------------------------------

    def write_file(
        self,
        path: Path,
        content: str,
        *,
        mode: int = 0o644,
        owner: str | None = None,
        backup: bool = True,
    ) -> bool:
        """Zapíše soubor. Vrátí True pokud se obsah změnil.

        Pokud `backup=True` a soubor existuje s jiným obsahem, uloží .bak.
        Idempotentní: pokud je obsah stejný, nedělá nic.
        """
        if self.dry_run:
            console.print(f"[yellow][DRY][/] zapsal bych {path} ({len(content)} B, mode={oct(mode)})")
            return False

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == content:
            console.print(f"[green]✓[/] {path} (beze změny)")
            return False

        if backup and existing is not None:
            backup_path = path.with_suffix(path.suffix + ".bak")
            backup_path.write_text(existing, encoding="utf-8")
            console.print(f"[dim]  záloha: {backup_path}[/]")

        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        if owner:
            self.sh(["chown", owner, str(path)])
        console.print(f"[blue]→[/] zapsáno {path}")
        log.info("WROTE: %s", path)
        return True

    # --- systemd ----------------------------------------------------------

    def systemd(self, action: str, unit: str) -> None:
        """systemctl wrapper. action: start/stop/restart/enable/disable/mask/unmask"""
        self.sh(["systemctl", action, unit])

    def systemd_enable_now(self, unit: str) -> None:
        self.sh(["systemctl", "enable", "--now", unit])

    def is_active(self, unit: str) -> bool:
        if self.dry_run:
            return False
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
        )
        return r.returncode == 0
