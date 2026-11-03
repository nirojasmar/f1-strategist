from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from f1_strategist.metrics import (
    Metric,
    Quality,
    Window,
    WindowKind,
    assess_quality,
    insufficient,
    unavailable,
)


@pytest.mark.parametrize(
    ("sample_count", "stale", "expected"),
    [
        (0, False, Quality.INSUFFICIENT),
        (2, False, Quality.INSUFFICIENT),
        (3, False, Quality.LOW),
        (4, False, Quality.LOW),
        (5, False, Quality.OK),
        (10, False, Quality.OK),
        (0, True, Quality.STALE),
        (4, True, Quality.STALE),
    ],
)
def test_assess_quality_thresholds(
    sample_count: int,
    stale: bool,
    expected: Quality,
) -> None:
    assert assess_quality(sample_count, ok_at=5, low_at=3, stale=stale) is expected


@pytest.mark.parametrize(
    ("sample_count", "ok_at", "low_at"),
    [(-1, 5, 3), (0, 0, 0), (0, 2, 3)],
)
def test_assess_quality_rejects_invalid_thresholds(
    sample_count: int,
    ok_at: int,
    low_at: int,
) -> None:
    with pytest.raises(ValueError):
        assess_quality(sample_count, ok_at=ok_at, low_at=low_at, stale=False)


def test_metric_is_immutable_and_freezes_sequence_fields() -> None:
    metric = Metric(
        key="fuel.in_tank_kg",
        value=12.5,
        unit="kg",
        window=Window("instant"),
        quality=Quality.OK,
        sample_count=1,
        computed_at_monotonic=10.0,
        sources=("car_status", "lap_data"),
        notes=("quality_note",),
    )

    assert metric.sources == ("car_status", "lap_data")
    assert metric.notes == ("quality_note",)
    with pytest.raises(FrozenInstanceError):
        metric.value = 10.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "quality"),
    [(unavailable, Quality.UNAVAILABLE), (insufficient, Quality.INSUFFICIENT)],
)
def test_missing_metric_helpers_set_value_quality_and_reason(
    factory: Callable[[str, str, str, float], Metric],
    quality: Quality,
) -> None:
    metric = factory("fuel.margin_kg", "kg", "metric_not_available", 12.5)

    assert metric.value is None
    assert metric.quality is quality
    assert metric.sample_count == 0
    assert metric.computed_at_monotonic == 12.5
    assert metric.window == Window("instant")
    assert metric.notes == ("metric_not_available",)


@pytest.mark.parametrize("quality", [Quality.INSUFFICIENT, Quality.UNAVAILABLE])
def test_missing_quality_cannot_have_a_value(quality: Quality) -> None:
    with pytest.raises(ValueError, match="value=None"):
        Metric(
            key="fuel.margin_kg",
            value=0.0,
            unit="kg",
            window=Window("instant"),
            quality=quality,
            sample_count=0,
            computed_at_monotonic=1.0,
        )


@pytest.mark.parametrize(
    ("kind", "size"),
    [
        ("instant", None),
        ("laps", 5),
        ("seconds", 10.0),
        ("samples", 3),
        ("stint", None),
        ("session", None),
    ],
)
def test_window_kinds(kind: WindowKind, size: int | float | None) -> None:
    assert Window(kind, size).kind == kind


@pytest.mark.parametrize(
    ("kind", "size"),
    [("unknown", None), ("laps", 0), ("seconds", float("inf")), ("samples", 1.5)],
)
def test_window_rejects_invalid_kind_or_size(kind: str, size: int | float | None) -> None:
    with pytest.raises(ValueError):
        Window(cast(WindowKind, kind), size)
