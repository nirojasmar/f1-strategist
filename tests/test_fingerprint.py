"""Huella determinista, metadatos y comando `verify`."""

from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.config import AppConfig, MetricsConfig
from f1_strategist.fingerprint import (
    build_run_info,
    compute_config_hash,
    compute_fingerprint,
    fingerprint_capture,
)
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import PARSER_VERSION, ParsedPacket
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
