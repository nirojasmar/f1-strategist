"""Primera interfaz: consola (Rich). Solo presenta; no contiene lógica de estrategia."""

from __future__ import annotations

import time

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .config import AppConfig
from .console_messages import render_diagnostic
from .diagnostics import ConnectionState, connection_state, diagnose
from .packets import packet_name
from .ports import SessionPort

_STATE_STYLE = {
    ConnectionState.WAITING: "yellow",
    ConnectionState.RECEIVING: "green",
    ConnectionState.STALLED: "red",
}
_STATE_LABEL = {
    ConnectionState.WAITING: "ESPERANDO",
    ConnectionState.RECEIVING: "RECIBIENDO",
    ConnectionState.STALLED: "SIN DATOS",
}


def format_bytes(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{nbytes} B"


def render(cfg: AppConfig, session: SessionPort, now: float | None = None) -> Group:
    now = time.monotonic() if now is None else now
    tele = cfg.telemetry
    snap = session.snapshot()
    state = connection_state(tele, snap, now)

    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold")
    info.add_column()
    scope = "solo este PC (loopback)" if tele.is_loopback_only else "red local"
    info.add_row("Escucha", f"{tele.listen_address}:{session.bound_port} · {scope}")
    if tele.allowed_sources:
        info.add_row("Orígenes permitidos", ", ".join(tele.allowed_sources))
    info.add_row("Estado", f"[{_STATE_STYLE[state]}]{_STATE_LABEL[state]}[/]")
    if snap.last_packet_monotonic is not None:
        info.add_row("Último paquete", f"hace {now - snap.last_packet_monotonic:.1f} s")
    if snap.sources:
        sources = ", ".join(
            f"{'otros' if ip == 'other' else ip} ({count})"
            for ip, count in sorted(snap.sources.items())
        )
        info.add_row("Origen", sources)
    info.add_row(
        "Paquetes",
        f"{snap.received} recibidos · {snap.rejected_source} rechazados · "
        f"{snap.invalid} inválidos · {snap.dropped_queue_full} descartados",
    )
    header = snap.last_header
    if header is not None:
        info.add_row(
            "Juego (cabecera)",
            f"formato {header.packet_format} · año {header.game_year} · "
            f"v{header.game_major_version}.{header.game_minor_version}",
        )
    if session.capture_path is not None:
        info.add_row(
            "Captura",
            f"{session.capture_path} · {snap.capture_packets} paquetes · "
            f"{format_bytes(snap.capture_bytes)}",
        )
    else:
        info.add_row("Captura", "desactivada")

    parts: list[RenderableType] = [
        Panel(info, title="F1 Strategist · telemetría UDP", expand=False)
    ]
    if snap.packets_by_id:
        table = Table(title="Paquetes por tipo · huecos estimados", expand=False)
        table.add_column("ID", justify="right")
        table.add_column("Tipo")
        table.add_column("Cantidad", justify="right")
        table.add_column("Huecos", justify="right")
        table.add_column("Duplicados", justify="right")
        table.add_column("Desorden", justify="right")
        for packet_id, count in sorted(snap.packets_by_id.items()):
            sequence = snap.sequence_by_id.get(packet_id)
            table.add_row(
                str(packet_id),
                packet_name(packet_id),
                str(count),
                str(sequence.frame_gaps if sequence else 0),
                str(sequence.duplicates if sequence else 0),
                str(sequence.out_of_order if sequence else 0),
            )
        parts.append(table)
    for diagnostic in diagnose(tele, snap, now):
        parts.append(f"[yellow]{render_diagnostic(diagnostic)}[/]")
    parts.append("[dim]Ctrl+C para salir[/]")
    return Group(*parts)


def run_console(cfg: AppConfig, session: SessionPort, console: Console | None = None) -> None:
    """Muestra el estado hasta Ctrl+C o hasta que falle el socket."""
    console = console or Console()
    with Live(render(cfg, session), console=console, refresh_per_second=4) as live:
        try:
            while session.receiver_error is None:
                time.sleep(0.25)
                live.update(render(cfg, session))
        except KeyboardInterrupt:
            pass
