from __future__ import annotations

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.comparability import classify_lap, is_clean
from f1_strategist.metrics.history import LapRecord


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (LapRecord(car_index=0, lap_number=1), frozenset({"first_lap"})),
        (LapRecord(car_index=0, lap_number=2, pit_entry=True), frozenset({"in_lap"})),
        (LapRecord(car_index=0, lap_number=2, pit_exit=True), frozenset({"out_lap"})),
        (LapRecord(car_index=0, lap_number=2, valid=False), frozenset({"invalid"})),
        (
            LapRecord(
                car_index=0,
                lap_number=2,
                track_status_seen=frozenset({"full_safety_car"}),
            ),
            frozenset({"safety_car"}),
        ),
        (
            LapRecord(
                car_index=0,
                lap_number=2,
                track_status_seen=frozenset({"virtual_safety_car"}),
            ),
            frozenset({"virtual_safety_car"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, track_status_seen=frozenset({"red_flag"})),
            frozenset({"red_flag"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, flags=frozenset({"flashback"})),
            frozenset({"flashback"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, flags=frozenset({"partial_lap"})),
            frozenset({"partial_lap"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, flags=frozenset({"incomplete"})),
            frozenset({"incomplete"}),
        ),
        (
            LapRecord(
                car_index=0,
                lap_number=2,
                weather_seen=frozenset({"clear", "light_rain"}),
            ),
            frozenset({"weather_change"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, damage_max_delta_pct=5.0),
            frozenset({"damage_change"}),
        ),
        (
            LapRecord(car_index=0, lap_number=2, traffic_fraction=0.5),
            frozenset({"traffic"}),
        ),
        (LapRecord(car_index=0, lap_number=2), frozenset()),
    ],
)
def test_classifies_each_comparability_flag(record: LapRecord, expected: frozenset[str]) -> None:
    assert classify_lap(record, MetricsConfig()) == expected


def test_classifies_combined_flags_without_order_dependence() -> None:
    first = LapRecord(
        car_index=0,
        lap_number=1,
        valid=False,
        pit_entry=True,
        pit_exit=True,
        track_status_seen=frozenset({"full_safety_car", "red_flag"}),
        weather_seen=frozenset({"clear", "storm"}),
        damage_max_delta_pct=10.0,
        traffic_fraction=0.8,
        flags=frozenset({"flashback"}),
    )
    second = LapRecord(
        car_index=0,
        lap_number=1,
        valid=False,
        pit_entry=True,
        pit_exit=True,
        track_status_seen=frozenset({"red_flag", "full_safety_car"}),
        weather_seen=frozenset({"storm", "clear"}),
        damage_max_delta_pct=10.0,
        traffic_fraction=0.8,
        flags=frozenset({"flashback"}),
    )
    expected = frozenset(
        {
            "first_lap",
            "in_lap",
            "out_lap",
            "invalid",
            "safety_car",
            "red_flag",
            "flashback",
            "weather_change",
            "damage_change",
            "traffic",
        }
    )

    assert classify_lap(first, MetricsConfig()) == expected
    assert classify_lap(second, MetricsConfig()) == expected


@pytest.mark.parametrize(
    ("damage_delta", "threshold", "expected"),
    [
        (0.0, 0.0, False),
        (0.01, 0.0, True),
        (4.99, 5.0, False),
        (5.0, 5.0, True),
        (5.01, 5.0, True),
    ],
)
def test_damage_threshold_is_inclusive(
    damage_delta: float,
    threshold: float,
    expected: bool,
) -> None:
    flags = classify_lap(
        LapRecord(car_index=0, lap_number=2, damage_max_delta_pct=damage_delta),
        MetricsConfig(damage_change_threshold_pct=threshold),
    )

    assert ("damage_change" in flags) is expected


@pytest.mark.parametrize(
    ("fraction", "threshold", "expected"),
    [
        (0.0, 0.0, False),
        (0.01, 0.0, True),
        (0.49, 0.5, False),
        (0.5, 0.5, True),
        (0.51, 0.5, True),
    ],
)
def test_traffic_threshold_is_inclusive(
    fraction: float,
    threshold: float,
    expected: bool,
) -> None:
    flags = classify_lap(
        LapRecord(car_index=0, lap_number=2, traffic_fraction=fraction),
        MetricsConfig(traffic_lap_fraction=threshold),
    )

    assert ("traffic" in flags) is expected


@pytest.mark.parametrize(
    ("flags", "allow_traffic", "expected"),
    [
        (frozenset(), False, True),
        (frozenset({"traffic"}), False, False),
        (frozenset({"traffic"}), True, True),
        (frozenset({"invalid", "traffic"}), True, False),
        (frozenset({"first_lap"}), True, False),
        (frozenset({"partial_lap"}), False, False),
        (frozenset({"incomplete"}), False, False),
    ],
)
def test_is_clean_only_allows_traffic_when_requested(
    flags: frozenset[str],
    allow_traffic: bool,
    expected: bool,
) -> None:
    assert is_clean(flags, allow_traffic=allow_traffic) is expected


@pytest.mark.parametrize("value", [None, 4.99])
def test_missing_or_subthreshold_damage_is_not_classified(value: float | None) -> None:
    flags = classify_lap(
        LapRecord(car_index=0, lap_number=2, damage_max_delta_pct=value),
        MetricsConfig(damage_change_threshold_pct=5.0),
    )

    assert "damage_change" not in flags


def test_missing_traffic_fraction_is_not_classified() -> None:
    flags = classify_lap(
        LapRecord(car_index=0, lap_number=2, traffic_fraction=None),
        MetricsConfig(),
    )

    assert "traffic" not in flags
