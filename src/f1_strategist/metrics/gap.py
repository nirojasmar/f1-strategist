"""Métricas de intervalos y comparación con un rival."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import mean
from typing import Literal

from ..race_state import CarSnapshot, PacketFreshness
from .comparability import classify_lap, is_clean
from .history import LapRecord, MetricInputs
from .types import Metric, Quality, Window, insufficient, unavailable

_RETIRED_STATUSES = frozenset({"retired", "inactive", "invalid"})


@dataclass(frozen=True, slots=True)
class GapSample:
    """Un muestreo monotónico; las distancias de intervalo son no negativas."""

    observed_at_monotonic: float
    ahead_s: float | None
    behind_s: float | None
    leader_s: float | None

    def __post_init__(self) -> None:
        if not math.isfinite(self.observed_at_monotonic):
            raise ValueError("observed_at_monotonic debe ser finito")
        for value in (self.ahead_s, self.behind_s, self.leader_s):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Los intervalos deben ser finitos y no negativos")


class GapTracker:
    """Buffer acotado de muestras para alimentar tendencias de intervalos."""

    def __init__(self, max_samples: int = 120) -> None:
        if max_samples < 2:
            raise ValueError("max_samples debe ser al menos 2")
        self._samples: deque[GapSample] = deque(maxlen=max_samples)

    @property
    def samples(self) -> tuple[GapSample, ...]:
        return tuple(self._samples)

    def add_sample(self, sample: GapSample) -> None:
        """Añade una muestra; una marca temporal repetida reemplaza la anterior."""
        if self._samples and sample.observed_at_monotonic < self._samples[-1].observed_at_monotonic:
            raise ValueError("Las muestras deben estar ordenadas por tiempo monotónico")
        if (
            self._samples
            and sample.observed_at_monotonic == self._samples[-1].observed_at_monotonic
        ):
            self._samples[-1] = sample
        else:
            self._samples.append(sample)

    def record(self, inputs: MetricInputs) -> GapSample:
        """Lee el estado actual y guarda sus intervalos para el motor o el módulo."""
        ahead, behind, leader, _ = _current_gaps(inputs)
        sample = GapSample(inputs.now, ahead, behind, leader)
        self.add_sample(sample)
        return sample

    def window(self, now: float, seconds: float) -> tuple[GapSample, ...]:
        """Devuelve las muestras dentro de la ventana móvil solicitada."""
        if seconds <= 0:
            raise ValueError("seconds debe ser positivo")
        cutoff = now - seconds
        return tuple(
            sample for sample in self._samples if cutoff <= sample.observed_at_monotonic <= now
        )


@dataclass(frozen=True, slots=True)
class CarComparison:
    """Resumen del rival respecto al piloto.

    ``interval_s`` es negativo cuando el rival va delante y positivo cuando va detrás.
    ``pace_delta_s`` es ritmo del rival menos ritmo del piloto (positivo = rival más lento).
    """

    rival_index: int
    interval_s: float | None
    interval_direction: Literal["ahead", "behind", "tied"] | None
    pace_delta_s: float | None
    compound: str | None
    tyre_age_laps: int | None
    stops_done: int | None
    quality: Quality
    notes: tuple[str, ...] = ()


def _freshness(inputs: MetricInputs) -> tuple[bool, tuple[str, ...]]:
    sources: list[str] = ["race_state"]
    try:
        freshness: PacketFreshness = inputs.state.freshness_for("lap_data")
    except KeyError:
        return False, tuple(sources)
    return freshness.is_stale(inputs.now), ("race_state", "lap_data")


def _active(car: CarSnapshot) -> bool:
    return car.result_status not in _RETIRED_STATUSES and car.driver_status not in _RETIRED_STATUSES


def _car_by_index(inputs: MetricInputs, car_index: int) -> CarSnapshot | None:
    return next((car for car in inputs.state.cars if car.car_index == car_index), None)


def _current_gaps(
    inputs: MetricInputs,
) -> tuple[float | None, float | None, float | None, str | None]:
    car = _car_by_index(inputs, inputs.car_index)
    if car is None or not _active(car):
        return None, None, None, "car_unavailable"
    if car.position is None:
        return None, None, None, "position_unavailable"
    active_cars = tuple(
        item for item in inputs.state.cars if _active(item) and item.position is not None
    )
    if any(
        item.car_index != car.car_index and item.position == car.position for item in active_cars
    ):
        return None, None, None, "position_tie"

    ahead_cars = tuple(
        item for item in active_cars if item.position is not None and item.position < car.position
    )
    behind_cars = tuple(
        item for item in active_cars if item.position is not None and item.position > car.position
    )
    ahead = (
        car.delta_to_car_in_front_ms / 1000.0
        if ahead_cars and car.delta_to_car_in_front_ms is not None
        else None
    )
    nearest_behind = min(behind_cars, key=lambda item: item.position or 0, default=None)
    behind = (
        nearest_behind.delta_to_car_in_front_ms / 1000.0
        if nearest_behind is not None and nearest_behind.delta_to_car_in_front_ms is not None
        else None
    )
    leader = (
        0.0
        if car.position == 1
        else car.delta_to_leader_ms / 1000.0
        if car.delta_to_leader_ms is not None
        else None
    )
    return ahead, behind, leader, None


def _trend(samples: tuple[GapSample, ...], field: Literal["ahead_s", "behind_s"]) -> float | None:
    points = tuple(
        (sample.observed_at_monotonic, value)
        for sample in samples
        if (value := getattr(sample, field)) is not None
    )
    if len(points) < 2:
        return None
    x_mean = mean(x for x, _ in points)
    y_mean = mean(y for _, y in points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator


def _metric(
    key: str,
    value: float | None,
    *,
    now: float,
    stale: bool,
    count: int,
    sources: tuple[str, ...],
    reason: str,
    window: Window | None = None,
    trend: bool = False,
) -> Metric:
    if value is None:
        return unavailable(
            key,
            "s" if not trend else "s/s",
            reason,
            now,
            window=window or Window("instant"),
            sample_count=count,
            sources=sources,
        )
    return Metric(
        key=key,
        value=value,
        unit="s/s" if trend else "s",
        window=window or Window("instant"),
        quality=Quality.STALE if stale else Quality.OK,
        sample_count=count,
        computed_at_monotonic=now,
        sources=sources,
    )


class GapMetrics:
    """Public módulo de intervalos; recibe un tracker opcional para tendencias."""

    name = "gap"

    def __init__(self, tracker: GapTracker | None = None) -> None:
        self.tracker = tracker or GapTracker()

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        self.tracker.record(inputs)
        stale, sources = _freshness(inputs)
        ahead, behind, leader, issue = _current_gaps(inputs)
        live_count = 1 if issue is None else 0
        sample_window = self.tracker.window(inputs.now, inputs.config.gap_trend_window_s)
        low, ok = inputs.config.thresholds("gap")
        results = [
            _metric(
                "gap.ahead_s",
                ahead,
                now=inputs.now,
                stale=stale,
                count=live_count,
                sources=sources,
                reason=issue or "no_car_ahead",
            ),
            _metric(
                "gap.behind_s",
                behind,
                now=inputs.now,
                stale=stale,
                count=live_count,
                sources=sources,
                reason=issue or "no_car_behind",
            ),
            _metric(
                "gap.leader_s",
                leader,
                now=inputs.now,
                stale=stale,
                count=live_count,
                sources=sources,
                reason=issue or "leader_gap_unavailable",
            ),
        ]
        trend_fields: tuple[tuple[Literal["ahead_s", "behind_s"], str], ...] = (
            ("ahead_s", "gap.ahead_trend_s_per_s"),
            ("behind_s", "gap.behind_trend_s_per_s"),
        )
        for field, key in trend_fields:
            points = tuple(item for item in sample_window if getattr(item, field) is not None)
            slope = _trend(sample_window, field) if len(points) >= 2 else None
            if slope is None:
                results.append(
                    insufficient(
                        key,
                        "s/s",
                        "insufficient_gap_samples",
                        inputs.now,
                        window=Window("seconds", inputs.config.gap_trend_window_s),
                        sample_count=len(points),
                        sources=sources,
                    )
                )
            elif stale:
                results.append(
                    Metric(
                        key,
                        slope,
                        "s/s",
                        Window("seconds", inputs.config.gap_trend_window_s),
                        Quality.STALE,
                        len(points),
                        inputs.now,
                        sources,
                    )
                )
            elif len(points) < low:
                results.append(
                    Metric(
                        key,
                        slope,
                        "s/s",
                        Window("seconds", inputs.config.gap_trend_window_s),
                        Quality.LOW,
                        len(points),
                        inputs.now,
                        sources,
                    )
                )
            else:
                results.append(
                    Metric(
                        key,
                        slope,
                        "s/s",
                        Window("seconds", inputs.config.gap_trend_window_s),
                        Quality.OK if len(points) >= ok else Quality.LOW,
                        len(points),
                        inputs.now,
                        sources,
                    )
                )
        return tuple(results)


def _current_stint_records(inputs: MetricInputs, car_index: int) -> tuple[LapRecord, ...]:
    records = tuple(
        record
        for record in inputs.history.records_for(car_index)
        if record.closed
        and record.lap_time_s is not None
        and record.valid is not False
        and is_clean(classify_lap(record, inputs.config))
    )
    stints = inputs.history.stints_for(car_index)
    if not stints:
        return records[-3:]
    current = stints[-1]
    return tuple(
        record
        for record in records
        if record.lap_number >= current.start_lap
        and (current.end_lap is None or record.lap_number <= current.end_lap)
    )


def compare_car(inputs: MetricInputs, rival_index: int) -> CarComparison:
    """Compara un rival con el piloto, con intervalos firmados y datos disponibles."""
    rival = _car_by_index(inputs, rival_index)
    player = _car_by_index(inputs, inputs.car_index)
    if rival is None or rival_index == inputs.car_index:
        return CarComparison(
            rival_index,
            None,
            None,
            None,
            None,
            None,
            None,
            Quality.UNAVAILABLE,
            ("rival_unavailable",),
        )
    if not _active(rival):
        return CarComparison(
            rival_index,
            None,
            None,
            None,
            rival.actual_tyre_compound,
            rival.tyre_age_laps,
            rival.pit_stops,
            Quality.UNAVAILABLE,
            ("rival_retired",),
        )
    if player is None or not _active(player) or player.position is None or rival.position is None:
        return CarComparison(
            rival_index,
            None,
            None,
            None,
            rival.actual_tyre_compound,
            rival.tyre_age_laps,
            rival.pit_stops,
            Quality.INSUFFICIENT,
            ("position_unavailable",),
        )
    if player.position == rival.position:
        direction: Literal["ahead", "behind", "tied"] = "tied"
        interval = None
    elif rival.position < player.position:
        direction = "ahead"
        if player.delta_to_leader_ms is not None and rival.delta_to_leader_ms is not None:
            interval = -(player.delta_to_leader_ms - rival.delta_to_leader_ms) / 1000.0
        elif rival.position == 1 and player.delta_to_leader_ms is not None:
            interval = -player.delta_to_leader_ms / 1000.0
        elif rival.position + 1 == player.position and player.delta_to_car_in_front_ms is not None:
            interval = -player.delta_to_car_in_front_ms / 1000.0
        else:
            interval = None
    else:
        direction = "behind"
        if rival.delta_to_leader_ms is not None and player.delta_to_leader_ms is not None:
            interval = (rival.delta_to_leader_ms - player.delta_to_leader_ms) / 1000.0
        elif player.position == 1 and rival.delta_to_leader_ms is not None:
            interval = rival.delta_to_leader_ms / 1000.0
        elif rival.position == player.position + 1 and rival.delta_to_car_in_front_ms is not None:
            interval = rival.delta_to_car_in_front_ms / 1000.0
        else:
            interval = None

    player_laps = _current_stint_records(inputs, inputs.car_index)
    rival_laps = _current_stint_records(inputs, rival_index)
    pace_delta = (
        mean(record.lap_time_s for record in rival_laps[-3:] if record.lap_time_s is not None)
        - mean(record.lap_time_s for record in player_laps[-3:] if record.lap_time_s is not None)
        if player_laps and rival_laps
        else None
    )
    latest = inputs.history.records_for(rival_index)
    latest_record = latest[-1] if latest else None
    compound = rival.actual_tyre_compound or (latest_record.compound if latest_record else None)
    age = rival.tyre_age_laps
    if age is None and latest_record is not None:
        age = latest_record.tyre_age_end
    stops = rival.pit_stops
    if stops is None and latest:
        stops = sum(record.pit_entry for record in latest if record.closed)
    notes = ("position_tie",) if direction == "tied" else ()
    quality = (
        Quality.OK
        if interval is not None
        and pace_delta is not None
        and compound is not None
        and age is not None
        and stops is not None
        else Quality.LOW
    )
    stale, _ = _freshness(inputs)
    if stale:
        quality = Quality.STALE
    return CarComparison(
        rival_index,
        interval,
        direction,
        pace_delta,
        compound,
        age,
        stops,
        quality,
        notes,
    )


GapModule = GapMetrics
