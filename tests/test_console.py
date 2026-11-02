from __future__ import annotations

from dataclasses import replace
from io import StringIO
from pathlib import Path

from rich.console import Console

from f1_strategist.config import AppConfig
from f1_strategist.console import render
from f1_strategist.console_messages import render_race_event
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
    first_participant = replace(participants.participants[0], name="Test Driver")
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
    console = Console(file=output, width=120, no_color=True)
    console.print(render(config, session, now=5.0))

    text = output.getvalue()
    assert "Estado de carrera" in text
    assert "Test Driver" in text
    assert "Vuelta" in text and "3/5" in text
    assert "Posición" in text and "2" in text
    assert "12.3 kg" in text
    assert "C3" in text


def test_race_event_with_out_of_range_car_index_renders_safely() -> None:
    event = RaceEvent("pit_entry", occurred_at_monotonic=1.0, car_index=22)
    cars = tuple(CarSnapshot(car_index=index) for index in range(22))

    assert render_race_event(event, cars) == "Coche 23: entra en boxes"
