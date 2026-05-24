"""CLI rozhraní pro Domain Manager.

Použití:
    dm --help
    dm config validate
    dm install dc1
    dm install dc2
    dm install pihole
    dm install monitoring
    dm web start
    dm import users users.csv
    dm import computers computers.csv
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import DEFAULT_CONFIG_PATH, load_config
from .runner import Runner, RunnerError

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True, show_path=False),
            logging.FileHandler("/var/log/domain-manager/dm.log", mode="a")
            if Path("/var/log/domain-manager").exists()
            else logging.NullHandler(),
        ],
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Detailní logování")
@click.option("--dry-run", is_flag=True, help="Vypiš co by se stalo, nic nedělej")
@click.option(
    "--config", "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=f"Cesta ke config.yaml (default: {DEFAULT_CONFIG_PATH})",
)
@click.pass_context
def main(ctx: click.Context, verbose: bool, dry_run: bool, config_path: Path | None) -> None:
    """Domain Manager - správa Samba AD, DHCP, Pi-hole a klientů."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["dry_run"] = dry_run
    ctx.obj["config_path"] = config_path
    ctx.obj["runner"] = Runner(dry_run=dry_run)


# --- config ----------------------------------------------------------------


@main.group()
def config() -> None:
    """Práce s konfigurací."""


