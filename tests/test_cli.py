from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import f1_strategist.cli as cli_module
from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main

from .conftest import make_packet


def test_run_with_invalid_config_exits_with_error(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[telemetry]\nlisten_port = 99999\n", encoding="utf-8")
    assert main(["run", "--config", str(config)]) == 2


def test_run_with_port_in_use_exits_with_error(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        assert main(["run", "--port", str(port), "--no-capture"]) == 2
    assert "No se pudo abrir UDP" in capsys.readouterr().err


def test_inspect_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.f1cap"
    with CaptureWriter(path) as writer:
        for i in range(4):
            writer.write(CapturedPacket(i * 1_000_000, "10.0.0.5", 4000, make_packet(2)))
    assert main(["inspect", str(path)]) == 0
    out = capsys.readouterr().out
    assert "4 paquetes" in out
    assert "LapData" in out
    assert "2025" in out


def test_inspect_missing_file_exits_with_error(tmp_path: Path) -> None:
    assert main(["inspect", str(tmp_path / "no-existe.f1cap")]) == 2


def test_events_summary_reads_rows_and_reports_suppression(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detection = SimpleNamespace(
        t_sim_s=12.0,
        lap=4,
        rule="fuel_margin",
        event_id="fuel:1",
        phase="opened",
        priority="critical",
        category="fuel",
        message_key="event.fuel_margin_critical",
        facts_json='[["margin_kg",1.0]]',
        material_key="fuel:4",
    )
    decision = SimpleNamespace(
        t_sim_s=12.0,
        event_id="fuel:1",
        phase="opened",
        outcome="suppress",
        reason="quiet_period",
        alert_id=None,
        interrupt=False,
    )

    class Store:
        def __enter__(self) -> Store:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get_session(self, session_id: int) -> object:
            return object()

        def iter_detected_events(self, session_id: int) -> tuple[object, ...]:
            return (detection,)

        def iter_gate_decisions(self, session_id: int) -> tuple[object, ...]:
            return (decision,)

    monkeypatch.setattr(cli_module.SessionStore, "open", lambda *_args, **_kwargs: Store())
    assert main(["events", "summary", "--session", "7", "--db", "unused.sqlite"]) == 0
    output = capsys.readouterr().out
    assert "Decisiones suprimidas: 1" in output
    assert "quiet_period" in output
    assert "fuel_margin" in output


def test_events_commands_report_missing_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Store:
        def __enter__(self) -> Store:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get_session(self, session_id: int) -> None:
            return None

    monkeypatch.setattr(cli_module.SessionStore, "open", lambda *_args, **_kwargs: Store())
    assert main(["events", "show", "--session", "404", "--db", "unused.sqlite"]) == 2
    assert "No existe la sesión 404" in capsys.readouterr().err
