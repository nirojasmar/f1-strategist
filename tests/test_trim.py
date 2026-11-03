from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureReader, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.packets import HEADER_SIZE, PacketHeader, parse_header
from f1_strategist.parser import (
    SESSION_LINK_IDENTIFIER_OFFSETS,
    ParticipantsPacket,
    parse_packet,
)
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


def _header(packet: CapturedPacket) -> PacketHeader:
    header = parse_header(packet.payload)
    assert header is not None
    return header


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


@pytest.mark.parametrize("packet_id", [11, 12])
@pytest.mark.parametrize(("interval", "expected_per_car"), [(2, 11), (11, 2), (22, 1)])
def test_trim_decimates_rotating_packets_per_car(
    tmp_path: Path,
    packet_id: int,
    interval: int,
    expected_per_car: int,
) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    packets = [
        CapturedPacket(
            1_000_000_000 + cycle * 22 + car,
            "127.0.0.1",
            1,
            make_packet(packet_id, extra=bytes([car])),
        )
        for cycle in range(22)
        for car in range(22)
    ]
    _write_capture(source, packets)

    trim_capture(source, destination, decimate={packet_id: interval})
    output = list(CaptureReader(destination))

    counts = [0] * 22
    for packet in output:
        counts[packet.payload[HEADER_SIZE]] += 1
    assert counts == [expected_per_car] * 22


def test_trim_anonymizes_multiple_session_uids_and_preserves_zero(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    packets = [
        CapturedPacket(
            1_000_000_000 + index,
            "192.168.1.1",
            1,
            make_packet(3, session_uid=uid),
        )
        for index, uid in enumerate((0, 100, 200, 100, 0, 200))
    ]
    _write_capture(source, packets)

    trim_capture(source, destination, anonymize=True)
    output_uids = [_header(packet).session_uid for packet in CaptureReader(destination)]

    assert output_uids == [0, 1, 2, 1, 0, 2]


def test_trim_anonymization_drops_lobby_info_and_scrubs_metadata_and_times(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    packets = [
        CapturedPacket(
            1_700_000_000_000_000_000 + i * 5_000_000_000,
            "10.0.0.2",
            40,
            payload,
        )
        for i, payload in enumerate((make_packet(3), make_packet(9), make_packet(3)))
    ]
    _write_capture(
        source,
        packets,
        {
            "platform": "private platform",
            "custom_private_field": "secret",
            "tool": "f1-strategist",
            "tool_version": "test",
        },
    )

    result = trim_capture(source, destination, anonymize=True)
    reader = CaptureReader(destination)
    output = list(reader)

    assert result.dropped_for_anonymization == 1
    assert [_header(packet).packet_id for packet in output] == [3, 3]
    assert [packet.recv_time_ns for packet in output] == [
        946_684_800_000_000_000,
        946_684_810_000_000_000,
    ]
    assert "platform" not in reader.metadata
    assert "created_utc" not in reader.metadata
    assert "custom_private_field" not in reader.metadata
    assert reader.metadata["anonymized"] is True


def test_trim_anonymizes_session_link_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    session = bytearray((_FIXTURES / "session.bin").read_bytes())
    for offset, value in zip(SESSION_LINK_IDENTIFIER_OFFSETS, (101, 202, 303), strict=True):
        struct.pack_into("<I", session, offset, value)
    _write_capture(
        source,
        [CapturedPacket(100, "127.0.0.1", 1, bytes(session))],
    )

    trim_capture(source, destination, anonymize=True)
    result = next(iter(CaptureReader(destination)))

    assert [
        struct.unpack_from("<I", result.payload, offset)[0]
        for offset in SESSION_LINK_IDENTIFIER_OFFSETS
    ] == [0, 0, 0]


def test_trim_anonymization_preserves_ai_names_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    participants = bytearray((_FIXTURES / "participants.bin").read_bytes())
    record_start = HEADER_SIZE + 1
    participants[record_start] = 1
    participants[record_start + 7 : record_start + 39] = b"Public AI".ljust(32, b"\0")
    _write_capture(source, [CapturedPacket(100, "127.0.0.1", 1, bytes(participants))])

    trim_capture(source, destination, anonymize=True, keep_ai_names=True)
    parsed = parse_packet(next(iter(CaptureReader(destination))).payload)

    assert isinstance(parsed, ParticipantsPacket)
    assert parsed.participants[0] is not None
    assert parsed.participants[0].name == "Public AI"


def test_trim_gzip_output_is_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    first = tmp_path / "first.f1cap.gz"
    second = tmp_path / "second.f1cap.gz"
    _write_capture(
        source,
        [CapturedPacket(100, "192.168.1.1", 10, make_packet(3))],
        {"created_utc": "source timestamp"},
    )

    trim_capture(source, first, anonymize=True)
    trim_capture(source, second, anonymize=True)

    assert first.read_bytes() == second.read_bytes()
    assert len(list(CaptureReader(first))) == 1


def test_trim_lap_range_keeps_last_session_and_participants_before_window(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap"
    packets = [
        CapturedPacket(
            500_000_000,
            "127.0.0.1",
            1,
            (_FIXTURES / "session.bin").read_bytes(),
        ),
        CapturedPacket(
            600_000_000,
            "127.0.0.1",
            1,
            (_FIXTURES / "participants.bin").read_bytes(),
        ),
        CapturedPacket(1_000_000_000, "127.0.0.1", 1, _lap_for_number(1)),
        CapturedPacket(2_000_000_000, "127.0.0.1", 1, _lap_for_number(2)),
        CapturedPacket(3_000_000_000, "127.0.0.1", 1, _lap_for_number(3)),
    ]
    _write_capture(source, packets)

    trim_capture(source, destination, laps=(2, 2))
    output = list(CaptureReader(destination))

    assert [_header(packet).packet_id for packet in output] == [1, 4, 2]
    assert [packet.recv_time_ns for packet in output[:2]] == [500_000_000, 600_000_000]


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


def test_trim_rejects_unknown_output_suffix(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.bin"
    _write_capture(source, [])

    with pytest.raises(ValueError, match="f1cap"):
        trim_capture(source, destination)


def test_trim_cli_accepts_gzip_output(tmp_path: Path) -> None:
    source = tmp_path / "source.f1cap"
    destination = tmp_path / "trimmed.f1cap.gz"
    _write_capture(source, [CapturedPacket(100, "127.0.0.1", 1, make_packet(3))])

    assert main(["trim", str(source), str(destination), "--anonymize"]) == 0
    assert len(list(CaptureReader(destination))) == 1
