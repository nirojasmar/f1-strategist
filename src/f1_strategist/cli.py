"""Línea de comandos: captura, reproducción, inspección y catálogo de escenarios."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import select
import signal
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .agent.bench import diagnostic_config, report_json, run_benchmark, run_check
from .agent.evaluation import cases_from_calls, load_cases, replay_client, run_evaluation
from .agent.llm_client import OpenAICompatibleClient
from .agent.types import CallOutcome, MessageSource
from .capture import CaptureError
from .config import AppConfig, ConfigError, LLMConfig, MetricsConfig, apply_overrides, load_config
from .console import format_bytes, render, run_console
from .console_messages import (
    AGENT_EMPTY_MESSAGES,
    AGENT_EXPORT_OVERWRITES_CAPTURE,
    AGENT_EXPORT_OVERWRITES_DB,
    AGENT_EXPORT_SCHEMA_VERSION,
    AGENT_EXPORT_WRITTEN,
    AGENT_MESSAGES_COLUMNS,
    AGENT_MESSAGES_TITLE,
    AGENT_READER_API_ERROR,
    AGENT_SCHEMA_ERROR,
    AGENT_SUMMARY_CAUSE_COLUMN,
    AGENT_SUMMARY_CODE_COLUMN,
    AGENT_SUMMARY_FALLBACKS_TITLE,
    AGENT_SUMMARY_LATENCY_TITLE,
    AGENT_SUMMARY_PERCENTILE_COLUMN,
    AGENT_SUMMARY_SECONDS_COLUMN,
    AGENT_SUMMARY_SOURCE_COLUMN,
    AGENT_SUMMARY_SOURCES_TITLE,
    AGENT_SUMMARY_TITLE,
    AGENT_SUMMARY_VALIDATION_TITLE,
    AGENT_UNKNOWN_VALUE,
    LLM_EVAL_OVERWRITES_CAPTURE,
    LLM_EVAL_OVERWRITES_CONFIG,
    LLM_EVAL_OVERWRITES_DATASET,
    LLM_EVAL_OVERWRITES_DB,
    LLM_EVAL_SKIPPED_CALLS,
    render_agent_priority,
    render_agent_source,
    render_event_message,
    render_strategy_rows,
)
from .datasets import DEFAULT_MANIFEST, Manifest, Scenario, load_manifest, verify_scenario
from .events.engine import EventEngine, EventEvaluation
from .events.types import (
    EventPhase,
    EventSignal,
    Fact,
    GateDecision,
)
from .fingerprint import build_run_info, fingerprint_capture
from .metrics.engine import MetricsEngine
from .metrics.export import EXPORT_FORMATS, render_metrics_export, run_metrics_capture
from .metrics.history import MetricInputs
from .offline import OfflineResult, run_capture_offline
from .packets import packet_name
from .parser import ParsedPacket
from .playback import PlaybackSession
from .race_state import RaceState
from .receiver import ReceiverError
from .recorder import ingest_capture
from .replay import replay_capture
from .session import CaptureSession
from .store import SessionInfo, SessionStore, StoreError
from .store.queries import (
    held_metric_values,
    list_session_summaries,
    render_session_export,
    require_session,
    session_events,
    session_laps,
)
from .strategy.engine import StrategyEngine, evaluate_once
from .strategy.export import STRATEGY_EXPORT_FORMATS, render_strategy_export
from .strategy.review import review_strategy_evaluations
from .strategy.types import StrategyEvaluation
from .summary import summarize
from .trim import trim_capture
from .weekend_profile import WeekendProfile
from .weekend_provider import profiles_for_capture, resolve_weekend_profile

_SPEED_STEPS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def _parse_packet_types(value: str) -> frozenset[int]:
    try:
        packet_types = frozenset(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("usa IDs separados por comas") from exc
    if not packet_types or any(not 0 <= packet_id <= 15 for packet_id in packet_types):
        raise argparse.ArgumentTypeError("los IDs de paquete deben estar entre 0 y 15")
    return packet_types


def _parse_decimation(value: str) -> tuple[int, int]:
    try:
        packet_id_text, interval_text = value.split("=", 1)
        packet_id, interval = int(packet_id_text), int(interval_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("formato esperado: ID=N") from exc
    if not 0 <= packet_id <= 15 or interval < 1:
        raise argparse.ArgumentTypeError("el ID debe estar entre 0 y 15 y N debe ser >= 1")
    return packet_id, interval


def _parse_lap_range(value: str) -> tuple[int, int]:
    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("formato esperado: A-B") from exc
    if not 1 <= start <= end <= 255:
        raise argparse.ArgumentTypeError("el rango debe cumplir 1 <= A <= B <= 255")
    return start, end


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("se esperaba un entero") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("el valor debe ser mayor o igual que 1")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("se esperaba un entero") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("el valor debe ser mayor o igual que 0")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-strategist",
        description="Ingeniero de carrera con IA para F1 25.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="recibe telemetría UDP, la graba y muestra el estado")
    run.add_argument("--config", type=Path, help="archivo TOML de configuración")
    run.add_argument("--listen-address", help="IP donde escuchar (0.0.0.0 para recibir de la LAN)")
    run.add_argument("--port", type=int, help="puerto UDP (por defecto 20777)")
    run.add_argument(
        "--allow-source",
        action="append",
        dest="allow_sources",
        metavar="IP",
        help="aceptar solo paquetes de esta IP (repetible)",
    )
    run.add_argument("--no-capture", action="store_true", help="no grabar la sesión")
    run.add_argument("--capture-dir", type=Path, help="directorio de capturas")
    run.add_argument(
        "--mute-agent",
        action="store_true",
        help="detectar y registrar eventos sin emitir avisos",
    )

    replay = sub.add_parser("replay", help="reenvía una captura por UDP (sin abrir el juego)")
    replay.add_argument("file", type=Path)
    replay.add_argument("--host", default="127.0.0.1", help="IP del PC que ejecuta la aplicación")
    replay.add_argument("--port", type=int, default=20777)
    replay.add_argument("--speed", type=float, default=1.0, help="velocidad (0 = sin pausas)")
    replay.add_argument("--loop", action="store_true", help="repetir hasta Ctrl+C")

    play = sub.add_parser("play", help="reproduce una captura sin red, con pausa y velocidad")
    play.add_argument("file", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    play.add_argument(
        "--speed", type=float, default=1.0, help="velocidad de reproducción (0 = sin pausas)"
    )
    play.add_argument("--loop", action="store_true", help="repetir hasta Ctrl+C")
    play.add_argument("--start-lap", type=int, help="empezar en esta vuelta (avance rápido)")
    play.add_argument("--config", type=Path, help="archivo TOML de configuración")

    inspect = sub.add_parser("inspect", help="resume el contenido de una captura")
    inspect.add_argument("file", type=Path)

    metrics = sub.add_parser("metrics", help="calcula y exporta métricas de una captura")
    metrics.add_argument("file", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    metrics.add_argument("--out", type=Path, help="archivo de salida (por defecto, stdout)")
    metrics.add_argument(
        "--format",
        default="json",
        metavar="FORMATO",
        help=f"formato de salida: {', '.join(EXPORT_FORMATS)}",
    )
    metrics.add_argument("--config", type=Path, help="archivo TOML de configuración")

    trim = sub.add_parser("trim", help="recorta y opcionalmente anonimiza una captura")
    trim.add_argument("input", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    trim.add_argument("output", type=Path, help="captura de salida (.f1cap o .f1cap.gz)")
    trim.add_argument(
        "--types",
        action="append",
        type=_parse_packet_types,
        metavar="ID[,ID...]",
        help="conservar solo estos IDs de paquete (repetible)",
    )
    trim.add_argument(
        "--decimate",
        action="append",
        type=_parse_decimation,
        metavar="ID=N",
        help="conservar un paquete de cada N para el ID (por coche para IDs 11 y 12)",
    )
    trim.add_argument(
        "--laps",
        type=_parse_lap_range,
        metavar="A-B",
        help="conservar paquetes desde el inicio de A hasta el inicio de B+1",
    )
    trim.add_argument(
        "--anonymize",
        action="store_true",
        help="eliminar LobbyInfo, anonimizar participantes, UID, enlaces, tiempos y origen",
    )
    trim.add_argument(
        "--keep-ai-names",
        action="store_true",
        help="conservar nombres de participantes controlados por IA al anonimizar",
    )

    verify = sub.add_parser(
        "verify", help="comprueba que las ejecuciones offline producen la misma huella"
    )
    verify.add_argument("file", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    verify.add_argument(
        "--runs",
        type=_positive_int,
        default=2,
        metavar="N",
        help="número de ejecuciones a comparar (por defecto 2)",
    )
    verify.add_argument("--expect", metavar="HUELLA", help="huella SHA-256 esperada")
    verify.add_argument("--config", type=Path, help="archivo TOML de configuración")

    ingest = sub.add_parser(
        "ingest", help="genera la base de datos a partir de una captura offline"
    )
    ingest.add_argument("file", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    ingest.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )
    ingest.add_argument("--config", type=Path, help="archivo TOML de configuración")
    ingest.add_argument(
        "--replace",
        action="store_true",
        help="reemplaza la sesión ya ingerida con la misma captura y configuración",
    )

    datasets = sub.add_parser("datasets", help="consulta el catálogo de capturas de referencia")
    datasets_sub = datasets.add_subparsers(dest="datasets_command", required=True)
    datasets_list = datasets_sub.add_parser("list", help="lista los escenarios del manifiesto")
    datasets_list.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifiesto TOML (por defecto, {DEFAULT_MANIFEST})",
    )
    datasets_verify = datasets_sub.add_parser(
        "verify", help="verifica el hash y las expectativas de los escenarios"
    )
    datasets_verify.add_argument("--id", help="verificar solo este escenario")
    datasets_verify.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifiesto TOML (por defecto, {DEFAULT_MANIFEST})",
    )

    sessions = sub.add_parser(
        "sessions", help="consulta las sesiones guardadas en la base de datos"
    )
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)

    sessions_list = sessions_sub.add_parser("list", help="lista las sesiones guardadas")
    sessions_list.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    sessions_show = sessions_sub.add_parser("show", help="muestra los metadatos de una sesión")
    sessions_show.add_argument("session_id", type=_positive_int, metavar="ID")
    sessions_show.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    sessions_laps = sessions_sub.add_parser("laps", help="lista las vueltas de una sesión")
    sessions_laps.add_argument("session_id", type=_positive_int, metavar="ID")
    sessions_laps.add_argument("--car", type=int, metavar="N", help="filtrar por índice de coche")
    sessions_laps.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    sessions_events = sessions_sub.add_parser("events", help="lista los eventos de una sesión")
    sessions_events.add_argument("session_id", type=_positive_int, metavar="ID")
    sessions_events.add_argument("--kind", metavar="TIPO", help="filtrar por tipo de evento")
    sessions_events.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    sessions_metrics = sessions_sub.add_parser(
        "metrics", help="muestra el valor mantenido de las métricas por vuelta"
    )
    sessions_metrics.add_argument("session_id", type=_positive_int, metavar="ID")
    sessions_metrics.add_argument(
        "--key",
        required=True,
        metavar="PATRÓN",
        help="patrón glob de la clave (por ejemplo, 'fuel.*')",
    )
    sessions_metrics.add_argument("--lap", type=int, metavar="N", help="mostrar solo esta vuelta")
    sessions_metrics.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    sessions_export = sessions_sub.add_parser(
        "export", help="exporta una sesión con el formato del comando metrics"
    )
    sessions_export.add_argument("session_id", type=_positive_int, metavar="ID")
    sessions_export.add_argument(
        "--format",
        default="json",
        metavar="FORMATO",
        help=f"formato de salida: {', '.join(EXPORT_FORMATS)}",
    )
    sessions_export.add_argument("--out", type=Path, help="archivo de salida (por defecto, stdout)")
    sessions_export.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    weekends = sub.add_parser(
        "weekends", help="agrupa y consulta los fines de semana de la base de datos"
    )
    weekends_sub = weekends.add_subparsers(dest="weekends_command", required=True)

    weekends_list = weekends_sub.add_parser("list", help="lista los fines de semana")
    weekends_list.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    weekends_show = weekends_sub.add_parser("show", help="muestra un fin de semana y sus sesiones")
    weekends_show.add_argument("weekend_id", type=_positive_int, metavar="ID")
    weekends_show.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    weekends_link = weekends_sub.add_parser(
        "link", help="enlaza manualmente sesiones del mismo circuito en un fin de semana"
    )
    weekends_link.add_argument(
        "session_ids",
        type=_positive_int,
        nargs="+",
        metavar="ID",
        help="sesiones a enlazar (deben ser del mismo circuito)",
    )
    weekends_link.add_argument("--label", metavar="TEXTO", help="nombre del fin de semana")
    weekends_link.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    weekends_unlink = weekends_sub.add_parser(
        "unlink", help="separa una sesión de su fin de semana"
    )
    weekends_unlink.add_argument("session_id", type=_positive_int, metavar="ID")
    weekends_unlink.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    weekend = sub.add_parser(
        "weekend", help="muestra el perfil del fin de semana usado por una carrera"
    )
    weekend_sub = weekend.add_subparsers(dest="weekend_command", required=True)

    weekend_show = weekend_sub.add_parser(
        "show", help="muestra el perfil del fin de semana de una sesión"
    )
    weekend_show.add_argument(
        "--session",
        type=_positive_int,
        metavar="ID",
        help="sesión cuya carrera usó el perfil (por defecto, la última que lo registró)",
    )
    weekend_show.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    strategy = sub.add_parser("strategy", help="consulta recomendaciones de estrategia")
    strategy_sub = strategy.add_subparsers(dest="strategy_command", required=True)
    strategy_at = strategy_sub.add_parser("at", help="evalúa una captura en una vuelta")
    strategy_at.add_argument("file", type=Path, help="captura de entrada (.f1cap o .f1cap.gz)")
    strategy_at.add_argument("--lap", type=_positive_int, required=True, help="vuelta de carrera")
    strategy_at.add_argument("--json", action="store_true", help="imprime la evaluación en JSON")
    strategy_at.add_argument("--config", type=Path, help="archivo TOML de configuración")

    strategy_show = strategy_sub.add_parser("show", help="muestra una evaluación guardada")
    strategy_show.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    strategy_show.add_argument("--lap", type=_positive_int, help="vuelta exacta")
    strategy_show.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    strategy_export = strategy_sub.add_parser("export", help="exporta evaluaciones guardadas")
    strategy_export.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    strategy_export.add_argument(
        "--format",
        choices=STRATEGY_EXPORT_FORMATS,
        default="json",
        help="formato de salida (por defecto: json)",
    )
    strategy_export.add_argument("--out", type=Path, help="archivo de salida (por defecto, stdout)")
    strategy_export.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    strategy_review = strategy_sub.add_parser(
        "review", help="compara proyecciones guardadas con vueltas reales"
    )
    strategy_review.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    strategy_review.add_argument(
        "--db",
        type=Path,
        help="base de datos SQLite (por defecto, la de la configuración)",
    )

    events = sub.add_parser("events", help="consulta eventos y alertas del agente")
    events_sub = events.add_subparsers(dest="events_command")
    events_show = events_sub.add_parser("show", help="muestra detecciones y decisiones guardadas")
    events_show.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    events_show.add_argument("--rule", help="filtra por clave de regla")
    events_show.add_argument("--outcome", help="filtra por resultado de la compuerta")
    events_show.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")
    events_summary = events_sub.add_parser("summary", help="resume detecciones y decisiones")
    events_summary.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    events_summary.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")
    events_simulate = events_sub.add_parser("simulate", help="simula detecciones sobre una captura")
    events_simulate.add_argument("file", type=Path, metavar="CAPTURA")
    events_simulate.add_argument("--from-lap", type=_positive_int, metavar="A")
    events_simulate.add_argument("--to-lap", type=_positive_int, metavar="B")
    events_simulate.add_argument("--config", type=Path, help="archivo TOML de configuración")

    agent = sub.add_parser("agent", help="panel y comandos del agente de radio")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_show = agent_sub.add_parser("show", help="muestra mensajes de radio guardados")
    agent_show.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    agent_show.add_argument(
        "--source",
        choices=tuple(source.value for source in MessageSource),
        help="filtra por origen (llm o template)",
    )
    agent_show.add_argument(
        "--outcome",
        choices=tuple(outcome.value for outcome in CallOutcome),
        help="filtra por resultado de la llamada",
    )
    agent_show.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")

    agent_summary = agent_sub.add_parser("summary", help="resume mensajes y llamadas guardados")
    agent_summary.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    agent_summary.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")

    agent_export = agent_sub.add_parser("export", help="exporta mensajes y llamadas guardados")
    agent_export.add_argument("--session", type=_positive_int, required=True, metavar="ID")
    agent_export.add_argument(
        "--format", choices=("json", "jsonl", "csv"), default="json", help="formato de salida"
    )
    agent_export.add_argument("--out", type=Path, help="archivo de salida (por defecto, stdout)")
    agent_export.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")

    llm = sub.add_parser("llm", help="diagnóstico y medición del LLM")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_check = llm_sub.add_parser("check", help="comprueba las capacidades del endpoint")
    llm_check.add_argument("--config", type=Path, help="archivo TOML de configuración")
    llm_check.add_argument("--endpoint", help="URL base compatible con OpenAI")
    llm_check.add_argument("--model", help="identificador del modelo")
    llm_bench = llm_sub.add_parser("bench", help="mide latencia y respuestas")
    llm_bench.add_argument("--runs", type=_positive_int, default=10, metavar="N")
    llm_bench.add_argument("--warmup", type=_non_negative_int, default=1, metavar="K")
    llm_bench.add_argument("--json", type=Path, metavar="ARCHIVO")
    llm_bench.add_argument("--note", help="etiqueta para las condiciones de la medición")
    llm_bench.add_argument("--config", type=Path, help="archivo TOML de configuración")
    llm_bench.add_argument("--endpoint", help="URL base compatible con OpenAI")
    llm_bench.add_argument("--model", help="identificador del modelo")
    llm_eval = llm_sub.add_parser("eval", help="evalúa respuestas sobre casos grabados")
    llm_eval_source = llm_eval.add_mutually_exclusive_group(required=True)
    llm_eval_source.add_argument("--dataset", type=Path, metavar="DIR")
    llm_eval_source.add_argument("--from-session", type=_positive_int, metavar="ID")
    llm_eval.add_argument("--model", help="modelo real (por defecto, respuestas grabadas)")
    llm_eval.add_argument("--out", type=Path, metavar="ARCHIVO")
    llm_eval.add_argument("--db", type=Path, help="base SQLite (por defecto, configuración)")
    llm_eval.add_argument("--config", type=Path, help="archivo TOML de configuración")
    llm_eval.add_argument("--endpoint", help="URL base compatible con OpenAI")

    sub.add_parser("ask", help="preguntas del piloto por texto")
    sub.add_parser("queries", help="consulta las preguntas registradas en la base de datos")
    return parser


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _install_signal_handlers() -> None:
    """Cierre limpio con Ctrl+C y con SIGTERM, aunque SIGINT venga ignorado."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def _cmd_run(args: argparse.Namespace, console: Console, err: Console) -> int:
    _install_signal_handlers()
    cfg = apply_overrides(
        load_config(args.config),
        listen_address=args.listen_address,
        listen_port=args.port,
        allowed_sources=args.allow_sources,
        capture_enabled=False if args.no_capture else None,
        capture_directory=args.capture_dir,
    )
    if args.mute_agent:
        cfg = cfg.model_copy(
            update={"gate": cfg.gate.model_copy(update={"muted": True})},
        )
    session = CaptureSession(cfg)
    try:
        session.start()
    except ReceiverError as exc:
        err.print(f"[red]Error:[/] {exc}")
        return 2
    try:
        run_console(cfg, session, console)
    finally:
        session.stop()
    snap = session.snapshot()
    console.print(f"Recibidos {snap.received} paquetes.")
    if session.capture_path is not None:
        console.print(f"Captura guardada en {session.capture_path}")
    if session.receiver_error:
        err.print(f"[red]Error:[/] {session.receiver_error}")
        return 1
    return 0


