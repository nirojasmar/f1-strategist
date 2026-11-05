"""Invariantes y tolerancias de las métricas sobre una captura real."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from f1_strategist.metrics.engine import MetricsEngine, MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.metrics.types import Quality
from f1_strategist.offline import OfflineResult, run_capture_offline
from f1_strategist.parser import (
    CarDamagePacket,
    CarStatusPacket,
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
    trend_jumps: tuple[tuple[str, float, int, tuple[str, ...]], ...]
    publications: tuple[tuple[RaceStateSnapshot, MetricsSnapshot, LapHistorySnapshot], ...]
    raw_fuel_samples: dict[int, tuple[tuple[int, float, float, str | None, int], ...]]
    raw_wear_samples: dict[int, tuple[tuple[int, tuple[float, ...]], ...]]


@dataclass
class _Collector:
    state: RaceState = field(default_factory=RaceState)
    engine: MetricsEngine = field(default_factory=MetricsEngine)
    classification: FinalClassificationPacket | None = None
    official: dict[tuple[int, int], tuple[int, int, int, int, int]] = field(default_factory=dict)
    trend_jumps: list[tuple[str, float, int, tuple[str, ...]]] = field(default_factory=list)
    publications: list[tuple[RaceStateSnapshot, MetricsSnapshot, LapHistorySnapshot]] = field(
        default_factory=list
    )
    raw_fuel_samples: dict[int, list[tuple[int, float, float, str | None, int]]] = field(
        default_factory=dict
    )
    raw_wear_samples: dict[int, list[tuple[int, tuple[float, ...]]]] = field(default_factory=dict)
    last_state: RaceStateSnapshot | None = None
    last_observed_at: float | None = None
    _last_snapshot: MetricsSnapshot | None = None
    _last_trends: dict[str, tuple[float, RaceStateSnapshot]] = field(default_factory=dict)

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        state = self.state.snapshot()
        self._record_raw_samples(packet, state)
        self.engine.apply(packet, observed_at, state)
        self.last_state = state
        self.last_observed_at = observed_at
        if isinstance(packet, FinalClassificationPacket):
            self.classification = packet
        if isinstance(packet, SessionHistoryPacket):
            self._record_official_times(packet)
        self._record_trend_jumps(state)

    def _record_raw_samples(self, packet: ParsedPacket, state: RaceStateSnapshot) -> None:
        if isinstance(packet, CarStatusPacket):
            for car_index, status in enumerate(packet.cars):
                car = state.cars[car_index]
                if car.current_lap is None or not math.isfinite(status.fuel_in_tank):
                    continue
                self.raw_fuel_samples.setdefault(car_index, []).append(
                    (
                        car.current_lap,
                        status.fuel_in_tank,
                        status.fuel_remaining_laps,
                        status.actual_tyre_compound.name,
                        status.tyres_age_laps,
                    )
                )
        elif isinstance(packet, CarDamagePacket):
            for car_index, damage in enumerate(packet.cars):
                lap_number = state.cars[car_index].current_lap
                if lap_number is not None and len(damage.tyres_wear) == 4:
                    self.raw_wear_samples.setdefault(car_index, []).append(
                        (lap_number, damage.tyres_wear)
                    )

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
        self.publications.append((state, snapshot, self.engine.history.snapshot()))
        lap_number = state.current_lap or 0
        for key in (
            "pace.trend_s_per_lap",
            "tyres.wear_rate_pct_per_lap",
            "gap.ahead_trend_s_per_s",
            "gap.behind_trend_s_per_s",
        ):
            metric = snapshot.get(key)
            value = None if metric is None else metric.value
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or metric is None
                or metric.quality not in {Quality.OK, Quality.LOW}
            ):
                self._last_trends.pop(key, None)
                continue
            previous = self._last_trends.get(key)
            if previous is not None:
                causes = _trend_causes(previous[1], state)
                self.trend_jumps.append((key, abs(value - previous[0]), lap_number, causes))
            self._last_trends[key] = (float(value), state)


def _capture_path() -> Path:
    override = os.environ.get("F1_CAPTURE_FIXTURE")
    if override is not None:
        path = Path(override)
        if not path.is_file():
            pytest.fail(f"F1_CAPTURE_FIXTURE no existe o no es un archivo: {path}")
        return path
    path = _FIXTURE
    if not path.is_file():
        pytest.skip(f"No existe la captura real: {path}")
    return path


def test_invalid_explicit_fixture_path_fails(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing.f1cap.gz"
    monkeypatch.setenv("F1_CAPTURE_FIXTURE", str(missing))

    with pytest.raises(pytest.fail.Exception, match="F1_CAPTURE_FIXTURE"):
        _capture_path()


def test_missing_optional_fixture_is_skipped(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("F1_CAPTURE_FIXTURE", raising=False)
    missing = tmp_path / "optional.f1cap.gz"
    monkeypatch.setattr("tests.test_metrics_real_capture._FIXTURE", missing)

    with pytest.raises(pytest.skip.Exception, match="No existe la captura real"):
        _capture_path()


def _trend_causes(previous: RaceStateSnapshot, current: RaceStateSnapshot) -> tuple[str, ...]:
    causes: set[str] = set()
    before = (
        previous.cars[previous.player_car_index] if previous.player_car_index is not None else None
    )
    after = current.cars[current.player_car_index] if current.player_car_index is not None else None
    if before is not None and after is not None:
        if before.pit_stops != after.pit_stops or before.pit_status != after.pit_status:
            causes.add("pit_stop")
        if before.actual_tyre_compound != after.actual_tyre_compound:
            causes.add("tyre_change")
        if (
            before.tyre_age_laps is not None
            and after.tyre_age_laps is not None
            and after.tyre_age_laps < before.tyre_age_laps
        ):
            causes.add("tyre_change")
        if before.damage != after.damage:
            causes.add("damage")
    if (
        previous.current_lap is not None
        and current.current_lap is not None
        and current.current_lap < previous.current_lap
    ):
        causes.add("flashback")
    if previous.session.safety_car_status != current.session.safety_car_status:
        causes.add("safety_car")
    if previous.session.weather != current.session.weather:
        causes.add("weather")
    if _neighbor_indices(previous) != _neighbor_indices(current):
        causes.add("rival_change")
    return tuple(sorted(causes))


def _neighbor_indices(state: RaceStateSnapshot) -> tuple[int | None, int | None]:
    if state.player_car_index is None:
        return None, None
    player = state.cars[state.player_car_index]
    if player.position is None:
        return None, None
    active = tuple(
        car
        for car in state.cars
        if car.position is not None and car.result_status not in {"retired", "inactive", "invalid"}
    )
    ahead = max(
        (car for car in active if car.position is not None and car.position < player.position),
        key=lambda car: car.position or 0,
        default=None,
    )
    behind = min(
        (car for car in active if car.position is not None and car.position > player.position),
        key=lambda car: car.position or 0,
        default=None,
    )
    return (
        ahead.car_index if ahead is not None else None,
        behind.car_index if behind is not None else None,
    )


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
        publications=tuple(collector.publications),
        raw_fuel_samples={
            car_index: tuple(samples) for car_index, samples in collector.raw_fuel_samples.items()
        },
        raw_wear_samples={
            car_index: tuple(samples) for car_index, samples in collector.raw_wear_samples.items()
        },
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
        valid_official_times = [
            record.lap_time_s
            for record in records
            if record.lap_time_s is not None
            and record.valid is True
            and record.lap_time_source == "session_history"
        ]
        assert valid_official_times, f"coche {car_index}: sin tiempos oficiales válidos"
        best_lap_s = min(valid_official_times)
        assert round(best_lap_s * 1000) == entry.best_lap_time_ms, (
            f"coche {car_index}: mejor vuelta esperada {entry.best_lap_time_ms} ms, "
            f"obtenida {best_lap_s * 1000:.3f} ms"
        )
        race_time_s = sum(record.lap_time_s or 0.0 for record in records)
        assert abs(race_time_s - entry.total_race_time) <= _RACE_TIME_TOLERANCE_S, (
            f"coche {car_index}: suma de vueltas {race_time_s:.3f} s frente a "
            f"{entry.total_race_time:.3f} s del juego"
        )
    player_index = replay.state.player_car_index
    assert player_index is not None
    player_entry = replay.classification.classification[player_index]
    assert player_entry is not None
    published_best = _metric_value(replay, "pace.best_lap_s")
    assert isinstance(published_best, (int, float))
    assert round(published_best * 1000) == player_entry.best_lap_time_ms, (
        f"pace.best_lap_s esperado {player_entry.best_lap_time_ms} ms, "
        f"obtenido {published_best * 1000:.3f} ms"
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


def test_published_progress_pits_gaps_and_fuel_deviation_match_state(replay: _Replay) -> None:
    checked = {
        key: 0
        for key in (
            "progress.laps_completed",
            "pit.stops_done",
            "progress.laps_remaining",
            "gap.ahead_s",
            "gap.leader_s",
            "fuel.game_deviation_laps",
            "pace.last_lap_s",
            "pace.best_lap_s",
        )
    }
    for state, snapshot, history in replay.publications:
        player_index = state.player_car_index
        if player_index is None:
            continue
        player = state.cars[player_index]

        for key, expected in (
            ("progress.laps_completed", len(history.records_for(player_index))),
            ("pit.stops_done", player.pit_stops),
        ):
            metric = snapshot.get(key)
            if metric is not None and metric.value is not None:
                assert metric.value == expected, (
                    f"{key} en vuelta {player.current_lap}: "
                    f"esperado {expected}, obtenido {metric.value}"
                )
                checked[key] += 1

        history_records = history.records_for(player_index)
        pace_records = [
            record
            for record in history_records
            if record.closed
            and record.lap_time_s is not None
            and not (record.flags & _UNUSABLE_FLAGS)
        ]
        if pace_records:
            last_lap = snapshot.get("pace.last_lap_s")
            last_lap_value = pace_records[-1].lap_time_s
            if (
                last_lap is not None
                and isinstance(last_lap.value, (int, float))
                and last_lap_value is not None
            ):
                assert math.isclose(last_lap.value, last_lap_value, abs_tol=1e-9)
                checked["pace.last_lap_s"] += 1
            valid_official_times = [
                record.lap_time_s
                for record in history_records
                if record.valid is True
                and record.lap_time_source == "session_history"
                and record.lap_time_s is not None
            ]
            best_lap = snapshot.get("pace.best_lap_s")
            if (
                valid_official_times
                and best_lap is not None
                and isinstance(best_lap.value, (int, float))
            ):
                assert math.isclose(best_lap.value, min(valid_official_times), abs_tol=1e-9)
                checked["pace.best_lap_s"] += 1

        remaining = snapshot.get("progress.laps_remaining")
        if (
            remaining is not None
            and isinstance(remaining.value, (int, float))
            and state.session.total_laps is not None
            and state.session.track_length_m
        ):
            if state.session.ended:
                expected_remaining = 0.0
            else:
                fraction = min(
                    max((player.lap_distance_m or 0.0) / state.session.track_length_m, 0.0),
                    1.0,
                )
                expected_remaining = max(
                    0.0,
                    state.session.total_laps - (player.current_lap or 1) + 1 - fraction,
                )
            assert abs(remaining.value - expected_remaining) <= _LAPS_REMAINING_TOLERANCE, (
                f"progress.laps_remaining en vuelta {player.current_lap}: "
                f"esperado {expected_remaining}, obtenido {remaining.value}"
            )
            checked["progress.laps_remaining"] += 1

        if player.delta_to_car_in_front_ms is not None:
            ahead = snapshot.get("gap.ahead_s")
            if ahead is not None and isinstance(ahead.value, (int, float)):
                expected_ahead = player.delta_to_car_in_front_ms / 1000.0
                assert math.isclose(ahead.value, expected_ahead, abs_tol=1e-9)
                checked["gap.ahead_s"] += 1
        leader = snapshot.get("gap.leader_s")
        if leader is not None and isinstance(leader.value, (int, float)):
            expected_leader = (
                0.0 if player.position == 1 else (player.delta_to_leader_ms or 0) / 1000.0
            )
            assert math.isclose(leader.value, expected_leader, abs_tol=1e-9)
            checked["gap.leader_s"] += 1

        margin = snapshot.get("fuel.margin_laps_at_finish")
        game_laps = snapshot.get("fuel.game_remaining_laps")
        deviation = snapshot.get("fuel.game_deviation_laps")
        if margin is not None and game_laps is not None and deviation is not None:
            margin_value = margin.value
            game_laps_value = game_laps.value
            deviation_value = deviation.value
            if (
                isinstance(margin_value, (int, float))
                and isinstance(game_laps_value, (int, float))
                and isinstance(deviation_value, (int, float))
            ):
                expected_deviation = margin_value - game_laps_value
                assert math.isclose(deviation_value, expected_deviation, abs_tol=1e-9)
                checked["fuel.game_deviation_laps"] += 1

    assert all(count > 0 for count in checked.values()), f"sin publicaciones verificadas: {checked}"


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
    player_index = replay.state.player_car_index
    assert player_index is not None
    records_by_lap = {
        record.lap_number: record
        for record in _records(replay, player_index)
        if record.fuel_start_kg is not None
        and record.fuel_end_kg is not None
        and record.closed
        and not (record.flags & _UNUSABLE_FLAGS)
        and record.fuel_start_kg >= record.fuel_end_kg
    }
    stint_records = [
        record
        for record in _records(replay, player_index)
        if record.lap_number in records_by_lap and record.lap_number >= 7
    ]
    assert len(stint_records) >= 5, "se requieren cinco vueltas de combustible posteriores a boxes"
    window = stint_records[-5:]
    consumptions: list[float] = []
    for record in window:
        samples = [
            sample
            for sample in replay.raw_fuel_samples[player_index]
            if sample[0] == record.lap_number
        ]
        assert len(samples) >= 2, (
            f"coche {player_index} vuelta {record.lap_number}: "
            f"solo {len(samples)} muestras crudas de combustible"
        )
        start, end = samples[0][1], samples[-1][1]
        assert record.fuel_start_kg is not None and record.fuel_end_kg is not None
        assert abs(start - record.fuel_start_kg) <= _FUEL_TOLERANCE_KG
        assert abs(end - record.fuel_end_kg) <= _FUEL_TOLERANCE_KG
        consumptions.append(start - end)
    expected = sum(consumptions) / len(consumptions)
    consumption = _metric_value(replay, "fuel.consumption_per_lap")
    assert isinstance(consumption, (int, float))
    assert abs(consumption - expected) <= _FUEL_TOLERANCE_KG, (
        f"fuel.consumption_per_lap esperado {expected:.5f} kg/vuelta, "
        f"obtenido {consumption:.5f} kg/vuelta"
    )


def test_wear_endpoints_match_independent_raw_packets(replay: _Replay) -> None:
    player_index = replay.state.player_car_index
    assert player_index is not None
    records = _records(replay, player_index)
    checked_by_stint: dict[int, int] = {}
    stint_index = 0
    previous_compound: str | None = None
    for record in records:
        if (
            record.pit_exit
            or record.tyre_age_start == 0
            or (
                record.compound is not None
                and previous_compound is not None
                and record.compound != previous_compound
            )
        ):
            stint_index += 1
        if record.compound is not None:
            previous_compound = record.compound
        if record.tyre_wear_end_pct is None:
            continue
        samples = [
            sample
            for sample in replay.raw_wear_samples[player_index]
            if sample[0] == record.lap_number
        ]
        assert len(samples) >= 2, (
            f"coche {player_index} vuelta {record.lap_number}: "
            f"solo {len(samples)} muestras crudas de desgaste"
        )
        for wheel, (expected, observed) in enumerate(
            zip(samples[-1][1], record.tyre_wear_end_pct, strict=True)
        ):
            assert abs(expected - observed) <= _WEAR_TOLERANCE_PCT, (
                f"coche {player_index} vuelta {record.lap_number} rueda {wheel}: "
                f"extremo crudo {expected}, registro {observed}"
            )
        checked_by_stint[stint_index] = checked_by_stint.get(stint_index, 0) + 1
    assert len(checked_by_stint) >= 2, (
        f"cobertura independiente de desgaste insuficiente por stint: {checked_by_stint}"
    )
    assert all(count >= 2 for count in checked_by_stint.values()), (
        f"menos de dos extremos crudos de desgaste por stint: {checked_by_stint}"
    )


def test_fuel_mfd_deviation_is_not_claimed_within_tolerance(replay: _Replay) -> None:
    deviations = [
        abs(metric.value)
        for _, snapshot, _ in replay.publications
        if (metric := snapshot.get("fuel.game_deviation_laps")) is not None
        and isinstance(metric.value, (int, float))
        and metric.quality in {Quality.OK, Quality.LOW}
    ]
    assert deviations, "no hay publicaciones comparables con el indicador MFD"
    # La semántica exacta de fuel_remaining_laps no está definida por la captura.
    # F3 verifica la fórmula, no la tolerancia de ±0,3 vueltas.
    assert max(deviations) > 0.3


def test_trends_have_no_unjustified_jumps(replay: _Replay) -> None:
    thresholds = {
        "pace.trend_s_per_lap": _PACE_TREND_JUMP_S_PER_LAP,
        "tyres.wear_rate_pct_per_lap": _TYRE_TREND_JUMP_PCT_PER_LAP,
    }
    sampled_metrics = {metric for metric, _, _, _ in replay.trend_jumps}
    assert set(thresholds) <= sampled_metrics, (
        f"faltan muestras de tendencia: {sorted(set(thresholds) - sampled_metrics)}"
    )
    gap_metrics = {"gap.ahead_trend_s_per_s", "gap.behind_trend_s_per_s"}
    assert gap_metrics <= sampled_metrics, (
        f"faltan muestras de tendencia de intervalo: {sorted(gap_metrics - sampled_metrics)}"
    )
    assert any(metric in gap_metrics and causes for metric, _, _, causes in replay.trend_jumps), (
        "no se atribuyeron causas a los saltos de tendencias de intervalo"
    )
    for metric, jump, lap_number, causes in replay.trend_jumps:
        threshold = thresholds.get(metric)
        if threshold is None or causes:
            continue
        assert jump <= threshold, (
            f"{metric} salta {jump:.5f} en la vuelta {lap_number}; máximo permitido {threshold}"
        )


def test_trend_causes_identify_rival_changes(replay: _Replay) -> None:
    state = replay.state
    player_index = state.player_car_index
    assert player_index is not None
    without_opponents = replace(
        state,
        cars=tuple(
            car if index == player_index else replace(car, position=None)
            for index, car in enumerate(state.cars)
        ),
    )
    assert "rival_change" in _trend_causes(state, without_opponents)


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
