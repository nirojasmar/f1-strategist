"""Catálogo de escenarios: manifiesto, verificación y comandos."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.datasets import (
    DEFAULT_MANIFEST,
    ManifestError,
    Scenario,
    ScenarioExpectations,
    load_manifest,
    verify_scenario,
)
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import PacketParseError, ParsedPacket
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed

_REAL_MANIFEST = Path(__file__).parent / "fixtures" / "real" / "manifest.toml"


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


def _write_capture(
    path: Path,
    packets: list[tuple[ParsedPacket, float]],
) -> None:
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


def _patch_parser(
    monkeypatch: pytest.MonkeyPatch,
    packets: list[tuple[ParsedPacket, float]],
) -> None:
    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)


def _synthetic_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    path = tmp_path / "race.f1cap"
    _write_capture(path, packets)
    _patch_parser(monkeypatch, packets)
    return path


def _expected(
    *,
    track: str = "monza",
    session_type: str = "race",
    total_laps: int = 3,
    cars: int = 2,
    pit_stops_total: int = 1,
    best_lap_ms: int = 90000,
    game_version: str = "1.26",
) -> ScenarioExpectations:
    return ScenarioExpectations(
        track=track,
        session_type=session_type,
        total_laps=total_laps,
        cars=cars,
        pit_stops_total=pit_stops_total,
        best_lap_ms=best_lap_ms,
        game_version=game_version,
    )


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"valor TOML no soportado: {value!r}")


def _scenario_entry(
    *,
    scenario_id: str = "synthetic",
    file: str = "race.f1cap",
    sha256: str,
    size: int,
    expected: ScenarioExpectations,
    description: str = "Escenario sintético",
    tags: tuple[str, ...] = ("pit_stops",),
) -> str:
    tags_text = ", ".join(json.dumps(tag) for tag in tags)
    expected_text = "\n".join(
        f"{key} = {_toml_value(getattr(expected, key))}"
        for key in (
            "track",
            "session_type",
            "total_laps",
            "cars",
            "pit_stops_total",
            "best_lap_ms",
            "game_version",
        )
    )
    return (
        "[[scenario]]\n"
        f"id = {json.dumps(scenario_id)}\n"
        f"file = {json.dumps(file)}\n"
        f"sha256 = {json.dumps(sha256)}\n"
        f"bytes = {size}\n"
        f"description = {json.dumps(description)}\n"
        f"tags = [{tags_text}]\n"
        "\n"
        "[scenario.expected]\n"
        f"{expected_text}\n"
    )


def _write_manifest(
    path: Path,
    capture: Path,
    expected: ScenarioExpectations | None = None,
    *,
    sha256: str | None = None,
    size: int | None = None,
    file: str = "race.f1cap",
) -> Path:
    data = capture.read_bytes()
    path.write_text(
        _scenario_entry(
            file=file,
            sha256=sha256 or hashlib.sha256(data).hexdigest(),
            size=len(data) if size is None else size,
            expected=expected or _expected(),
        ),
        encoding="utf-8",
    )
    return path


def test_load_manifest_and_verify_synthetic_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = load_manifest(_write_manifest(tmp_path / "manifest.toml", capture))

    scenario = manifest.get("synthetic")
    assert scenario.available
    result = verify_scenario(scenario)

    assert result.ok, result.problems
    assert result.observation is not None
    assert result.observation.track == "monza"
    assert result.observation.session_type == "race"
    assert result.observation.total_laps == 3
    assert result.observation.cars == 2
    assert result.observation.pit_stops_total == 1
    assert result.observation.best_lap_ms == 90000
    assert result.observation.game_version == "1.26"


def test_default_manifest_is_independent_of_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    manifest = load_manifest()

    assert DEFAULT_MANIFEST.is_absolute()
    assert manifest.path == DEFAULT_MANIFEST
    assert manifest.get("monza-13-laps").id == "monza-13-laps"


def test_verify_detects_wrong_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = load_manifest(_write_manifest(tmp_path / "manifest.toml", capture, sha256="0" * 64))

    result = verify_scenario(manifest.get("synthetic"))

    assert not result.ok
    assert any("sha256 no coincide" in problem for problem in result.problems)


def test_verify_rejects_truncated_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    with capture.open("ab") as handle:
        handle.write(b"\x01\x02\x03")
    manifest = load_manifest(_write_manifest(tmp_path / "manifest.toml", capture))

    result = verify_scenario(manifest.get("synthetic"))

    assert not result.ok
    assert "la captura termina con un registro incompleto" in result.problems


def test_verify_rejects_parser_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)

    def reject(_: bytes) -> ParsedPacket:
        raise PacketParseError("bad_packet")

    monkeypatch.setattr("f1_strategist.offline.parse_packet", reject)
    manifest = load_manifest(_write_manifest(tmp_path / "manifest.toml", capture))

    result = verify_scenario(manifest.get("synthetic"))

    assert not result.ok
    assert any("paquete(s) rechazado(s) por el parser" in problem for problem in result.problems)


def test_verify_detects_unmet_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.toml",
            capture,
            replace(_expected(), total_laps=4, cars=3),
        )
    )

    result = verify_scenario(manifest.get("synthetic"))

    assert not result.ok
    messages = "\n".join(result.problems)
    assert "expectativa 'total_laps' incumplida" in messages
    assert "esperado 4, obtenido 3" in messages
    assert "expectativa 'cars' incumplida" in messages


def test_verify_reports_missing_capture_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = load_manifest(
        _write_manifest(tmp_path / "manifest.toml", capture, file="missing.f1cap")
    )

    scenario = manifest.get("synthetic")
    result = verify_scenario(scenario)

    assert not scenario.available
    assert not result.available
    assert not result.ok
    assert result.problems == (f"archivo no disponible: {scenario.file}",)


def test_manifest_rejects_duplicate_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    data = capture.read_bytes()
    entry = _scenario_entry(
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        expected=_expected(),
    )
    path = tmp_path / "manifest.toml"
    path.write_text(entry + "\n" + entry, encoding="utf-8")

    with pytest.raises(ManifestError, match="identificador duplicado"):
        load_manifest(path)


def test_cli_datasets_list_shows_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = _write_manifest(tmp_path / "manifest.toml", capture)

    assert main(["datasets", "list", "--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "synthetic" in out
    assert "disponible" in out
    assert "Escenario sintético" in out
    assert "pit_stops" in out


def test_cli_datasets_verify_reports_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = _write_manifest(tmp_path / "manifest.toml", capture, sha256="1" * 64)

    assert main(["datasets", "verify", "--manifest", str(manifest)]) == 1
    err = capsys.readouterr().err
    assert "verificación fallida" in err
    assert "sha256 no coincide" in err


def test_cli_datasets_verify_unavailable_is_not_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = _write_manifest(tmp_path / "manifest.toml", capture, file="missing.f1cap")

    assert main(["datasets", "verify", "--manifest", str(manifest)]) == 0
    captured = capsys.readouterr()
    assert "no disponible" in captured.out
    assert captured.err == ""


def test_cli_datasets_verify_unknown_id_exits_with_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _synthetic_capture(tmp_path, monkeypatch)
    manifest = _write_manifest(tmp_path / "manifest.toml", capture)

    assert main(["datasets", "verify", "--id", "otro", "--manifest", str(manifest)]) == 2
    assert "No existe el escenario 'otro'" in capsys.readouterr().err


_REAL_SCENARIOS = load_manifest(_REAL_MANIFEST).scenarios


@pytest.mark.real_capture
@pytest.mark.parametrize("scenario", _REAL_SCENARIOS, ids=[s.id for s in _REAL_SCENARIOS])
def test_real_manifest_scenario_matches_expectations(scenario: Scenario) -> None:
    if not scenario.available:
        pytest.skip(f"captura no disponible: {scenario.file}")
    result = verify_scenario(scenario)
    assert result.ok, result.problems
