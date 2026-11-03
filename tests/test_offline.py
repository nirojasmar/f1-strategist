from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.offline import run_capture_offline
from f1_strategist.parser import PacketParseError, ParseIssue
from f1_strategist.race_state import RaceState

from .conftest import make_packet


@dataclass
class RecordingConsumer:
    received: list[tuple[object, float]] = field(default_factory=list)

    def apply(self, packet: object, observed_at: float) -> None:
        self.received.append((packet, observed_at))


def _write_capture(path: Path, payloads: list[bytes], times_ns: list[int]) -> None:
    with CaptureWriter(path) as writer:
        for payload, recv_time_ns in zip(payloads, times_ns, strict=True):
            writer.write(CapturedPacket(recv_time_ns, "127.0.0.1", 20777, payload))


def test_offline_runner_is_deterministic_and_scales_observed_time(tmp_path: Path) -> None:
    path = tmp_path / "offline.f1cap"
    fixture_dir = Path(__file__).parent / "fixtures"
    payloads = [
        (fixture_dir / "session.bin").read_bytes(),
        (fixture_dir / "car_status.bin").read_bytes(),
    ]
    _write_capture(path, payloads, [20_000_000_000, 22_000_000_000])

    first_consumer = RecordingConsumer()
    second_consumer = RecordingConsumer()
    first_state = RaceState()
    second_state = RaceState()
    first = run_capture_offline(path, [first_consumer, first_state], speed=2.0)
    second = run_capture_offline(path, [second_consumer, second_state], speed=2.0)

    assert first == second
    assert first.packets_read == first.packets_parsed == 2
    assert first.parser_rejections == {}
    assert first.ignored_packets_by_id == {}
    assert not first.truncated
    assert [observed_at for _, observed_at in first_consumer.received] == [0.0, 1.0]
    assert first_consumer.received == second_consumer.received
    assert first_state.snapshot() == second_state.snapshot()
    assert first_state.snapshot().freshness_for("car_status").updated_at_monotonic == 1.0


def test_offline_runner_counts_rejections_and_ignored_packets(tmp_path: Path) -> None:
    path = tmp_path / "counts.f1cap"
    _write_capture(
        path,
        [make_packet(7), make_packet(0)],
        [1_000_000_000, 2_000_000_000],
    )

    result = run_capture_offline(path, [])

    assert result.packets_read == 2
    assert result.packets_parsed == 0
    assert sum(result.parser_rejections.values()) == 1
    assert result.ignored_packets_by_id == {0: 1}
    assert not result.truncated


def test_offline_runner_bounds_distinct_parser_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "many-rejections.f1cap"
    payloads = [f"issue-{index}".encode() for index in range(20)]
    _write_capture(path, payloads, list(range(1_000_000_000, 1_000_000_000 + len(payloads))))

    def reject_with_unique_issue(payload: bytes) -> None:
        raise PacketParseError(payload.decode())

    monkeypatch.setattr("f1_strategist.offline.parse_packet", reject_with_unique_issue)
    result = run_capture_offline(path, [])

    assert len(result.parser_rejections) == 17
    assert result.parser_rejections[ParseIssue("other_parser_rejections_limited")] == 4
    assert sum(result.parser_rejections.values()) == len(payloads)


def test_offline_runner_reports_truncated_capture(tmp_path: Path) -> None:
    path = tmp_path / "truncated.f1cap"
    fixture = (Path(__file__).parent / "fixtures" / "car_status.bin").read_bytes()
    _write_capture(path, [fixture, fixture], [1_000_000_000, 2_000_000_000])
    path.write_bytes(path.read_bytes()[:-10])

    result = run_capture_offline(path, [])

    assert result.packets_read == result.packets_parsed == 1
    assert result.truncated


@pytest.mark.parametrize("speed", [-1.0, float("nan"), float("inf")])
def test_offline_runner_rejects_invalid_speed(tmp_path: Path, speed: float) -> None:
    path = tmp_path / "empty.f1cap"
    with CaptureWriter(path):
        pass

    with pytest.raises(ValueError, match="speed"):
        run_capture_offline(path, [], speed=speed)