def _cmd_replay(args: argparse.Namespace, console: Console, err: Console) -> int:
    console.print(f"Reenviando {args.file} a {args.host}:{args.port} (velocidad {args.speed})")
    try:
        sent = replay_capture(args.file, args.host, args.port, args.speed, args.loop)
    except KeyboardInterrupt:
        console.print("Interrumpido.")
        return 0
    console.print(f"{sent} paquetes enviados.")
    return 0


class _KeyReader:
    """Lectura de teclas sin bloquear; se desactiva si stdin no es un terminal."""

    def __init__(self) -> None:
        self._enabled = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._windows = os.name == "nt"
        self._saved_attributes: Any | None = None
        self._fd: int | None = None
        if not self._enabled:
            return
        if not self._windows:
            import termios  # noqa: PLC0415
            import tty  # noqa: PLC0415

            self._fd = sys.stdin.fileno()
            self._saved_attributes = termios.tcgetattr(self._fd)  # type: ignore[reportAttributeAccessIssue]
            tty.setcbreak(self._fd)  # type: ignore[reportAttributeAccessIssue]

    def read(self) -> str | None:
        if not self._enabled:
            return None
        if self._windows:
            import msvcrt  # noqa: PLC0415

            # Las stubs de msvcrt en plataformas no Windows no declaran estas
            # funciones, aunque solo se usan cuando os.name == "nt".
            kbhit = getattr(msvcrt, "kbhit", None)
            getwch = getattr(msvcrt, "getwch", None)
            if kbhit is None or getwch is None or not kbhit():
                return None
            key = getwch()
            return key if key != "\x03" else "q"
        fd = self._fd
        if fd is None:
            return None
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return None
        data = os.read(fd, 1)
        if not data:
            return None
        return data.decode("utf-8", errors="ignore") or None

    def close(self) -> None:
        if self._saved_attributes is None or self._fd is None:
            return
        import termios  # noqa: PLC0415

        termios.tcsetattr(  # type: ignore[reportAttributeAccessIssue]
            self._fd,
            termios.TCSADRAIN,  # type: ignore[reportAttributeAccessIssue]
            self._saved_attributes,
        )
        self._saved_attributes = None
        self._fd = None


