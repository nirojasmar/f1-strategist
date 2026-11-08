from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.history import LapHistory, LapRecord, TyreStint
from f1_strategist.metrics.history_ingest import ingest_session_history
from f1_strategist.metrics.lap_tracker import LapTracker
from f1_strategist.parser import (
    EnumValue,
    SessionHistoryPacket,
    TyreStintHistory,
    parse_packet,
)
from f1_strategist.race_state import RaceState
from tests.scenarios import ScenarioCar, build_race_timed


def _car() -> ScenarioCar:
    return ScenarioCar(
        index=0,
        name="Driver",
        grid_position=1,
        base_lap_time_s=90.0,
        initial_fuel_kg=50.0,
        fuel_per_lap_kg=2.0,
        tyre_wear_per_lap_pct=1.0,
    )


def test_official_times_replace_lap_data_after_lap_closes() -> None:
    race = RaceState()
    tracker = LapTracker(config=MetricsConfig())
    packets = build_race_timed((_car(),), 3)

    for packet, now in packets:
        race.apply(packet, now)
        tracker.observe(race.snapshot(), now)
        if isinstance(packet, SessionHistoryPacket):
            ingest_session_history(packet, tracker.history)

    records = tracker.history.snapshot().records_for(0)
    assert [record.lap_number for record in records] == [1, 2, 3]
    assert [record.lap_time_s for record in records] == [90.0, 90.0, 90.0]
    assert all(record.lap_time_source == "session_history" for record in records)
    assert [(record.sector1_s, record.sector2_s, record.sector3_s) for record in records] == [
        (27.0, 31.5, 31.5),
        (27.0, 31.5, 31.5),
        (27.0, 31.5, 31.5),
    ]


def test_early_official_time_is_applied_when_c2_creates_the_record() -> None:
    history = LapHistory()
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 2)
        if isinstance(packet, SessionHistoryPacket)
    )

    ingest_session_history(packet, history)

    assert history.get(0, 1) is None
    assert history.snapshot().records_for(0) == ()

    history.upsert(
        LapRecord(car_index=0, lap_number=1, lap_time_s=91.0, lap_time_source="lap_data")
    )
    record = history.get(0, 1)
    assert record is not None
    assert record.lap_time_s == 90.0
    assert record.lap_time_source == "session_history"
    assert record.valid is True


def test_invalid_lap_flags_preserve_official_time_but_clear_invalid_sectors() -> None:
    race = RaceState()
    tracker = LapTracker(config=MetricsConfig())
    packets = build_race_timed((_car(),), 2, invalid_laps=frozenset({(0, 1)}))

    for packet, now in packets:
        race.apply(packet, now)
        tracker.observe(race.snapshot(), now)
        if isinstance(packet, SessionHistoryPacket):
            ingest_session_history(packet, tracker.history)

    record = tracker.history.get(0, 1)
    assert record is not None
    assert record.lap_time_s == 90.0
    assert record.valid is False
    assert (record.sector1_s, record.sector2_s, record.sector3_s) == (None, None, None)


def test_session_history_updates_an_existing_lap_and_duplicate_is_idempotent() -> None:
    history = LapHistory()
    history.reset(0xF125)
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 2)
        if isinstance(packet, SessionHistoryPacket) and packet.num_laps >= 1
    )
    history.upsert(
        LapRecord(car_index=0, lap_number=1, lap_time_s=95.0, lap_time_source="lap_data")
    )

    ingest_session_history(packet, history)
    first = history.get(0, 1)
    ingest_session_history(packet, history)

    assert history.get(0, 1) == first
    assert first is not None
    assert first.lap_time_s == 90.0
    assert first.lap_time_source == "session_history"


def test_zero_time_slot_does_not_create_a_future_lap() -> None:
    packet = parse_packet((Path(__file__).parent / "fixtures" / "session_history.bin").read_bytes())
    assert isinstance(packet, SessionHistoryPacket)
    packet = replace(packet, car_index=0)
    history = LapHistory()
    for lap_number in range(1, 5):
        history.upsert(LapRecord(car_index=0, lap_number=lap_number))

    ingest_session_history(packet, history)

    assert history.get(0, 5) is None
    assert [record.lap_number for record in history.snapshot().records_for(0)] == [1, 2, 3, 4]
    last_record = history.get(0, 4)
    assert last_record is not None
    assert last_record.lap_time_s == 84.461


