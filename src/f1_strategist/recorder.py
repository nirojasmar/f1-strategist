"""Grabador de sesiones en vivo y reingesta de capturas en el almacén."""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .config import AppConfig, MetricsConfig, RecordingConfig
from .fingerprint import RunInfo, build_run_info, compute_capture_fingerprint
from .messages import DiagnosticMessage
from .metrics.engine import MetricsEngine, MetricsSnapshot
from .metrics.export import snapshot_to_dict
from .metrics.history import LapHistory, LapHistorySnapshot, LapRecord
from .offline import OfflineResult, run_capture_offline
from .packets import PacketHeader
from .parser import ParsedPacket
from .race_state import RaceEvent, RaceState, RaceStateSnapshot
from .store import (
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
    StoreError,
)
from .store.sqlite_store import SessionSource

_EVENT_MEMORY_LIMIT = 8192
_PENDING_ERROR_LIMIT = 100


class RecorderStore(Protocol):
    """Subconjunto del almacén que necesita el grabador (facilita dobles de prueba)."""

    def transaction(self) -> AbstractContextManager[Any]: ...

    def create_session(self, info: SessionInfo) -> int: ...

    def finish_session(
        self,
        session_id: int,
        *,
        ended_at_utc: str,
        incomplete: bool = False,
        fingerprint: str | None = None,
        final_metrics_json: str | None = None,
        capture_sha256: str | None = None,
    ) -> None: ...

    def update_session(self, session_id: int, info: SessionInfo) -> None: ...

    def add_participants(self, session_id: int, participants: Iterable[Participant]) -> None: ...

    def upsert_laps(self, session_id: int, laps: Iterable[LapRecord]) -> None: ...

    def add_events(self, session_id: int, events: Iterable[StoredEvent]) -> None: ...

    def add_metric_samples(self, session_id: int, samples: Iterable[MetricSample]) -> None: ...

    def add_errors(self, session_id: int, errors: Iterable[StoredError]) -> None: ...


@dataclass(frozen=True, slots=True)
class _Batch:
    """Trabajo pendiente de escribir para una sesión concreta."""

    session_uid: int
    session_info: SessionInfo
    participants: tuple[Participant, ...]
    laps: tuple[LapRecord, ...]
    events: tuple[StoredEvent, ...]
    metrics: tuple[MetricSample, ...]
    errors: tuple[StoredError, ...]
    incomplete: bool
    incomplete_uids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class _Stop:
    """Orden de cierre enviada al hilo escritor."""

    fingerprint: str | None
    incomplete: bool
    incomplete_uids: frozenset[int] = frozenset()
    final_metrics_json: str | None = None
    capture_sha256: str | None = None