def _next_speed(current: float, direction: int) -> float:
    if current <= 0:
        return _SPEED_STEPS[0] if direction > 0 else 0.0
    if direction > 0:
        return next(
            (step for step in _SPEED_STEPS if step > current),
            _SPEED_STEPS[-1],
        )
    return next(
        (step for step in reversed(_SPEED_STEPS) if step < current),
        _SPEED_STEPS[0],
    )


def _render_playback(cfg: AppConfig, session: PlaybackSession) -> Group:
    help_text = "espacio: pausa · +/-: velocidad · q: salir"
    return Group(
        render(cfg, session, time.monotonic(), race_now=session.observed_at),
        Text(help_text, style="dim"),
    )


def _run_playback_console(
    cfg: AppConfig,
    session: PlaybackSession,
    console: Console,
) -> None:
    keys = _KeyReader()
    try:
        with Live(_render_playback(cfg, session), console=console, refresh_per_second=4) as live:
            try:
                while not session.finished and session.error is None:
                    key = keys.read()
                    if key == " ":
                        if session.paused:
                            session.resume()
                        else:
                            session.pause()
                    elif key in {"+", "="}:
                        session.set_speed(_next_speed(session.speed, 1))
                    elif key == "-":
                        session.set_speed(_next_speed(session.speed, -1))
                    elif key in {"q", "Q"}:
                        break
                    time.sleep(0.05)
                    live.update(_render_playback(cfg, session))
            except KeyboardInterrupt:
                pass
    finally:
        keys.close()


def _cmd_play(args: argparse.Namespace, console: Console, err: Console) -> int:
    cfg = load_config(args.config)
    profiles = _profiles_for_capture(cfg, args.file, err)
    session = PlaybackSession(
        args.file,
        cfg.metrics,
        loop=args.loop,
        speed=args.speed,
        start_lap=args.start_lap,
        profiles_by_uid=profiles,
        strategy_config=cfg.strategy,
        events_config=cfg.events,
        gate_config=cfg.gate,
    )
    _install_signal_handlers()
    try:
        session.play()
        _run_playback_console(cfg, session, console)
    finally:
        session.stop()
    if session.error is not None:
        err.print(f"[red]Error:[/] {session.error}")
        return 1
    if session.finished:
        snap = session.snapshot()
        console.print(f"Reproducción completada: {snap.received} paquetes.")
    return 0


def _cmd_verify(args: argparse.Namespace, console: Console, err: Console) -> int:
    if args.runs < 1:
        raise ValueError("--runs debe ser mayor o igual que 1")
    cfg = load_config(args.config)
    profiles = _profiles_for_capture(cfg, args.file, err)
    first = None
    for index in range(args.runs):
        result = fingerprint_capture(args.file, cfg, profiles_by_uid=profiles)
        integrity_problems = result.offline.integrity_problems()
        if integrity_problems:
            err.print("[red]Error:[/] No se puede verificar una captura incompleta:")
            for problem in integrity_problems:
                err.print(f"  - {problem}")
            return 1
        if first is None:
            first = result
        elif result.fingerprint != first.fingerprint:
            err.print(
                "[red]Error:[/] Las ejecuciones no son deterministas: "
                f"ejecución 1 = {first.fingerprint}, ejecución {index + 1} = "
                f"{result.fingerprint}"
            )
            return 1
    assert first is not None
    if args.expect is not None:
        expected = args.expect.strip().lower()
        if expected != first.fingerprint:
            err.print(
                "[red]Error:[/] La huella no coincide: "
                f"esperada {expected}, obtenida {first.fingerprint}"
            )
            return 1
    console.print(f"Huella determinista ({args.runs} ejecución(es)): [bold]{first.fingerprint}[/]")
    info = first.run_info
    console.print(
        f"Configuración {info.config_hash} · parser {info.parser_version} · "
        f"Python {info.python_version}"
    )
    return 0


def _profiles_for_capture(
    cfg: AppConfig,
    path: Path,
    err: Console,
) -> dict[int, WeekendProfile]:
    """Carga perfiles registrados para la captura, si existe la base configurada."""
    database = cfg.recording.database
    if not path.is_file() or not database.is_file():
        return {}
    run_info = build_run_info(cfg, path)
    try:
        with SessionStore.open(database, readonly=True) as store:
            return profiles_for_capture(store, run_info.capture_sha256)
    except StoreError as exc:
        err.print(
            "[yellow]Aviso:[/] No se pudieron cargar perfiles registrados; "
            f"la reproducción continuará sin ellos ({exc})."
        )
        return {}


def _cmd_inspect(args: argparse.Namespace, console: Console, err: Console) -> int:
    info = summarize(args.file)
    console.print(f"[bold]{info.path}[/]")
    console.print(
        f"{info.packets} paquetes · {format_bytes(info.total_bytes)} · "
        f"{info.duration_s:.1f} s · {info.invalid} inválidos"
    )
    if info.truncated:
        console.print("[yellow]La captura termina con un registro incompleto (se ignoró).[/]")
    for key in ("created_utc", "platform", "listen_address", "listen_port"):
        if key in info.metadata:
            console.print(f"{key}: {info.metadata[key]}")
    if info.sources:
        console.print("Orígenes: " + ", ".join(f"{ip} ({n})" for ip, n in info.sources.items()))
    if info.formats:
        table = Table(title="Cabecera de los paquetes", expand=False)
        for column in ("Formato", "Año", "Versión", "Paquetes"):
            table.add_column(column, justify="right")
        for (fmt, year, major, minor), count in sorted(info.formats.items()):
            table.add_row(str(fmt), str(year), f"{major}.{minor}", str(count))
        console.print(table)
    if info.packets_by_id:
        table = Table(title="Paquetes por tipo", expand=False)
        table.add_column("ID", justify="right")
        table.add_column("Tipo")
        table.add_column("Cantidad", justify="right")
        for packet_id, count in sorted(info.packets_by_id.items()):
            table.add_row(str(packet_id), packet_name(packet_id), str(count))
        console.print(table)
    return 0


def _cmd_metrics(args: argparse.Namespace, console: Console, err: Console) -> int:
    export_format = args.format.lower()
    if export_format not in EXPORT_FORMATS:
        valid = ", ".join(EXPORT_FORMATS)
        raise ValueError(f"Formato de exportación no válido: {args.format!r}; usa {valid}")
    if args.out is not None:
        protected = [args.file]
        if args.config is not None:
            protected.append(args.config)
        if any(_same_file(args.out, path) for path in protected):
            raise ValueError("La salida no puede sobrescribir la captura ni la configuración.")
    cfg = load_config(args.config)
    result = run_metrics_capture(args.file, cfg.metrics)
    _warn_offline_issues(result.offline, err)
    output = render_metrics_export(result.snapshot, result.history, export_format)
    if args.out is None:
        _write_stdout(output)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as handle:
            handle.write(output)
        console.print(f"Métricas exportadas en {args.out}")
    return 0


def _cmd_ingest(args: argparse.Namespace, console: Console, err: Console) -> int:
    cfg = load_config(args.config)
    if args.db is not None:
        cfg = cfg.model_copy(
            update={
                "recording": cfg.recording.model_copy(update={"database": args.db}),
            }
        )
    with SessionStore.open(cfg.recording.database) as store:
        result = ingest_capture(args.file, store, cfg, replace=args.replace)
    if result.skipped:
        console.print(
            f"La captura ya estaba ingerida en la sesión {result.session_id}; "
            "usa --replace para reemplazarla."
        )
        return 0
    console.print(
        f"Sesión {result.session_id} ingerida: {result.laps} vueltas, "
        f"{result.events} eventos, {result.metric_samples} muestras y "
        f"{result.errors} errores."
    )
    console.print(f"Huella determinista: [bold]{result.fingerprint}[/]")
    if result.offline is not None:
        _warn_offline_issues(result.offline, err)
        if result.offline.truncated:
            err.print("[yellow]Aviso:[/] La sesión se guardó marcada como incompleta.")
    return 0


def _sessions_database(args: argparse.Namespace) -> Path:
    """Devuelve la base indicada o la configurada por defecto."""
    if args.db is not None:
        return args.db
    return load_config(None).recording.database


def _text(value: object) -> str:
    """Convierte un valor opcional a texto para las tablas."""
    return "-" if value is None else str(value)


def _yes_no(value: bool | None) -> str:
    """Convierte un booleano opcional a texto en español."""
    if value is None:
        return "-"
    return "sí" if value else "no"