def test_session_history_replaces_tyre_stints_for_one_and_two_compounds() -> None:
    packet_with_stop = next(
        packet
        for packet, _ in build_race_timed(
            (
                replace(
                    _car(),
                    pit_laps=(2,),
                    compounds=("soft", "medium"),
                ),
            ),
            4,
        )
        if isinstance(packet, SessionHistoryPacket) and packet.num_laps >= 3
    )
    history = LapHistory()
    history.reset(packet_with_stop.header.session_uid)

    ingest_session_history(packet_with_stop, history)

    assert history.snapshot().stints_for(0) == (
        TyreStint("soft", 1, 2, "soft"),
        TyreStint("medium", 3, None, "medium"),
    )


def test_session_history_ingests_a_single_tyre_stint() -> None:
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 2)
        if isinstance(packet, SessionHistoryPacket)
    )
    history = LapHistory()

    ingest_session_history(packet, history)

    assert history.snapshot().stints_for(0) == (TyreStint("soft", 1, None, "soft"),)


@pytest.mark.parametrize(
    ("ends", "expected_anomaly"),
    [
        ((None, 0), "open_stint_not_last"),
        ((0, 0), "invalid_stint_end_lap"),
        ((0, None), "invalid_stint_end_lap"),
        ((2, 0), "invalid_stint_end_lap"),
    ],
)
def test_invalid_stint_history_preserves_last_valid_stints_and_official_times(
    ends: tuple[int | None, int | None],
    expected_anomaly: str,
) -> None:
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 4)
        if isinstance(packet, SessionHistoryPacket) and packet.num_laps >= 1
    )
    history = LapHistory()
    history.reset(packet.header.session_uid)
    valid_stints = (TyreStint("soft", 1, None, "soft"),)
    history.replace_tyre_stints(0, valid_stints)
    history.upsert(
        LapRecord(car_index=0, lap_number=1, lap_time_s=91.0, lap_time_source="lap_data")
    )
    malformed = replace(
        packet,
        tyre_stint_history=tuple(
            TyreStintHistory(
                EnumValue(-1, "unknown"),
                EnumValue(-1, "unknown"),
                end_lap,
            )
            for end_lap in ends
        ),
    )

    anomaly = ingest_session_history(malformed, history)

    assert anomaly == expected_anomaly
    assert history.snapshot().stints_for(0) == valid_stints
    record = history.get(0, 1)
    assert record is not None
    assert record.lap_time_source == "session_history"


def test_valid_unknown_compounds_are_preserved_as_unavailable() -> None:
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 2)
        if isinstance(packet, SessionHistoryPacket) and packet.num_laps >= 1
    )
    packet = replace(
        packet,
        tyre_stint_history=(
            TyreStintHistory(
                EnumValue(-1, "unknown"),
                EnumValue(-1, "unknown"),
                None,
            ),
        ),
    )
    history = LapHistory()

    assert ingest_session_history(packet, history) is None
    assert history.snapshot().stints_for(0) == (TyreStint(None, 1, None, None),)


def test_flashback_discards_pending_times_from_later_laps() -> None:
    history = LapHistory()
    history.update_lap_timing(
        0,
        3,
        lap_time_s=90.0,
        sector1_s=30.0,
        sector2_s=30.0,
        sector3_s=30.0,
        valid=True,
    )

    history.discard_after(0, 1)
    history.upsert(
        LapRecord(car_index=0, lap_number=3, lap_time_s=92.0, lap_time_source="lap_data")
    )

    record = history.get(0, 3)
    assert record is not None
    assert record.lap_time_s == 92.0
    assert record.lap_time_source == "lap_data"


def test_stale_session_packet_is_ignored() -> None:
    history = LapHistory()
    history.reset(123)
    packet = next(
        packet
        for packet, _ in build_race_timed((_car(),), 2)
        if isinstance(packet, SessionHistoryPacket)
    )

    ingest_session_history(packet, history)

    assert history.snapshot().records_for(0) == ()
    assert history.snapshot().stints_for(0) == ()


def test_official_time_preserves_partial_telemetry_coverage_flags() -> None:
    history = LapHistory()
    history.reset(1)
    history.upsert(
        LapRecord(
            car_index=0,
            lap_number=3,
            lap_time_s=90.0,
            lap_time_source="lap_data",
            flags=frozenset({"incomplete", "partial_lap", "first_lap"}),
            closed=True,
        )
    )

    history.update_lap_timing(
        0,
        3,
        lap_time_s=70.0,
        sector1_s=None,
        sector2_s=None,
        sector3_s=None,
        valid=True,
    )

    record = history.get(0, 3)
    assert record is not None
    assert record.lap_time_s == 70.0
    assert record.flags == frozenset({"telemetry_partial", "first_lap"})
