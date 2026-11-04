"""Métricas de paradas en boxes y estimación empírica de pérdida."""

from __future__ import annotations

from statistics import median

from ..race_state import PacketFreshness
from .comparability import classify_lap, is_clean
from .history import LapRecord, MetricInputs
from .types import Metric, Quality, Window, insufficient

_SAFETY_FLAGS = frozenset({"safety_car", "virtual_safety_car", "red_flag"})
# Field-wide evidence needs at least three observed stops to be considered representative.
_MIN_FIELD_STOPS = 3


def _freshness(inputs: MetricInputs) -> tuple[bool, tuple[str, ...]]:
    sources: list[str] = ["race_state", "lap_history"]
    stale = False
    for group in ("lap_data", "session"):
        try:
            freshness: PacketFreshness = inputs.state.freshness_for(group)
        except KeyError:
            continue
        stale = stale or freshness.is_stale(inputs.now)
        sources.append(group)
    return stale, tuple(sources)


def _stops_for(records: tuple[LapRecord, ...]) -> int:
    return sum(record.pit_entry for record in records)


def _stop_pairs(
    inputs: MetricInputs,
) -> tuple[tuple[LapRecord, LapRecord], ...]:
    pairs: list[tuple[LapRecord, LapRecord]] = []
    for car_index in inputs.history.records:
        records = tuple(record for record in inputs.history.records_for(car_index) if record.closed)
        entries = tuple(record for record in records if record.pit_entry)
        exits = tuple(record for record in records if record.pit_exit)
        for entry in entries:
            exit_record = next(
                (
                    record
                    for record in exits
                    if record.lap_number >= entry.lap_number
                    and not any(
                        entry.lap_number < other.lap_number < record.lap_number for other in entries
                    )
                ),
                None,
            )
            if exit_record is None:
                continue
            entry_flags = classify_lap(entry, inputs.config)
            exit_flags = classify_lap(exit_record, inputs.config)
            if _SAFETY_FLAGS.intersection(entry_flags | exit_flags):
                continue
            if entry.lap_time_s is None or exit_record.lap_time_s is None:
                continue
            pairs.append((entry, exit_record))
    return tuple(pairs)


def _loss_samples(inputs: MetricInputs) -> tuple[float, ...]:
    losses: list[float] = []
    for entry, exit_record in _stop_pairs(inputs):
        records = inputs.history.records_for(entry.car_index)
        reference = tuple(
            record.lap_time_s
            for record in records
            if record.closed
            and record.lap_time_s is not None
            and record.lap_number not in {entry.lap_number, exit_record.lap_number}
            and entry.lap_number - 3 <= record.lap_number <= exit_record.lap_number + 3
            and record.valid is not False
            and is_clean(classify_lap(record, inputs.config))
        )
        if not reference:
            continue
        entry_time = entry.lap_time_s
        exit_time = exit_record.lap_time_s
        if entry_time is None or exit_time is None:
            continue
        losses.append(entry_time + exit_time - 2 * median(reference))
    return tuple(losses)


def _metric(
    key: str,
    value: int | float | None,
    *,
    unit: str,
    now: float,
    stale: bool,
    sample_count: int,
    sources: tuple[str, ...],
    reason: str,
    window: Window | None = None,
    quality: Quality | None = None,
) -> Metric:
    if value is None:
        return insufficient(
            key,
            unit,
            reason,
            now,
            window=window or Window("session"),
            sample_count=sample_count,
            sources=sources,
        )
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=window or Window("session"),
        quality=Quality.STALE if stale else quality or Quality.OK,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
    )


class PitMetrics:
    """Calcula métricas de boxes a partir del estado actual y el histórico."""

    name = "pit"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        records = tuple(
            record for record in inputs.history.records_for(inputs.car_index) if record.closed
        )
        stale, sources = _freshness(inputs)
        last_entry = next(
            (record for record in reversed(records) if record.pit_entry),
            None,
        )
        if car is None:
            stops_done = None
        elif car.pit_stops is not None:
            stops_done = car.pit_stops
        else:
            stops_done = _stops_for(records) if records else None
        field_stop_count = sum(
            record.pit_entry
            for car_records in inputs.history.records.values()
            for record in car_records
            if record.closed
        )
        losses = _loss_samples(inputs)
        _, ok = inputs.config.thresholds("pit")
        sample_count_quality = (
            Quality.OK
            if len(losses) >= ok and field_stop_count >= _MIN_FIELD_STOPS
            else Quality.LOW
        )
        loss_quality = sample_count_quality if losses else None
        result = [
            _metric(
                "pit.stops_done",
                stops_done,
                unit="stops",
                now=inputs.now,
                stale=stale,
                sample_count=1 if stops_done is not None else 0,
                sources=sources,
                reason="pit_stop_count_unavailable",
                window=Window("session"),
            ),
            _metric(
                "pit.last_lane_time_s",
                last_entry.pit_lane_time_s if last_entry is not None else None,
                unit="s",
                now=inputs.now,
                stale=stale,
                sample_count=1 if last_entry and last_entry.pit_lane_time_s is not None else 0,
                sources=sources,
                reason="pit_lane_time_unavailable",
                window=Window("instant"),
            ),
            _metric(
                "pit.last_stationary_s",
                last_entry.pit_stop_time_s if last_entry is not None else None,
                unit="s",
                now=inputs.now,
                stale=stale,
                sample_count=1 if last_entry and last_entry.pit_stop_time_s is not None else 0,
                sources=sources,
                reason="stationary_time_unavailable",
                window=Window("instant"),
            ),
            _metric(
                "pit.loss_estimate_s",
                median(losses) if losses else None,
                unit="s",
                now=inputs.now,
                stale=stale,
                sample_count=len(losses),
                sources=sources,
                reason="no_comparable_pit_stops",
                quality=loss_quality,
            ),
            _metric(
                "pit.sample_count",
                len(losses),
                unit="stops",
                now=inputs.now,
                stale=stale,
                sample_count=len(losses),
                sources=sources,
                reason="no_comparable_pit_stops",
                quality=sample_count_quality,
            ),
        ]
        session = inputs.state.session
        for key, value, unit in (
            ("pit.window_ideal_lap", session.pit_window_ideal_lap, "lap"),
            ("pit.window_latest_lap", session.pit_window_latest_lap, "lap"),
            ("pit.rejoin_position", session.pit_rejoin_position, "position"),
        ):
            result.append(
                _metric(
                    key,
                    value,
                    unit=unit,
                    now=inputs.now,
                    stale=stale,
                    sample_count=1 if value is not None else 0,
                    sources=("race_state", "session"),
                    reason="game_pit_data_unavailable",
                    window=Window("instant"),
                )
            )
        return tuple(result)


PitModule = PitMetrics
