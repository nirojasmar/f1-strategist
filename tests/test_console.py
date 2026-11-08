from __future__ import annotations

from dataclasses import replace
from io import StringIO
from pathlib import Path

from rich.console import Console

import f1_strategist.console as console_module
from f1_strategist.config import AppConfig
from f1_strategist.console import _render_metrics_summary, _render_race_summary, render
from f1_strategist.console_messages import render_diagnostic, render_race_event
from f1_strategist.messages import message
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.parser import (
    CarStatusPacket,
    EnumValue,
    LapDataPacket,
    ParticipantsPacket,
    SessionPacket,
    parse_packet,
)
from f1_strategist.race_state import CarSnapshot, RaceEvent
from f1_strategist.session import CaptureSession


def test_console_renders_race_summary_fields() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    config = AppConfig()
    session = CaptureSession(config)

    session_packet = parse_packet((fixtures / "session.bin").read_bytes())
    participants = parse_packet((fixtures / "participants.bin").read_bytes())
    lap_data = parse_packet((fixtures / "lap_data.bin").read_bytes())
    car_status = parse_packet((fixtures / "car_status.bin").read_bytes())
    assert isinstance(session_packet, SessionPacket)
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(lap_data, LapDataPacket)
    assert isinstance(car_status, CarStatusPacket)

    session_packet = replace(
        session_packet,
        session_type=EnumValue(15, "race"),
        track_id=EnumValue(11, "monza"),
        total_laps=5,
    )
    first_participant = participants.participants[0]
    assert first_participant is not None
    first_participant = replace(first_participant, name="Test Driver")
    participants = replace(
        participants,
        participants=(first_participant, *participants.participants[1:]),
    )
    first_lap = replace(
        lap_data.cars[0],
        current_lap_number=3,
        car_position=2,
    )
    lap_data = replace(lap_data, cars=(first_lap, *lap_data.cars[1:]))
    first_status = replace(
        car_status.cars[0],
        fuel_in_tank=12.3,
        actual_tyre_compound=EnumValue(18, "c3"),
    )
    car_status = replace(car_status, cars=(first_status, *car_status.cars[1:]))

    for observed_at, packet in enumerate(
        (session_packet, participants, lap_data, car_status), start=1
    ):
        session._race_state.apply(
            replace(
                packet,
                header=replace(packet.header, player_car_index=0),
            ),
            float(observed_at),
        )

    output = StringIO()
    console = Console(file=output, width=250, no_color=True)
    console.print(render(config, session, now=100.0, race_now=5.0))

    text = output.getvalue()
    assert "Estado de carrera" in text
    assert "Test Driver" in text
    assert "Vuelta" in text and "3/5" in text
    assert "Posición" in text and "2" in text
    assert "12.3 kg" in text
    assert "C3" in text
    assert "combustible/neumáticos: 1.0 s" in text


def test_race_event_with_out_of_range_car_index_renders_safely() -> None:
    event = RaceEvent("pit_entry", occurred_at_monotonic=1.0, car_index=22)
    exit_event = RaceEvent("pit_exit", occurred_at_monotonic=2.0, car_index=0)
    cars = tuple(CarSnapshot(car_index=index) for index in range(22))

    assert render_race_event(event, cars) == "Coche 23: entra en boxes"
    assert render_race_event(exit_event, cars) == "Coche 1: sale de boxes"


def test_console_renders_untrusted_markup_as_literal_text(monkeypatch) -> None:
    config = AppConfig()
    session = CaptureSession(config)
    race = session.race_snapshot()
    name = "[red]Driver[/red]"
    race = replace(
        race,
        player_car_index=0,
        cars=(replace(race.cars[0], name=name), *race.cars[1:]),
        events=(RaceEvent("pit_entry", 1.0, car_index=0),),
    )
    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(_render_race_summary(race, now=2.0))

    assert output.getvalue().count(name) == 2

    diagnostics = (
        message("rejected_sources", ips=("[bold]192.0.2.1[/bold]",)),
        message("capture_write_error", error="disk [red]failure[/red]"),
    )
    monkeypatch.setattr(console_module, "diagnose", lambda *_: diagnostics)
    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(render(config, session, now=2.0))

    rendered = output.getvalue()
    assert "[bold]192.0.2.1[/bold]" in rendered
    assert "disk [red]failure[/red]" in rendered


def _metric(
    key: str,
    value: float | None,
    unit: str,
    quality: Quality,
    *,
    sample_count: int = 4,
    notes: tuple[str, ...] = (),
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=quality,
        sample_count=sample_count,
        computed_at_monotonic=3.0,
        notes=notes,
    )


