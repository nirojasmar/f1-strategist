from __future__ import annotations

import gzip
import struct
from pathlib import Path

from f1_strategist.capture import CapturedPacket, CaptureReader, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.parser import ParticipantsPacket, parse_packet
from f1_strategist.trim import trim_capture

from .conftest import make_packet

_FIXTURES = Path(__file__).parent / "fixtures"


def _lap_for_number(lap_number: int) -> bytes:
    payload = bytearray((_FIXTURES / "lap_data.bin").read_bytes())
    struct.pack_into("<B", payload, 29 + 33, lap_number)
    return bytes(payload)


def _write_capture(
    path: Path,
    packets: list[CapturedPacket],
    metadata: dict[str, object] | None = None,
) -> None:
    with CaptureWriter(path, metadata) as writer:
        for packet in packets:
            writer.write(packet)


def test_trim_filters_types_decimates_and_preserves_times(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    source_packets = [
        CapturedPacket(10_000_000_000, "10.0.0.5", 4000, _lap_for_number(1)),
        CapturedPacket(10_500_000_000, "10.0.0.5", 4000, make_packet(3)),
        CapturedPacket(11_000_000_000, "10.0.0.5", 4000, _lap_for_number(2)),
        CapturedPacket(11_500_000_000, "10.0.0.5", 4000, make_packet(3)),
        CapturedPacket(12_000_000_000, "10.0.0.5", 4000, _lap_for_number(3)),
        CapturedPacket(12_500_000_000, "10.0.0.5", 4000, _lap_for_number(3)),
    ]
    _write_capture(source, source_packets)

    result = trim_capture(
        source,
        destination,
        packet_types=frozenset({2}),
        decimate={2: 2},
    )

    output = list(CaptureReader(destination))
    assert result.packets_read == len(source_packets)
    assert result.packets_written == 2
    assert [packet.payload for packet in output] == [
        source_packets[0].payload,
        source_packets[4].payload,
    ]
    assert [packet.recv_time_ns for packet in output] == [
        source_packets[0].recv_time_ns,
        source_packets[4].recv_time_ns,
    ]


def test_trim_lap_range_keeps_packets_in_the_selected_time_window(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    packets = [
        CapturedPacket(1_000_000_000, "10.0.0.5", 4000, _lap_for_number(1)),
        CapturedPacket(1_500_000_000, "10.0.0.5", 4000, make_packet(3)),
        CapturedPacket(2_000_000_000, "10.0.0.5", 4000, _lap_for_number(2)),
        CapturedPacket(2_500_000_000, "10.0.0.5", 4000, make_packet(3)),
        CapturedPacket(3_000_000_000, "10.0.0.5", 4000, _lap_for_number(3)),
        CapturedPacket(3_500_000_000, "10.0.0.5", 4000, make_packet(3)),
    ]
    _write_capture(source, packets)

    trim_capture(source, destination, laps=(2, 2))
    output = list(CaptureReader(destination))

    assert [packet.payload for packet in output] == [packets[2].payload, packets[3].payload]
    assert [packet.recv_time_ns for packet in output] == [
        packets[2].recv_time_ns,
        packets[3].recv_time_ns,
    ]


def test_trim_anonymizes_participants_and_network_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    participants = bytearray((_FIXTURES / "participants.bin").read_bytes())
    struct.pack_into("<Q", participants, 7, 987654321)
    participant_start = 30
    participants[participant_start + 2] = 77
    participants[participant_start + 7 : participant_start + 39] = b"Real Driver".ljust(32, b"\0")
    packet = CapturedPacket(
        123_000_000,
        "192.168.1.25",
        50000,
        bytes(participants),
    )
    _write_capture(
        source,
        [packet],
        {
            "listen_address": "192.168.1.10",
            "listen_port": 20777,
            "allowed_sources": ["192.168.1.25"],
        },
    )

    trim_capture(source, destination, anonymize=True)
    reader = CaptureReader(destination)
    output = next(iter(reader))
    parsed = parse_packet(output.payload)
    assert isinstance(parsed, ParticipantsPacket)
    first_participant = parsed.participants[0]
    assert first_participant is not None
    assert first_participant.name == "Driver 01"
    assert first_participant.network_id == 0
    assert parsed.header.session_uid == 1
    assert output.source_ip == "127.0.0.1"
    assert output.source_port == 0
    assert reader.metadata["listen_address"] == "127.0.0.1"
    assert reader.metadata["listen_port"] == 0
    assert reader.metadata["allowed_sources"] == []
    assert reader.metadata["anonymized"] is True


def test_trim_cli_accepts_compressed_input(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.f1cap"
    compressed = tmp_path / "source.f1cap.gz"
    destination = tmp_path / "trimmed.f1cap"
    packets = [CapturedPacket(1_000_000_000, "10.0.0.5", 4000, make_packet(3))]
    _write_capture(source, packets)
    with source.open("rb") as raw, gzip.open(compressed, "wb") as zipped:
        zipped.write(raw.read())

    assert main(["trim", str(compressed), str(destination), "--types", "3"]) == 0
    assert "1 de 1 paquetes" in capsys.readouterr().out
    assert [packet.payload for packet in CaptureReader(destination)] == [packets[0].payload]


def test_trim_rejects_overwriting_source(tmp_path: Path) -> None:
    path = tmp_path / "same.f1cap"
    _write_capture(path, [])

    result = main(["trim", str(path), str(path)])

    assert result == 2
