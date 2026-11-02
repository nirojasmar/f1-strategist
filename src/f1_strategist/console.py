"""Primera interfaz: consola (Rich). Solo presenta; no contiene lógica de estrategia."""

from __future__ import annotations

import time

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .config import AppConfig
from .console_messages import render_diagnostic, render_race_event
from .diagnostics import ConnectionState, connection_state, diagnose
from .packets import packet_name
from .ports import SessionPort
from .race_state import RaceStateSnapshot

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
_FRESHNESS_LABELS = {
    "session": "sesión",
    "lap_data": "vuelta",
    "participants": "participantes",
    "car_status": "combustible/neumáticos",
}


def _display_enum(value: str | None) -> str:
    return value.replace("_", " ").title() if value is not None else "—"


def _render_race_summary(race: RaceStateSnapshot, now: float) -> Panel:
    player = race.player_car
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row(
        "Sesión",
        f"{_display_enum(race.session.session_type)} · {_display_enum(race.session.track)}",
    )
    table.add_row("Piloto", escape(player.name) if player is not None and player.name else "—")
    lap = str(race.current_lap) if race.current_lap is not None else "—"
    if race.session.total_laps is not None:
        lap = f"{lap}/{race.session.total_laps}"
    table.add_row("Vuelta", lap)
    table.add_row(
        "Posición",
        str(player.position) if player is not None and player.position is not None else "—",
    )
    table.add_row(
        "Combustible",
        f"{player.fuel_kg:.1f} kg" if player is not None and player.fuel_kg is not None else "—",
    )
    compound = None
    if player is not None:
        compound = player.actual_tyre_compound or player.visual_tyre_compound
    table.add_row("Compuesto", _display_enum(compound))

    freshness_items = []
    for item in race.freshness:
        label = _FRESHNESS_LABELS.get(item.group)
        if label is None:
            continue
        age = item.age_seconds(now)
        if age is None:
            value = "sin datos"
        else:
            value = f"{age:.1f} s"
            if item.is_stale(now):
                value += " (obsoleto)"
        freshness_items.append(f"{label}: {value}")
    table.add_row("Frescura", " · ".join(freshness_items))
    if race.session.started is True:
        session_status = "finalizada" if race.session.ended else "en curso"
        table.add_row("Estado de sesión", session_status)
    elif race.session.ended is True:
        table.add_row("Estado de sesión", "finalizada")
    if race.events:
        table.add_row(
            "Eventos",
            "\n".join(escape(render_race_event(event, race.cars)) for event in race.events[-4:]),
        )
    return Panel(table, title="Estado de carrera", expand=False)


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
        Panel(info, title="F1 Strategist · telemetría UDP", expand=False),
        _render_race_summary(session.race_snapshot(), now),
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
        parts.append(f"[yellow]{escape(render_diagnostic(diagnostic))}[/]")
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