@config.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """Načte a zvaliduje config.yaml. Vypíše souhrn."""
    try:
        cfg = load_config(ctx.obj["config_path"])
    except Exception as e:
        console.print(f"[red]✗ Konfigurace neplatná:[/] {e}")
        sys.exit(1)

    console.print("[green]✓ Konfigurace v pořádku[/]")
    table = Table(show_header=False, box=None)
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("Doména:", f"{cfg.domain.realm} ({cfg.domain.netbios})")
    table.add_row("Síť:", f"{cfg.network.subnet} (gw {cfg.network.gateway})")
    table.add_row("DC1:", f"{cfg.servers.dc1.hostname} @ {cfg.servers.dc1.ip}")
    table.add_row("DC2:", f"{cfg.servers.dc2.hostname} @ {cfg.servers.dc2.ip}")
    table.add_row("DHCP:", "ano" if cfg.dhcp.enabled else "ne")
    table.add_row("Pi-hole:", "ano" if cfg.pihole.enabled else "ne")
    table.add_row("Monitoring:", "ano" if cfg.monitoring.enabled else "ne")
    table.add_row("Manager web:", f"{cfg.manager.bind_host}:{cfg.manager.bind_port}")
    console.print(table)


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Vypíše efektivní konfiguraci (hesla skrytá)."""
    cfg = load_config(ctx.obj["config_path"])
    d = cfg.model_dump(mode="json")
    _mask_secrets(d)
    import json
    console.print_json(json.dumps(d))


def _mask_secrets(d: dict) -> None:
    for k, v in list(d.items()):
        if isinstance(v, dict):
            _mask_secrets(v)
        elif isinstance(k, str) and ("password" in k.lower() or "secret" in k.lower()):
            d[k] = "***"


# --- install ---------------------------------------------------------------


@main.group()
def install() -> None:
    """Instalace komponent."""


@install.command("dc1")
@click.option("--skip-provision", is_flag=True, help="Přeskoč provisioning AD (jen instalace balíčků)")
@click.pass_context
def install_dc1(ctx: click.Context, skip_provision: bool) -> None:
    """Nainstaluje a vyprovisuje primární řadič domény."""
    from .installers.samba_dc1 import SambaDC1Installer
    cfg = load_config(ctx.obj["config_path"])
    runner = ctx.obj["runner"]
    try:
        SambaDC1Installer(cfg, runner).run(skip_provision=skip_provision)
    except RunnerError as e:
        console.print(f"[red]✗ Instalace selhala:[/] {e}")
        sys.exit(1)


@install.command("dc2")
@click.option("--skip-join", is_flag=True)
@click.pass_context
def install_dc2(ctx: click.Context, skip_join: bool) -> None:
    """Nainstaluje sekundární řadič domény (join do existující domény)."""
    from .installers.samba_dc2 import SambaDC2Installer
    cfg = load_config(ctx.obj["config_path"])
    runner = ctx.obj["runner"]
    try:
        SambaDC2Installer(cfg, runner).run(skip_join=skip_join)
    except RunnerError as e:
        console.print(f"[red]✗ Instalace selhala:[/] {e}")
        sys.exit(1)


@install.command("docker")
@click.pass_context
def install_docker(ctx: click.Context) -> None:
    """Nainstaluje Docker engine."""
    from .installers.docker import DockerInstaller
    cfg = load_config(ctx.obj["config_path"])
    DockerInstaller(cfg, ctx.obj["runner"]).run()


@install.command("pihole")
@click.option("--instance", type=click.Choice(["1", "2"]), required=True,
              help="1=na DC1, 2=na DC2 (pro replikaci)")
@click.pass_context
def install_pihole(ctx: click.Context, instance: str) -> None:
    """Nainstaluje Pi-hole v Dockeru."""
    from .installers.pihole import PiholeInstaller
    cfg = load_config(ctx.obj["config_path"])
    PiholeInstaller(cfg, ctx.obj["runner"]).run(instance=int(instance))


@install.command("monitoring")
@click.pass_context
def install_monitoring(ctx: click.Context) -> None:
    """Prometheus + Grafana + Zabbix (běží na DC2)."""
    from .installers.monitoring import MonitoringInstaller
    cfg = load_config(ctx.obj["config_path"])
    MonitoringInstaller(cfg, ctx.obj["runner"]).run()


@install.command("dhcp")
@click.pass_context
def install_dhcp(ctx: click.Context) -> None:
    """ISC Kea DHCP s MAC reservacemi."""
    from .installers.dhcp_kea import KeaDhcpInstaller
    cfg = load_config(ctx.obj["config_path"])
    KeaDhcpInstaller(cfg, ctx.obj["runner"]).run()


@install.command("firewall")
@click.pass_context
def install_firewall(ctx: click.Context) -> None:
    """Aplikuje nftables pravidla podle configu."""
    from .installers.firewall import FirewallInstaller
    cfg = load_config(ctx.obj["config_path"])
    FirewallInstaller(cfg, ctx.obj["runner"]).run()


@install.command("postgres")
@click.option("--role", type=click.Choice(["primary", "standby"]), required=True)
@click.pass_context
def install_postgres(ctx: click.Context, role: str) -> None:
    """PostgreSQL pro management engine."""
    from .installers.postgres import PostgresInstaller
    cfg = load_config(ctx.obj["config_path"])
    PostgresInstaller(cfg, ctx.obj["runner"]).run(role=role)


# --- import ----------------------------------------------------------------


@main.group(name="import")
def import_() -> None:
    """Hromadný import z CSV."""


@import_.command("users")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run/--apply", default=True)
@click.pass_context
def import_users(ctx: click.Context, csv_path: Path, dry_run: bool) -> None:
    """Import uživatelů z CSV. Sloupce: username,first_name,last_name,email,password,groups."""
    from .ad.importers import UserImporter
    cfg = load_config(ctx.obj["config_path"])
    UserImporter(cfg).import_csv(csv_path, dry_run=dry_run)


@import_.command("computers")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run/--apply", default=True)
@click.pass_context
def import_computers(ctx: click.Context, csv_path: Path, dry_run: bool) -> None:
    """Import počítačů z CSV. Sloupce: hostname,mac,ip,groups,notes."""
    from .ad.importers import ComputerImporter
    cfg = load_config(ctx.obj["config_path"])
    ComputerImporter(cfg).import_csv(csv_path, dry_run=dry_run)


@import_.command("groups")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def import_groups(ctx: click.Context, csv_path: Path) -> None:
    """Import skupin z CSV. Sloupce: name,description,kind (user|computer)."""
    from .ad.importers import GroupImporter
    cfg = load_config(ctx.obj["config_path"])
    GroupImporter(cfg).import_csv(csv_path)


# --- web -------------------------------------------------------------------

_SYSTEMD_UNIT = "domain-manager"
_PID_CANDIDATES = [
    Path("/var/run/domain-manager/dm.pid"),
    Path("/tmp/dm-web.pid"),
]
_LOG_CANDIDATES = [
    Path("/var/log/domain-manager/web.log"),
    Path("/tmp/dm-web.log"),
]


def _pid_file() -> Path:
    for p in _PID_CANDIDATES:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        except PermissionError:
            continue
    return _PID_CANDIDATES[-1]


def _log_file() -> Path:
    for p in _LOG_CANDIDATES:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        except PermissionError:
            continue
    return _LOG_CANDIDATES[-1]


def _read_pid() -> int | None:
    for p in _PID_CANDIDATES:
        if p.exists():
            try:
                return int(p.read_text().strip())
            except (ValueError, OSError):
                pass
    return None


def _clear_pid() -> None:
    for p in _PID_CANDIDATES:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _service_installed() -> bool:
    import subprocess
    r = subprocess.run(
        ["systemctl", "list-unit-files", f"{_SYSTEMD_UNIT}.service"],
        capture_output=True, text=True,
    )
    return _SYSTEMD_UNIT in r.stdout


def _service_active() -> bool:
    import subprocess
    r = subprocess.run(
        ["systemctl", "is-active", "--quiet", _SYSTEMD_UNIT],
        capture_output=True,
    )
    return r.returncode == 0


def _pid_alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@main.group()
def web() -> None:
    """Management web UI."""


@web.command("start")
@click.option("--reload", is_flag=True, help="Auto-reload (vývoj)")
@click.option("--daemon", is_flag=True, help="Spustit na pozadí (zapíše PID soubor)")
@click.option("--demo", is_flag=True, help="Demo/testovací režim — automatické přihlášení, žádné skutečné akce")
@click.pass_context
def web_start(ctx: click.Context, reload: bool, daemon: bool, demo: bool) -> None:
    """Spustí FastAPI server."""
    import os
    if demo:
        os.environ["DM_DEMO"] = "1"
        console.print(
            "[yellow bold]⚠  Demo režim[/] — přihlášení automatické (demo/demo), "
            "všechny write akce jsou blokovány a zobrazeny jako toast."
        )
    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError as e:
        if demo:
            from .config import _demo_config
            cfg = _demo_config()
            console.print("[yellow]config.yaml nenalezen, používám demo konfiguraci[/]")
        else:
            console.print(f"[red]{e}[/]")
            sys.exit(1)

    if daemon:
        _web_start_daemon(cfg, demo)
        return

    import uvicorn
    uvicorn.run(
        "domain_manager.web.app:app",
        host=cfg.manager.bind_host,
        port=cfg.manager.bind_port,
        reload=reload,
    )


def _web_start_daemon(cfg, demo: bool) -> None:
    """Spustí uvicorn jako odloučený background proces a zapíše PID soubor."""
    import os
    import subprocess

    pid = _read_pid()
    if pid and _pid_alive(pid):
        console.print(f"[yellow]⚠  Server již běží (PID {pid}). Použij 'dm web restart' nebo 'dm web stop'.[/]")
        sys.exit(1)

    env = os.environ.copy()
    if demo:
        env["DM_DEMO"] = "1"

    log_path = _log_file()
    log_fh = open(log_path, "a")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "domain_manager.web.app:app",
            "--host", cfg.manager.bind_host,
            "--port", str(cfg.manager.bind_port),
        ],
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        env=env,
    )
    pid_path = _pid_file()
    pid_path.write_text(str(proc.pid))
    console.print(
        f"[green]✓[/] Domain Manager spuštěn na pozadí\n"
        f"  PID:  [cyan]{proc.pid}[/]\n"
        f"  URL:  [cyan]http://{cfg.manager.bind_host}:{cfg.manager.bind_port}[/]\n"
        f"  Log:  [dim]{log_path}[/]\n"
        f"  PID soubor: [dim]{pid_path}[/]"
    )


@web.command("stop")
def web_stop() -> None:
    """Zastaví běžící server (systemd nebo PID soubor)."""
    import signal

    if _service_installed():
        if _service_active():
            console.print(f"[blue]→[/] systemctl stop {_SYSTEMD_UNIT}")
            import subprocess
            subprocess.run(["systemctl", "stop", _SYSTEMD_UNIT], check=True)
            console.print("[green]✓[/] Služba zastavena.")
        else:
            console.print("[yellow]Služba není spuštěna.[/]")
        return

    pid = _read_pid()
    if not pid:
        console.print("[yellow]PID soubor nenalezen — server pravděpodobně neběží.[/]")
        sys.exit(1)
    if not _pid_alive(pid):
        console.print(f"[yellow]Proces PID {pid} neexistuje, mažu zastaralý PID soubor.[/]")
        _clear_pid()
        sys.exit(1)

    import os
    console.print(f"[blue]→[/] SIGTERM → PID {pid}")
    os.kill(pid, signal.SIGTERM)

    import time
    for _ in range(10):
        time.sleep(0.5)
        if not _pid_alive(pid):
            _clear_pid()
            console.print("[green]✓[/] Server zastaven.")
            return

    console.print(f"[yellow]Proces stále běží, posílám SIGKILL...[/]")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_pid()
    console.print("[green]✓[/] Server ukončen (SIGKILL).")


@web.command("restart")
@click.option("--demo", is_flag=True, help="Demo/testovací režim")
@click.pass_context
def web_restart(ctx: click.Context, demo: bool) -> None:
    """Restartuje server (systemd nebo stop+start)."""
    if _service_installed():
        console.print(f"[blue]→[/] systemctl restart {_SYSTEMD_UNIT}")
        import subprocess
        subprocess.run(["systemctl", "restart", _SYSTEMD_UNIT], check=True)
        console.print("[green]✓[/] Služba restartována.")
        return

    # PID-based restart
    pid = _read_pid()
    if pid and _pid_alive(pid):
        ctx.invoke(web_stop)

    import os
    if demo:
        os.environ["DM_DEMO"] = "1"
    try:
        cfg = load_config(ctx.obj["config_path"])
    except FileNotFoundError:
        if demo:
            from .config import _demo_config
            cfg = _demo_config()
        else:
            console.print("[red]config.yaml nenalezen[/]")
            sys.exit(1)
    _web_start_daemon(cfg, demo)


@web.command("status")
def web_status() -> None:
    """Zobrazí stav serveru."""
    from rich.table import Table as RichTable

    if _service_installed():
        import subprocess
        r = subprocess.run(
            ["systemctl", "status", _SYSTEMD_UNIT, "--no-pager", "-l"],
            capture_output=True, text=True,
        )
        console.print(r.stdout or r.stderr)
        return

    pid = _read_pid()
    t = RichTable(show_header=False, box=None)
    t.add_column(style="cyan", width=18)
    t.add_column()

    if pid and _pid_alive(pid):
        t.add_row("Stav:", "[green]● běží[/]")
        t.add_row("PID:", str(pid))
        log_path = next((p for p in _LOG_CANDIDATES if p.exists()), None)
        if log_path:
            t.add_row("Log:", str(log_path))
    else:
        t.add_row("Stav:", "[red]● zastaveno[/]")
        if pid:
            _clear_pid()

    console.print(t)


@web.command("install-service")
@click.pass_context
def web_install_service(ctx: click.Context) -> None:
    """Nainstaluje systemd unit pro management web."""
    from .installers.manager import ManagerServiceInstaller
    cfg = load_config(ctx.obj["config_path"])
    ManagerServiceInstaller(cfg, ctx.obj["runner"]).run()


# --- entrypoint ------------------------------------------------------------


if __name__ == "__main__":
    main()