def serialize_config(config: AppConfig) -> str:
    """Serializa la configuración de forma canónica para guardarla como metadato."""
    return json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class SessionRecorder:
    """Escribe en el almacén lo que publica una sesión sin bloquear al consumidor.

    ``observe`` solo construye un lote y lo encola; un hilo propio agrupa los
    lotes por tamaño o intervalo y los confirma en una transacción.
    """

    def __init__(
        self,
        store: RecorderStore,
        config: RecordingConfig,
        *,
        source: SessionSource = "live",
        run_info: RunInfo | None = None,
        capture_path: Path | None = None,
        config_json: str | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._source: SessionSource = source
        self._run_info = run_info or build_run_info()
        self._capture_path = str(capture_path) if capture_path is not None else None
        self._config_json = config_json
        self._lock = threading.Lock()
        self._queue: queue.Queue[_Batch | _Stop] = queue.Queue(maxsize=config.queue_size)

        self._dropped_by_uid: dict[int, int] = {}
        self._writer_incomplete = False
        self._error: Exception | None = None
        self._stopped = False
        self._stop_requested = False

        self._session_uid: int | None = None
        self._producer_info: SessionInfo | None = None
        self._session_id: int | None = None
        self._last_session_id: int | None = None
        self._stored_uid: int | None = None
        self._writer_info: SessionInfo | None = None
        self._session_incomplete = False
        self._pending_errors: list[StoredError] = []

        self._last_signature: tuple[Any, ...] | None = None
        self._last_observed_at = 0.0
        self._last_state: RaceStateSnapshot | None = None
        self._last_metrics: MetricsSnapshot | None = None
        self._last_header_time: float | None = None
        self._lap_state: dict[tuple[int, int], LapRecord] = {}
        self._participant_state: dict[int, tuple[str | None, str | None]] = {}
        self._event_window: tuple[tuple[Any, ...], ...] = ()
        self._event_seq = 0
        self._metric_state: dict[str, tuple[object, object]] = {}
        self._last_metrics_at: float | None = None
        self._last_metrics_computed_at: float | None = None
        self._last_metrics_lap: int | None = None

        self._thread = threading.Thread(target=self._run, name="session-recorder", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # Consulta de estado
    # ------------------------------------------------------------------

    @property
    def dropped_batches(self) -> int:
        """Lotes descartados por falta de espacio en la cola."""
        with self._lock:
            return sum(self._dropped_by_uid.values())

    @property
    def incomplete(self) -> bool:
        """Indica si la sesión actual perdió datos o sufrió errores de escritura."""
        with self._lock:
            uid = self._session_uid
            return self._writer_incomplete or (uid is not None and uid in self._dropped_by_uid)

    @property
    def session_id(self) -> int | None:
        """Id de la sesión abierta en el almacén, si ya se ha creado."""
        with self._lock:
            return self._session_id if self._session_id is not None else self._last_session_id

    @property
    def session_uid(self) -> int | None:
        """Identificador de la sesión que se está grabando."""
        with self._lock:
            return self._session_uid

    @property
    def error(self) -> Exception | None:
        """Primer error del hilo escritor, conservado para diagnóstico."""
        with self._lock:
            return self._error

    @property
    def stopped(self) -> bool:
        """Indica si el hilo escritor ya terminó."""
        with self._lock:
            return self._stopped

    # ------------------------------------------------------------------
    # Consumidor
    # ------------------------------------------------------------------

    def observe(
        self,
        header_session_time: float | None,
        observed_at: float,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        history: LapHistory | LapHistorySnapshot,
        *,
        header: PacketHeader | None = None,
    ) -> None:
        """Registra el estado publicado sin bloquear al hilo consumidor."""
        try:
            self._observe(
                header_session_time,
                observed_at,
                state,
                metrics,
                history,
                header,
            )
        except Exception as exc:  # noqa: BLE001 - el grabador nunca detiene la captura
            self._remember_error(exc)

    def record_error(
        self,
        issue: DiagnosticMessage,
        *,
        t_sim_s: float | None = None,
    ) -> None:
        """Guarda un error del consumidor con su clave y parámetros."""
        error = StoredError(
            t_sim_s=self._last_observed_at if t_sim_s is None else t_sim_s,
            code=issue.key,
            params_json=_canonical_json(dict(issue.parameters)),
        )
        with self._lock:
            if self._producer_info is None:
                if len(self._pending_errors) >= _PENDING_ERROR_LIMIT:
                    self._pending_errors.pop(0)
                    self._writer_incomplete = True
                self._pending_errors.append(error)
                return
            info = self._producer_info
            incomplete = self._writer_incomplete or info.session_uid in self._dropped_by_uid
        batch = _Batch(
            session_uid=info.session_uid,
            session_info=info,
            participants=(),
            laps=(),
            events=(),
            metrics=(),
            errors=(error,),
            incomplete=incomplete,
            incomplete_uids=self._incomplete_uids(),
        )
        self._enqueue(batch)

    def stop(
        self,
        *,
        fingerprint: str | None = None,
        incomplete: bool = False,
        final_metrics: MetricsSnapshot | None = None,
        capture_sha256: str | None = None,
    ) -> None:
        """Vacía la cola, cierra la sesión abierta y detiene el hilo escritor."""
        with self._lock:
            if self._stop_requested:
                return
            self._stop_requested = True
        final_batch = self._final_batch()
        with self._lock:
            uid = self._session_uid
            is_incomplete = (
                incomplete
                or self._writer_incomplete
                or (uid is not None and uid in self._dropped_by_uid)
            )
            incomplete_uids = frozenset(self._dropped_by_uid)
        if final_batch is not None:
            self._queue.put(final_batch)
        self._queue.put(
            _Stop(
                fingerprint=fingerprint,
                incomplete=is_incomplete,
                incomplete_uids=incomplete_uids,
                final_metrics_json=_final_metrics_json(final_metrics),
                capture_sha256=capture_sha256,
            )
        )
        timeout = 5.0 + self._config.flush_interval_s
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Lógica del consumidor
    # ------------------------------------------------------------------

    def _observe(
        self,
        header_session_time: float | None,
        observed_at: float,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        history: LapHistory | LapHistorySnapshot,
        header: PacketHeader | None,
    ) -> None:
        uid = state.session.uid
        if uid is None:
            return

        signature = (
            uid,
            metrics.computed_at_monotonic,
            state.current_lap,
            len(state.events),
            state.events[-1] if state.events else None,
            tuple((car.car_index, car.name, car.team) for car in state.cars),
            state.session.track,
            state.session.session_type,
            state.session.total_laps,
            header.packet_format if header is not None else None,
            header.game_year if header is not None else None,
            header.game_major_version if header is not None else None,
            header.game_minor_version if header is not None else None,
        )
        self._last_state = state
        self._last_metrics = metrics
        self._last_header_time = header_session_time
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self._last_observed_at = observed_at

        session_changed = uid != self._session_uid
        if session_changed:
            self._reset_session(uid)
        info = self._build_session_info(uid, state, header)
        metadata_changed = info != self._producer_info
        self._producer_info = info

        participants = self._collect_participants(state)
        laps = self._collect_laps(history)
        events = self._collect_events(state.events)
        samples = self._collect_metric_samples(observed_at, header_session_time, state, metrics)

        with self._lock:
            errors = tuple(self._pending_errors)
            self._pending_errors.clear()
            incomplete = self._writer_incomplete or uid in self._dropped_by_uid

        if not (
            session_changed
            or metadata_changed
            or participants
            or laps
            or events
            or samples
            or errors
        ):
            return
        self._enqueue(
            _Batch(
                session_uid=uid,
                session_info=info,
                participants=participants,
                laps=laps,
                events=events,
                metrics=samples,
                errors=errors,
                incomplete=incomplete,
                incomplete_uids=self._incomplete_uids(),
            )
        )

    def _reset_session(self, uid: int) -> None:
        self._session_uid = uid
        self._session_incomplete = False
        self._producer_info = None
        self._lap_state.clear()
        self._participant_state.clear()
        self._event_window = ()
        self._event_seq = 0
        self._metric_state.clear()
        self._last_metrics_at = None
        self._last_metrics_computed_at = None
        self._last_metrics_lap = None
        self._last_state = None
        self._last_metrics = None
        self._last_header_time = None

    def _build_session_info(
        self,
        uid: int,
        state: RaceStateSnapshot,
        header: PacketHeader | None,
    ) -> SessionInfo:
        run_info = self._run_info
        game_format = str(header.packet_format) if header is not None else None
        game_year = header.game_year if header is not None else None
        game_version = (
            f"{header.game_major_version}.{header.game_minor_version}"
            if header is not None
            else None
        )
        previous = self._producer_info
        base = (
            previous
            if previous is not None and previous.session_uid == uid
            else SessionInfo(
                session_uid=uid,
                source=self._source,
                started_at_utc=datetime.now(UTC).isoformat(),
            )
        )
        updates: dict[str, object] = {}
        for field, value in (
            ("game_format", game_format),
            ("game_year", game_year),
            ("game_version", game_version),
            ("track", state.session.track),
            ("session_type", state.session.session_type),
            ("total_laps", state.session.total_laps),
            ("capture_path", self._capture_path),
            ("capture_sha256", run_info.capture_sha256 or None),
            ("tool_version", run_info.tool_version),
            ("parser_version", run_info.parser_version),
            ("config_hash", run_info.config_hash),
            ("config_json", self._config_json),
        ):
            if value is not None:
                updates[field] = value
        return replace(base, **updates)  # type: ignore[arg-type]

    def _collect_participants(self, state: RaceStateSnapshot) -> tuple[Participant, ...]:
        participants: list[Participant] = []
        for car in state.cars:
            key = (car.name, car.team)
            if self._participant_state.get(car.car_index) == key:
                continue
            self._participant_state[car.car_index] = key
            participants.append(Participant(car_index=car.car_index, name=car.name, team=car.team))
        return tuple(participants)

    def _collect_laps(self, history: LapHistory | LapHistorySnapshot) -> tuple[LapRecord, ...]:
        snapshot = history.snapshot() if isinstance(history, LapHistory) else history
        laps: list[LapRecord] = []
        for car_index in sorted(snapshot.records):
            for record in snapshot.records[car_index]:
                key = (record.car_index, record.lap_number)
                if self._lap_state.get(key) == record:
                    continue
                self._lap_state[key] = record
                laps.append(record)
        return tuple(laps)

    def _collect_events(self, events: Iterable[RaceEvent]) -> tuple[StoredEvent, ...]:
        current_events = tuple(events)
        current_keys = tuple(_event_key(event) for event in current_events)
        overlap = _event_overlap(self._event_window, current_keys)
        self._event_window = current_keys[-_EVENT_MEMORY_LIMIT:]
        stored: list[StoredEvent] = []
        for event in current_events[overlap:]:
            self._event_seq += 1
            stored.append(
                StoredEvent(
                    seq=self._event_seq,
                    kind=event.kind,
                    t_sim_s=event.occurred_at_monotonic,
                    car_index=event.car_index,
                    lap_number=event.lap_number,
                    value=event.value,
                    previous_value=event.previous_value,
                )
            )
        return tuple(stored)

    def _collect_metric_samples(
        self,
        observed_at: float,
        header_session_time: float | None,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        *,
        force: bool = False,
    ) -> tuple[MetricSample, ...]:
        lap_changed = (
            self._last_metrics_lap is not None and state.current_lap != self._last_metrics_lap
        )
        interval_elapsed = (
            self._last_metrics_at is None
            or observed_at - self._last_metrics_at >= self._config.metrics_interval_s
        )
        publication_changed = metrics.computed_at_monotonic != self._last_metrics_computed_at
        value_changed = any(
            self._metric_state.get(key) != (metric.value, metric.quality)
            for key, metric in metrics.metrics.items()
        )
        should_sample = bool(metrics.metrics) and (
            force or lap_changed or value_changed or (publication_changed and interval_elapsed)
        )
        session_time = (
            header_session_time
            if header_session_time is not None
            and math.isfinite(header_session_time)
            and header_session_time >= 0
            else None
        )
        samples: list[MetricSample] = []
        if should_sample:
            for key in sorted(metrics.metrics):
                metric = metrics.metrics[key]
                marker: tuple[object, object] = (metric.value, metric.quality)
                if not (force or lap_changed) and self._metric_state.get(key) == marker:
                    continue
                self._metric_state[key] = marker
                value_num, value_text = _metric_value(metric.value)
                samples.append(
                    MetricSample(
                        t_sim_s=observed_at,
                        t_session_s=session_time,
                        lap=state.current_lap,
                        key=key,
                        value_num=value_num,
                        value_text=value_text,
                        unit=metric.unit,
                        quality=metric.quality.value,
                        sample_count=metric.sample_count,
                    )
                )
            self._last_metrics_at = observed_at
            self._last_metrics_computed_at = metrics.computed_at_monotonic
        self._last_metrics_lap = state.current_lap
        return tuple(samples)

    def _final_batch(self) -> _Batch | None:
        uid = self._session_uid
        info = self._producer_info
        state = self._last_state
        metrics = self._last_metrics
        if uid is None or info is None or state is None or metrics is None:
            return None
        samples = self._collect_metric_samples(
            self._last_observed_at,
            self._last_header_time,
            state,
            metrics,
            force=True,
        )
        if not samples:
            return None
        with self._lock:
            incomplete = self._writer_incomplete or uid in self._dropped_by_uid
        return _Batch(
            session_uid=uid,
            session_info=info,
            participants=(),
            laps=(),
            events=(),
            metrics=samples,
            errors=(),
            incomplete=incomplete,
            incomplete_uids=self._incomplete_uids(),
        )

    def _incomplete_uids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._dropped_by_uid)

    def _enqueue(self, batch: _Batch) -> None:
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            with self._lock:
                self._dropped_by_uid[batch.session_uid] = (
                    self._dropped_by_uid.get(batch.session_uid, 0) + 1
                )

    def _remember_error(self, exc: Exception) -> None:
        with self._lock:
            if self._error is None:
                self._error = exc
            self._writer_incomplete = True

    # ------------------------------------------------------------------
    # Hilo escritor
    # ------------------------------------------------------------------

    def _run(self) -> None:
        pending: list[_Batch] = []
        flush_interval = self._config.flush_interval_s
        deadline = time.monotonic() + flush_interval
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                self._flush(pending)
                pending = []
                deadline = time.monotonic() + flush_interval
                continue
            if isinstance(item, _Stop):
                self._flush(pending)
                pending = []
                self._finish(item)
                return
            pending.append(item)
            if len(pending) >= self._config.batch_size:
                self._flush(pending)
                pending = []
                deadline = time.monotonic() + flush_interval

    def _flush(self, batches: list[_Batch]) -> None:
        if not batches:
            return
        try:
            with self._store.transaction():
                for batch in batches:
                    self._apply_batch(batch)
        except Exception as exc:  # noqa: BLE001 - se aísla del hilo consumidor
            self._remember_error(exc)
            with self._lock:
                self._session_id = None
                self._stored_uid = None
                self._writer_info = None

    def _apply_batch(self, batch: _Batch) -> None:
        if self._stored_uid != batch.session_uid:
            previous_uid = self._stored_uid
            self._close_current(
                incomplete=(
                    self._writer_incomplete
                    or (previous_uid is not None and previous_uid in batch.incomplete_uids)
                )
            )
            session_id = self._store.create_session(batch.session_info)
            with self._lock:
                self._session_id = session_id
                self._stored_uid = batch.session_uid
                self._writer_info = batch.session_info
        session_id = self._session_id
        if session_id is None:
            return
        if self._writer_info != batch.session_info:
            self._store.update_session(session_id, batch.session_info)
            with self._lock:
                self._writer_info = batch.session_info
        self._session_incomplete = (
            self._writer_incomplete or batch.session_uid in batch.incomplete_uids
        )
        if batch.participants:
            self._store.add_participants(session_id, batch.participants)
        if batch.laps:
            self._store.upsert_laps(session_id, batch.laps)
        if batch.events:
            self._store.add_events(session_id, batch.events)
        if batch.metrics:
            self._store.add_metric_samples(session_id, batch.metrics)
        if batch.errors:
            self._store.add_errors(session_id, batch.errors)

    def _close_current(
        self,
        *,
        fingerprint: str | None = None,
        final_metrics_json: str | None = None,
        capture_sha256: str | None = None,
        incomplete: bool | None = None,
    ) -> None:
        session_id = self._session_id
        if session_id is None:
            return
        self._store.finish_session(
            session_id,
            ended_at_utc=datetime.now(UTC).isoformat(),
            incomplete=self._session_incomplete if incomplete is None else incomplete,
            fingerprint=fingerprint,
            final_metrics_json=final_metrics_json,
            capture_sha256=capture_sha256,
        )
        with self._lock:
            self._last_session_id = session_id
            self._session_id = None
            self._stored_uid = None
            self._writer_info = None

    def _finish(self, stop: _Stop) -> None:
        try:
            with self._store.transaction():
                uid = self._stored_uid
                self._session_incomplete = (
                    stop.incomplete
                    or self._writer_incomplete
                    or (uid is not None and uid in stop.incomplete_uids)
                )
                self._close_current(
                    fingerprint=stop.fingerprint,
                    final_metrics_json=stop.final_metrics_json,
                    capture_sha256=stop.capture_sha256,
                )
        except Exception as exc:  # noqa: BLE001 - se conserva para diagnóstico
            self._remember_error(exc)
        finally:
            with self._lock:
                self._stopped = True


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Resumen de la reingesta de una captura en el almacén."""

    session_id: int
    session_uid: int
    fingerprint: str
    packets_read: int
    packets_parsed: int
    laps: int
    events: int
    metric_samples: int
    errors: int
    skipped: bool = False
    offline: OfflineResult | None = None


class _IngestCollector:
    """Consumidor offline que alimenta estado, métricas y grabador a la vez."""

    def __init__(self, engine: MetricsEngine, recorder: SessionRecorder) -> None:
        self.engine = engine
        self.recorder = recorder
        self.events: list[RaceEvent] = []
        self.state = RaceState(self.events.append)
        self.last_state: RaceStateSnapshot | None = None
        self.last_observed_at: float | None = None
        self.last_header_time: float | None = None
        self.last_header: PacketHeader | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        snapshot = self.state.snapshot()
        self.last_state = snapshot
        self.last_observed_at = observed_at
        self.last_header_time = packet.header.session_time
        self.last_header = packet.header
        self.engine.apply(packet, observed_at, snapshot)
        self.recorder.observe(
            packet.header.session_time,
            observed_at,
            snapshot,
            self.engine.snapshot(),
            self.engine.history,
            header=packet.header,
        )


def ingest_capture(
    path: Path,
    store: SessionStore,
    config: AppConfig | MetricsConfig | None = None,
    *,
    replace: bool = False,
) -> IngestResult:
    """Reproduce una captura y guarda en el almacén la misma sesión que exporta métricas."""
    app_config = _resolve_config(config)
    run_info = build_run_info(app_config, path)
    existing = _find_ingested(store, run_info)
    if existing is not None and not replace:
        return _result_from_session(store, existing, skipped=True)
    old_sessions = _find_same_capture(store, run_info) if replace else ()

    engine = MetricsEngine(app_config.metrics)
    recorder = SessionRecorder(
        store,
        app_config.recording,
        source="ingest",
        run_info=run_info,
        capture_path=path,
        config_json=serialize_config(app_config),
    )
    collector = _IngestCollector(engine, recorder)
    offline: OfflineResult | None = None
    metrics: MetricsSnapshot | None = None
    fingerprint: str | None = None
    try:
        offline = run_capture_offline(path, [collector])
        if collector.last_state is not None and collector.last_observed_at is not None:
            engine.finalize(collector.last_state, collector.last_observed_at)
            recorder.observe(
                collector.last_header_time,
                collector.last_observed_at,
                collector.last_state,
                engine.snapshot(),
                engine.history,
                header=collector.last_header,
            )
        metrics = engine.snapshot()
        fingerprint = compute_capture_fingerprint(
            tuple(collector.events),
            engine.history,
            metrics,
            app_config,
            path,
            run_info=run_info,
        )
    finally:
        recorder.stop(
            fingerprint=fingerprint,
            incomplete=offline is None or offline.truncated,
            final_metrics=metrics,
            capture_sha256=run_info.capture_sha256 or None,
        )

    if recorder.error is not None:
        raise StoreError(f"No se pudo grabar la sesión: {recorder.error}") from recorder.error
    session_id = recorder.session_id
    if session_id is None:
        raise StoreError("La captura no contiene una sesión reconocible")

    if old_sessions:
        with store.transaction():
            for old in old_sessions:
                if old.id is not None and old.id != session_id:
                    store.delete_session(old.id)

    session = store.get_session(session_id)
    if session is None:
        raise StoreError(f"No existe la sesión {session_id}")
    return _result_from_session(store, session, skipped=False, offline=offline)


def _resolve_config(config: AppConfig | MetricsConfig | None) -> AppConfig:
    if config is None:
        return AppConfig()
    if isinstance(config, AppConfig):
        return config
    return AppConfig(metrics=config)


def _find_ingested(store: SessionStore, run_info: RunInfo) -> SessionInfo | None:
    for session in _find_same_capture(store, run_info):
        if (
            session.tool_version == run_info.tool_version
            and session.parser_version == run_info.parser_version
            and session.fingerprint
            and session.ended_at_utc is not None
            and not session.incomplete
            and session.final_metrics_json is not None
        ):
            return session
    return None


def _find_same_capture(store: SessionStore, run_info: RunInfo) -> tuple[SessionInfo, ...]:
    return tuple(
        session
        for session in store.list_sessions()
        if session.source == "ingest"
        and session.id is not None
        and session.capture_sha256 == run_info.capture_sha256
        and session.config_hash == run_info.config_hash
    )


def _result_from_session(
    store: SessionStore,
    session: SessionInfo,
    *,
    skipped: bool,
    offline: OfflineResult | None = None,
) -> IngestResult:
    assert session.id is not None
    session_id = session.id
    return IngestResult(
        session_id=session_id,
        session_uid=session.session_uid,
        fingerprint=session.fingerprint or "",
        packets_read=offline.packets_read if offline is not None else 0,
        packets_parsed=offline.packets_parsed if offline is not None else 0,
        laps=sum(1 for _ in store.iter_laps(session_id)),
        events=sum(1 for _ in store.iter_events(session_id)),
        metric_samples=sum(1 for _ in store.iter_metric_samples(session_id)),
        errors=sum(1 for _ in store.iter_errors(session_id)),
        skipped=skipped,
        offline=offline,
    )


def _event_key(event: RaceEvent) -> tuple[Any, ...]:
    return (
        event.kind,
        event.occurred_at_monotonic,
        event.car_index,
        event.lap_number,
        event.value,
        event.previous_value,
    )


def _event_overlap(
    previous: tuple[tuple[Any, ...], ...],
    current: tuple[tuple[Any, ...], ...],
) -> int:
    """Longitud del solape ordenado entre la ventana anterior y la nueva.

    Preservar la multiplicidad permite distinguir dos eventos idénticos
    consecutivos de uno solo repetido en la observación.
    """
    limit = min(len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _final_metrics_json(metrics: MetricsSnapshot | None) -> str | None:
    if metrics is None:
        return None
    return _canonical_json(snapshot_to_dict(metrics))


def _metric_value(value: object) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "true" if value else "false"
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        return None, value
    if isinstance(value, tuple):
        return None, _canonical_json(list(value))
    return None, _canonical_json(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
