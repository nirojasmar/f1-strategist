"""Modelos del histórico de vueltas y contrato de los módulos de métricas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from ..config import MetricsConfig
from ..race_state import CarSnapshot, RaceStateSnapshot
from .types import Metric

LapTimeSource = Literal["session_history", "lap_data"]


@dataclass(frozen=True, slots=True)
class _LapTimingUpdate:
    lap_time_s: float
    sector1_s: float | None
    sector2_s: float | None
    sector3_s: float | None
    valid: bool

    def apply(self, record: LapRecord) -> LapRecord:
        return replace(
            record,
            lap_time_s=self.lap_time_s,
            lap_time_source="session_history",
            sector1_s=self.sector1_s,
            sector2_s=self.sector2_s,
            sector3_s=self.sector3_s,
            valid=self.valid,
        )


@dataclass(frozen=True, slots=True)
class LapRecord:
    """Datos agregados de una vuelta de un coche."""

    car_index: int
    lap_number: int
    lap_time_s: float | None = None
    lap_time_source: LapTimeSource | None = None
    sector1_s: float | None = None
    sector2_s: float | None = None
    sector3_s: float | None = None
    valid: bool | None = None
    compound: str | None = None
    tyre_age_start: int | None = None
    tyre_age_end: int | None = None
    fuel_start_kg: float | None = None
    fuel_end_kg: float | None = None
    tyre_wear_start_pct: tuple[float, ...] | None = None
    tyre_wear_end_pct: tuple[float, ...] | None = None
    position_start: int | None = None
    position_end: int | None = None
    gap_ahead_start_s: float | None = None
    gap_ahead_end_s: float | None = None
    ers_harvested_j: float | None = None
    ers_deployed_j: float | None = None
    pit_entry: bool = False
    pit_exit: bool = False
    pit_lane_time_s: float | None = None
    pit_stop_time_s: float | None = None
    track_status_seen: frozenset[str] = frozenset()
    weather_seen: frozenset[str] = frozenset()
    traffic_fraction: float | None = None
    damage_max_delta_pct: float | None = None
    flags: frozenset[str] = frozenset()
    closed: bool = False
    started_at_monotonic: float | None = None
    ended_at_monotonic: float | None = None

    def __post_init__(self) -> None:
        if self.car_index < 0:
            raise ValueError("car_index no puede ser negativo")
        if self.lap_number < 1:
            raise ValueError("lap_number debe ser positivo")
        if self.lap_time_source not in {None, "session_history", "lap_data"}:
            raise ValueError(f"lap_time_source no válido: {self.lap_time_source!r}")
        object.__setattr__(self, "track_status_seen", frozenset(self.track_status_seen))
        object.__setattr__(self, "weather_seen", frozenset(self.weather_seen))
        object.__setattr__(self, "flags", frozenset(self.flags))
        for name in ("tyre_wear_start_pct", "tyre_wear_end_pct"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True, slots=True)
class TyreStint:
    """Tramo de una carrera con el mismo compuesto de neumáticos."""

    compound: str | None = None
    start_lap: int = 1
    end_lap: int | None = None
    visual_compound: str | None = None

    def __post_init__(self) -> None:
        if self.start_lap < 1:
            raise ValueError("start_lap debe ser positivo")
        if self.end_lap is not None and self.end_lap < self.start_lap:
            raise ValueError("end_lap no puede ser anterior a start_lap")


@dataclass(frozen=True, slots=True)
class LapHistorySnapshot:
    """Vista inmutable del histórico de vueltas y stints."""

    records: Mapping[int, tuple[LapRecord, ...]] = field(default_factory=dict)
    tyre_stints: Mapping[int, tuple[TyreStint, ...]] = field(default_factory=dict)
    session_uid: int | None = None

    def __post_init__(self) -> None:
        records = {car_index: tuple(car_records) for car_index, car_records in self.records.items()}
        stints = {
            car_index: tuple(car_stints) for car_index, car_stints in self.tyre_stints.items()
        }
        object.__setattr__(self, "records", MappingProxyType(records))
        object.__setattr__(self, "tyre_stints", MappingProxyType(stints))

    def records_for(self, car_index: int) -> tuple[LapRecord, ...]:
        return self.records.get(car_index, ())

    def stints_for(self, car_index: int) -> tuple[TyreStint, ...]:
        return self.tyre_stints.get(car_index, ())


class LapHistory:
    """Histórico mutable de un único escritor, acotado por coche."""

    def __init__(self, history_max_laps: MetricsConfig | int = 100) -> None:
        if isinstance(history_max_laps, MetricsConfig):
            limit = history_max_laps.history_max_laps
        else:
            limit = history_max_laps
        if limit < 1:
            raise ValueError("history_max_laps debe ser positivo")
        self._history_max_laps = limit
        self._session_uid: int | None = None
        self._records: dict[int, dict[int, LapRecord]] = {}
        self._tyre_stints: dict[int, tuple[TyreStint, ...]] = {}
        self._pending_timing: dict[int, dict[int, _LapTimingUpdate]] = {}

    @property
    def session_uid(self) -> int | None:
        return self._session_uid

    @property
    def history_max_laps(self) -> int:
        return self._history_max_laps

    def reset(self, session_uid: int | None = None) -> None:
        """Descarta todo el histórico y fija la sesión activa."""
        self._session_uid = session_uid
        self._records.clear()
        self._tyre_stints.clear()
        self._pending_timing.clear()

    def upsert(self, record: LapRecord) -> None:
        """Inserta o reemplaza una vuelta, descartando primero las más antiguas."""
        pending = self._pending_timing.get(record.car_index)
        if pending is not None:
            update = pending.pop(record.lap_number, None)
            if not pending:
                del self._pending_timing[record.car_index]
            if update is not None:
                record = update.apply(record)
        car_records = self._records.setdefault(record.car_index, {})
        car_records[record.lap_number] = record
        while len(car_records) > self._history_max_laps:
            del car_records[min(car_records)]

    def update_lap_timing(
        self,
        car_index: int,
        lap_number: int,
        *,
        lap_time_s: float,
        sector1_s: float | None,
        sector2_s: float | None,
        sector3_s: float | None,
        valid: bool,
    ) -> None:
        """Aplica o pospone los tiempos oficiales sin crear un registro de vuelta."""
        if car_index < 0:
            raise ValueError("car_index no puede ser negativo")
        if lap_number < 1:
            raise ValueError("lap_number debe ser positivo")
        update = _LapTimingUpdate(
            lap_time_s=lap_time_s,
            sector1_s=sector1_s,
            sector2_s=sector2_s,
            sector3_s=sector3_s,
            valid=valid,
        )
        record = self.get(car_index, lap_number)
        if record is not None:
            self.upsert(update.apply(record))
            return

        pending = self._pending_timing.setdefault(car_index, {})
        pending[lap_number] = update
        while len(pending) > self._history_max_laps:
            del pending[min(pending)]

    def get(self, car_index: int, lap_number: int) -> LapRecord | None:
        return self._records.get(car_index, {}).get(lap_number)

    def discard_after(self, car_index: int, lap_number: int) -> None:
        """Descarta las vueltas posteriores, por ejemplo tras un flashback."""
        car_records = self._records.get(car_index)
        if car_records is not None:
            for recorded_lap in tuple(car_records):
                if recorded_lap > lap_number:
                    del car_records[recorded_lap]
            if not car_records:
                del self._records[car_index]
        pending = self._pending_timing.get(car_index)
        if pending is not None:
            for pending_lap in tuple(pending):
                if pending_lap > lap_number:
                    del pending[pending_lap]
            if not pending:
                del self._pending_timing[car_index]

    def replace_tyre_stints(self, car_index: int, stints: Iterable[TyreStint]) -> None:
        """Reemplaza los stints conocidos de un coche."""
        if car_index < 0:
            raise ValueError("car_index no puede ser negativo")
        self._tyre_stints[car_index] = tuple(stints)

    def snapshot(self) -> LapHistorySnapshot:
        """Devuelve una copia inmutable sin compartir estado con el histórico."""
        records = {
            car_index: tuple(car_records[lap] for lap in sorted(car_records))
            for car_index, car_records in sorted(self._records.items())
            if car_records
        }
        stints = {
            car_index: tuple(car_stints)
            for car_index, car_stints in sorted(self._tyre_stints.items())
            if car_stints
        }
        return LapHistorySnapshot(
            records=records,
            tyre_stints=stints,
            session_uid=self._session_uid,
        )


@dataclass(frozen=True, slots=True)
class MetricInputs:
    """Entradas inmutables que recibe cada módulo de métricas."""

    state: RaceStateSnapshot
    history: LapHistorySnapshot
    config: MetricsConfig
    now: float
    car_index: int

    @property
    def car(self) -> CarSnapshot | None:
        if 0 <= self.car_index < len(self.state.cars):
            return self.state.cars[self.car_index]
        return None


@runtime_checkable
class MetricModule(Protocol):
    """Contrato de un módulo puro de métricas."""

    name: str

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        """Calcula las métricas del coche indicado en las entradas."""
        ...
