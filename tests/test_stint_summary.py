"""Pruebas del resumen de tandas (función pura de la Fase 3.5)."""

from __future__ import annotations

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.history import LapRecord, TyreStint
from f1_strategist.metrics.stint_summary import summarize_stints
from f1_strategist.metrics.types import Quality


def _lap(
    lap: int,
    *,
    compound: str = "soft",
    tyre_age_start: int | None = None,
    lap_time_s: float | None = None,
    fuel_start_kg: float | None = None,
    fuel_end_kg: float | None = None,
    wear_start: float | None = None,
    wear_end: float | None = None,
    weather: frozenset[str] = frozenset({"clear"}),
    track_temperature_c: float | None = 30.0,
    flags: frozenset[str] = frozenset(),
    pit_entry: bool = False,
    pit_exit: bool = False,
    closed: bool = True,
) -> LapRecord:
    age_start = tyre_age_start if tyre_age_start is not None else lap - 1
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=lap_time_s,
        compound=compound,
        tyre_age_start=age_start,
        tyre_age_end=age_start + 1,
        fuel_start_kg=fuel_start_kg,
        fuel_end_kg=fuel_end_kg,
        tyre_wear_start_pct=None if wear_start is None else (wear_start,) * 4,
        tyre_wear_end_pct=None if wear_end is None else (wear_end,) * 4,
        track_temperature_c=track_temperature_c,
        weather_seen=weather,
        flags=flags,
        pit_entry=pit_entry,
        pit_exit=pit_exit,
        closed=closed,
    )


def _green_stint(
    first_lap: int,
    last_lap: int,
    *,
    compound: str,
    base_time_s: float = 90.0,
    wear_per_lap: float = 0.5,
    fuel_per_lap: float = 1.5,
    fuel_kg: float = 100.0,
    out_lap: bool = False,
) -> list[LapRecord]:
    laps: list[LapRecord] = []
    for offset, lap_number in enumerate(range(first_lap, last_lap + 1)):
        laps.append(
            _lap(
                lap_number,
                compound=compound,
                tyre_age_start=offset,
                lap_time_s=base_time_s + 0.2 * offset,
                fuel_start_kg=fuel_kg - fuel_per_lap * offset,
                fuel_end_kg=fuel_kg - fuel_per_lap * (offset + 1),
                wear_start=wear_per_lap * offset,
                wear_end=wear_per_lap * (offset + 1),
                pit_exit=out_lap and offset == 0,
            )
        )
    return laps


def _two_compound_race() -> tuple[tuple[LapRecord, ...], tuple[TyreStint, ...]]:
    """Tanda blanda de 5 vueltas, parada, y tanda media de 5 vueltas."""
    soft = _green_stint(1, 5, compound="soft", base_time_s=90.0)
    # La vuelta 5 es la de entrada a boxes.
    soft[-1] = _lap(
        5,
        compound="soft",
        tyre_age_start=4,
        lap_time_s=92.5,
        fuel_start_kg=94.0,
        fuel_end_kg=92.5,
        wear_start=2.0,
        wear_end=2.5,
        pit_entry=True,
    )
    medium = _green_stint(
        6, 10, compound="medium", base_time_s=90.5, wear_per_lap=0.3, out_lap=True
    )
    stints = (
        TyreStint(compound="soft", visual_compound="soft", start_lap=1, end_lap=5),
        TyreStint(compound="medium", visual_compound="medium", start_lap=6, end_lap=10),
    )
    return tuple(soft + medium), stints


def test_two_compounds_and_a_pit_stop_produce_one_summary_per_stint() -> None:
    laps, stints = _two_compound_race()
    summaries = summarize_stints(laps, stints, MetricsConfig())

    assert len(summaries) == 2
    soft, medium = summaries
    assert (soft.compound, medium.compound) == ("soft", "medium")
    assert (soft.start_lap, soft.end_lap) == (1, 5)
    assert (medium.start_lap, medium.end_lap) == (6, 10)
    assert (soft.laps, medium.laps) == (5, 5)
    # Primera vuelta e «in lap» fuera en la blanda; «out lap» fuera en la media.
    assert (soft.clean_laps, medium.clean_laps) == (3, 4)
    assert soft.representative is False
    assert medium.representative is True
    assert soft.quality is Quality.LOW
    assert soft.tyre_age_start == 0
    assert soft.tyre_age_end == 5
    assert soft.fuel_start_kg == pytest.approx(100.0)
    assert soft.fuel_end_kg == pytest.approx(92.5)
    assert soft.wear_start_pct == pytest.approx(0.0)
    assert soft.wear_end_pct == pytest.approx(2.5)
    assert soft.pace_mean_s == pytest.approx((90.2 + 90.4 + 90.6) / 3)
    assert soft.wear_rate_pct_per_lap == pytest.approx(0.5)
    assert soft.pace_trend_s_per_lap == pytest.approx(0.2)
    assert soft.track_temperature_mean_c == pytest.approx(30.0)
    assert soft.weather == ("clear",)


