from __future__ import annotations

import json
import math
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.fingerprint import file_sha256, fingerprint_capture
from f1_strategist.metrics.export import laps_to_rows, run_metrics_capture, snapshot_to_dict
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import PacketParseError, ParsedPacket
from f1_strategist.playback import PlaybackSession
from f1_strategist.store import SessionInfo, SessionStore
from f1_strategist.weekend_profile import WeekendProfile
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


def _write_payload_capture(path: Path, payloads: list[bytes], times_ns: list[int]) -> None:
    with CaptureWriter(path) as writer:
        for payload, recv_time_ns in zip(payloads, times_ns, strict=True):
            writer.write(
                CapturedPacket(
                    recv_time_ns=recv_time_ns,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=payload,
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


def test_missing_start_lap_fails_without_looping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    session = PlaybackSession(path, speed=0, loop=True, start_lap=99)

    session.play()
    assert session.wait(timeout=10)

    assert session.finished
    assert session.error is not None
    assert "No se encontró la vuelta solicitada 99" in session.error
    assert session.runs_completed == 0


def test_seek_fails_when_capture_has_no_lap_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = _scenario()
    path = tmp_path / "session-without-laps.f1cap"
    _write_payload_capture(path, [make_packet(1, extra=struct.pack("<I", 0))], [0])
    _patch_parser(monkeypatch, packets[:1])
    session = PlaybackSession(path, speed=0, loop=True, start_lap=1)

    session.play()
    assert session.wait(timeout=10)

    assert session.error is not None
    assert "No se encontró la vuelta solicitada 1" in session.error


def test_playback_clock_advances_for_ignored_packets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = _scenario()
    path = tmp_path / "ignored-after-session.f1cap"
    _write_payload_capture(
        path,
        [make_packet(1, extra=struct.pack("<I", 0)), make_packet(0)],
        [0, 4_000_000_000],
    )
    _patch_parser(monkeypatch, packets[:1])

    session = PlaybackSession(path, speed=0)
    _play_to_end(session)

    assert session.observed_at == 4.0
    assert session.snapshot().ignored_packets_by_id == {0: 1}
    freshness = session.race_snapshot().freshness_for("session")
    assert freshness.age_seconds(session.observed_at) == pytest.approx(4.0)
    assert freshness.is_stale(session.observed_at)


def test_playback_rejects_truncated_capture_and_parser_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    with path.open("ab") as handle:
        handle.write(b"\x01\x02\x03")
    truncated = PlaybackSession(path, speed=0)
    truncated.play()
    assert truncated.wait(timeout=10)
    assert truncated.error is not None
    assert "registro incompleto" in truncated.error

    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir()
    valid_path, _ = _capture(rejected_dir, monkeypatch)

    def reject(_: bytes) -> ParsedPacket:
        raise PacketParseError("bad_packet")

    monkeypatch.setattr("f1_strategist.playback.parse_packet", reject)
    rejected = PlaybackSession(valid_path, speed=0)
    rejected.play()
    assert rejected.wait(timeout=10)
    assert rejected.error is not None
    assert "rechazado(s) por parser" in rejected.error


def test_playback_rejects_capture_without_valid_packets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ignored-only.f1cap"
    _write_payload_capture(path, [make_packet(0)], [0])
    session = PlaybackSession(path, speed=0)

    session.play()
    assert session.wait(timeout=10)

    assert session.error is not None
    assert "no contiene paquetes válidos" in session.error


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


def test_play_and_verify_load_the_profile_registered_for_the_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, packets = _capture(tmp_path, monkeypatch)
    session_uid = packets[0][0].header.session_uid
    profile = WeekendProfile()
    database = tmp_path / "sessions.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[recording]\ndatabase = {json.dumps(str(database))}\n",
        encoding="utf-8",
    )
    with SessionStore.open(database) as store:
        store.create_session(
            SessionInfo(
                session_uid=session_uid,
                source="live",
                started_at_utc="2026-11-05T10:00:00Z",
                session_type="race",
                capture_sha256=file_sha256(path),
                profile_json=profile.to_json(),
                profile_hash=profile.hash,
            )
        )

    playback_profiles: dict[int, WeekendProfile] = {}
    real_playback = PlaybackSession

    def create_playback(*args: Any, **kwargs: Any) -> PlaybackSession:
        playback_profiles.update(kwargs["profiles_by_uid"])
        return real_playback(*args, **kwargs)

    monkeypatch.setattr("f1_strategist.cli.PlaybackSession", create_playback)
    monkeypatch.setattr(
        "f1_strategist.cli._run_playback_console",
        lambda _cfg, session, _console: (session.play(), session.wait(timeout=10)),
    )
    monkeypatch.setattr("f1_strategist.cli._install_signal_handlers", lambda: None)

    assert main(["play", str(path), "--speed", "0", "--config", str(config_path)]) == 0
    assert playback_profiles == {session_uid: profile}

    verify_profiles: dict[int, WeekendProfile] = {}
    real_fingerprint = fingerprint_capture

    def fingerprint_with_profiles(*args: Any, **kwargs: Any) -> Any:
        verify_profiles.update(kwargs["profiles_by_uid"])
        return real_fingerprint(*args, **kwargs)

    monkeypatch.setattr("f1_strategist.cli.fingerprint_capture", fingerprint_with_profiles)
    assert main(["verify", str(path), "--runs", "1", "--config", str(config_path)]) == 0
    assert verify_profiles == {session_uid: profile}


def test_play_command_rejects_a_missing_capture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["play", str(tmp_path / "missing.f1cap"), "--speed", "0"]) == 2
    assert "No se pudo leer" in capsys.readouterr().err


def test_play_command_fails_when_requested_lap_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)

    assert main(["play", str(path), "--speed", "0", "--start-lap", "99"]) == 1
    assert "No se encontró la vuelta solicitada 99" in capsys.readouterr().err