def _metric_text(value: object) -> str:
    """Representa un valor de métrica en una celda de tabla."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def _cmd_sessions(args: argparse.Namespace, console: Console, err: Console) -> int:
    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        command = args.sessions_command
        if command == "list":
            return _sessions_list(store, console)
        if command == "show":
            return _sessions_show(store, args.session_id, console)
        if command == "laps":
            return _sessions_laps(store, args.session_id, args.car, console)
        if command == "events":
            return _sessions_events(store, args.session_id, args.kind, console)
        if command == "metrics":
            return _sessions_metrics(store, args, console)
        return _sessions_export(store, args, db, console)


def _sessions_list(store: SessionStore, console: Console) -> int:
    summaries = list_session_summaries(store)
    if not summaries:
        console.print("No hay sesiones grabadas.")
        return 0
    table = Table(title="Sesiones grabadas", expand=False)
    table.add_column("ID", justify="right")
    table.add_column("Circuito", overflow="fold")
    table.add_column("Tipo")
    table.add_column("Vueltas", justify="right")
    table.add_column("Origen")
    table.add_column("Huella")
    table.add_column("Incompleta")
    for summary in summaries:
        session = summary.session
        fingerprint = session.fingerprint or "-"
        if len(fingerprint) > 12:
            fingerprint = fingerprint[:12] + "…"
        table.add_row(
            str(session.id),
            _text(session.track),
            _text(session.session_type),
            _text(session.total_laps),
            session.source,
            fingerprint,
            _yes_no(session.incomplete),
        )
    console.print(table)
    return 0


def _sessions_show(store: SessionStore, session_id: int, console: Console) -> int:
    session = require_session(store, session_id)
    assert session.id is not None
    table = Table(title=f"Sesión {session.id}", show_header=False, expand=False)
    table.add_column("Campo", style="bold")
    table.add_column("Valor", overflow="fold")
    rows = (
        ("ID", session.id),
        ("UID", session.session_uid),
        ("Origen", session.source),
        ("Inicio (UTC)", session.started_at_utc),
        ("Fin (UTC)", session.ended_at_utc),
        ("Incompleta", session.incomplete),
        ("Juego", session.game_format),
        ("Año", session.game_year),
        ("Versión", session.game_version),
        ("Circuito", session.track),
        ("Tipo de sesión", session.session_type),
        ("Vueltas totales", session.total_laps),
        ("Captura", session.capture_path),
        ("SHA-256 de la captura", session.capture_sha256),
        ("Herramienta", session.tool_version),
        ("Parser", session.parser_version),
        ("Hash de configuración", session.config_hash),
        ("Configuración", session.config_json),
        ("Huella", session.fingerprint),
        ("Fin de semana", session.weekend_id),
        ("Enlace del juego", session.weekend_link_id),
        ("Enlace de sesión", session.session_link_id),
        ("Enlace de temporada", session.season_link_id),
        ("Estructura del fin de semana", session.weekend_structure_json),
        ("Vueltas guardadas", sum(1 for _ in store.iter_laps(session.id))),
        ("Eventos", sum(1 for _ in store.iter_events(session.id))),
        ("Muestras de métricas", sum(1 for _ in store.iter_metric_samples(session.id))),
        ("Errores", sum(1 for _ in store.iter_errors(session.id))),
    )
    for label, value in rows:
        table.add_row(label, _yes_no(value) if isinstance(value, bool) else _text(value))
    console.print(table)
    return 0


def _sessions_laps(
    store: SessionStore, session_id: int, car_index: int | None, console: Console
) -> int:
    laps = session_laps(store, session_id, car_index=car_index)
    if not laps:
        console.print("No hay vueltas para los filtros indicados.")
        return 0
    table = Table(title=f"Vueltas de la sesión {session_id}", expand=False)
    table.add_column("Coche", justify="right")
    table.add_column("Vuelta", justify="right")
    table.add_column("Tiempo", justify="right")
    table.add_column("Compuesto")
    table.add_column("Edad", justify="right")
    table.add_column("Válida")
    table.add_column("Pos.", justify="right")
    table.add_column("Parada")
    table.add_column("Cerrada")
    for lap in laps:
        lap_time = "-" if lap.lap_time_s is None else f"{lap.lap_time_s:.3f} s"
        table.add_row(
            str(lap.car_index),
            str(lap.lap_number),
            lap_time,
            _text(lap.compound),
            _text(lap.tyre_age_end),
            _yes_no(lap.valid),
            _text(lap.position_end),
            _yes_no(lap.pit_entry),
            _yes_no(lap.closed),
        )
    console.print(table)
    return 0


def _sessions_events(
    store: SessionStore, session_id: int, kind: str | None, console: Console
) -> int:
    events = session_events(store, session_id, kind=kind)
    if not events:
        console.print("No hay eventos para los filtros indicados.")
        return 0
    table = Table(title=f"Eventos de la sesión {session_id}", expand=False)
    table.add_column("Secuencia", justify="right")
    table.add_column("Tipo", overflow="fold")
    table.add_column("t_sim (s)", justify="right")
    table.add_column("Coche", justify="right")
    table.add_column("Vuelta", justify="right")
    table.add_column("Valor", overflow="fold")
    table.add_column("Valor anterior", overflow="fold")
    for event in events:
        table.add_row(
            str(event.seq),
            event.kind,
            f"{event.t_sim_s:.3f}",
            _text(event.car_index),
            _text(event.lap_number),
            _text(event.value),
            _text(event.previous_value),
        )
    console.print(table)
    return 0


def _sessions_metrics(store: SessionStore, args: argparse.Namespace, console: Console) -> int:
    held = held_metric_values(store, args.session_id, key_pattern=args.key, lap=args.lap)
    if not held:
        console.print("No hay métricas para los filtros indicados.")
        return 0
    table = Table(title=f"Métricas de la sesión {args.session_id}", expand=False)
    table.add_column("Vuelta", justify="right")
    table.add_column("Clave", overflow="fold")
    table.add_column("Valor", overflow="fold")
    table.add_column("Unidad")
    table.add_column("Calidad")
    table.add_column("Muestras", justify="right")
    for item in held:
        table.add_row(
            str(item.lap),
            item.key,
            _metric_text(item.value),
            _text(item.unit),
            item.quality,
            str(item.sample_count),
        )
    console.print(table)
    return 0


def _sessions_export(
    store: SessionStore,
    args: argparse.Namespace,
    db: Path,
    console: Console,
) -> int:
    export_format = args.format.lower()
    if export_format not in EXPORT_FORMATS:
        valid = ", ".join(EXPORT_FORMATS)
        raise ValueError(f"Formato de exportación no válido: {args.format!r}; usa {valid}")
    session = require_session(store, args.session_id)
    if args.out is not None:
        if _same_file(args.out, db):
            raise ValueError("La salida no puede sobrescribir la base de datos.")
        if session.capture_path is not None and _same_file(args.out, Path(session.capture_path)):
            raise ValueError("La salida no puede sobrescribir la captura asociada.")
    output = render_session_export(store, args.session_id, export_format)
    if args.out is None:
        _write_stdout(output)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as handle:
            handle.write(output)
        console.print(f"Exportación de la sesión {args.session_id} escrita en {args.out}")
    return 0


def _cmd_weekends(args: argparse.Namespace, console: Console, err: Console) -> int:
    db = _sessions_database(args)
    command = args.weekends_command
    if command in ("link", "unlink"):
        with SessionStore.open(db) as store:
            if command == "link":
                return _weekends_link(store, args, console)
            return _weekends_unlink(store, args.session_id, console)
    with SessionStore.open(db, readonly=True) as store:
        if command == "list":
            return _weekends_list(store, console)
        return _weekends_show(store, args.weekend_id, console)


def _weekends_list(store: SessionStore, console: Console) -> int:
    weekends = store.list_weekends()
    if not weekends:
        console.print("No hay fines de semana enlazados.")
    else:
        table = Table(title="Fines de semana", expand=False)
        table.add_column("ID", justify="right")
        table.add_column("Etiqueta", overflow="fold")
        table.add_column("Circuito")
        table.add_column("Origen")
        table.add_column("Sesiones", justify="right")
        table.add_column("Enlace del juego")
        for weekend in weekends:
            sessions = store.list_weekend_sessions(weekend.id)
            table.add_row(
                str(weekend.id),
                _text(weekend.label),
                _text(weekend.track),
                weekend.source,
                str(len(sessions)),
                _text(weekend.weekend_link_id),
            )
        console.print(table)
    unlinked = store.unlinked_sessions()
    if unlinked:
        console.print(
            f"Sesiones sin fin de semana: {len(unlinked)} "
            f"({', '.join(str(session.id) for session in unlinked)})."
        )
    return 0


def _weekends_show(store: SessionStore, weekend_id: int, console: Console) -> int:
    weekend = store.get_weekend(weekend_id)
    if weekend is None:
        raise StoreError(f"No existe el fin de semana {weekend_id}")
    table = Table(title=f"Fin de semana {weekend.id}", show_header=False, expand=False)
    table.add_column("Campo", style="bold")
    table.add_column("Valor", overflow="fold")
    rows = (
        ("ID", weekend.id),
        ("Etiqueta", weekend.label),
        ("Circuito", weekend.track),
        ("Origen", weekend.source),
        ("Enlace del juego", weekend.weekend_link_id),
        ("Enlace de temporada", weekend.season_link_id),
        ("Creado (UTC)", weekend.created_at_utc),
        ("Sesiones", len(store.list_weekend_sessions(weekend.id))),
    )
    for label, value in rows:
        table.add_row(label, _text(value))
    console.print(table)

    sessions = store.list_weekend_sessions(weekend.id)
    if not sessions:
        return 0
    session_table = Table(title="Sesiones del fin de semana", expand=False)
    session_table.add_column("ID", justify="right")
    session_table.add_column("Tipo")
    session_table.add_column("Enlace del juego")
    session_table.add_column("Origen")
    session_table.add_column("Huella")
    for session in sessions:
        fingerprint = session.fingerprint or "-"
        if len(fingerprint) > 12:
            fingerprint = fingerprint[:12] + "…"
        session_table.add_row(
            str(session.id),
            _text(session.session_type),
            _text(session.session_link_id),
            session.source,
            fingerprint,
        )
    console.print(session_table)
    return 0


def _weekends_link(store: SessionStore, args: argparse.Namespace, console: Console) -> int:
    weekend_id = store.link_sessions(args.session_ids, label=args.label)
    joined = ", ".join(str(session_id) for session_id in dict.fromkeys(args.session_ids))
    console.print(f"Sesiones {joined} enlazadas en el fin de semana {weekend_id}.")
    return 0


def _weekends_unlink(store: SessionStore, session_id: int, console: Console) -> int:
    weekend_id = store.unlink_session(session_id)
    if weekend_id is None:
        console.print(f"La sesión {session_id} no estaba enlazada a ningún fin de semana.")
        return 0
    console.print(f"Sesión {session_id} separada del fin de semana {weekend_id}.")
    return 0


def _quality_text(value: object) -> str:
    """Traduce la calidad de un valor del perfil a texto en español."""
    labels = {
        "ok": "ok",
        "low": "low (bajo)",
        "stale": "stale (obsoleto)",
        "insufficient": "insufficient (insuficiente)",
        "unavailable": "unavailable (no disponible)",
    }
    if not isinstance(value, str):
        return "-"
    return labels.get(value, value)


def _profile_number(value: object, digits: int = 3) -> str:
    """Representa un valor numérico opcional del perfil con sus decimales."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def _profile_value_text(entry: object, digits: int = 3) -> str:
    """Muestra el número de un valor del perfil y avisa si su calidad es baja."""
    if not isinstance(entry, dict):
        return "-"
    text = _profile_number(entry.get("value"), digits)
    quality = entry.get("quality")
    if text != "-" and quality not in (None, "ok"):
        return f"{text} ({_quality_text(quality)})"
    return text


