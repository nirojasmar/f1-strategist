"""Huella determinista, metadatos y comando `verify`."""

from __future__ import annotations

import hashlib
import shutil
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.config import (
    AppConfig,
    CaptureConfig,
    MetricsConfig,
    StrategyConfig,
    StrategyTrackOverride,
)
from f1_strategist.fingerprint import (
    _FingerprintCollector,
    build_run_info,
    compute_config_hash,
    compute_fingerprint,
    compute_strategy_fingerprint,
    fingerprint_capture,
)
from f1_strategist.metrics.engine import MetricsEngine
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import PARSER_VERSION, PacketParseError, ParsedPacket
from f1_strategist.race_state import RaceEvent
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(
            1,
            "Driver B",
            2,
            90.5,
            40.0,
            2.0,
            2.3,
            pit_laps=(2,),
            compounds=("soft", "medium"),
        ),
    )


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    path = tmp_path / "race.f1cap"
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 1_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path


def test_two_runs_have_the_same_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    first = fingerprint_capture(path)
    second = fingerprint_capture(path)

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_fingerprint_does_not_depend_on_capture_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    copied = tmp_path / "copy.f1cap"
    shutil.copyfile(path, copied)

    first = fingerprint_capture(path)
    second = fingerprint_capture(copied)

    assert first.fingerprint == second.fingerprint
    assert first.run_info.capture_sha256 == second.run_info.capture_sha256


def test_fingerprint_does_not_depend_on_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    first = fingerprint_capture(path)

    monkeypatch.setattr("f1_strategist.fingerprint.platform.platform", lambda: "otro-sistema")
    monkeypatch.setattr("f1_strategist.fingerprint.platform.python_version", lambda: "0.0.0")
    second = fingerprint_capture(path)

    assert first.fingerprint == second.fingerprint
    assert first.run_info.platform != second.run_info.platform
    assert first.run_info.python_version != second.run_info.python_version


def test_metric_config_change_changes_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    default = fingerprint_capture(path)
    changed = fingerprint_capture(
        path,
        AppConfig(metrics=MetricsConfig(traffic_gap_s=2.0)),
    )

    assert default.run_info.config_hash != changed.run_info.config_hash
    assert default.fingerprint != changed.fingerprint


def test_non_metric_config_change_changes_hash_and_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    default = fingerprint_capture(path)
    changed = fingerprint_capture(
        path,
        AppConfig(capture=CaptureConfig(enabled=False)),
    )

    assert default.run_info.config_hash != changed.run_info.config_hash
    assert default.fingerprint != changed.fingerprint


def test_equivalent_config_shapes_produce_same_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    default = fingerprint_capture(path)
    metrics_only = fingerprint_capture(path, MetricsConfig())
    app_config = fingerprint_capture(path, AppConfig())

    assert default.run_info.config_hash == metrics_only.run_info.config_hash
    assert metrics_only.run_info.config_hash == app_config.run_info.config_hash
    assert default.fingerprint == metrics_only.fingerprint == app_config.fingerprint


def test_fingerprint_ignores_key_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    run = fingerprint_capture(path)
    metrics = {
        "metric_a": {"value": 1.5, "quality": "ok", "unit": "s"},
        "metric_b": {"value": 2, "quality": "low", "unit": "kg"},
    }
    reversed_metrics = {
        "metric_b": {"unit": "kg", "quality": "low", "value": 2},
        "metric_a": {"unit": "s", "quality": "ok", "value": 1.5},
    }

    first = compute_fingerprint(run.events, run.history, metrics)
    assert run.state is not None
    second = compute_fingerprint(run.state, run.history, reversed_metrics)

    assert first == second

    changed_metrics = {**metrics, "metric_a": {**metrics["metric_a"], "value": 1.6}}
    assert compute_fingerprint(run.events, run.history, changed_metrics) != first


def test_strategy_fingerprint_ignores_track_order_and_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = StrategyConfig(
        tracks={
            "monza": StrategyTrackOverride(min_gain_s=2.0),
            "spa": StrategyTrackOverride(pit_loss_override_s=21.0),
        }
    )
    reordered = StrategyConfig(
        tracks={
            "spa": StrategyTrackOverride(pit_loss_override_s=21.0),
            "monza": StrategyTrackOverride(min_gain_s=2.0),
        }
    )
    fingerprint = compute_strategy_fingerprint(first)
    monkeypatch.setattr("f1_strategist.fingerprint.platform.platform", lambda: "otro-sistema")
    monkeypatch.setattr("f1_strategist.fingerprint.platform.python_version", lambda: "0.0.0")

    assert fingerprint == compute_strategy_fingerprint(reordered)
    assert len(fingerprint) == 64


def test_run_info_has_versions_and_capture_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    info = build_run_info(AppConfig(), path)

    assert info.tool_version
    assert info.parser_version == PARSER_VERSION
    assert info.capture_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info.config_hash == compute_config_hash(AppConfig())
    assert info.python_version
    assert info.platform


def test_fingerprint_includes_simulated_event_time() -> None:
    event = RaceEvent("pit_entry", occurred_at_monotonic=10.0, car_index=0)

    empty_history = LapHistorySnapshot(records={})
    first = compute_fingerprint((event,), empty_history, {})
    second = compute_fingerprint((replace(event, occurred_at_monotonic=20.0),), empty_history, {})

    assert first != second


def test_fingerprint_collector_keeps_events_beyond_state_snapshot_limit() -> None:
    collector = _FingerprintCollector(MetricsEngine())
    for index in range(60):
        collector.state._append_event("test", float(index), value=str(index))

    assert len(collector.state._build_snapshot().events) == 50
    assert len(collector.events) == 60
    empty_history = LapHistorySnapshot(records={})
    original = compute_fingerprint(collector.events, empty_history, {})
    changed_events = (replace(collector.events[0], value="changed"), *collector.events[1:])

    assert compute_fingerprint(changed_events, empty_history, {}) != original


def test_cli_verify_accepts_matching_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _capture(tmp_path, monkeypatch)
    fingerprint = fingerprint_capture(path).fingerprint

    assert main(["verify", str(path), "--runs", "2", "--expect", fingerprint.upper()]) == 0
    out = capsys.readouterr().out
    assert fingerprint in out
    assert "2 ejecución(es)" in out


def test_cli_verify_rejects_wrong_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _capture(tmp_path, monkeypatch)

    assert main(["verify", str(path), "--runs", "1", "--expect", "0" * 64]) == 1
    assert "La huella no coincide" in capsys.readouterr().err


def test_cli_verify_rejects_non_positive_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        main(["verify", str(path), "--runs", "0"])

    assert exc.value.code == 2


def test_cli_verify_rejects_truncated_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _capture(tmp_path, monkeypatch)
    with path.open("ab") as handle:
        handle.write(b"\x01\x02\x03")

    assert main(["verify", str(path), "--runs", "1"]) == 1
    assert "registro incompleto" in capsys.readouterr().err


def test_cli_verify_rejects_capture_without_valid_packets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "empty.f1cap"
    with CaptureWriter(path):
        pass

    assert main(["verify", str(path), "--runs", "1"]) == 1
    assert "no contiene paquetes válidos" in capsys.readouterr().err


def test_cli_verify_rejects_parser_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _capture(tmp_path, monkeypatch)

    def reject(_: bytes) -> ParsedPacket:
        raise PacketParseError("bad_packet")

    monkeypatch.setattr("f1_strategist.offline.parse_packet", reject)

    assert main(["verify", str(path), "--runs", "1"]) == 1
    assert "paquete(s) rechazado(s) por el parser" in capsys.readouterr().err