def test_console_renders_metrics_panel_with_quality_markers(monkeypatch) -> None:
    config = AppConfig()
    session = CaptureSession(config)
    metrics = MetricsSnapshot(
        metrics={
            "progress.laps_remaining": _metric("progress.laps_remaining", 3.5, "lap", Quality.OK),
            "fuel.margin_laps_at_finish": _metric(
                "fuel.margin_laps_at_finish", None, "lap", Quality.INSUFFICIENT
            ),
            "tyres.wear_rate_pct_per_lap": _metric(
                "tyres.wear_rate_pct_per_lap", 0.125, "%/lap", Quality.STALE
            ),
            "pace.trend_s_per_lap": _metric(
                "pace.trend_s_per_lap", None, "s/lap", Quality.UNAVAILABLE
            ),
            "gap.ahead_s": _metric("gap.ahead_s", 0.82, "s", Quality.OK),
            "gap.behind_s": _metric("gap.behind_s", None, "s", Quality.INSUFFICIENT),
        },
        computed_at_monotonic=3.0,
        session_uid=1,
    )
    monkeypatch.setattr(session, "metrics_snapshot", lambda: metrics)

    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(render(config, session, now=5.0))
    text = output.getvalue()

    assert "Métricas" in text
    for label in (
        "Vueltas restantes",
        "Margen de combustible",
        "Tasa de desgaste",
        "Tendencia de ritmo",
        "Intervalo delante",
        "Intervalo detrás",
    ):
        assert label in text
    assert "3.5 vueltas" in text
    assert "0.125 %/lap" in text
    assert "0.82 s" in text
    assert "stale (obsoleto)" in text
    assert "insufficient (insuficiente)" in text
    assert "unavailable (no disponible)" in text

    lines = text.splitlines()
    fuel_line = next(line for line in lines if "Margen de combustible" in line)
    pace_line = next(line for line in lines if "Tendencia de ritmo" in line)
    assert "—" in fuel_line
    assert "—" in pace_line


def test_console_metrics_panel_renders_empty_snapshot_with_dashes() -> None:
    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(_render_metrics_summary(MetricsSnapshot()))
    text = output.getvalue()

    for label in (
        "Vueltas restantes",
        "Margen de combustible",
        "Tasa de desgaste",
        "Tendencia de ritmo",
        "Intervalo delante",
        "Intervalo detrás",
    ):
        line = next(line for line in text.splitlines() if label in line)
        assert "—" in line


def test_console_explains_required_samples_for_insufficient_metrics(monkeypatch) -> None:
    config = AppConfig()
    session = CaptureSession(config)
    metrics = MetricsSnapshot(
        metrics={
            "pace.trend_s_per_lap": _metric(
                "pace.trend_s_per_lap",
                None,
                "s/lap",
                Quality.INSUFFICIENT,
                sample_count=1,
                notes=("required_samples:3",),
            ),
        },
        computed_at_monotonic=3.0,
        session_uid=1,
    )
    monkeypatch.setattr(session, "metrics_snapshot", lambda: metrics)

    output = StringIO()
    Console(file=output, width=120, no_color=True).print(render(config, session, now=5.0))

    assert "1/3 muestras válidas" in output.getvalue()


def test_console_renders_pilot_result_and_retirement_reason_without_inference() -> None:
    race = CaptureSession(AppConfig()).race_snapshot()
    player = replace(race.cars[0], result_status="retired")
    race = replace(
        race,
        player_car_index=0,
        cars=(player, *race.cars[1:]),
        events=(RaceEvent("retirement", 1.0, car_index=0, value="invalid"),),
    )
    output = StringIO()
    Console(file=output, width=120, no_color=True).print(_render_race_summary(race, now=2.0))

    assert "Estado del piloto" in output.getvalue()
    assert "Retirado" in output.getvalue()
    assert render_race_event(race.events[0], race.cars) == (
        "Coche 1: retirada comunicada (motivo del juego: invalid)"
    )


def test_metrics_update_error_renders_in_spanish() -> None:
    rendered = render_diagnostic(message("metrics_update_error", error="boom"))

    assert "métricas" in rendered
    assert "boom" in rendered


def test_metrics_panel_marks_live_rows_stale_when_lap_data_ages(monkeypatch) -> None:
    config = AppConfig()
    session = CaptureSession(config)
    fixtures = Path(__file__).parent / "fixtures"
    session_packet = parse_packet((fixtures / "session.bin").read_bytes())
    lap_data = parse_packet((fixtures / "lap_data.bin").read_bytes())
    session._race_state.apply(
        replace(session_packet, header=replace(session_packet.header, player_car_index=0)), 1.0
    )
    session._race_state.apply(
        replace(lap_data, header=replace(lap_data.header, player_car_index=0)), 1.0
    )
    metrics = MetricsSnapshot(
        metrics={
            "progress.laps_remaining": _metric("progress.laps_remaining", 4.8, "lap", Quality.OK),
            "fuel.margin_laps_at_finish": _metric(
                "fuel.margin_laps_at_finish", 2.0, "lap", Quality.OK
            ),
            "tyres.wear_rate_pct_per_lap": _metric(
                "tyres.wear_rate_pct_per_lap", 0.1, "%/lap", Quality.OK
            ),
        },
        computed_at_monotonic=1.0,
        session_uid=session_packet.header.session_uid,
    )
    monkeypatch.setattr(session, "metrics_snapshot", lambda: metrics)

    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(render(config, session, now=10.0))
    lines = output.getvalue().splitlines()

    remaining = next(line for line in lines if "Vueltas restantes" in line)
    fuel = next(line for line in lines if "Margen de combustible" in line)
    wear = next(line for line in lines if "Tasa de desgaste" in line)
    assert "stale (obsoleto)" in remaining
    assert "stale (obsoleto)" in fuel
    assert "stale (obsoleto)" not in wear


def test_metrics_panel_reports_module_failures() -> None:
    metrics = MetricsSnapshot(
        metrics={
            "broken.error": Metric(
                "broken.error",
                None,
                "",
                Window("instant"),
                Quality.UNAVAILABLE,
                0,
                3.0,
                notes=("metric_module_failed", "RuntimeError"),
            )
        },
        computed_at_monotonic=3.0,
        session_uid=1,
    )
    output = StringIO()
    console = Console(file=output, width=120, no_color=True)
    console.print(_render_metrics_summary(metrics))
    text = output.getvalue()

    assert "Módulo broken" in text
    assert "error de cálculo (RuntimeError)" in text