def _cmd_weekend(args: argparse.Namespace, console: Console, err: Console) -> int:
    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        return _weekend_show(store, args.session, console, err)


def _weekend_show(
    store: SessionStore,
    session_id: int | None,
    console: Console,
    err: Console,
) -> int:
    session = None
    if session_id is None:
        for candidate in reversed(store.list_sessions()):
            if candidate.profile_json is not None:
                session = candidate
                break
        if session is None:
            console.print("No hay ninguna sesión con un perfil del fin de semana registrado.")
            return 0
    else:
        session = require_session(store, session_id)

    if session.profile_json is not None:
        payload = json.loads(session.profile_json)
        origin = "registrado al empezar la carrera"
    else:
        result = resolve_weekend_profile(
            store,
            session,
            load_config(None).metrics,
            exclude_session_id=session.id,
        )
        if result.error is not None:
            err.print(
                f"[red]Error:[/] no se pudo calcular el perfil del fin de semana: {result.error}"
            )
            return 2
        if result.profile is None:
            console.print(
                f"La sesión {session.id} no tiene perfil registrado ni un fin de semana "
                "enlazado del que calcularlo."
            )
            return 0
        payload = result.profile.to_dict()
        session = replace(session, profile_hash=result.profile.hash)
        origin = "calculado ahora (no registrado)"

    _render_weekend_profile(session, payload, origin, console)
    return 0


