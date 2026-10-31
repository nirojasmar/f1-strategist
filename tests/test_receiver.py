from __future__ import annotations

import queue
import socket
import time

import pytest

from f1_strategist.capture import CapturedPacket
from f1_strategist.config import TelemetryConfig
from f1_strategist.receiver import ReceiverError, Stats, UdpReceiver


def _send(port: int, payloads: list[bytes]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for payload in payloads:
            sock.sendto(payload, ("127.0.0.1", port))


def _start(cfg: TelemetryConfig) -> tuple[UdpReceiver, queue.Queue[CapturedPacket], Stats]:
    out: queue.Queue[CapturedPacket] = queue.Queue()
    stats = Stats()
    receiver = UdpReceiver(cfg, out, stats)
    receiver.start()
    return receiver, out, stats


def test_receives_datagrams_in_order() -> None:
    receiver, out, stats = _start(TelemetryConfig(listen_port=0))
    try:
        _send(receiver.bound_port, [b"uno", b"dos", b"tres"])
        got = [out.get(timeout=3) for _ in range(3)]
    finally:
        receiver.stop()
    assert [p.payload for p in got] == [b"uno", b"dos", b"tres"]
    assert all(p.source_ip == "127.0.0.1" for p in got)
    snap = stats.snapshot()
    assert snap.received == 3
    assert snap.sources == {"127.0.0.1": 3}


def test_source_filter_rejects_other_origins() -> None:
    cfg = TelemetryConfig(listen_port=0, allowed_sources=["10.255.255.1"])
    receiver, out, stats = _start(cfg)
    try:
        _send(receiver.bound_port, [b"x", b"y"])
        deadline = time.monotonic() + 3
        while stats.snapshot().rejected_source < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        receiver.stop()
    snap = stats.snapshot()
    assert snap.rejected_source == 2
    assert snap.received == 0
    assert out.empty()


def test_full_queue_counts_drops() -> None:
    out: queue.Queue[CapturedPacket] = queue.Queue(maxsize=1)
    stats = Stats()
    receiver = UdpReceiver(TelemetryConfig(listen_port=0), out, stats)
    receiver.start()
    try:
        _send(receiver.bound_port, [b"a", b"b", b"c"])
        deadline = time.monotonic() + 3
        while stats.snapshot().dropped_queue_full < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        receiver.stop()
    assert stats.snapshot().dropped_queue_full == 2


def test_port_in_use_gives_clear_error() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        with pytest.raises(ReceiverError, match="No se pudo abrir UDP"):
            _start(TelemetryConfig(listen_port=port))
