"""Pruebas del catálogo de respaldo y del formato único de cifras (L6)."""

from __future__ import annotations

import pytest

from f1_strategist.agent.templates import _EVENT_TEMPLATES, format_fact, render_template
from f1_strategist.console_messages import render_event_message
from f1_strategist.events.types import Fact

#: Salida exacta de la consola anterior a L6: el núcleo no cambia ni un carácter.
CASES: tuple[tuple[str, tuple[Fact, ...], str], ...] = (
    (
        "event.strategy_pit_recommended",
        (
            ("action", "PIT_NOW"),
            ("pit_lap", 14),
            ("compound", "C3"),
            ("gain_s", 1.5),
            ("gain_low_s", 0.25),
            ("gain_high_s", 2.0),
            ("confidence", 0.8),
            ("laps_remaining", 10),
        ),
        "Parada recomendada: Parar ahora, vuelta 14, C3; ganancia 1.5 s "
        "(rango 0.25–2 s), confianza 0.8, 10 vueltas restantes.",
    ),
    (
        "event.pit_window_open",
        (("ideal_lap", 18), ("latest_lap", 20), ("laps_to_ideal", 3)),
        "Ventana de parada abierta: vuelta ideal 18, última 20; faltan 3 vueltas para la ideal.",
    ),
    (
        "event.fuel_margin_low",
        (
            ("margin_laps", 1.5),
            ("margin_kg", 1.25),
            ("laps_remaining", 5),
            ("consumption_per_lap", 2.0),
        ),
        "Margen de combustible bajo: 1.5 vueltas / 1.25 kg; "
        "quedan 5 vueltas (consumo 2 kg/vuelta).",
    ),
    (
        "event.fuel_margin_critical",
        (
            ("margin_laps", 0.5),
            ("margin_kg", 0.75),
            ("laps_remaining", 3),
            ("consumption_per_lap", 2.5),
        ),
        "Margen de combustible crítico: 0.5 vueltas / 0.75 kg; "
        "quedan 3 vueltas (consumo 2.5 kg/vuelta).",
    ),
    (
        "event.tyre_life_near_limit",
        (("compound", "C2"), ("age_laps", 12), ("wear_mean_pct", 64.5), ("laps_left", 6)),
        "Neumático C2 cerca del límite: edad 12 vueltas, desgaste 64.5 %, vida estimada 6 vueltas.",
    ),
    (
        "event.tyre_degradation_abnormal",
        (
            ("wear_rate_pct_per_lap", 0.85),
            ("expected_rate_pct_per_lap", 0.5),
            ("ratio", 1.7),
            ("pace_trend_s_per_lap", 0.12),
        ),
        "Degradación anormal: 0.85 %/vuelta frente a 0.5 %/vuelta esperados (x1.7); "
        "tendencia de ritmo 0.12 s/vuelta.",
    ),
    (
        "event.safety_car_deployed",
        (("lap", 23),),
        "Coche de seguridad desplegado en la vuelta 23.",
    ),
    (
        "event.virtual_safety_car_deployed",
        (("lap", 23),),
        "Virtual Safety Car desplegado en la vuelta 23.",
    ),
    (
        "event.safety_car_cleared",
        (("lap", 24),),
        "Coche de seguridad retirado en la vuelta 24.",
    ),
    ("event.red_flag", (("lap", 25),), "Bandera roja en la vuelta 25."),
    (
        "event.weather_changed",
        (
            ("previous", "clear"),
            ("current", "light_rain"),
            ("track_temperature_c", 31.5),
            ("air_temperature_c", 22.25),
        ),
        "Cambio de clima: despejado → lluvia ligera; pista 31.5 °C, aire 22.25 °C.",
    ),
    (
        "event.rain_expected",
        (("probability_pct", 80.0), ("horizon_min", 10)),
        "Lluvia prevista: probabilidad 80 % en 10 min.",
    ),
    (
        "event.data_stale",
        (("group", "lap_data"), ("age_s", 3.5)),
        "Datos de lap_data obsoletos: 3.5 s.",
    ),
    (
        "event.damage_detected",
        (("component", "alerón delantero"), ("damage_pct", 12.0), ("delta_pct", 2.5)),
        "Daño en alerón delantero: 12 % (cambio 2.5 %).",
    ),
    (
        "event.rival_pit_stop",
        (
            ("car_index", 0),
            ("name", "VER"),
            ("position", 2),
            ("gap_s", 1.25),
            ("lap", 30),
        ),
        "Parada de rival: VER (coche 1, posición 2), hueco 1.25 s, vuelta 30.",
    ),
)


@pytest.mark.parametrize(("key", "facts", "expected"), CASES)
def test_template_matches_previous_console_output(
    key: str, facts: tuple[Fact, ...], expected: str
) -> None:
    assert render_template(key, facts, "es") == expected
    assert render_event_message(key, facts) == expected


def test_every_event_key_of_phase_5_is_covered() -> None:
    assert {key for key, _, _ in CASES} == set(_EVENT_TEMPLATES)


def test_missing_facts_stay_unknown_with_their_unit() -> None:
    rendered = render_event_message(
        "event.fuel_margin_critical",
        (("margin_kg", 1.25), ("margin_laps", None)),
    )

    assert rendered == (
        "Margen de combustible crítico: desconocido vueltas / 1.25 kg; "
        "quedan desconocido vueltas (consumo desconocido kg/vuelta)."
    )


def test_unknown_key_gives_a_generic_text_instead_of_raising() -> None:
    rendered = render_event_message("event.not_invented", ())

    assert rendered == "Falta la traducción al español del mensaje 'event.not_invented'."


def test_format_fact_rounds_and_formats_units() -> None:
    assert format_fact("gain_s", 2.006) == "2.01 s"
    assert format_fact("gain_s", 2.0) == "2 s"
    assert format_fact("margin_kg", 1.256) == "1.26 kg"
    assert format_fact("wear_rate_pct_per_lap", 0.1234) == "0.12 %/vuelta"
    assert format_fact("track_temperature_c", 31.5) == "31.5 °C"
    assert format_fact("gap_s", 1.0) == "1 s"


def test_format_fact_keeps_unknown_and_labeled_values() -> None:
    assert format_fact("margin_laps", None) == "desconocido vueltas"
    assert format_fact("action", "PIT_IN") == "Parar más adelante"
    assert format_fact("action", "NOT_AN_ACTION") == "NOT_AN_ACTION"
    assert format_fact("previous", "storm") == "tormenta"
    assert format_fact("previous", "foggy") == "foggy"
    assert format_fact("car_index", 4) == "5"
    assert format_fact("position", 2) == "2"
