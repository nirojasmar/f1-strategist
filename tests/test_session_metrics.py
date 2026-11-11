from __future__ import annotations

import time
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureReader
from f1_strategist.config import (
    AppConfig,
    CaptureConfig,
    MetricsConfig,
    RecordingConfig,
    TelemetryConfig,
)
from f1_strategist.metrics.engine import MetricsEngine, MetricsSnapshot
from f1_strategist.packets import HEADER_STRUCT
from f1_strategist.parser import ParsedPacket
from f1_strategist.ports import SessionPort
from f1_strategist.race_state import RaceStateSnapshot
from f1_strategist.session import CaptureSession
from tests.scenarios import ScenarioCar, build_race_timed


class _FailingEngine(MetricsEngine):
    def apply(
        self,
        packet: ParsedPacket,
        observed_at: float,
        state_snapshot: RaceStateSnapshot,
    ) -> None:
        raise RuntimeError("metrics boom")


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _payload(packet: ParsedPacket) -> bytes:
    header = packet.header
    return HEADER_STRUCT.pack(
        header.packet_format,
        header.game_year,
        header.game_major_version,
        header.game_minor_version,
        header.packet_version,
        header.packet_id,
        header.session_uid,
        header.session_time,
        header.frame_identifier,
        header.overall_frame_identifier,
        header.player_car_index,
        header.secondary_player_car_index,
    )


def _metrics_via_port(port: SessionPort) -> MetricsSnapshot:
    return port.metrics_snapshot()


def test_metrics_snapshot_starts_empty_and_is_exposed_by_the_session_port() -> None:
    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
        )
    )

    snapshot = session.metrics_snapshot()
    assert snapshot.metrics == {}
    assert snapshot.session_uid is None
    assert _metrics_via_port(session) is snapshot


def test_consumer_feeds_the_engine_with_a_replayed_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    parsed = iter(packet for packet, _ in packets)
    monkeypatch.setattr("f1_strategist.session.parse_packet", lambda _: next(parsed))

    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
            metrics=MetricsConfig(publish_interval_s=0.01),
        )
    )
    session._stop_consumer.set()
    for packet, observed_at in packets:
        session._queue.put(
            CapturedPacket(
                recv_time_ns=0,
                source_ip="127.0.0.1",
                source_port=4000,
                payload=_payload(packet),
                recv_monotonic=observed_at,
            )
        )

    session._consume()

    snapshot = session.metrics_snapshot()
    assert snapshot.session_uid == packets[0][0].header.session_uid
    assert snapshot.computed_at_monotonic == packets[-1][1]
    for key in (
        "progress.laps_completed",
        "fuel.in_tank_kg",
        "tyres.compound",
        "pace.last_lap_s",
        "gap.ahead_s",
        "pit.stops_done",
        "ers.store_j",
        "damage.aero_max_pct",
        "env.weather",
        "track.safety_car_status",
    ):
        assert snapshot.get(key) is not None, key
    assert snapshot.get("progress.laps_completed").value == 3  # type: ignore[union-attr]
    assert session.race_snapshot().current_lap == 3
    assert session.snapshot().consumer_errors == 0


def test_engine_failure_does_not_stop_state_or_capture(tmp_path: Path) -> None:
    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=True, directory=tmp_path / "out"),
            recording=RecordingConfig(enabled=False),
        ),
        metrics_engine=_FailingEngine(),
    )
    payload = (Path(__file__).parent / "fixtures" / "car_status.bin").read_bytes()
    session.start()
    try:
        session._queue.put(
            CapturedPacket(
                recv_time_ns=0,
                source_ip="127.0.0.1",
                source_port=4000,
                payload=payload,
                recv_monotonic=10.0,
            ),
            timeout=1,
        )
        deadline = time.monotonic() + 3
        while session.snapshot().consumer_errors == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session._consumer is not None and session._consumer.is_alive()
    finally:
        session.stop()

    snapshot = session.snapshot()
    assert snapshot.consumer_errors == 1
    assert snapshot.consumer_error is not None
    assert snapshot.consumer_error.key == "metrics_update_error"
    assert snapshot.consumer_error.get("error") == "metrics boom"

    race_snapshot = session.race_snapshot()
    assert race_snapshot.freshness_for("car_status").updated_at_monotonic == 10.0
    assert race_snapshot.player_car is not None
    assert race_snapshot.player_car.fuel_kg is not None

    assert session.metrics_snapshot().metrics == {}
    assert session.capture_path is not None
    assert len(list(CaptureReader(session.capture_path))) == 1
