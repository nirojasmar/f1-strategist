from __future__ import annotations

import csv
import io
import json
import struct
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.export import (
    EXPORT_SCHEMA_VERSION,
    LAP_COLUMNS,
    laps_to_rows,
    render_metrics_export,
    snapshot_to_dict,
)
from f1_strategist.metrics.history import LapHistory, LapRecord
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


def _metric(
    key: str,
    value: float | int | str | bool | tuple[str, ...] | None,
    unit: str,
    quality: Quality,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("laps", 5),
        quality=quality,
        sample_count=3,
        computed_at_monotonic=12.5,
        sources=("lap_data", "car_status"),
        notes=("test_note",),
    )


def _snapshot() -> MetricsSnapshot:
    return MetricsSnapshot(
        metrics={
            "fuel.margin_laps_at_finish": _metric(
                "fuel.margin_laps_at_finish", 1.5, "lap", Quality.OK
            ),
            "tyres.wear_rate_pct_per_lap": _metric(
                "tyres.wear_rate_pct_per_lap", None, "%/lap", Quality.INSUFFICIENT
            ),
        },
        computed_at_monotonic=12.5,
        session_uid=0xF125,
    )


def _history() -> LapHistory:
    history = LapHistory(10)
    history.upsert(
        LapRecord(
            car_index=1,
            lap_number=2,
            lap_time_s=91.0,
            tyre_wear_start_pct=(1.0, 2.0, 3.0, 4.0),
            track_status_seen=frozenset({"safety_car", "green"}),
            flags=frozenset({"first_lap"}),
            closed=True,
        )
    )
    history.upsert(LapRecord(car_index=0, lap_number=1, lap_time_s=90.0, closed=True))
    history.upsert(LapRecord(car_index=1, lap_number=1, lap_time_s=90.5, closed=True))
    return history


def _write_capture(path: Path, packets: list[tuple[ParsedPacket, float]]) -> None:
    with CaptureWriter(path) as writer:
        for index, (_, observed_at) in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=int(observed_at * 1_000_000_000),
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


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def test_snapshot_to_dict_is_stable_and_serializable() -> None:
    payload = snapshot_to_dict(_snapshot())

    assert payload["schema_version"] == EXPORT_SCHEMA_VERSION
    assert payload["session_uid"] == 0xF125
    assert payload["computed_at_monotonic"] == 12.5
    assert list(payload["metrics"]) == [
        "fuel.margin_laps_at_finish",
        "tyres.wear_rate_pct_per_lap",
    ]
    fuel = payload["metrics"]["fuel.margin_laps_at_finish"]
    assert fuel["value"] == 1.5
    assert fuel["unit"] == "lap"
    assert fuel["quality"] == "ok"
    assert fuel["window"] == {"kind": "laps", "size": 5}
    assert fuel["sample_count"] == 3
    assert fuel["sources"] == ["lap_data", "car_status"]
    assert fuel["notes"] == ["test_note"]
    tyres = payload["metrics"]["tyres.wear_rate_pct_per_lap"]
    assert tyres["value"] is None
    assert tyres["quality"] == "insufficient"

    assert json.loads(json.dumps(payload)) == payload


def test_empty_snapshot_to_dict_has_no_metrics() -> None:
    payload = snapshot_to_dict(MetricsSnapshot())

    assert payload == {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "session_uid": None,
        "computed_at_monotonic": None,
        "metrics": {},
    }


def test_laps_to_rows_is_sorted_and_normalizes_collections() -> None:
    rows = laps_to_rows(_history())

    assert [(row["car_index"], row["lap_number"]) for row in rows] == [
        (0, 1),
        (1, 1),
        (1, 2),
    ]
    assert set(rows[0]) == set(LAP_COLUMNS)
    lap = rows[2]
    assert lap["track_status_seen"] == ["green", "safety_car"]
    assert lap["flags"] == ["first_lap"]
    assert lap["tyre_wear_start_pct"] == [1.0, 2.0, 3.0, 4.0]
    assert lap["closed"] is True
    assert json.loads(json.dumps(rows)) == rows


def test_laps_to_rows_accepts_empty_history() -> None:
    assert laps_to_rows(LapHistory()) == []


def test_render_metrics_export_supports_json_jsonl_and_csv() -> None:
    snapshot = _snapshot()
    history = _history()

    json_text = render_metrics_export(snapshot, history, "json")
    assert json.loads(json_text) == snapshot_to_dict(snapshot)

    jsonl_text = render_metrics_export(snapshot, history, "jsonl")
    jsonl_rows = [json.loads(line) for line in jsonl_text.splitlines()]
    assert jsonl_rows == laps_to_rows(history)

    csv_text = render_metrics_export(snapshot, history, "csv")
    parsed = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(parsed) == 3
    assert [(row["car_index"], row["lap_number"]) for row in parsed] == [
        ("0", "1"),
        ("1", "1"),
        ("1", "2"),
    ]
    assert parsed[0]["lap_time_source"] == ""
    assert parsed[0]["pit_entry"] == "false"
    assert parsed[2]["tyre_wear_start_pct"] == "[1.0,2.0,3.0,4.0]"
    assert parsed[2]["track_status_seen"] == '["green","safety_car"]'
    assert csv_text.splitlines()[0].split(",") == list(LAP_COLUMNS)
    assert "\r\n" not in csv_text


def test_render_metrics_export_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Formato de exportación"):
        render_metrics_export(_snapshot(), _history(), "xml")


def test_metrics_command_exports_a_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    _patch_parser(monkeypatch, packets)
    capture = tmp_path / "race.f1cap"
    _write_capture(capture, packets)

    assert main(["metrics", str(capture)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == EXPORT_SCHEMA_VERSION
    assert payload["session_uid"] == packets[0][0].header.session_uid
    assert payload["computed_at_monotonic"] == pytest.approx(packets[-1][1])
    for family in (
        "progress.",
        "fuel.",
        "tyres.",
        "pace.",
        "gap.",
        "pit.",
        "ers.",
        "damage.",
        "env.",
        "track.",
    ):
        assert any(key.startswith(family) for key in payload["metrics"]), family

    jsonl_out = tmp_path / "laps.jsonl"
    assert main(["metrics", str(capture), "--format", "jsonl", "--out", str(jsonl_out)]) == 0
    rows = [json.loads(line) for line in jsonl_out.read_text(encoding="utf-8").splitlines()]
    assert [(row["car_index"], row["lap_number"]) for row in rows[:6]] == [
        (0, 1),
        (0, 2),
        (0, 3),
        (1, 1),
        (1, 2),
        (1, 3),
    ]
    assert all("incomplete" in row["flags"] for row in rows[6:])

    csv_out = tmp_path / "laps.csv"
    assert main(["metrics", str(capture), "--format", "csv", "--out", str(csv_out)]) == 0
    csv_text = csv_out.read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].split(",") == list(LAP_COLUMNS)
    assert len(list(csv.DictReader(io.StringIO(csv_text)))) == len(rows)


def test_metrics_command_rejects_invalid_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "empty.f1cap"
    with CaptureWriter(capture):
        pass

    assert main(["metrics", str(capture), "--format", "xml"]) == 2
    assert "Formato de exportación" in capsys.readouterr().err


def test_metrics_command_handles_an_empty_capture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "empty.f1cap"
    with CaptureWriter(capture):
        pass

    assert main(["metrics", str(capture)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "session_uid": None,
        "computed_at_monotonic": None,
        "metrics": {},
    }
