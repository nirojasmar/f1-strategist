"""Sesión de captura: receptor -> cola acotada -> consumidor (registro + estadísticas)."""

from __future__ import annotations

import logging
import platform
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .capture import CapturedPacket, CaptureWriter
from .config import AppConfig
from .fingerprint import build_run_info, file_sha256
from .messages import DiagnosticMessage, message
from .metrics.engine import MetricsEngine, MetricsSnapshot
from .packets import PacketHeader, parse_header
from .parser import PacketParseError, is_ignored_by_design, parse_packet
from .race_state import RaceState, RaceStateSnapshot
from .receiver import Stats, StatsSnapshot, UdpReceiver
from .recorder import _RACE_SESSION_TYPES, SessionRecorder, serialize_config
from .store import SessionInfo, SessionStore
from .weekend_profile import WeekendProfile
from .weekend_provider import resolve_weekend_profile

_LOGGER = logging.getLogger(__name__)


class CaptureSession:
    def __init__(
        self,
        config: AppConfig,
        queue_size: int = 20000,
        *,
        metrics_engine: MetricsEngine | None = None,
    ) -> None:
        self.config = config
        self.stats = Stats()
        self._race_state = RaceState()
        self._metrics_engine = metrics_engine or MetricsEngine(config.metrics)
        self._queue: queue.Queue[CapturedPacket] = queue.Queue(maxsize=queue_size)
        self._receiver = UdpReceiver(config.telemetry, self._queue, self.stats)
        self._writer: CaptureWriter | None = None
        self._consumer: threading.Thread | None = None
        self._stop_consumer = threading.Event()
        self.capture_path: Path | None = None
        self._store: SessionStore | None = None
        self._recorder: SessionRecorder | None = None
        self._reported_recorder_error: Exception | None = None
        self._reported_dropped_batches = 0
        self._last_observed_at: float | None = None
        self._last_header: PacketHeader | None = None
        self._profiled_uids: set[int] = set()
        self._weekend_profile: WeekendProfile | None = None

    @property
    def weekend_profile(self) -> WeekendProfile | None:
        """Perfil del fin de semana con el que arrancó la carrera, si lo hay."""
        return self._weekend_profile

    @property
    def bound_port(self) -> int:
        return self._receiver.bound_port

    @property
    def receiver_error(self) -> str | None:
        return self._receiver.error

    def start(self) -> None:
        # El socket se abre primero: si falla no se crea ningún archivo de captura.
        self._receiver.start()
        if self.config.capture.enabled:
            name = datetime.now().strftime("session-%Y%m%d-%H%M%S.f1cap")
            self.capture_path = self.config.capture.directory / name
            tele = self.config.telemetry
            self._writer = CaptureWriter(
                self.capture_path,
                metadata={
                    "tool": "f1-strategist",
                    "tool_version": __version__,
                    "platform": platform.platform(),
                    "listen_address": tele.listen_address,
                    "listen_port": self._receiver.bound_port,
                    "allowed_sources": tele.allowed_sources,
                },
            )
        if self.config.recording.enabled:
            self._start_recorder()
        self._consumer = threading.Thread(
            target=self._consume, name="capture-consumer", daemon=True
        )
        self._consumer.start()

    def stop(self) -> None:
        self._receiver.stop()
        self._stop_consumer.set()
        if self._consumer is not None:
            self._consumer.join()
        capture_sha256: str | None = None
        writer = self._writer
        if writer is not None:
            try:
                writer.close()
                self._writer = None
                if self.capture_path is not None and self.capture_path.exists():
                    capture_sha256 = file_sha256(self.capture_path)
            except Exception as exc:
                _LOGGER.exception("Unable to close or hash the capture")
                self.stats.set_capture_error(message("capture_flush_error", error=str(exc)))
                self._writer = None
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.stop(
                    final_metrics=self._metrics_engine.snapshot(),
                    capture_sha256=capture_sha256,
                )
            except Exception as exc:
                _LOGGER.exception("Unable to stop the session recorder")
                self.stats.record_consumer_error(message("recorder_error", error=str(exc)))
            finally:
                self._check_recorder_state()
                self._recorder = None
        store = self._store
        if store is not None:
            try:
                store.close()
            except Exception as exc:
                _LOGGER.exception("Unable to close the session store")
                self.stats.record_consumer_error(message("recorder_error", error=str(exc)))
            finally:
                self._store = None
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:
                _LOGGER.exception("Unable to close capture")
                self.stats.set_capture_error(message("capture_close_error", error=str(exc)))

    def snapshot(self) -> StatsSnapshot:
        return self.stats.snapshot()

    def _start_recorder(self) -> None:
        """Abre el almacén y arranca el grabador; un fallo no detiene la captura."""
        try:
            store = SessionStore.open(self.config.recording.database)
            try:
                recorder = SessionRecorder(
                    store,
                    self.config.recording,
                    run_info=build_run_info(self.config),
                    capture_path=self.capture_path,
                    config_json=serialize_config(self.config),
                    metrics_config=self.config.metrics,
                )
            except BaseException:
                store.close()
                raise
        except Exception as exc:
            _LOGGER.exception("Unable to start the session recorder")
            self.stats.record_consumer_error(message("recorder_error", error=str(exc)))
            return
        self._store = store
        self._recorder = recorder

    def _record_error(self, issue: DiagnosticMessage) -> None:
        """Registra un error en las estadísticas y, si existe, en el grabador."""
        self.stats.record_consumer_error(issue)
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.record_error(issue)
            except Exception:
                _LOGGER.exception("Unable to record a consumer error")

    def _check_recorder_state(self) -> None:
        """Propaga una sola vez los fallos y las pérdidas del grabador."""
        recorder = self._recorder
        if recorder is None:
            return
        error = recorder.error
        if error is not None and error is not self._reported_recorder_error:
            self._reported_recorder_error = error
            self.stats.record_consumer_error(message("recorder_error", error=str(error)))
        dropped = recorder.dropped_batches
        if dropped > self._reported_dropped_batches:
            self._reported_dropped_batches = dropped
            self.stats.record_consumer_error(message("recorder_dropped", count=dropped))

    def _prepare_weekend_profile(self, state: RaceStateSnapshot) -> None:
        """Resuelve una sola vez el perfil del fin de semana al empezar una carrera.

        Sin fin de semana enlazado no hay perfil y la captura continúa. Un fallo
        de lectura se registra como diagnóstico, nunca detiene el consumidor.
        """
        session = state.session
        uid = session.uid
        if uid is None or uid in self._profiled_uids:
            return
        if session.session_type is None or session.session_type.lower() not in _RACE_SESSION_TYPES:
            return
        self._profiled_uids.add(uid)
        store = self._store
        recorder = self._recorder
        if store is None or recorder is None:
            return
        try:
            info = SessionInfo(
                session_uid=uid,
                source="live",
                started_at_utc=datetime.now(UTC).isoformat(),
                track=session.track,
                session_type=session.session_type,
                season_link_id=session.season_link_id,
                weekend_link_id=session.weekend_link_id,
                session_link_id=session.session_link_id,
            )
            result = resolve_weekend_profile(store, info, self.config.metrics)
        except Exception as exc:
            _LOGGER.exception("Unable to prepare the weekend profile")
            self._record_error(message("weekend_profile_error", error=str(exc)))
            return
        if result.error is not None:
            self._record_error(message("weekend_profile_error", error=result.error))
        profile = result.profile
        if profile is None:
            return
        self._weekend_profile = profile
        try:
            recorder.set_weekend_profile(uid, profile.to_json(), profile.hash)
        except Exception as exc:
            _LOGGER.exception("Unable to register the weekend profile")
            self._record_error(message("weekend_profile_error", error=str(exc)))

    def race_snapshot(self) -> RaceStateSnapshot:
        return self._race_state.snapshot()

    def metrics_snapshot(self) -> MetricsSnapshot:
        return self._metrics_engine.snapshot()

    def _consume(self) -> None:
        while not (self._stop_consumer.is_set() and self._queue.empty()):
            try:
                packet = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            self._check_recorder_state()
            try:
                header = parse_header(packet.payload)
                self.stats.record_header(header)
            except Exception as exc:
                _LOGGER.exception("Unexpected error while recording packet header")
                self._record_error(message("consumer_header_error", error=str(exc)))
                header = None

            self._write_packet(packet)

            if header is not None and is_ignored_by_design(header):
                self.stats.record_ignored_packet(header.packet_id)
                continue
            try:
                parsed_packet = parse_packet(packet.payload)
            except PacketParseError as exc:
                self.stats.record_parser_rejection(exc.issue)
            except Exception as exc:
                _LOGGER.exception("Unexpected error while parsing UDP packet")
                self._record_error(message("consumer_parse_error", error=str(exc)))
            else:
                try:
                    observed_at = packet.recv_monotonic
                    if observed_at is None:
                        observed_at = time.monotonic()
                    previous_state = self._race_state.snapshot()
                    self._race_state.apply(parsed_packet, observed_at)
                    state_snapshot = self._race_state.snapshot()
                except Exception as exc:
                    _LOGGER.exception("Unexpected error while updating race state")
                    self._record_error(message("race_state_update_error", error=str(exc)))
                else:
                    try:
                        if (
                            previous_state.session.uid is not None
                            and state_snapshot.session.uid != previous_state.session.uid
                            and self._last_observed_at is not None
                        ):
                            self._metrics_engine.finalize(previous_state, self._last_observed_at)
                            recorder = self._recorder
                            if recorder is not None and self._last_header is not None:
                                recorder.end_session(
                                    self._last_header.session_time,
                                    self._last_observed_at,
                                    previous_state,
                                    self._metrics_engine.snapshot(),
                                    self._metrics_engine.history,
                                    header=self._last_header,
                                )
                        self._metrics_engine.apply(parsed_packet, observed_at, state_snapshot)
                    except Exception as exc:
                        _LOGGER.exception("Unexpected error while updating metrics")
                        self._record_error(message("metrics_update_error", error=str(exc)))
                    else:
                        recorder = self._recorder
                        if recorder is not None:
                            self._prepare_weekend_profile(state_snapshot)
                            try:
                                recorder.observe(
                                    parsed_packet.header.session_time,
                                    observed_at,
                                    state_snapshot,
                                    self._metrics_engine.snapshot(),
                                    self._metrics_engine.history,
                                    header=parsed_packet.header,
                                )
                            except Exception as exc:
                                _LOGGER.exception("Unexpected error while recording the session")
                                self._record_error(message("recorder_error", error=str(exc)))
                        self._last_observed_at = observed_at
                        self._last_header = parsed_packet.header
        writer = self._writer
        if writer is not None:
            try:
                writer.flush()
            except Exception as exc:
                _LOGGER.exception("Unable to flush capture")
                self.stats.set_capture_error(message("capture_flush_error", error=str(exc)))

    def _write_packet(self, packet: CapturedPacket) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(packet)
        except Exception as exc:
            _LOGGER.exception("Unable to write packet to capture")
            self.stats.set_capture_error(message("capture_write_error", error=str(exc)))
            self._writer = None
            try:
                writer.close()
            except Exception as close_exc:
                _LOGGER.exception("Unable to close failed capture")
                self.stats.set_capture_error(message("capture_close_error", error=str(close_exc)))
            return
        self.stats.set_capture(writer.packets_written, writer.bytes_written)
