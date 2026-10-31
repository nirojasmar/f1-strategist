"""Prueba de extremo a extremo: reemisión UDP -> receptor -> captura en disco."""

from __future__ import annotations

import time
from pathlib import Path

from f1_strategist.capture import CapturedPacket, CaptureReader, CaptureWriter
from f1_strategist.config import AppConfig, CaptureConfig, TelemetryConfig
from f1_strategist.replay import replay_capture
from f1_strategist.session import CaptureSession
from f1_strategist.summary import summarize

from .conftest import make_packet


def _write_source_capture(path: Path, count: int) -> list[bytes]:
    payloads = [make_packet(i % 3, session_time=i * 0.016) for i in range(count)]
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
    assert snap.packets_by_id == {0: 20, 1: 20, 2: 20}
    assert snap.last_header is not None
    assert (snap.last_header.packet_format, snap.last_header.game_year) == (2025, 25)

    assert session.capture_path is not None
    recorded = CaptureReader(session.capture_path)
    assert [p.payload for p in recorded] == payloads
    summary = summarize(session.capture_path)
    assert summary.packets == 60
    assert summary.formats == {(2025, 25, 1, 5): 60}


def test_replay_respects_original_timing(tmp_path: Path) -> None:
    source = tmp_path / "origen.f1cap"
    _write_source_capture(source, 11)  # 10 intervalos de 16 ms = 160 ms
    cfg = AppConfig(
        telemetry=TelemetryConfig(listen_port=0),
        capture=CaptureConfig(enabled=False),
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