def test_in_lap_and_out_lap_are_excluded_from_pace_and_wear() -> None:
    laps, stints = _two_compound_race()
    summaries = summarize_stints(laps, stints, MetricsConfig())

    # La vuelta de entrada (92.5 s) y la de salida no contaminan el ritmo limpio.
    assert summaries[0].pace_mean_s is not None
    assert summaries[0].pace_mean_s < 91.0
    assert summaries[1].pace_mean_s == pytest.approx((90.7 + 90.9 + 91.1 + 91.3) / 4)


@pytest.mark.parametrize(
    "flag",
    ["first_lap", "invalid", "safety_car", "virtual_safety_car", "red_flag", "traffic"],
)
def test_each_comparability_flag_removes_the_lap(flag: str) -> None:
    laps = _green_stint(1, 5, compound="soft")
    if flag == "red_flag":
        laps[2] = _lap(
            3,
            tyre_age_start=2,
            lap_time_s=90.4,
            fuel_start_kg=97.0,
            fuel_end_kg=95.5,
            wear_start=1.0,
            wear_end=1.5,
            weather=frozenset({"clear"}),
            track_temperature_c=30.0,
        )
        from dataclasses import replace

        laps[2] = replace(laps[2], track_status_seen=frozenset({"red_flag"}))
    elif flag == "invalid":
        from dataclasses import replace

        laps[2] = replace(laps[2], valid=False)
    elif flag == "traffic":
        from dataclasses import replace

        laps[2] = replace(laps[2], traffic_fraction=0.9)
    elif flag == "first_lap":
        pass
    else:
        from dataclasses import replace

        laps[2] = replace(laps[2], track_status_seen=frozenset({flag}))

    summaries = summarize_stints(
        laps, (TyreStint(compound="soft", start_lap=1, end_lap=5),), MetricsConfig()
    )
    assert len(summaries) == 1
    # La vuelta 1 siempre se excluye; además se excluye la vuelta con la bandera.
    expected = 4 if flag == "first_lap" else 3
    assert summaries[0].clean_laps == expected


def test_a_short_stint_without_clean_laps_is_insufficient() -> None:
    laps = (
        _lap(1, tyre_age_start=0, lap_time_s=91.0, fuel_end_kg=99.0, pit_exit=True),
        _lap(
            2,
            lap_time_s=88.0,
            fuel_start_kg=97.5,
            flags=frozenset({"incomplete"}),
        ),
    )
    summaries = summarize_stints(
        laps, (TyreStint(compound="soft", start_lap=1, end_lap=2),), MetricsConfig()
    )

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.clean_laps == 0
    assert summary.quality is Quality.INSUFFICIENT
    assert summary.representative is False
    assert summary.pace_mean_s is None
    assert summary.wear_rate_pct_per_lap is None
    # El rango de combustible se conserva aunque no haya vueltas limpias.
    assert summary.fuel_start_kg is not None
    assert summary.fuel_end_kg is not None


def test_the_fuel_range_is_always_recorded_even_without_some_extremes() -> None:
    laps = (
        _lap(1, tyre_age_start=0, fuel_end_kg=99.0),
        _lap(2, tyre_age_start=1, fuel_start_kg=97.5),
    )
    summaries = summarize_stints(
        laps, (TyreStint(compound="soft", start_lap=1, end_lap=2),), MetricsConfig()
    )

    assert len(summaries) == 1
    assert summaries[0].fuel_start_kg == pytest.approx(99.0)
    assert summaries[0].fuel_end_kg == pytest.approx(97.5)


def test_pace_correction_is_only_reported_when_configured() -> None:
    laps, stints = _two_compound_race()

    raw_only = summarize_stints(laps, stints, MetricsConfig())
    assert raw_only[0].pace_mean_fuel_corrected_s is None
    assert raw_only[0].pace_trend_fuel_corrected_s_per_lap is None

    corrected = summarize_stints(laps, stints, MetricsConfig(fuel_effect_s_per_kg=0.03))
    summary = corrected[0]
    assert summary.pace_mean_s == pytest.approx(raw_only[0].pace_mean_s or 0.0)
    # La corrección se aplica solo con el parámetro configurado.
    expected_mean = (90.2 - 98.5 * 0.03 + 90.4 - 97.0 * 0.03 + 90.6 - 95.5 * 0.03) / 3
    assert summary.pace_mean_fuel_corrected_s == pytest.approx(expected_mean)
    assert summary.pace_trend_fuel_corrected_s_per_lap == pytest.approx(0.245)


