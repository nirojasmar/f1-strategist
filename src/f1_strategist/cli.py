"""Línea de comandos: `run` (captura), `replay` (reemisión UDP) e `inspect`."""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .capture import CaptureError
from .config import ConfigError, apply_overrides, load_config
from .console import format_bytes, run_console
from .metrics.export import EXPORT_FORMATS, render_metrics_export, run_metrics_capture
from .packets import packet_name
from .receiver import ReceiverError
from .replay import replay_capture
from .session import CaptureSession
from .summary import summarize
from .trim import trim_capture


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

    replay = sub.add_parser("replay", help="reenvía una captura por UDP (sin abrir el juego)")
    replay.add_argument("file", type=Path)
    replay.add_argument("--host", default="127.0.0.1", help="IP del PC que ejecuta la aplicación")
    replay.add_argument("--port", type=int, default=20777)
    replay.add_argument("--speed", type=float, default=1.0, help="velocidad (0 = sin pausas)")
    replay.add_argument("--loop", action="store_true", help="repetir hasta Ctrl+C")

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
    cfg = load_config(args.config)
    result = run_metrics_capture(args.file, cfg.metrics)
    output = render_metrics_export(result.snapshot, result.history, export_format)
    if args.out is None:
        sys.stdout.write(output)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        console.print(f"Métricas exportadas en {args.out}")
    return 0


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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    err = Console(stderr=True)
    handlers = {
        "run": _cmd_run,
        "replay": _cmd_replay,
        "inspect": _cmd_inspect,
        "metrics": _cmd_metrics,
        "trim": _cmd_trim,
    }
    try:
        return handlers[args.command](args, console, err)
    except (ConfigError, CaptureError, ValueError) as exc:
        err.print(f"[red]Error:[/] {exc}")
        return 2
    except OSError as exc:
        err.print(f"[red]Error:[/] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
