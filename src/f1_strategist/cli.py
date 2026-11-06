"""Línea de comandos: captura, reproducción, inspección y catálogo de escenarios."""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import __version__
from .capture import CaptureError
from .config import AppConfig, ConfigError, apply_overrides, load_config
from .console import format_bytes, render, run_console
from .datasets import DEFAULT_MANIFEST, Manifest, Scenario, load_manifest, verify_scenario
from .fingerprint import fingerprint_capture
from .metrics.export import EXPORT_FORMATS, render_metrics_export, run_metrics_capture
from .offline import OfflineResult
from .packets import packet_name
from .playback import PlaybackSession
from .receiver import ReceiverError
from .replay import replay_capture
from .session import CaptureSession
from .summary import summarize
from .trim import trim_capture

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


class _KeyReader:
    """Lectura de teclas sin bloquear; se desactiva si stdin no es un terminal."""

    def __init__(self) -> None:
        self._enabled = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._windows = os.name == "nt"
        self._saved_attributes: list[object] | None = None
        if not self._enabled:
            return
        if not self._windows:
            import termios  # noqa: PLC0415
            import tty  # noqa: PLC0415

            fd = sys.stdin.fileno()
            self._saved_attributes = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]
            tty.setcbreak(fd)  # type: ignore[reportAttributeAccessIssue]

    def read(self) -> str | None:
        if not self._enabled:
            return None
        if self._windows:
            import msvcrt  # noqa: PLC0415

            if not msvcrt.kbhit():
                return None
            key = msvcrt.getwch()
            return key if key != "\x03" else "q"
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        return sys.stdin.read(1)

    def close(self) -> None:
        if self._saved_attributes is None:
            return
        import termios  # noqa: PLC0415

        termios.tcsetattr(  # type: ignore[reportAttributeAccessIssue]
            sys.stdin.fileno(),
            termios.TCSADRAIN,  # type: ignore[reportAttributeAccessIssue]
            self._saved_attributes,
        )
        self._saved_attributes = None


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
    return Group(render(cfg, session), Text(help_text, style="dim"))


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
    session = PlaybackSession(
        args.file,
        cfg.metrics,
        loop=args.loop,
        speed=args.speed,
        start_lap=args.start_lap,
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
    first = None
    for index in range(args.runs):
        result = fingerprint_capture(args.file, cfg)
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
        return left.resolve() == right.resolve()
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
        "datasets": _cmd_datasets,
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