def _render_weekend_profile(
    session: SessionInfo,
    payload: object,
    origin: str,
    console: Console,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("El perfil del fin de semana no tiene el formato esperado.")
    pit_loss = payload.get("pit_loss_s")
    sessions = payload.get("sessions")
    table = Table(
        title=f"Perfil del fin de semana de la sesión {session.id}",
        show_header=False,
        expand=False,
    )
    table.add_column("Campo", style="bold")
    table.add_column("Valor", overflow="fold")
    rows = (
        ("Sesión", session.id),
        ("Circuito", session.track),
        ("Tipo de sesión", session.session_type),
        ("Fin de semana", session.weekend_id),
        ("Origen del perfil", origin),
        ("Esquema", payload.get("schema_version")),
        ("Calidad", _quality_text(payload.get("quality"))),
        ("Compuesto de referencia", payload.get("reference_compound")),
        ("Pérdida en boxes", _profile_value_text(pit_loss, 2)),
        ("Sesiones usadas", ", ".join(str(item) for item in sessions) if sessions else "-"),
        ("Huella", session.profile_hash),
    )
    for label, value in rows:
        table.add_row(label, _text(value))
    console.print(table)

    compounds = payload.get("compounds")
    if isinstance(compounds, list) and compounds:
        compound_table = Table(title="Compuestos", expand=False)
        compound_table.add_column("Compuesto")
        compound_table.add_column("Tandas", justify="right")
        compound_table.add_column("Vueltas", justify="right")
        compound_table.add_column("Limpias", justify="right")
        compound_table.add_column("Desgaste %/vuelta", justify="right")
        compound_table.add_column("Ritmo medio (s)", justify="right")
        compound_table.add_column("Delta (s)", justify="right")
        compound_table.add_column("Consumo (kg/vuelta)", justify="right")
        compound_table.add_column("Rango combustible (kg)", justify="right")
        compound_table.add_column("Temp. (°C)", justify="right")
        compound_table.add_column("Calidad")
        for entry in compounds:
            if not isinstance(entry, dict):
                continue
            fuel_range = entry.get("fuel_range_kg")
            if isinstance(fuel_range, list) and len(fuel_range) == 2:
                fuel_range_text = (
                    f"{_profile_number(fuel_range[0], 1)}–{_profile_number(fuel_range[1], 1)}"
                )
            else:
                fuel_range_text = "-"
            compound_table.add_row(
                _text(entry.get("compound")),
                str(entry.get("stints", 0)),
                str(entry.get("laps", 0)),
                str(entry.get("clean_laps", 0)),
                _profile_value_text(entry.get("wear_rate_pct_per_lap")),
                _profile_value_text(entry.get("pace_mean_s")),
                _profile_value_text(entry.get("pace_delta_vs_reference_s")),
                _profile_value_text(entry.get("fuel_consumption_kg_per_lap")),
                fuel_range_text,
                _profile_value_text(entry.get("track_temperature_c"), 1),
                _quality_text(entry.get("quality")),
            )
        console.print(compound_table)

    inventory = payload.get("inventory")
    if isinstance(inventory, list) and inventory:
        inventory_table = Table(title="Inventario de neumáticos", expand=False)
        inventory_table.add_column("Tipo")
        inventory_table.add_column("Juego", justify="right")
        inventory_table.add_column("Compuesto")
        inventory_table.add_column("Visual")
        inventory_table.add_column("Desgaste %", justify="right")
        inventory_table.add_column("Disponible")
        inventory_table.add_column("Vida", justify="right")
        inventory_table.add_column("Recomendada")
        inventory_table.add_column("Montado")
        for entry in inventory:
            if not isinstance(entry, dict):
                continue
            inventory_table.add_row(
                _text(entry.get("kind")),
                str(entry.get("set_index", 0)),
                _text(entry.get("actual_tyre_compound")),
                _text(entry.get("visual_tyre_compound")),
                _profile_number(entry.get("wear_pct"), 1),
                _yes_no(entry.get("available")),
                _profile_number(entry.get("usable_life_laps"), 1),
                _text(entry.get("recommended_session")),
                _yes_no(entry.get("fitted")),
            )
        console.print(inventory_table)


def _cmd_datasets(args: argparse.Namespace, console: Console, err: Console) -> int:
    manifest = load_manifest(args.manifest)
    if args.datasets_command == "list":
        return _datasets_list(manifest, console)
    scenarios = manifest.scenarios
    if args.id is not None:
        try:
            scenarios = (manifest.get(args.id),)
        except KeyError as exc:
            raise ValueError(f"No existe el escenario {args.id!r} en {args.manifest}") from exc
    return _datasets_verify(scenarios, console, err)


def _datasets_list(manifest: Manifest, console: Console) -> int:
    scenarios = manifest.scenarios
    table = Table(title=f"Escenarios de referencia ({len(scenarios)})")
    table.add_column("Escenario")
    table.add_column("Estado")
    table.add_column("Vuelta", justify="right")
    table.add_column("Coches", justify="right")
    table.add_column("Descripción")
    table.add_column("Etiquetas")
    for scenario in scenarios:
        state = "[green]disponible[/]" if scenario.available else "[yellow]no disponible[/]"
        table.add_row(
            scenario.id,
            state,
            str(scenario.expected.total_laps),
            str(scenario.expected.cars),
            scenario.description,
            ", ".join(scenario.tags),
        )
    console.print(table)
    missing = sum(1 for scenario in scenarios if not scenario.available)
    if missing:
        console.print(f"[yellow]{missing} escenario(s) sin archivo de captura.[/]")
    return 0


def _datasets_verify(
    scenarios: Sequence[Scenario],
    console: Console,
    err: Console,
) -> int:
    verified = 0
    unavailable = 0
    failures = 0
    for scenario in scenarios:
        result = verify_scenario(scenario)
        if not result.available:
            unavailable += 1
            console.print(f"[yellow]{scenario.id}: no disponible[/] ({result.problems[0]})")
        elif result.ok:
            verified += 1
            console.print(f"[green]{scenario.id}: correcto[/]")
        else:
            failures += 1
            err.print(f"[red]{scenario.id}: verificación fallida[/]")
            for problem in result.problems:
                err.print(f"  - {problem}")
    if failures:
        err.print(f"[red]{failures} escenario(s) han fallado.[/]")
        return 1
    if verified:
        console.print(f"{verified} escenario(s) verificado(s) correctamente.")
    if unavailable:
        console.print(f"{unavailable} escenario(s) no disponible(s); no se han verificado.")
    if not verified and not unavailable:
        console.print("No hay escenarios que verificar.")
    return 0


def _same_file(left: Path, right: Path) -> bool:
    try:
        if left.resolve() == right.resolve():
            return True
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _warn_offline_issues(offline: OfflineResult, err: Console) -> None:
    if offline.truncated:
        err.print("[yellow]Aviso:[/] La captura termina con un registro incompleto.")
    rejections = sum(offline.parser_rejections.values())
    if rejections:
        err.print(f"[yellow]Aviso:[/] {rejections} paquete(s) rechazado(s) por el parser.")
    if offline.packets_parsed == 0:
        err.print("[yellow]Aviso:[/] La captura no contiene paquetes válidos.")


def _write_stdout(text: str) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except OSError:
            reconfigure = None
    if reconfigure is None:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
            return
    sys.stdout.write(text)


def _cmd_trim(args: argparse.Namespace, console: Console, err: Console) -> int:
    packet_types = frozenset().union(*args.types) if args.types else None
    decimate: dict[int, int] = {}
    for packet_id, interval in args.decimate or ():
        if packet_id in decimate:
            raise ValueError(f"El ID {packet_id} aparece más de una vez en --decimate")
        decimate[packet_id] = interval
    result = trim_capture(
        args.input,
        args.output,
        packet_types=packet_types,
        decimate=decimate,
        laps=args.laps,
        anonymize=args.anonymize,
        keep_ai_names=args.keep_ai_names,
    )
    console.print(
        f"Captura recortada: {result.packets_written} de {result.packets_read} paquetes "
        f"guardados en {args.output}."
    )
    if result.dropped_for_anonymization:
        console.print(
            f"Paquetes LobbyInfo omitidos al anonimizar: {result.dropped_for_anonymization}."
        )
    if result.truncated:
        console.print("[yellow]La captura de entrada termina con un registro incompleto.[/]")
    return 0


class _StrategyAtCollector:
    """Conserva las entradas de la vuelta solicitada durante la reproducción offline."""

    def __init__(
        self,
        metrics_config: MetricsConfig,
        profiles_by_uid: dict[int, WeekendProfile],
        target_lap: int,
    ) -> None:
        self.metrics_engine = MetricsEngine(metrics_config)
        self.profiles_by_uid = profiles_by_uid
        self.target_lap = target_lap
        self.state = RaceState()
        self.target_inputs: MetricInputs | None = None
        self._current_lap: int | None = None
        self._is_race = False

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        state = self.state.snapshot()
        session = state.session
        self._is_race = session.session_type is not None and session.session_type.lower() in {
            "race",
            "race_2",
            "race_3",
        }
        profile = (
            self.profiles_by_uid.get(session.uid)
            if self._is_race and session.uid is not None
            else None
        )
        self.metrics_engine.set_profile(profile)
        self.metrics_engine.apply(packet, observed_at, state)
        player = state.player_car
        self._current_lap = player.current_lap if player is not None else state.current_lap
        if self._is_race and self._current_lap == self.target_lap:
            car_index = state.player_car_index
            self.target_inputs = MetricInputs(
                state=state,
                history=self.metrics_engine.history.snapshot(),
                config=self.metrics_engine.config,
                now=observed_at,
                car_index=car_index if car_index is not None else -1,
                profile=profile,
            )

    def should_stop(self) -> bool:
        return (
            self._is_race and self._current_lap is not None and self._current_lap >= self.target_lap
        )


def _cmd_strategy_at(args: argparse.Namespace, console: Console, err: Console) -> int:
    cfg = load_config(args.config)
    profiles = _profiles_for_capture(cfg, args.file, err)
    collector = _StrategyAtCollector(cfg.metrics, profiles, args.lap)
    offline = run_capture_offline(args.file, [collector], stop_when=collector.should_stop)
    _warn_offline_issues(offline, err)
    inputs = collector.target_inputs
    if inputs is None:
        raise ValueError(
            f"No se encontró la vuelta {args.lap} en una sesión de carrera de la captura."
        )

    evaluation = evaluate_once(
        inputs,
        cfg.strategy.for_track(inputs.state.session.track),
    )
    if args.json:
        _write_stdout(json.dumps(evaluation.to_dict(), ensure_ascii=False, indent=2) + "\n")
    else:
        recommendation = evaluation.recommendation
        console.print(
            f"Vuelta {evaluation.lap} · {recommendation.action.value} · "
            f"alternativa {recommendation.key}"
        )
        if recommendation.gain_vs_baseline_s is not None:
            console.print(
                f"Ganancia estimada: {recommendation.gain_vs_baseline_s:.2f} s "
                f"(confianza {recommendation.confidence:.0%})"
            )
        if inputs.profile is not None:
            console.print(f"Perfil: {inputs.profile.hash}")
        console.print(f"Calidad de datos: {evaluation.data_quality.value}")
    return 0


def _cmd_strategy(args: argparse.Namespace, console: Console, err: Console) -> int:
    if args.strategy_command == "at":
        return _cmd_strategy_at(args, console, err)

    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        session = require_session(store, args.session)
        assert session.id is not None
        evaluations = list(store.iter_strategy_evaluations(session.id))

        if args.strategy_command == "show":
            matching = (
                [item for item in evaluations if item.lap == args.lap]
                if args.lap is not None
                else evaluations
            )
            if not matching:
                if not evaluations:
                    console.print(f"La sesión {session.id} no tiene evaluaciones de estrategia.")
                    return 0
                err.print(
                    f"[red]Error:[/] la sesión {session.id} no tiene una evaluación "
                    f"para la vuelta {args.lap}."
                )
                return 2
            stored = matching[-1]
            evaluation = StrategyEvaluation.from_json(stored.evaluation_json)
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold")
            table.add_column(overflow="fold")
            table.add_row("Secuencia", Text(str(stored.seq)))
            table.add_row("Revisión", Text(str(evaluation.revision)))
            for label, value in render_strategy_rows(evaluation):
                table.add_row(label, Text(value))
            console.print(
                Panel(
                    table,
                    title=f"Estrategia · sesión {session.id} · vuelta {evaluation.lap}",
                    expand=False,
                )
            )
            return 0

        if args.strategy_command == "export":
            if args.out is not None:
                if _same_file(args.out, db):
                    raise ValueError("La salida no puede sobrescribir la base de datos.")
                if session.capture_path is not None and _same_file(
                    args.out, Path(session.capture_path)
                ):
                    raise ValueError("La salida no puede sobrescribir la captura asociada.")
            output = render_strategy_export(session.id, evaluations, args.format)
            if args.out is None:
                _write_stdout(output)
            else:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(output)
                console.print(f"Exportación de estrategia de la sesión {session.id} escrita.")
            return 0

        if not evaluations:
            console.print(f"La sesión {session.id} no tiene evaluaciones de estrategia.")
            return 0
        review = review_strategy_evaluations(evaluations, store.iter_laps(session.id))
        console.print(
            f"Evaluaciones utilizadas: {review.evaluations_used}/{review.evaluations_total} "
            "(última revisión por vuelta; solo vueltas válidas sin parada y con el mismo "
            "compuesto)."
        )
        if not review.horizons:
            console.print(
                "No hay comparaciones válidas. Las evaluaciones antiguas sin proyecciones "
                "vuelta a vuelta siguen siendo consultables, pero no pueden revisarse."
            )
            return 0
        table = Table(title=f"Error de proyección · sesión {session.id}", expand=False)
        table.add_column("Horizonte", justify="right")
        table.add_column("Muestras", justify="right")
        table.add_column("Error absoluto medio", justify="right")
        for item in review.horizons:
            table.add_row(
                f"{item.horizon_laps} vueltas",
                str(item.samples),
                f"{item.mean_absolute_error_s:.3f} s",
            )
        console.print(table)
        return 0


class _DetectedEventRow(Protocol):
    t_sim_s: float
    lap: int | None
    rule: str
    event_id: str
    phase: str
    priority: str
    category: str
    message_key: str
    facts_json: str
    material_key: str | None


class _GateDecisionRow(Protocol):
    t_sim_s: float
    event_id: str
    phase: str
    outcome: str
    reason: str
    alert_id: str | None
    interrupt: bool | None


class _EventSessionReader(Protocol):
    """Contrato de lectura E9, intencionadamente separado del SessionStore actual."""

    connection: sqlite3.Connection

    def iter_detected_events(self, session_id: int) -> Iterable[_DetectedEventRow]: ...

    def iter_gate_decisions(self, session_id: int) -> Iterable[_GateDecisionRow]: ...


def _event_session_rows(
    store: SessionStore, session_id: int
) -> tuple[Sequence[_DetectedEventRow], Sequence[_GateDecisionRow]]:
    require_session(store, session_id)
    reader = cast(_EventSessionReader, store)
    version_row = reader.connection.execute("PRAGMA user_version").fetchone()
    schema_version = 0 if version_row is None else int(version_row[0])
    if schema_version < _EVENTS_SCHEMA_VERSION:
        raise StoreError(
            "El historial de eventos requiere esquema "
            f"v{_EVENTS_SCHEMA_VERSION}; la base actual es v{schema_version}."
        )
    try:
        return (
            tuple(reader.iter_detected_events(session_id)),
            tuple(reader.iter_gate_decisions(session_id)),
        )
    except AttributeError as exc:
        raise StoreError(
            "La base de datos no ofrece todavía la API de lectura de eventos E9."
        ) from exc


def _cmd_events_show(args: argparse.Namespace, console: Console) -> int:
    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        detections, decisions = _event_session_rows(store, args.session)
    decision_map: dict[int, list[_GateDecisionRow]] = defaultdict(list)
    detection_indices: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, detection in enumerate(detections):
        detection_indices[(detection.event_id, detection.phase)].append(index)
    for decision in decisions:
        candidates = detection_indices.get((decision.event_id, decision.phase), [])
        if candidates:
            nearest = min(
                candidates,
                key=lambda index: (
                    abs(detections[index].t_sim_s - decision.t_sim_s),
                    detections[index].t_sim_s > decision.t_sim_s,
                    index,
                ),
            )
            decision_map[nearest].append(decision)
    table = Table(title=f"Eventos · sesión {args.session}", expand=False)
    for name in ("Tiempo", "Vuelta", "Regla", "Fase", "Prioridad", "Detección", "Compuerta"):
        table.add_column(name, overflow="fold")
    shown = 0
    for index, detection in enumerate(detections):
        matching = decision_map.get(index, [])
        if args.outcome is not None:
            matching = [item for item in matching if item.outcome == args.outcome]
            if not matching:
                continue
        if args.rule is not None and detection.rule != args.rule:
            continue
        facts_payload = json.loads(detection.facts_json)
        facts = cast(
            tuple[Fact, ...],
            tuple((str(name), value) for name, value in facts_payload.items()),
        )
        message = render_event_message(detection.message_key, facts)
        decision_text = (
            ", ".join(f"{item.outcome}/{item.reason}" for item in matching) or "sin decisión"
        )
        table.add_row(
            f"{detection.t_sim_s:.1f} s",
            "—" if detection.lap is None else str(detection.lap),
            detection.rule,
            detection.phase,
            detection.priority,
            message,
            decision_text,
        )
        shown += 1
    if not shown:
        console.print(f"La sesión {args.session} no tiene eventos que coincidan.")
    else:
        console.print(table)
    return 0


def _cmd_events_summary(args: argparse.Namespace, console: Console) -> int:
    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        detections, decisions = _event_session_rows(store, args.session)
    by_rule = Counter(item.rule for item in detections)
    by_outcome = Counter(item.outcome for item in decisions)
    by_reason = Counter(item.reason for item in decisions)
    detections_by_key: dict[tuple[str, str], list[_DetectedEventRow]] = defaultdict(list)
    for item in detections:
        detections_by_key[(item.event_id, item.phase)].append(item)
    alert_laps: Counter[int] = Counter()
    for item in decisions:
        if item.alert_id is None:
            continue
        candidates = detections_by_key.get((item.event_id, item.phase), [])
        if not candidates:
            continue
        nearest = min(candidates, key=lambda row: abs(row.t_sim_s - item.t_sim_s))
        if nearest.lap is not None:
            alert_laps[nearest.lap] += 1
    material_counts = Counter(
        (item.rule, item.material_key) for item in detections if item.material_key is not None
    )
    repeated = sum(count - 1 for count in material_counts.values() if count > 1)
    suppressed = sum(by_outcome.get(outcome, 0) for outcome in ("suppress", "defer", "cancel"))
    console.print(
        f"Sesión {args.session}: {len(detections)} detecciones, {len(decisions)} decisiones."
    )
    console.print(f"Avisos emitidos: {by_outcome.get('emit', 0) + by_outcome.get('update', 0)}")
    console.print(f"Decisiones suprimidas: {suppressed}; repeticiones de material: {repeated}")
    _print_counter_table(console, "Detecciones por regla", "Regla", by_rule)
    _print_counter_table(console, "Decisiones por resultado", "Resultado", by_outcome)
    _print_counter_table(console, "Decisiones por motivo", "Motivo", by_reason)
    _print_counter_table(console, "Avisos por vuelta", "Vuelta", alert_laps)
    return 0


def _print_counter_table(console: Console, title: str, label: str, counts: Counter[Any]) -> None:
    table = Table(title=title, expand=False)
    table.add_column(label)
    table.add_column("Cantidad", justify="right")
    for key, count in sorted(counts.items(), key=lambda item: str(item[0])):
        table.add_row(str(key), str(count))
    if counts:
        console.print(table)


class _EventsCaptureCollector:
    def __init__(
        self,
        cfg: AppConfig,
        from_lap: int | None,
        to_lap: int | None,
        profiles_by_uid: Mapping[int, WeekendProfile],
    ) -> None:
        self.cfg = cfg
        self.from_lap = from_lap
        self.to_lap = to_lap
        self.profiles_by_uid = profiles_by_uid
        self.race = RaceState()
        self.metrics = MetricsEngine(cfg.metrics)
        self.strategy = StrategyEngine(cfg.strategy, cfg.metrics)
        self.engine = EventEngine(cfg)
        self.timeline: list[tuple[float, int, str, str, str, str, str]] = []
        self._known_signals: dict[str, EventSignal] = {}
        self._last_session_uid: int | None = None
        self._last_lap: int | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.race.apply(packet, observed_at)
        state = self.race.snapshot()
        if state.session.uid != self._last_session_uid or (
            state.current_lap is not None
            and self._last_lap is not None
            and state.current_lap < self._last_lap
        ):
            self._known_signals.clear()
        self._last_session_uid = state.session.uid
        if state.current_lap is not None:
            self._last_lap = state.current_lap
        session = state.session
        profile = (
            self.profiles_by_uid.get(session.uid)
            if session.uid is not None
            and session.session_type is not None
            and session.session_type.lower() in {"race", "race_2", "race_3"}
            else None
        )
        self.metrics.set_profile(profile)
        self.metrics.apply(packet, observed_at, state)
        metrics = self.metrics.snapshot()
        self.strategy.apply(
            state,
            metrics,
            self.metrics.history.snapshot(),
            profile,
            observed_at,
        )
        self.engine.apply(state, metrics, self.strategy.snapshot(), observed_at)
        evaluation = self.engine.last_evaluation
        if evaluation is None:
            return
        self._append_evaluation(evaluation, state.current_lap or 0)

    def _append_evaluation(self, evaluation: EventEvaluation, current_lap: int) -> None:
        decisions_by_signal: dict[tuple[str, EventPhase], list[tuple[int, GateDecision]]] = (
            defaultdict(list)
        )
        for index, decision in enumerate(evaluation.decisions):
            decisions_by_signal[(decision.event_id, decision.phase)].append((index, decision))

        consumed_decisions: set[int] = set()
        for event_signal in evaluation.signals:
            self._known_signals[event_signal.event_id] = event_signal
            matching = decisions_by_signal.get((event_signal.event_id, event_signal.phase), [])
            consumed_decisions.update(index for index, _ in matching)
            if not self._lap_in_range(event_signal.lap):
                continue
            self.timeline.append(
                (
                    event_signal.observed_at,
                    event_signal.lap,
                    event_signal.rule,
                    event_signal.priority.value,
                    render_event_message(event_signal.message_key, event_signal.facts),
                    ", ".join(item.outcome.value for _, item in matching) or "sin decisión",
                    ", ".join(item.reason for _, item in matching) or "—",
                )
            )

        for index, decision in enumerate(evaluation.decisions):
            if index in consumed_decisions:
                continue
            event_signal = self._known_signals.get(decision.event_id)
            lap = event_signal.lap if event_signal is not None else current_lap
            if not self._lap_in_range(lap):
                continue
            self.timeline.append(
                (
                    decision.observed_at,
                    lap,
                    "—" if event_signal is None else event_signal.rule,
                    "—" if event_signal is None else event_signal.priority.value,
                    (
                        "—"
                        if event_signal is None
                        else render_event_message(event_signal.message_key, event_signal.facts)
                    ),
                    decision.outcome.value,
                    decision.reason,
                )
            )

    def _lap_in_range(self, lap: int) -> bool:
        return not (
            (self.from_lap is not None and lap < self.from_lap)
            or (self.to_lap is not None and lap > self.to_lap)
        )

    def should_stop(self) -> bool:
        if self.to_lap is None:
            return False
        state = self.race.snapshot()
        return state.current_lap is not None and state.current_lap > self.to_lap


def _cmd_events_simulate(args: argparse.Namespace, console: Console, err: Console) -> int:
    if args.from_lap is not None and args.to_lap is not None and args.from_lap > args.to_lap:
        raise ValueError("--from-lap no puede ser mayor que --to-lap.")
    cfg = load_config(args.config)
    profiles = _profiles_for_capture(cfg, args.file, err)
    collector = _EventsCaptureCollector(cfg, args.from_lap, args.to_lap, profiles)
    result = run_capture_offline(args.file, [collector], stop_when=collector.should_stop)
    _warn_offline_issues(result, err)
    table = Table(title="Simulación de eventos", expand=False)
    for name in ("Tiempo", "Vuelta", "Regla", "Prioridad", "Detección", "Resultado", "Motivo"):
        table.add_column(name, overflow="fold")
    for row in collector.timeline:
        table.add_row(f"{row[0]:.1f} s", str(row[1]), *row[2:])
    if collector.timeline:
        console.print(table)
    else:
        console.print("No se detectaron eventos en el rango solicitado.")
    return 0


def _cmd_pending_group(args: argparse.Namespace, console: Console, err: Console) -> int:
    """Grupo de L1 sin subcomandos; L9 y L10 los añaden."""
    err.print(f"El grupo «{args.command}» todavía no tiene subcomandos.")
    return 2


class _AgentSessionReader(Protocol):
    connection: sqlite3.Connection

    def iter_llm_calls(self, session_id: int) -> Iterable[object]: ...

    def iter_agent_messages(self, session_id: int) -> Iterable[object]: ...


_AGENT_SCHEMA_VERSION = 9
_EVENTS_SCHEMA_VERSION = 8
_MISSING = object()


def _agent_session_rows(
    store: SessionStore, session_id: int
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    require_session(store, session_id)
    reader = cast(_AgentSessionReader, store)
    try:
        version_row = reader.connection.execute("PRAGMA user_version").fetchone()
        schema_version = 0 if version_row is None else int(version_row[0])
        if schema_version < _AGENT_SCHEMA_VERSION:
            raise StoreError(
                AGENT_SCHEMA_ERROR.format(required=_AGENT_SCHEMA_VERSION, actual=schema_version)
            )
        return (
            tuple(reader.iter_llm_calls(session_id)),
            tuple(reader.iter_agent_messages(session_id)),
        )
    except AttributeError as exc:
        raise StoreError(AGENT_READER_API_ERROR) from exc


def _agent_field(row: object, *names: str, default: object = None) -> object:
    for name in names:
        if isinstance(row, Mapping):
            if name in row:
                return row[name]
        else:
            value = getattr(row, name, _MISSING)
            if value is not _MISSING:
                return value
    return default


def _agent_value_text(value: object, default: str = "—") -> str:
    if value is None:
        return default
    enum_value = getattr(value, "value", _MISSING)
    return str(value if enum_value is _MISSING else enum_value)


def _agent_call_id(call: object) -> str | None:
    value = _agent_field(call, "llm_call_id", "call_id", "id")
    if value is not None:
        return str(value)
    alert_id = _agent_field(call, "alert_id")
    if alert_id is not None:
        return f"{alert_id}:llm"
    seq = _agent_field(call, "seq")
    return None if seq is None else str(seq)


def _agent_show(
    session_id: int,
    calls: tuple[object, ...],
    messages: tuple[object, ...],
    source: str | None,
    outcome: str | None,
    console: Console,
) -> int:
    outcomes_by_call = {
        call_id: _agent_value_text(_agent_field(call, "outcome", "result"))
        for call in calls
        if (call_id := _agent_call_id(call)) is not None
    }
    table = Table(title=f"{AGENT_MESSAGES_TITLE} · sesión {session_id}", expand=False)
    for column in AGENT_MESSAGES_COLUMNS:
        table.add_column(column, overflow="fold")
    shown = 0
    for message in messages:
        message_source = _agent_value_text(_agent_field(message, "source", "origin"), "")
        if source is not None and message_source != source:
            continue
        call_id = _agent_field(message, "llm_call_id")
        if outcome is not None and (
            call_id is None or outcomes_by_call.get(str(call_id)) != outcome
        ):
            continue
        priority = _agent_value_text(_agent_field(message, "priority"), "")
        lap = _agent_field(message, "lap")
        emitted_at = _agent_field(message, "t_sim_s", "emitted_at")
        text = _agent_field(message, "text", "message")
        table.add_row(
            _agent_value_text(emitted_at),
            render_agent_priority(priority),
            _agent_value_text(lap),
            render_agent_source(message_source),
            Text("" if text is None else str(text)),
        )
        shown += 1
    if not shown:
        console.print(AGENT_EMPTY_MESSAGES.format(session_id=session_id))
    else:
        console.print(table)
    return 0


def _agent_summary(
    session_id: int, calls: tuple[object, ...], messages: tuple[object, ...], console: Console
) -> int:
    console.print(AGENT_SUMMARY_TITLE.format(session_id=session_id))
    sources = Counter(
        render_agent_source(
            _agent_value_text(_agent_field(message, "source", "origin"), AGENT_UNKNOWN_VALUE)
        )
        for message in messages
    )
    _print_counter_table(console, AGENT_SUMMARY_SOURCES_TITLE, AGENT_SUMMARY_SOURCE_COLUMN, sources)
    fallbacks = Counter(
        _agent_value_text(_agent_field(call, "fallback_reason", "reason"), AGENT_UNKNOWN_VALUE)
        for call in calls
        if _agent_value_text(_agent_field(call, "outcome", "result")) == CallOutcome.FALLBACK.value
        and _agent_field(call, "fallback_reason", "reason") is not None
    )
    _print_counter_table(
        console, AGENT_SUMMARY_FALLBACKS_TITLE, AGENT_SUMMARY_CAUSE_COLUMN, fallbacks
    )
    validation_errors = Counter(code for call in calls for code in _agent_validation_codes(call))
    _print_counter_table(
        console, AGENT_SUMMARY_VALIDATION_TITLE, AGENT_SUMMARY_CODE_COLUMN, validation_errors
    )

    latencies = sorted(
        value
        for call in calls
        if isinstance(value := _agent_field(call, "total_s", "latency_s"), (int, float))
        and not isinstance(value, bool)
    )
    latency_table = Table(title=AGENT_SUMMARY_LATENCY_TITLE, expand=False)
    latency_table.add_column(AGENT_SUMMARY_PERCENTILE_COLUMN)
    latency_table.add_column(AGENT_SUMMARY_SECONDS_COLUMN, justify="right")
    if latencies:
        latency_table.add_row("p50", f"{_percentile(latencies, 0.50):.3f}")
        latency_table.add_row("p95", f"{_percentile(latencies, 0.95):.3f}")
    else:
        latency_table.add_row("p50 / p95", "—")
    console.print(latency_table)
    return 0


def _agent_validation_codes(call: object) -> tuple[str, ...]:
    value = _agent_field(
        call,
        "validation_errors",
        "validation_errors_json",
        "validation_codes",
        "validation_json",
        "errors",
    )
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return (value,)
    if isinstance(value, Mapping):
        value = _agent_field(value, "errors", "codes", default=())
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(_agent_value_text(item, "") for item in value)
    return ()


def _percentile(values: Sequence[int | float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return float(values[lower]) + (float(values[upper]) - float(values[lower])) * (position - lower)


def _agent_row_dict(row: object) -> dict[str, object]:
    if isinstance(row, Mapping):
        return _redact_agent_row(cast(Mapping[str, object], row))
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return _redact_agent_row(cast(Mapping[str, object], result))
    if is_dataclass(row) and not isinstance(row, type):
        values = {field.name: getattr(row, field.name) for field in fields(row)}
        return _redact_agent_row(values)
    raise ValueError(f"Tipo de fila de agente no exportable: {type(row).__name__}")


def _redact_agent_row(values: Mapping[str, object]) -> dict[str, object]:
    forbidden = {"api_key", "authorization", "headers"}
    return {
        key: _redact_agent_value(value)
        for key, value in values.items()
        if key.lower() not in forbidden
    }


def _redact_agent_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _redact_agent_row(cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple)):
        return [_redact_agent_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _agent_row_dict(value)
    return value


def _agent_json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    enum_value = getattr(value, "value", _MISSING)
    if enum_value is not _MISSING:
        return enum_value
    if is_dataclass(value) and not isinstance(value, type):
        return _agent_row_dict(value)
    return str(value)


def _render_agent_export(
    session_id: int, calls: tuple[object, ...], messages: tuple[object, ...], export_format: str
) -> str:
    records = [{"record_type": "llm_call", **_agent_row_dict(call)} for call in calls] + [
        {"record_type": "agent_message", **_agent_row_dict(message)} for message in messages
    ]
    if export_format == "json":
        payload = {
            "schema_version": AGENT_EXPORT_SCHEMA_VERSION,
            "session_id": session_id,
            "llm_calls": [_agent_row_dict(call) for call in calls],
            "agent_messages": [_agent_row_dict(message) for message in messages],
        }
        return (
            json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True, default=_agent_json_default
            )
            + "\n"
        )
    if export_format == "jsonl":
        return "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=_agent_json_default) + "\n"
            for row in records
        )
    fieldnames = [
        "record_type",
        *sorted({key for row in records for key in row if key != "record_type"}),
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in records:
        writer.writerow(
            {
                key: (
                    json.dumps(value, ensure_ascii=False, default=_agent_json_default)
                    if isinstance(value, (dict, list, tuple, set, frozenset))
                    else ""
                    if value is None
                    else str(value)
                )
                for key, value in row.items()
            }
        )
    return output.getvalue()


def _cmd_agent(args: argparse.Namespace, console: Console) -> int:
    db = _sessions_database(args)
    with SessionStore.open(db, readonly=True) as store:
        session = require_session(store, args.session)
        calls, messages = _agent_session_rows(store, args.session)
    if args.agent_command == "show":
        return _agent_show(args.session, calls, messages, args.source, args.outcome, console)
    if args.agent_command == "summary":
        return _agent_summary(args.session, calls, messages, console)
    if args.out is not None:
        if _same_file(args.out, db):
            raise ValueError(AGENT_EXPORT_OVERWRITES_DB)
        capture_path = getattr(session, "capture_path", None)
        if capture_path is not None and _same_file(args.out, Path(capture_path)):
            raise ValueError(AGENT_EXPORT_OVERWRITES_CAPTURE)
    output = _render_agent_export(args.session, calls, messages, args.format)
    if args.out is None:
        _write_stdout(output)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as handle:
            handle.write(output)
        console.print(AGENT_EXPORT_WRITTEN.format(session_id=args.session, path=args.out))
    return 0


def _cmd_events(args: argparse.Namespace, console: Console, err: Console) -> int:
    if args.events_command is None:
        return 0
    if args.events_command == "show":
        return _cmd_events_show(args, console)
    if args.events_command == "summary":
        return _cmd_events_summary(args, console)
    return _cmd_events_simulate(args, console, err)


def _cmd_llm(args: argparse.Namespace, console: Console) -> int:
    if args.llm_command == "eval":
        return _cmd_llm_eval(args, console)
    json_path: Path | None = getattr(args, "json", None)
    if json_path is not None and args.config is not None and _same_file(json_path, args.config):
        raise ValueError("El informe no puede sobrescribir la configuración.")
    base_config: LLMConfig = load_config(args.config).llm
    config = diagnostic_config(base_config, endpoint=args.endpoint, model=args.model)
    client = OpenAICompatibleClient(config)
    if args.llm_command == "check":
        report = run_check(client, config)
        console.print(report_json(report), markup=False, end="")
        return 0 if report["ok"] else 1

    report = run_benchmark(
        client,
        config,
        runs=args.runs,
        warmup=args.warmup,
        note=args.note,
    )
    output = report_json(report)
    if json_path is not None:
        with json_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(output)
    console.print(output, markup=False, end="")
    return 0


def _cmd_llm_eval(args: argparse.Namespace, console: Console) -> int:
    out_path: Path | None = args.out
    app_config = load_config(args.config)
    database = args.db if args.db is not None else app_config.recording.database
    if out_path is not None:
        if _same_file(out_path, database):
            raise ValueError(LLM_EVAL_OVERWRITES_DB)
        if args.config is not None and _same_file(out_path, args.config):
            raise ValueError(LLM_EVAL_OVERWRITES_CONFIG)
        if args.dataset is not None and out_path.resolve().is_relative_to(args.dataset.resolve()):
            raise ValueError(LLM_EVAL_OVERWRITES_DATASET)
    skipped: tuple[str, ...] = ()
    if args.dataset is not None:
        cases = load_cases(args.dataset)
    else:
        with SessionStore.open(database, readonly=True) as store:
            session = require_session(store, args.from_session)
            calls, _messages = _agent_session_rows(store, args.from_session)
            if out_path is not None:
                capture_path = getattr(session, "capture_path", None)
                if capture_path is not None and _same_file(out_path, Path(capture_path)):
                    raise ValueError(LLM_EVAL_OVERWRITES_CAPTURE)
        recovered = cases_from_calls(calls)
        cases = recovered.cases
        skipped = recovered.skipped
    if skipped:
        print(LLM_EVAL_SKIPPED_CALLS.format(calls=", ".join(skipped)), file=sys.stderr)
    if not cases:
        raise ValueError("El conjunto no contiene casos evaluables.")

    config: LLMConfig = app_config.llm
    if args.model is not None:
        config = diagnostic_config(config, endpoint=args.endpoint, model=args.model)
        client = OpenAICompatibleClient(config)
    else:
        client = replay_client(cases)
        if client is None:
            raise ValueError("El conjunto no contiene respuestas grabadas; indica --model.")
    output = report_json(run_evaluation(client, cases, config, agent=app_config.agent))
    if out_path is not None:
        with out_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(output)
    _write_stdout(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    err = Console(stderr=True)
    handlers = {
        "run": _cmd_run,
        "replay": _cmd_replay,
        "play": _cmd_play,
        "verify": _cmd_verify,
        "inspect": _cmd_inspect,
        "metrics": _cmd_metrics,
        "trim": _cmd_trim,
        "ingest": _cmd_ingest,
        "datasets": _cmd_datasets,
        "sessions": _cmd_sessions,
        "weekends": _cmd_weekends,
        "weekend": _cmd_weekend,
        "strategy": _cmd_strategy,
        "events": _cmd_events,
        "agent": lambda args, console, _err: _cmd_agent(args, console),
        "llm": lambda args, console, _err: _cmd_llm(args, console),
        "ask": _cmd_pending_group,
        "queries": _cmd_pending_group,
    }
    try:
        return handlers[args.command](args, console, err)
    except (ConfigError, CaptureError, StoreError, ValueError) as exc:
        err.print(f"[red]Error:[/] {exc}")
        return 2
    except OSError as exc:
        err.print(f"[red]Error:[/] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
