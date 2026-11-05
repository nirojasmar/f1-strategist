"""Invariantes y tolerancias de las métricas sobre una captura real."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from f1_strategist.metrics.engine import MetricsEngine, MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.offline import OfflineResult, run_capture_offline
from f1_strategist.parser import (
    FinalClassification,
    FinalClassificationPacket,
    ParsedPacket,
    SessionHistoryPacket,
)
from f1_strategist.race_state import RaceState, RaceStateSnapshot

pytestmark = pytest.mark.real_capture

_FIXTURE = Path(__file__).parent / "fixtures" / "real" / "monza-13-laps.f1cap.gz"
_OFFICIAL_TIME_TOLERANCE_S = 1e-9
_RACE_TIME_TOLERANCE_S = 0.05
_FUEL_TOLERANCE_KG = 0.05
_WEAR_TOLERANCE_PCT = 0.01
_LAPS_REMAINING_TOLERANCE = 0.05
_PACE_TREND_JUMP_S_PER_LAP = 0.5
_TYRE_TREND_JUMP_PCT_PER_LAP = 0.1
_UNUSABLE_FLAGS = frozenset({"incomplete", "partial_lap", "retired"})


@dataclass(frozen=True, slots=True)
class _Replay:
    offline: OfflineResult
    state: RaceStateSnapshot
    snapshot: MetricsSnapshot
    history: LapHistorySnapshot
    classification: FinalClassificationPacket
    official: dict[tuple[int, int], tuple[int, int, int, int, int]]
    trend_jumps: tuple[tuple[str, float, int], ...]


@dataclass
class _Collector:
    state: RaceState = field(default_factory=RaceState)
    engine: MetricsEngine = field(default_factory=MetricsEngine)
    classification: FinalClassificationPacket | None = None
    official: dict[tuple[int, int], tuple[int, int, int, int, int]] = field(default_factory=dict)
    trend_jumps: list[tuple[str, float, int]] = field(default_factory=list)
    last_state: RaceStateSnapshot | None = None
    last_observed_at: float | None = None
    _last_snapshot: MetricsSnapshot | None = None
    _last_trends: dict[str, float] = field(default_factory=dict)

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        state = self.state.snapshot()
        self.engine.apply(packet, observed_at, state)
        self.last_state = state
        self.last_observed_at = observed_at
        if isinstance(packet, FinalClassificationPacket):
            self.classification = packet
        if isinstance(packet, SessionHistoryPacket):
            self._record_official_times(packet)
        self._record_trend_jumps(state)

    def _record_official_times(self, packet: SessionHistoryPacket) -> None:
        for lap_number, lap in enumerate(packet.lap_history, start=1):
            if lap.lap_time_ms > 0:
                self.official[(packet.car_index, lap_number)] = (
                    lap.lap_time_ms,
                    lap.sector1_time_ms,
                    lap.sector2_time_ms,
                    lap.sector3_time_ms,
                    lap.valid_bit_flags,
                )

    def _record_trend_jumps(self, state: RaceStateSnapshot) -> None:
        snapshot = self.engine.snapshot()
        if snapshot is self._last_snapshot:
            return
        self._last_snapshot = snapshot
        lap_number = state.current_lap or 0
        for key in ("pace.trend_s_per_lap", "tyres.wear_rate_pct_per_lap"):
            metric = snapshot.get(key)
            value = None if metric is None else metric.value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            previous = self._last_trends.get(key)
            if previous is not None:
                self.trend_jumps.append((key, abs(value - previous), lap_number))
            self._last_trends[key] = float(value)


def _capture_path() -> Path:
    override = os.environ.get("F1_CAPTURE_FIXTURE")
    path = Path(override) if override else _FIXTURE
    if not path.is_file():
        pytest.skip(f"No existe la captura real: {path}")
    return path


@pytest.fixture(scope="module")
def replay() -> _Replay:
    collector = _Collector()
    offline = run_capture_offline(_capture_path(), [collector])
    if collector.classification is None or collector.last_state is None:
        pytest.fail("La captura no contiene FinalClassification o estado final")
    if collector.last_observed_at is not None:
        collector.engine.finalize(collector.last_state, collector.last_observed_at)
    return _Replay(
        offline=offline,
        state=collector.last_state,
        snapshot=collector.engine.snapshot(),
        history=collector.engine.history.snapshot(),
        classification=collector.classification,
        official=collector.official,
        trend_jumps=tuple(collector.trend_jumps),
    )


def _classified_entries(replay: _Replay) -> list[tuple[int, FinalClassification]]:
    return [
        (car_index, entry)
        for car_index, entry in enumerate(replay.classification.classification)
        if entry is not None
    ]


def _records(replay: _Replay, car_index: int) -> tuple[LapRecord, ...]:
    return replay.history.records_for(car_index)


def _metric_value(replay: _Replay, key: str) -> object:
    metric = replay.snapshot.get(key)
    assert metric is not None, f"Falta la métrica {key}"
    return metric.value


def test_replay_is_complete_and_records_every_classified_lap(replay: _Replay) -> None:
    assert not replay.offline.truncated, "La captura real está truncada"
    assert not replay.offline.parser_rejections, (
        f"Rechazos del parser: {dict(replay.offline.parser_rejections)}"
    )
    assert replay.offline.packets_parsed > 0
    assert replay.state.session.total_laps == 13
    assert replay.classification.num_cars == 20

    for car_index, entry in _classified_entries(replay):
        records = _records(replay, car_index)
        assert len(records) == entry.laps, (
            f"coche {car_index}: se esperaban {entry.laps} vueltas y hay {len(records)}"
        )
        assert [record.lap_number for record in records] == list(range(1, entry.laps + 1)), (
            f"coche {car_index}: vueltas no consecutivas"
        )
        assert all(record.closed for record in records), (
            f"coche {car_index}: hay registros abiertos"
        )
        assert all(record.lap_time_source == "session_history" for record in records), (
            f"coche {car_index}: hay vueltas sin tiempo oficial"
        )
        assert all(record.lap_time_s is not None for record in records), (
            f"coche {car_index}: hay vueltas sin tiempo"
        )


def test_official_lap_and_sector_times_are_exact(replay: _Replay) -> None:
    assert replay.official, "No se ingirió ningún tiempo oficial"
    for (car_index, lap_number), official in replay.official.items():
        record = next(
            (
                item
                for item in replay.history.records_for(car_index)
                if item.lap_number == lap_number
            ),
            None,
        )
        assert record is not None, f"coche {car_index} vuelta {lap_number}: sin registro"
        lap_ms, sector1_ms, sector2_ms, sector3_ms, flags = official
        assert math.isclose(
            record.lap_time_s or 0.0,
            lap_ms / 1000.0,
            abs_tol=_OFFICIAL_TIME_TOLERANCE_S,
        ), (
            f"coche {car_index} vuelta {lap_number}: tiempo esperado "
            f"{lap_ms / 1000.0}, obtenido {record.lap_time_s}"
        )
        for name, value, flag, expected_ms in (
            ("sector1_s", record.sector1_s, 0b0010, sector1_ms),
            ("sector2_s", record.sector2_s, 0b0100, sector2_ms),
            ("sector3_s", record.sector3_s, 0b1000, sector3_ms),
        ):
            if flags & flag and expected_ms > 0:
                assert math.isclose(
                    value or 0.0,
                    expected_ms / 1000.0,
                    abs_tol=_OFFICIAL_TIME_TOLERANCE_S,
                ), (
                    f"coche {car_index} vuelta {lap_number}: {name} esperado "
                    f"{expected_ms / 1000.0}, obtenido {value}"
                )
            else:
                assert value is None, (
                    f"coche {car_index} vuelta {lap_number}: {name} debería ser None"
                )
        assert record.valid == bool(flags & 0b0001), (
            f"coche {car_index} vuelta {lap_number}: validez esperada "
            f"{bool(flags & 0b0001)}, obtenida {record.valid}"
        )
        assert not (record.flags & _UNUSABLE_FLAGS), (
            f"coche {car_index} vuelta {lap_number}: banderas inesperadas "
            f"{sorted(record.flags & _UNUSABLE_FLAGS)}"
        )


def test_best_lap_and_race_time_match_classification(replay: _Replay) -> None:
    for car_index, entry in _classified_entries(replay):
        records = _records(replay, car_index)
        best_lap_s = min(record.lap_time_s for record in records if record.lap_time_s)
        assert round(best_lap_s * 1000) == entry.best_lap_time_ms, (
            f"coche {car_index}: mejor vuelta esperada {entry.best_lap_time_ms} ms, "
            f"obtenida {best_lap_s * 1000:.3f} ms"
        )
        race_time_s = sum(record.lap_time_s or 0.0 for record in records)
        assert abs(race_time_s - entry.total_race_time) <= _RACE_TIME_TOLERANCE_S, (
            f"coche {car_index}: suma de vueltas {race_time_s:.3f} s frente a "
            f"{entry.total_race_time:.3f} s del juego"
        )


def test_pit_counts_and_tyre_age_progression(replay: _Replay) -> None:
    for car_index, entry in _classified_entries(replay):
        records = _records(replay, car_index)
        pit_entries = sum(record.pit_entry for record in records)
        pit_exits = sum(record.pit_exit for record in records)
        assert pit_entries == entry.pit_stops, (
            f"coche {car_index}: paradas esperadas {entry.pit_stops}, entradas {pit_entries}"
        )
        assert pit_exits == entry.pit_stops, (
            f"coche {car_index}: paradas esperadas {entry.pit_stops}, salidas {pit_exits}"
        )
        assert records[0].tyre_age_start == 0, (
            f"coche {car_index}: la primera vuelta debe empezar con edad 0"
        )
        for previous, record in zip(records, records[1:], strict=False):
            assert previous.tyre_age_end is not None
            assert record.tyre_age_start is not None
            assert record.tyre_age_end is not None
            expected_start = 0 if record.pit_exit else previous.tyre_age_end
            assert record.tyre_age_start == expected_start, (
                f"coche {car_index} vuelta {record.lap_number}: edad inicial esperada "
                f"{expected_start}, obtenida {record.tyre_age_start}"
            )
            assert record.tyre_age_end == record.tyre_age_start + 1, (
                f"coche {car_index} vuelta {record.lap_number}: la edad debe subir una "
                f"vuelta ({record.tyre_age_start} -> {record.tyre_age_end})"
            )


def test_fuel_and_wear_are_monotonic_within_stints(replay: _Replay) -> None:
    for car_index, _ in _classified_entries(replay):
        records = _records(replay, car_index)
        for previous, record in zip(records, records[1:], strict=False):
            if previous.fuel_end_kg is not None and record.fuel_start_kg is not None:
                assert record.fuel_start_kg <= previous.fuel_end_kg + _FUEL_TOLERANCE_KG, (
                    f"coche {car_index} vuelta {record.lap_number}: combustible sube de "
                    f"{previous.fuel_end_kg} a {record.fuel_start_kg} kg"
                )
            if (
                previous.tyre_wear_end_pct is None
                or record.tyre_wear_start_pct is None
                or record.pit_exit
            ):
                continue
            for wheel, (before, after) in enumerate(
                zip(previous.tyre_wear_end_pct, record.tyre_wear_start_pct, strict=False)
            ):
                assert after >= before - _WEAR_TOLERANCE_PCT, (
                    f"coche {car_index} vuelta {record.lap_number} rueda {wheel}: "
                    f"desgaste baja de {before} a {after} %"
                )
        for record in records:
            if record.fuel_start_kg is not None and record.fuel_end_kg is not None:
                assert record.fuel_end_kg <= record.fuel_start_kg + _FUEL_TOLERANCE_KG, (
                    f"coche {car_index} vuelta {record.lap_number}: combustible no baja "
                    f"dentro de la vuelta ({record.fuel_start_kg} -> {record.fuel_end_kg})"
                )
            if record.tyre_wear_start_pct is None or record.tyre_wear_end_pct is None:
                continue
            for wheel, (before, after) in enumerate(
                zip(record.tyre_wear_start_pct, record.tyre_wear_end_pct, strict=False)
            ):
                assert after >= before - _WEAR_TOLERANCE_PCT, (
                    f"coche {car_index} vuelta {record.lap_number} rueda {wheel}: "
                    f"desgaste baja dentro de la vuelta ({before} -> {after} %)"
                )


def test_laps_remaining_and_intervals_follow_the_state(replay: _Replay) -> None:
    player_index = replay.state.player_car_index
    assert player_index is not None
    player = replay.state.cars[player_index]
    total_laps = replay.state.session.total_laps
    track_length = replay.state.session.track_length_m
    assert total_laps is not None and track_length
    if replay.state.session.ended:
        expected_remaining = 0.0
    else:
        distance_fraction = min(max((player.lap_distance_m or 0.0) / track_length, 0.0), 1.0)
        expected_remaining = max(
            0.0,
            total_laps - (player.current_lap or 1) + 1 - distance_fraction,
        )
    remaining = _metric_value(replay, "progress.laps_remaining")
    assert isinstance(remaining, (int, float))
    assert abs(remaining - expected_remaining) <= _LAPS_REMAINING_TOLERANCE, (
        f"progress.laps_remaining esperado {expected_remaining}, obtenido {remaining}"
    )

    if player.delta_to_car_in_front_ms is not None:
        ahead = _metric_value(replay, "gap.ahead_s")
        assert isinstance(ahead, (int, float))
        assert math.isclose(ahead, player.delta_to_car_in_front_ms / 1000.0, abs_tol=1e-9), (
            f"gap.ahead_s esperado {player.delta_to_car_in_front_ms / 1000.0}, obtenido {ahead}"
        )
    expected_leader = 0.0 if player.position == 1 else (player.delta_to_leader_ms or 0) / 1000.0
    leader = _metric_value(replay, "gap.leader_s")
    assert isinstance(leader, (int, float))
    assert math.isclose(leader, expected_leader, abs_tol=1e-9), (
        f"gap.leader_s esperado {expected_leader}, obtenido {leader}"
    )


def test_fuel_consumption_matches_independent_window(replay: _Replay) -> None:
    # El juego publica fuel_remaining_laps con otra semántica en esta captura:
    # ronda 1.5 vueltas durante toda la carrera aunque el depósito rinde ~12.
    # Por eso F3 valida el consumo contra la diferencia de combustible observada,
    # que es la referencia independiente fiable de la sección 8.
    player_index = replay.state.player_car_index
    assert player_index is not None
    records = [
        record
        for record in _records(replay, player_index)
        if record.fuel_start_kg is not None
        and record.fuel_end_kg is not None
        and record.closed
        and not (record.flags & _UNUSABLE_FLAGS)
        and record.fuel_start_kg >= record.fuel_end_kg
    ]
    assert records
    window = records[-5:]
    consumptions: list[float] = []
    for record in window:
        start = record.fuel_start_kg
        end = record.fuel_end_kg
        assert start is not None and end is not None
        consumptions.append(start - end)
    expected = sum(consumptions) / len(consumptions)
    consumption = _metric_value(replay, "fuel.consumption_per_lap")
    assert isinstance(consumption, (int, float))
    assert abs(consumption - expected) <= _FUEL_TOLERANCE_KG, (
        f"fuel.consumption_per_lap esperado {expected:.5f} kg/vuelta, "
        f"obtenido {consumption:.5f} kg/vuelta"
    )


def test_trends_have_no_unjustified_jumps(replay: _Replay) -> None:
    thresholds = {
        "pace.trend_s_per_lap": _PACE_TREND_JUMP_S_PER_LAP,
        "tyres.wear_rate_pct_per_lap": _TYRE_TREND_JUMP_PCT_PER_LAP,
    }
    for metric, jump, lap_number in replay.trend_jumps:
        threshold = thresholds.get(metric)
        if threshold is None:
            continue
        assert jump <= threshold, (
            f"{metric} salta {jump:.5f} en la vuelta {lap_number}; máximo permitido {threshold}"
        )


def test_finished_laps_are_not_marked_as_flashback(replay: _Replay) -> None:
    for car_index, entry in _classified_entries(replay):
        if entry.result_status.name != "finished":
            continue
        records = _records(replay, car_index)
        last = records[-1]
        assert "flashback" not in last.flags, (
            f"coche {car_index} vuelta {last.lap_number}: la última vuelta terminada "
            "no debe marcarse como flashback"
        )