def test_representative_stints_need_the_configured_clean_laps() -> None:
    laps = _green_stint(1, 4, compound="soft")
    stints = (TyreStint(compound="soft", start_lap=1, end_lap=4),)

    # Tres vueltas limpias (la primera se descarta): por debajo del mínimo de 4.
    summary = summarize_stints(laps, stints, MetricsConfig())[0]
    assert summary.clean_laps == 3
    assert summary.representative is False
    assert summary.quality is Quality.LOW
    assert (
        summarize_stints(laps, stints, MetricsConfig(min_representative_laps=3))[0].representative
        is True
    )


def test_empty_inputs_return_an_empty_tuple() -> None:
    assert summarize_stints((), (), MetricsConfig()) == ()
    assert summarize_stints((), (TyreStint(compound="soft", start_lap=1),), MetricsConfig()) == ()
    laps = _green_stint(1, 3, compound="soft")
    assert summarize_stints(laps, (), MetricsConfig()) == ()


def test_laps_outside_any_stint_are_ignored() -> None:
    laps = _green_stint(1, 10, compound="soft")
    summaries = summarize_stints(
        laps, (TyreStint(compound="soft", start_lap=3, end_lap=6),), MetricsConfig()
    )

    assert len(summaries) == 1
    assert (summaries[0].start_lap, summaries[0].end_lap) == (3, 6)
    assert summaries[0].laps == 4


def test_the_result_does_not_depend_on_the_input_order() -> None:
    laps, stints = _two_compound_race()
    forward = summarize_stints(laps, stints, MetricsConfig())
    backward = summarize_stints(tuple(reversed(laps)), tuple(reversed(stints)), MetricsConfig())

    assert forward == backward


def test_laps_from_several_cars_are_rejected() -> None:
    laps = _green_stint(1, 3, compound="soft")
    other = LapRecord(
        car_index=1, lap_number=1, closed=True, compound="soft", tyre_age_start=0, tyre_age_end=1
    )

    with pytest.raises(ValueError, match="un solo coche"):
        summarize_stints(
            (*laps, other), (TyreStint(compound="soft", start_lap=1, end_lap=3),), MetricsConfig()
        )


def test_open_stint_without_end_lap_is_summarized_up_to_the_last_lap() -> None:
    laps = _green_stint(1, 4, compound="soft")
    summaries = summarize_stints(laps, (TyreStint(compound="soft", start_lap=1),), MetricsConfig())

    assert len(summaries) == 1
    assert summaries[0].end_lap is None
    assert summaries[0].laps == 4


def test_practice_in_lap_stays_in_the_previous_compound() -> None:
    """SessionHistory cierra C2 en la 5, pero la in-lap 6 todavía es C2."""
    c2 = _green_stint(1, 6, compound="medium", base_time_s=91.0, wear_per_lap=0.5)
    c2[-1] = _lap(
        6,
        compound="medium",
        tyre_age_start=5,
        lap_time_s=95.0,
        fuel_start_kg=92.5,
        fuel_end_kg=91.0,
        wear_start=2.5,
        wear_end=3.0,
        pit_entry=True,
    )
    c1 = _green_stint(
        7,
        11,
        compound="hard",
        base_time_s=89.5,
        wear_per_lap=0.4,
        out_lap=True,
    )
    stints = (
        TyreStint(compound="medium", visual_compound="medium", start_lap=1, end_lap=5),
        TyreStint(compound="hard", visual_compound="hard", start_lap=6),
    )

    summaries = summarize_stints((*c2, *c1), stints, MetricsConfig())

    assert [(item.compound, item.start_lap, item.end_lap) for item in summaries] == [
        ("medium", 1, 6),
        ("hard", 7, None),
    ]
    assert summaries[0].laps == 6
    # La salida de boxes y la primera vuelta no cuentan; quedan 4 limpias de C1.
    assert summaries[1].clean_laps == 4
    assert summaries[1].laps == 5
    assert summaries[1].wear_rate_pct_per_lap == pytest.approx(0.4)


