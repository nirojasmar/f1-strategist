from __future__ import annotations

from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureError, CaptureReader, CaptureWriter


def _packets() -> list[CapturedPacket]:
    return [
        CapturedPacket(1_000_000_000, "192.168.1.20", 50000, b"abc"),
        CapturedPacket(1_016_000_000, "192.168.1.20", 50000, b""),
        CapturedPacket(1_032_000_000, "fe80::1", 50001, bytes(range(200))),
    ]


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "a.f1cap"
    with CaptureWriter(path, {"nota": "prueba"}) as writer:
        for packet in _packets():
            writer.write(packet)
    reader = CaptureReader(path)
    assert reader.metadata["nota"] == "prueba"
    assert reader.metadata["format_version"] == 1
    assert list(reader) == _packets()
    assert not reader.truncated


def test_truncated_last_record_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "a.f1cap"
    with CaptureWriter(path) as writer:
        for packet in _packets():
            writer.write(packet)
    path.write_bytes(path.read_bytes()[:-10])
    reader = CaptureReader(path)
    assert list(reader) == _packets()[:2]
    assert reader.truncated


def test_bad_magic_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "x.f1cap"
    path.write_bytes(b"NOPE" * 10)
    with pytest.raises(CaptureError):
        CaptureReader(path)


def test_missing_file_is_a_capture_error(tmp_path: Path) -> None:
    with pytest.raises(CaptureError):
        CaptureReader(tmp_path / "no-existe.f1cap")
