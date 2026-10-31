from __future__ import annotations

import socket
from pathlib import Path

import pytest

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