def test_race_in_lap_does_not_contaminate_the_next_compound() -> None:
    """SessionHistory cierra C3 en la 1, pero la parada real es en la 2."""
    c3 = _green_stint(1, 2, compound="soft", base_time_s=91.0, wear_per_lap=0.6)
    c3[-1] = _lap(
        2,
        compound="soft",
        tyre_age_start=1,
        lap_time_s=96.0,
        fuel_start_kg=97.0,
        fuel_end_kg=95.5,
        wear_start=0.6,
        wear_end=1.2,
        pit_entry=True,
    )
    c1 = _green_stint(3, 12, compound="hard", base_time_s=90.0, wear_per_lap=0.3, out_lap=True)
    stints = (
        TyreStint(compound="soft", visual_compound="soft", start_lap=1, end_lap=1),
        TyreStint(compound="hard", visual_compound="hard", start_lap=2),
    )

    summaries = summarize_stints((*c3, *c1), stints, MetricsConfig())

    assert [(item.compound, item.start_lap, item.end_lap) for item in summaries] == [
        ("soft", 1, 2),
        ("hard", 3, None),
    ]
    assert summaries[0].laps == 2
    assert summaries[1].clean_laps == 9


def test_age_reset_starts_a_new_stint_without_compound_change() -> None:
    laps = (
        *_green_stint(1, 3, compound="soft"),
        _lap(4, compound="soft", tyre_age_start=0, wear_end=0.5),
        _lap(5, compound="soft", tyre_age_start=1, wear_end=1.0),
    )

    summaries = summarize_stints(
        laps, (TyreStint(compound="soft", start_lap=1, end_lap=5),), MetricsConfig()
    )

    assert [(item.start_lap, item.end_lap) for item in summaries] == [(1, 3), (4, 5)]


def test_two_pit_stops_produce_three_stints() -> None:
    laps = (
        *_green_stint(1, 4, compound="soft"),
        *_green_stint(5, 8, compound="medium", out_lap=True),
        *_green_stint(9, 12, compound="hard", out_lap=True),
    )
    stints = (
        TyreStint(compound="soft", start_lap=1, end_lap=4),
        TyreStint(compound="medium", start_lap=5, end_lap=8),
        TyreStint(compound="hard", start_lap=9),
    )

    summaries = summarize_stints(laps, stints, MetricsConfig())

    assert [(item.compound, item.start_lap, item.end_lap) for item in summaries] == [
        ("soft", 1, 4),
        ("medium", 5, 8),
        ("hard", 9, None),
    ]


def test_a_late_session_history_cannot_move_observed_stint_boundaries() -> None:
    laps = (
        *_green_stint(1, 5, compound="soft"),
        *_green_stint(6, 10, compound="hard", out_lap=True),
    )
    incomplete = (TyreStint(compound="soft", start_lap=1),)
    complete = (
        TyreStint(compound="soft", start_lap=1, end_lap=5),
        TyreStint(compound="hard", start_lap=6),
    )

    before = summarize_stints(laps, incomplete, MetricsConfig())
    after = summarize_stints(laps, complete, MetricsConfig())

    expected = [("soft", 1, 5), ("hard", 6, None)]
    assert [(item.compound, item.start_lap, item.end_lap) for item in before] == expected
    assert [(item.compound, item.start_lap, item.end_lap) for item in after] == expected


def test_declared_boundaries_are_used_when_observed_data_is_missing() -> None:
    laps = (
        LapRecord(car_index=0, lap_number=1, closed=True),
        LapRecord(car_index=0, lap_number=2, closed=True),
        LapRecord(car_index=0, lap_number=3, closed=True, compound="hard"),
    )
    stints = (
        TyreStint(compound="soft", start_lap=1, end_lap=2),
        TyreStint(compound="hard", start_lap=3),
    )

    summaries = summarize_stints(laps, stints, MetricsConfig())

    assert [(item.compound, item.start_lap, item.end_lap) for item in summaries] == [
        ("soft", 1, 2),
        ("hard", 3, None),
    ]


def test_declared_boundary_splits_same_compound_without_observed_age() -> None:
    laps = (
        LapRecord(car_index=0, lap_number=1, closed=True, compound="soft"),
        LapRecord(car_index=0, lap_number=2, closed=True, compound="soft"),
        LapRecord(car_index=0, lap_number=3, closed=True, compound="soft"),
    )
    stints = (
        TyreStint(compound="soft", start_lap=1, end_lap=2),
        TyreStint(compound="soft", start_lap=3),
    )

    summaries = summarize_stints(laps, stints, MetricsConfig())

    assert [(item.start_lap, item.end_lap) for item in summaries] == [(1, 2), (3, None)]
