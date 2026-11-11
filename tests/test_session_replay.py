"""Prueba de extremo a extremo: reemisión UDP -> receptor -> captura en disco."""

from __future__ import annotations

import struct
import time
from pathlib import Path

from f1_strategist.capture import CapturedPacket, CaptureReader, CaptureWriter
from f1_strategist.config import AppConfig, CaptureConfig, RecordingConfig, TelemetryConfig
from f1_strategist.diagnostics import diagnose
from f1_strategist.packets import HEADER_STRUCT
from f1_strategist.replay import replay_capture
from f1_strategist.session import CaptureSession
from f1_strategist.summary import summarize


def _write_source_capture(path: Path, count: int) -> list[bytes]:
    payloads = []
    for i in range(count):
        packet_id = 3 if i % 3 == 0 else 0
        body = struct.pack("<4s12s", b"SSTA", bytes(12)) if packet_id == 3 else bytes(132)
        header = HEADER_STRUCT.pack(
            2025,
            25,
            1,
            26,
            1,
            packet_id,
            0xABCDEF,
            i * 0.016,
            i,
            i,
            0,
            255,
        )
        payloads.append(header + body)
    with CaptureWriter(path) as writer:
        for i, payload in enumerate(payloads):
            writer.write(CapturedPacket(1_000_000_000 + i * 16_000_000, "10.0.0.5", 4000, payload))
    return payloads


def test_replay_is_received_and_recorded(tmp_path: Path) -> None:
    source = tmp_path / "origen.f1cap"
    payloads = _write_source_capture(source, 60)

    cfg = AppConfig(
        telemetry=TelemetryConfig(listen_port=0),
        capture=CaptureConfig(enabled=True, directory=tmp_path / "out"),
        recording=RecordingConfig(enabled=False),
    )
    session = CaptureSession(cfg)
    session.start()
    try:
        sent = replay_capture(source, "127.0.0.1", session.bound_port, speed=0)
        assert sent == 60
        deadline = time.monotonic() + 5
        while session.snapshot().capture_packets < 60 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        session.stop()

    snap = session.snapshot()
    assert snap.received == 60
    assert snap.invalid == 0
    assert snap.packets_by_id == {0: 40, 3: 20}
    assert snap.parser_rejections == {}
    assert snap.ignored_packets_by_id == {0: 40}
    assert snap.last_packet_monotonic is not None
    assert diagnose(cfg.telemetry, snap, snap.last_packet_monotonic) == []
    assert snap.last_header is not None
    assert (snap.last_header.packet_format, snap.last_header.game_year) == (2025, 25)

    assert session.capture_path is not None
    recorded = CaptureReader(session.capture_path)
    assert [p.payload for p in recorded] == payloads
    summary = summarize(session.capture_path)
    assert summary.packets == 60
    assert summary.formats == {(2025, 25, 1, 26): 60}


def test_capture_writer_failure_does_not_kill_consumer(tmp_path: Path) -> None:
    cfg = AppConfig(
        telemetry=TelemetryConfig(listen_port=0),
        capture=CaptureConfig(enabled=True, directory=tmp_path / "out"),
        recording=RecordingConfig(enabled=False),
    )
    session = CaptureSession(cfg)
    session.start()
    try:
        session._queue.put(
            CapturedPacket(
                time.time_ns(),
                "x" * 46,
                4000,
                HEADER_STRUCT.pack(2025, 25, 1, 26, 1, 3, 1, 0.0, 1, 1, 0, 255)
                + struct.pack("<4s12s", b"SSTA", bytes(12)),
            )
        )
        deadline = time.monotonic() + 3
        while session.snapshot().capture_error is None and time.monotonic() < deadline:
            time.sleep(0.01)

        assert session.snapshot().capture_error is not None
        assert session._consumer is not None and session._consumer.is_alive()
    finally:
        session.stop()


def test_replay_respects_original_timing(tmp_path: Path) -> None:
    source = tmp_path / "origen.f1cap"
    _write_source_capture(source, 11)  # 10 intervalos de 16 ms = 160 ms
    cfg = AppConfig(
        telemetry=TelemetryConfig(listen_port=0),
        capture=CaptureConfig(enabled=False),
        recording=RecordingConfig(enabled=False),
    )
    session = CaptureSession(cfg)
    session.start()
    try:
        started = time.perf_counter()
        replay_capture(source, "127.0.0.1", session.bound_port, speed=1.0)
        elapsed = time.perf_counter() - started
    finally:
        session.stop()
    assert elapsed >= 0.14


def test_consumer_publishes_parsed_packets_to_race_state() -> None:
    payload = (Path(__file__).parent / "fixtures" / "car_status.bin").read_bytes()
    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
            recording=RecordingConfig(enabled=False),
        )
    )
    session.start()
    try:
        session._queue.put(
            CapturedPacket(
                time.time_ns(),
                "127.0.0.1",
                4000,
                payload,
                recv_monotonic=10.0,
            ),
            timeout=1,
        )
        deadline = time.monotonic() + 3
        while (
            session.race_snapshot().freshness_for("car_status").updated_at_monotonic is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        session.stop()

    snapshot = session.race_snapshot()
    assert snapshot.freshness_for("car_status").updated_at_monotonic == 10.0
    assert snapshot.player_car is not None
    assert snapshot.player_car.fuel_kg is not None
    assert session.snapshot().parser_rejections == {}
