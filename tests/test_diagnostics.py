from __future__ import annotations

from f1_strategist.config import TelemetryConfig
from f1_strategist.diagnostics import ConnectionState, connection_state, diagnose
from f1_strategist.receiver import PacketSequenceStats, StatsSnapshot

CFG = TelemetryConfig(no_data_warning_s=3.0)


def test_waiting_then_hint_after_timeout() -> None:
    snap = StatsSnapshot(started_monotonic=100.0)
    assert connection_state(CFG, snap, 101.0) is ConnectionState.WAITING
    assert diagnose(CFG, snap, 101.0) == []
    messages = diagnose(CFG, snap, 105.0)
    assert any("firewall" in m for m in messages)
    assert any("loopback" in m for m in messages)  # escucha solo en loopback


def test_rejected_sources_are_reported() -> None:
    snap = StatsSnapshot(started_monotonic=0.0, rejected_source=4, rejected_sources={"10.0.0.9": 4})
    messages = diagnose(CFG, snap, 1.0)
    assert "10.0.0.9" in messages[0]


def test_stalled_after_data() -> None:
    snap = StatsSnapshot(started_monotonic=0.0, received=10, last_packet_monotonic=10.0)
    assert connection_state(CFG, snap, 11.0) is ConnectionState.RECEIVING
    assert connection_state(CFG, snap, 20.0) is ConnectionState.STALLED
    assert "Sin datos desde hace" in diagnose(CFG, snap, 20.0)[0]


def test_sequence_anomalies_are_reported_with_loss_estimation_caveat() -> None:
    snap = StatsSnapshot(
        started_monotonic=0.0,
        received=4,
        sequence_by_id={
            6: PacketSequenceStats(frame_gaps=2, duplicates=1, out_of_order=1)
        },
    )

    messages = diagnose(CFG, snap, 1.0)

    assert any("CarTelemetry" in message for message in messages)
    assert any("2 huecos de frame" in message for message in messages)
    assert any("cadencia varía" in message for message in messages)
