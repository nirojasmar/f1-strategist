"""Primera interfaz: consola (Rich). Solo presenta; no contiene lógica de estrategia."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import urlsplit

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .agent.types import AgentSnapshot, CallOutcome
from .config import AppConfig
from .console_messages import (
    AGENT_COUNTERS_LABEL,
    AGENT_LATENCY_LABEL,
    AGENT_PANEL_TITLE,
    AGENT_STATUS_LABEL,
    EVENTS_PANEL_TITLE,
    METRICS_PANEL_TITLE,
    PILOT_ACK_TEXT,
    PILOT_ANSWER_LABEL,
    PILOT_CLARIFICATION_LABEL,
    PILOT_EMPTY,
    PILOT_INPUT_BUSY,
    PILOT_INPUT_DISABLED,
    PILOT_INPUT_ERROR,
    PILOT_INPUT_HELP_RUN,
    PILOT_INPUT_NO_TERMINAL,
    PILOT_INPUT_PROMPT,
    PILOT_PANEL_TITLE,
    PILOT_QUESTION_LABEL,
    STRATEGY_PANEL_TITLE,
    render_agent_priority,
    render_agent_source,
    render_agent_status,
    render_diagnostic,
    render_event_message,
    render_metrics_rows,
    render_pilot_answer,
    render_pilot_clarification,
    render_pilot_kind,
    render_race_event,
    render_strategy_recording_warning,
    render_strategy_rows,
)
from .diagnostics import ConnectionState, connection_state, diagnose
from .events.types import EventsSnapshot
from .metrics.engine import MetricsSnapshot
from .packets import packet_name
from .pilot.console_input import LineEditor, TerminalKeyReader
from .pilot.types import PilotAnswer, PilotSnapshot
from .ports import SessionPort
from .race_state import RaceStateSnapshot
from .strategy.types import StrategyEvaluation, StrategySnapshot

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
_EVENT_PRIORITY_LABELS = {
    "critical": "crítica",
    "strategic": "estratégica",
    "informational": "informativa",
}


def _display_enum(value: str | None) -> str:
    return value.replace("_", " ").title() if value is not None else "—"


def _display_driver_result(value: str) -> str:
    labels = {
        "active": "En curso",
        "finished": "Finalizado",
        "did_not_finish": "No terminó (DNF)",
        "disqualified": "Descalificado",
        "not_classified": "No clasificado",
        "retired": "Retirado",
        "invalid": "Inválido",
        "inactive": "Inactivo",
    }
    if value.startswith("unknown:"):
        return f"Desconocido ({value.removeprefix('unknown:')})"
    return labels.get(value, _display_enum(value))


def _render_race_summary(race: RaceStateSnapshot, now: float) -> Panel:
    player = race.player_car
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
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
    if player is not None and player.result_status is not None:
        table.add_row("Estado del piloto", _display_driver_result(player.result_status))
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


def _render_metrics_summary(
    metrics: MetricsSnapshot,
    race: RaceStateSnapshot | None = None,
    now: float | None = None,
) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_column()
    for label, value, quality in render_metrics_rows(metrics, race, now):
        table.add_row(label, value, quality)
    return Panel(table, title=METRICS_PANEL_TITLE, expand=False)


def _evaluation_session_ended(
    race: RaceStateSnapshot | None, evaluation: StrategyEvaluation
) -> bool:
    """Solo una evaluación de la sesión actual puede quedar marcada como histórica."""
    if race is None or race.session.ended is not True:
        return False
    uid = race.session.uid
    return (str(uid) if uid is not None else "unknown") == evaluation.session_uid


def _render_strategy_summary(
    snapshot: StrategySnapshot,
    cfg: AppConfig,
    race: RaceStateSnapshot | None = None,
) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    if not cfg.strategy.enabled:
        table.add_row("Estado", Text("desactivada"))
    elif snapshot.evaluation is None:
        table.add_row("Estado", Text("Esperando la primera evaluación"))
    else:
        evaluation = snapshot.evaluation
        for label, value in render_strategy_rows(
            evaluation, session_ended=_evaluation_session_ended(race, evaluation)
        ):
            table.add_row(label, Text(value))
    if snapshot.error is not None:
        table.add_row("Error del motor", Text(snapshot.error))
    if cfg.strategy.enabled and not cfg.recording.enabled:
        table.add_row("Registro", Text(render_strategy_recording_warning(), style="yellow"))
    return Panel(table, title=STRATEGY_PANEL_TITLE, expand=False)


def _render_events_summary(snapshot: EventsSnapshot) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    alerts = snapshot.alerts
    counters = snapshot.counters
    emitted = counters.get("emit", 0) + counters.get("update", 0)
    suppressed = sum(counters.get(key, 0) for key in ("suppress", "defer", "cancel"))
    table.add_row("Emitidos / suprimidos", f"{emitted} / {suppressed}")
    if snapshot.muted:
        table.add_row("Estado", Text("Avisos silenciados", style="yellow"))
    if snapshot.error is not None:
        table.add_row("Error", Text(snapshot.error, style="yellow"))
    if not alerts:
        table.add_row("Avisos", "Sin avisos recientes.")
    else:
        for alert in reversed(alerts[-5:]):
            lap = "—" if alert.lap == 0 else str(alert.lap)
            priority = _EVENT_PRIORITY_LABELS.get(alert.priority.value, alert.priority.value)
            message = render_event_message(alert.message_key, alert.facts)
            table.add_row(
                f"{priority} · V{lap}",
                Text(message),
            )
    return Panel(table, title=EVENTS_PANEL_TITLE, expand=False)


def _render_agent_summary(snapshot: AgentSnapshot, cfg: AppConfig) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    health = snapshot.llm_status
    status = "disabled" if not cfg.llm.enabled else health.status.value
    host = None
    if status == "unavailable":
        endpoint = urlsplit(cfg.llm.endpoint)
        host = endpoint.netloc
    table.add_row(AGENT_STATUS_LABEL, render_agent_status(status, host))

    if cfg.llm.enabled:
        if snapshot.last_latency_s is not None:
            table.add_row(AGENT_LATENCY_LABEL, f"{snapshot.last_latency_s:.3f} s")
        counters = snapshot.counters
        used = counters.get(CallOutcome.USED.value, 0)
        fallback = counters.get(CallOutcome.FALLBACK.value, 0)
        discarded = counters.get(CallOutcome.DISCARDED_OBSOLETE.value, 0)
        table.add_row(AGENT_COUNTERS_LABEL, f"{used} / {fallback} / {discarded}")
        for item in reversed(snapshot.messages[-5:]):
            table.add_row(
                f"{render_agent_priority(item.priority.value)} · "
                f"V{item.lap if item.lap else '—'} · "
                f"{render_agent_source(item.source.value)}",
                Text(item.text),
            )
    return Panel(table, title=AGENT_PANEL_TITLE, expand=False)


class _PilotConsole:
    """Lanza la consulta fuera del hilo de refresco para poder mostrar el acuse."""

    def __init__(self, session: SessionPort) -> None:
        self._session = session
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future[PilotAnswer] | None = None
        self._pending: str | None = None
        self._error: str | None = None

    @property
    def pending_text(self) -> str | None:
        return self._pending

    @property
    def error_text(self) -> str | None:
        return self._error

    def submit(self, text: str) -> None:
        if not text:
            return
        if self._pending is not None:
            self._error = PILOT_INPUT_BUSY
            return
        self._pending = text
        self._error = None
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pilot")
        self._future = self._executor.submit(self._session.ask_pilot, text)

    def poll(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        self._pending = None
        try:
            future.result()
        except Exception:  # la consola no debe caerse por un módulo opcional
            self._error = PILOT_INPUT_ERROR
        else:
            self._error = None

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)


def _render_pilot_summary(
    snapshot: PilotSnapshot,
    cfg: AppConfig,
    *,
    pending: str | None = None,
    error: str | None = None,
) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    limit = max(1, min(5, cfg.pilot.history_size))
    exchanges = snapshot.recent[-limit:]
    for exchange in exchanges:
        table.add_row(PILOT_QUESTION_LABEL, Text(exchange.query.text))
        answer = exchange.answer
        table.add_row(
            PILOT_ANSWER_LABEL,
            Text(f"{render_pilot_kind(answer.kind.value)} · {render_pilot_answer(answer)}"),
        )
    if pending is not None:
        table.add_row(PILOT_QUESTION_LABEL, Text(pending))
        table.add_row("", Text(PILOT_ACK_TEXT, style="yellow"))
    clarification = snapshot.pending_clarification
    if clarification is not None:
        table.add_row(PILOT_CLARIFICATION_LABEL, Text(render_pilot_clarification(clarification)))
    if error is not None:
        table.add_row("", Text(error, style="red"))
    if not exchanges and pending is None and clarification is None and error is None:
        table.add_row("", PILOT_EMPTY)
    return Panel(table, title=PILOT_PANEL_TITLE, expand=False)


def format_bytes(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{nbytes} B"


def render(
    cfg: AppConfig,
    session: SessionPort,
    now: float | None = None,
    *,
    race_now: float | None = None,
    pilot_pending: str | None = None,
    pilot_draft: str | None = None,
    pilot_input_warning: str | None = None,
    pilot_error: str | None = None,
    pilot_help: str | None = None,
) -> Group:
    """Renderiza el estado.

    ``now`` es el reloj monotónico de la interfaz (diagnóstico y tramas). En
    reproducción, ``race_now`` permite usar el instante de la captura para la
    frescura de los datos de carrera sin mezclarlo con ese reloj.
    """
    now = time.monotonic() if now is None else now
    race_now = now if race_now is None else race_now
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

    race = session.race_snapshot()
    parts: list[RenderableType] = [
        Panel(info, title="F1 Strategist · telemetría UDP", expand=False),
        _render_race_summary(race, race_now),
        _render_metrics_summary(session.metrics_snapshot(), race, race_now),
        _render_strategy_summary(session.strategy_snapshot(), cfg, race),
        _render_events_summary(session.events_snapshot()),
        _render_agent_summary(session.agent_snapshot(), cfg),
        _render_pilot_summary(
            session.pilot_snapshot(),
            cfg,
            pending=pilot_pending,
            error=pilot_error,
        ),
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
    if pilot_draft is not None:
        parts.append(Text(f"{PILOT_INPUT_PROMPT}{pilot_draft}", style="bold"))
    if pilot_input_warning is not None:
        parts.append(Text(pilot_input_warning, style="yellow"))
    if pilot_help is not None:
        parts.append(Text(pilot_help, style="dim"))
    elif pilot_draft is not None:
        parts.append(Text(PILOT_INPUT_HELP_RUN, style="dim"))
    else:
        parts.append("[dim]Ctrl+C para salir[/]")
    return Group(*parts)


def run_console(
    cfg: AppConfig,
    session: SessionPort,
    console: Console | None = None,
    *,
    input_enabled: bool | None = None,
) -> None:
    """Muestra el estado hasta Ctrl+C o hasta que falle el socket."""
    console = console or Console()
    keys = TerminalKeyReader(enabled=input_enabled)
    editor = LineEditor(max_chars=cfg.pilot.max_question_chars)
    pilot = _PilotConsole(session)
    if input_enabled is False:
        warning: str | None = PILOT_INPUT_DISABLED
    elif not keys.enabled:
        warning = PILOT_INPUT_NO_TERMINAL
    else:
        warning = None

    def build() -> Group:
        pilot.poll()
        return render(
            cfg,
            session,
            pilot_pending=pilot.pending_text,
            pilot_draft=editor.text if keys.enabled else None,
            pilot_input_warning=warning,
            pilot_error=pilot.error_text,
        )

    try:
        with Live(build(), console=console, refresh_per_second=4) as live:
            try:
                while session.receiver_error is None:
                    if keys.enabled:
                        key = keys.read()
                        question = editor.feed(key) if key is not None else None
                        if question is not None:
                            pilot.submit(question)
                            live.update(build(), refresh=True)
                    time.sleep(0.05)
                    live.update(build(), refresh=True)
            except KeyboardInterrupt:
                pass
    finally:
        pilot.close()
        keys.close()
