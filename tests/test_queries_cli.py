"""Comandos `queries`: presentación, resumen y exportación de consultas."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from f1_strategist.cli import main
from f1_strategist.store import SessionInfo, SessionStore, StoredPilotQuery


def _session() -> SessionInfo:
    return SessionInfo(
        session_uid=0xF125,
        source="live",
        started_at_utc="2026-11-05T10:00:00Z",
        track="monza",
        session_type="race",
    )


def _query(
    seq: int,
    *,
    query_id: str,
    text: str,
    intent: str,
    answer_kind: str,
    answer_text: str,
    latency_s: float | None,
    followup_status: str = "none",
) -> StoredPilotQuery:
    return StoredPilotQuery(
        seq=seq,
        t_sim_s=float(seq),
        query_id=query_id,
        text=text,
        language="es",
        intent=intent,
        answer_text=answer_text,
        answer_kind=answer_kind,
        message_key=f"pilot_{intent}",
        latency_s=latency_s,
        followup_status=followup_status,
    )


def _database(tmp_path: Path, *, with_queries: bool = True) -> tuple[Path, int]:
    path = tmp_path / "sessions.db"
    with SessionStore.open(path) as store:
        session_id = store.create_session(_session())
        if with_queries:
            store.add_pilot_queries(
                session_id,
                [
                    _query(
                        1,
                        query_id="q-1",
                        text="¿Cuánto combustible queda?",
                        intent="fuel_status",
                        answer_kind="answer",
                        answer_text="Quedan 12,4 kg (medido).",
                        latency_s=0.004,
                    ),
                    _query(
                        2,
                        query_id="q-2",
                        text="Bla bla",
                        intent="unknown",
                        answer_kind="unknown",
                        answer_text="No he entendido la pregunta.",
                        latency_s=0.01,
                    ),
                    _query(
                        3,
                        query_id="q-3",
                        text="¿Cómo voy respecto a Hamilton?",
                        intent="compare_driver",
                        answer_kind="clarification",
                        answer_text="¿A cuál te refieres?",
                        latency_s=0.02,
                        followup_status="pending",
                    ),
                ],
            )
    return path, session_id


def test_queries_show_lists_and_filters_by_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, session_id = _database(tmp_path)
    assert main(["queries", "show", "--session", str(session_id), "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "fuel_status" in output
    assert "compare_driver" in output
    assert "clarification" in output

    assert (
        main(
            [
                "queries",
                "show",
                "--session",
                str(session_id),
                "--intent",
                "fuel_status",
                "--db",
                str(path),
            ]
        )
        == 0
    )
    filtered = capsys.readouterr().out
    assert "fuel_status" in filtered
    assert "unknown" not in filtered


def test_queries_summary_reports_rates_and_latencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, session_id = _database(tmp_path)
    assert main(["queries", "summary", "--session", str(session_id), "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "33.3 %" in output
    assert "p50" in output and "0.010" in output
    assert "p95" in output and "0.019" in output
    assert "fuel_status" in output and "unknown" in output


@pytest.mark.parametrize("export_format", ("json", "jsonl", "csv"))
def test_queries_export_formats(
    export_format: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, session_id = _database(tmp_path)
    assert (
        main(
            [
                "queries",
                "export",
                "--session",
                str(session_id),
                "--format",
                export_format,
                "--db",
                str(path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    if export_format == "json":
        payload = json.loads(output)
        assert payload["session_id"] == session_id
        assert [row["query_id"] for row in payload["pilot_queries"]] == ["q-1", "q-2", "q-3"]
    elif export_format == "jsonl":
        rows = [json.loads(line) for line in output.splitlines()]
        assert [row["query_id"] for row in rows] == ["q-1", "q-2", "q-3"]
    else:
        rows = list(csv.DictReader(io.StringIO(output)))
        assert [row["query_id"] for row in rows] == ["q-1", "q-2", "q-3"]
        assert rows[0]["intent"] == "fuel_status"


def test_queries_commands_report_missing_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = _database(tmp_path, with_queries=False)
    assert main(["queries", "show", "--session", "99", "--db", str(path)]) == 2
    assert "No existe la sesión 99" in capsys.readouterr().err


def test_queries_show_without_queries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, session_id = _database(tmp_path, with_queries=False)
    assert main(["queries", "show", "--session", str(session_id), "--db", str(path)]) == 0
    assert "no tiene consultas" in capsys.readouterr().out


def test_queries_reads_do_not_modify_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, session_id = _database(tmp_path)
    before = path.read_bytes()
    for command in ("show", "summary", "export"):
        assert main(["queries", command, "--session", str(session_id), "--db", str(path)]) == 0
        capsys.readouterr()
    assert path.read_bytes() == before
