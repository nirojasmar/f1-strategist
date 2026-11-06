"""Reproductor interno controlable de capturas.

Reproduce una captura a través del mismo pipeline que la sesión en vivo
(``RaceState`` + ``MetricsEngine``) en un hilo propio, sin red. La velocidad
solo escala el tiempo real de espera: ``observed_at`` es siempre el tiempo
original de la captura, para que el resultado sea determinista.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from .capture import CaptureReader
from .config import MetricsConfig
from .metrics.engine import MetricsEngine, MetricsSnapshot
from .metrics.history import LapHistorySnapshot
from .packets import parse_header
from .parser import PacketParseError, is_ignored_by_design, parse_packet
from .race_state import RaceState, RaceStateSnapshot
from .receiver import Stats, StatsSnapshot

_STOP = "stop"
_RESTART = "restart"
_EOF = "eof"
_POLL_INTERVAL_S = 0.1


class PlaybackSession:
    """Reproduce una captura con pausa, velocidad, bucle y salto a una vuelta."""

    def __init__(
        self,
        path: Path,
        config: MetricsConfig | None = None,
        *,
        loop: bool = False,
        speed: float = 1.0,
        start_lap: int | None = None,
    ) -> None:
        if start_lap is not None and start_lap < 1:
            raise ValueError("start_lap debe ser positivo")
        self.path = Path(path)
        self._config = config
        self._loop = loop
        self._speed = _validate_speed(speed)
        self._seek_lap = start_lap
        self._pending_seek: int | None = None
        self._condition = threading.Condition()
        self._paused = False
        self._stop = False
        self._finished = False
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._runs_completed = 0
        self._race_state = RaceState()
        self._metrics_engine = MetricsEngine(config)
        self._stats = Stats()
        # Valida cabecera y metadatos ahora para que los errores sean inmediatos.
        CaptureReader(self.path)

    # ------------------------------------------------------------------
    # Controles
    # ------------------------------------------------------------------

    def play(self) -> None:
        """Arranca la reproducción; si estaba en pausa, la reanuda."""
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                self._paused = False
                self._condition.notify_all()
                return
            self._stop = False
            self._paused = False
            self._finished = False
            self._error = None
            self._pending_seek = self._seek_lap
            self._thread = threading.Thread(target=self._run, name="playback", daemon=True)
            thread = self._thread
        thread.start()

    def pause(self) -> None:
        with self._condition:
            self._paused = True
            self._condition.notify_all()

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def set_speed(self, speed: float) -> None:
        with self._condition:
            self._speed = _validate_speed(speed)
            self._condition.notify_all()

    def seek_to_lap(self, lap_number: int) -> None:
        """Reinicia y avanza sin esperas hasta el inicio de la vuelta indicada."""
        if lap_number < 1:
            raise ValueError("lap_number debe ser positivo")
        with self._condition:
            self._seek_lap = lap_number
            self._pending_seek = lap_number
            self._condition.notify_all()

    def stop(self) -> None:
        """Detiene la reproducción y espera a que el hilo termine."""
        with self._condition:
            self._stop = True
            self._paused = False
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def wait(self, timeout: float | None = None) -> bool:
        """Espera a que termine la reproducción; devuelve si el hilo acabó."""
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> PlaybackSession:
        self.play()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Estado y puerto de sesión
    # ------------------------------------------------------------------

    @property
    def speed(self) -> float:
        with self._condition:
            return self._speed

    @property
    def playing(self) -> bool:
        with self._condition:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and not self._paused
                and not self._finished
                and not self._stop
            )

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    @property
    def finished(self) -> bool:
        with self._condition:
            return self._finished

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def runs_completed(self) -> int:
        with self._condition:
            return self._runs_completed

    @property
    def bound_port(self) -> int:
        return 0

    @property
    def receiver_error(self) -> str | None:
        return None

    @property
    def capture_path(self) -> Path | None:
        return None

    def snapshot(self) -> StatsSnapshot:
        return self._stats.snapshot()

    def race_snapshot(self) -> RaceStateSnapshot:
        return self._race_state.snapshot()

    def metrics_snapshot(self) -> MetricsSnapshot:
        return self._metrics_engine.snapshot()

    def history_snapshot(self) -> LapHistorySnapshot:
        """Histórico de vueltas procesado hasta el momento."""
        return self._metrics_engine.history.snapshot()

    # ------------------------------------------------------------------
    # Hilo de reproducción
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._pending_seek is not None:
                        self._seek_lap = self._pending_seek
                    self._pending_seek = None
                    seek_lap = self._seek_lap
                self._reset_pipeline()
                outcome = self._play_once(seek_lap)
                if outcome == _STOP:
                    return
                if outcome == _RESTART:
                    continue
                with self._condition:
                    self._runs_completed += 1
                    if not self._loop:
                        self._paused = False
                        self._finished = True
                        self._condition.notify_all()
                        return
        except BaseException as exc:  # pragma: no cover - se prueba por API
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._paused = False
                self._finished = True
                self._condition.notify_all()

    def _reset_pipeline(self) -> None:
        self._race_state = RaceState()
        self._metrics_engine = MetricsEngine(self._config)
        self._stats = Stats()

    def _play_once(self, seek_lap: int | None) -> str:
        reader = CaptureReader(self.path)
        first_ns: int | None = None
        last_observed = 0.0
        seeking = seek_lap is not None
        last_state: RaceStateSnapshot | None = None
        last_observed_at: float | None = None

        for captured in reader:
            if first_ns is None:
                first_ns = captured.recv_time_ns
            elapsed = (captured.recv_time_ns - first_ns) / 1_000_000_000
            observed_at = max(last_observed, elapsed)
            gap = observed_at - last_observed

            control = self._check_control() if seeking else self._wait_gap(gap)
            if control is not None:
                return control
            last_observed = observed_at

            self._stats.record_received(captured.source_ip, len(captured.payload))
            header = parse_header(captured.payload)
            self._stats.record_header(header)
            if header is not None and is_ignored_by_design(header):
                self._stats.record_ignored_packet(header.packet_id)
                continue
            try:
                packet = parse_packet(captured.payload)
            except PacketParseError as exc:
                self._stats.record_parser_rejection(exc.issue)
                continue

            self._race_state.apply(packet, observed_at)
            state = self._race_state.snapshot()
            self._metrics_engine.apply(packet, observed_at, state)
            last_state = state
            last_observed_at = observed_at

            if seeking and state.current_lap is not None and state.current_lap >= (seek_lap or 1):
                seeking = False

        if last_state is not None and last_observed_at is not None:
            self._metrics_engine.finalize(last_state, last_observed_at)
        return _EOF

    def _check_control(self) -> str | None:
        with self._condition:
            while True:
                if self._stop:
                    return _STOP
                if self._pending_seek is not None:
                    return _RESTART
                if not self._paused:
                    return None
                self._condition.wait()

    def _wait_gap(self, gap_s: float) -> str | None:
        if gap_s <= 0:
            return self._check_control()
        wall_start = time.monotonic()
        while True:
            with self._condition:
                if self._stop:
                    return _STOP
                if self._pending_seek is not None:
                    return _RESTART
                if self._paused:
                    paused_at = time.monotonic()
                    while self._paused and not self._stop and self._pending_seek is None:
                        self._condition.wait()
                    wall_start += time.monotonic() - paused_at
                    continue
                speed = self._speed
                if speed == 0:
                    return None
                remaining = gap_s / speed - (time.monotonic() - wall_start)
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=min(remaining, _POLL_INTERVAL_S))


def _validate_speed(speed: float) -> float:
    if not math.isfinite(speed) or speed < 0:
        raise ValueError("speed debe ser un número finito no negativo")
    return speed
