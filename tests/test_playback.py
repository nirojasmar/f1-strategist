from __future__ import annotations

import math
import struct
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.metrics.export import laps_to_rows, run_metrics_capture, snapshot_to_dict
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.playback import PlaybackSession
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _scenario() -> list[tuple[ParsedPacket, float]]:
    return list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))


def _write_capture(
    path: Path,
    packets: list[tuple[ParsedPacket, float]],
    *,
    interval_s: float = 0.002,
) -> None:
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=int(index * interval_s * 1_000_000_000),
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )


def _patch_parser(
    monkeypatch: pytest.MonkeyPatch,
    packets: list[tuple[ParsedPacket, float]],
) -> None:
    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval_s: float = 0.002,
) -> tuple[Path, list[tuple[ParsedPacket, float]]]:
    packets = _scenario()
    path = tmp_path / "race.f1cap"
    _write_capture(path, packets, interval_s=interval_s)
    _patch_parser(monkeypatch, packets)
    return path, packets


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _play_to_end(session: PlaybackSession, timeout: float = 10.0) -> None:
    session.play()
    assert session.wait(timeout=timeout), "la reproducción no terminó a tiempo"
    assert session.error is None
    assert session.finished


def _result(session: PlaybackSession) -> tuple[dict[str, object], list[dict[str, object]]]:
    return (
        snapshot_to_dict(session.metrics_snapshot()),
        laps_to_rows(session.history_snapshot()),
    )


def test_speed_does_not_change_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    results = []
    for speed in (0.0, 1.0, 4.0):
        session = PlaybackSession(path, speed=speed)
        _play_to_end(session)
        assert session.runs_completed == 1
        results.append(_result(session))

    assert results[0] == results[1] == results[2]


def test_play_matches_the_offline_metrics_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    session = PlaybackSession(path, speed=0)
    _play_to_end(session)

    offline = run_metrics_capture(path)
    assert snapshot_to_dict(session.metrics_snapshot()) == snapshot_to_dict(offline.snapshot)
    assert laps_to_rows(session.history_snapshot()) == laps_to_rows(offline.history)

    stats = session.snapshot()
    assert stats.received == offline.offline.packets_read
    assert stats.consumer_errors == 0


def test_pause_and_resume_do_not_lose_or_duplicate_packets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch, interval_s=0.03)
    baseline = PlaybackSession(path, speed=0)
    _play_to_end(baseline)

    session = PlaybackSession(path, speed=1.0)
    session.play()
    assert _wait_until(lambda: session.snapshot().received >= 3)
    session.pause()
    assert session.paused
    time.sleep(0.05)
    paused_count = session.snapshot().received
    time.sleep(0.15)
    assert session.snapshot().received == paused_count
    assert not session.finished

    session.resume()
    assert session.wait(timeout=10)
    assert session.error is None
    assert _result(session) == _result(baseline)


def test_loop_restarts_from_the_beginning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    session = PlaybackSession(path, speed=0, loop=True)
    try:
        session.play()
        assert _wait_until(lambda: session.runs_completed >= 2)
        assert not session.finished
    finally:
        session.stop()
    assert not session.playing
    assert not session.paused


def test_seek_to_lap_fast_forwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch, interval_s=0.03)
    baseline = PlaybackSession(path, speed=0)
    _play_to_end(baseline)

    session = PlaybackSession(path, speed=1.0)
    session.play()
    assert _wait_until(lambda: session.snapshot().received >= 1)
    session.seek_to_lap(2)
    assert session.wait(timeout=10)
    assert session.error is None
    assert session.race_snapshot().current_lap == 3
    assert _result(session) == _result(baseline)


def test_start_lap_skips_the_earlier_laps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    baseline = PlaybackSession(path, speed=0)
    _play_to_end(baseline)

    session = PlaybackSession(path, speed=0, start_lap=2)
    _play_to_end(session)
    assert session.race_snapshot().current_lap == 3
    assert _result(session) == _result(baseline)


def test_stop_during_a_pause_is_immediate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch, interval_s=0.05)
    session = PlaybackSession(path, speed=1.0)
    session.play()
    assert _wait_until(lambda: session.snapshot().received >= 1)
    session.pause()
    assert _wait_until(lambda: session.paused)

    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 2.0
    assert not session.playing
    assert not session.paused
    assert session.error is None


def test_network_fields_are_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    session = PlaybackSession(path, speed=0)
    assert session.bound_port == 0
    assert session.receiver_error is None
    assert session.capture_path is None


def test_controls_validate_their_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="speed"):
        PlaybackSession(path, speed=-1)
    with pytest.raises(ValueError, match="speed"):
        PlaybackSession(path, speed=math.nan)
    with pytest.raises(ValueError, match="start_lap"):
        PlaybackSession(path, start_lap=0)
    session = PlaybackSession(path, speed=0)
    with pytest.raises(ValueError, match="lap_number"):
        session.seek_to_lap(0)


def test_play_command_at_speed_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)

    assert main(["play", str(path), "--speed", "0"]) == 0
    assert "Reproducción completada" in capsys.readouterr().out


def test_play_command_rejects_a_missing_capture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["play", str(tmp_path / "missing.f1cap"), "--speed", "0"]) == 2
    assert "No se pudo leer" in capsys.readouterr().err
