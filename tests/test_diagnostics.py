from __future__ import annotations

from f1_strategist.config import TelemetryConfig
from f1_strategist.console_messages import render_diagnostic
from f1_strategist.diagnostics import ConnectionState, connection_state, diagnose
from f1_strategist.parser import ParseIssue
from f1_strategist.receiver import PacketSequenceStats, StatsSnapshot

CFG = TelemetryConfig(no_data_warning_s=3.0)


def test_waiting_then_hint_after_timeout() -> None:
    snap = StatsSnapshot(started_monotonic=100.0)
    assert connection_state(CFG, snap, 101.0) is ConnectionState.WAITING
    assert diagnose(CFG, snap, 101.0) == []
    messages = [render_diagnostic(item) for item in diagnose(CFG, snap, 105.0)]
    assert any("firewall" in m for m in messages)
    assert any("loopback" in m for m in messages)  # escucha solo en loopback


def test_rejected_sources_are_reported() -> None:
    snap = StatsSnapshot(started_monotonic=0.0, rejected_source=4, rejected_sources={"10.0.0.9": 4})
    messages = [render_diagnostic(item) for item in diagnose(CFG, snap, 1.0)]
    assert "10.0.0.9" in messages[0]


def test_stalled_after_data() -> None:
    snap = StatsSnapshot(started_monotonic=0.0, received=10, last_packet_monotonic=10.0)
    assert connection_state(CFG, snap, 11.0) is ConnectionState.RECEIVING
    assert connection_state(CFG, snap, 20.0) is ConnectionState.STALLED
    assert "Sin datos desde hace" in render_diagnostic(diagnose(CFG, snap, 20.0)[0])


def test_sequence_anomalies_are_reported_as_cadence_adjusted_estimates() -> None:
    snap = StatsSnapshot(
        started_monotonic=0.0,
        received=4,
        sequence_by_id={6: PacketSequenceStats(frame_gaps=2, duplicates=1, out_of_order=1)},
    )

    diagnostics = diagnose(CFG, snap, 1.0)
    messages = [render_diagnostic(item) for item in diagnostics]

    assert any("CarTelemetry" in message for message in messages)
    assert any("2 huecos de frame" in message for message in messages)
    assert any("overall_frame_identifier" in message for message in messages)
    assert diagnostics[0].key == "sequence_anomalies"


def test_ignored_packet_types_do_not_generate_diagnostics() -> None:
    snap = StatsSnapshot(
        started_monotonic=0.0,
        received=100,
        ignored_packets_by_id={0: 80, 5: 10, 6: 10},
    )

    assert diagnose(CFG, snap, 1.0) == []


def test_unverified_minor_version_is_reported_once_per_version() -> None:
    snap = StatsSnapshot(
        started_monotonic=0.0,
        received=2,
        unverified_game_versions={"1.27": 2},
    )

    messages = diagnose(CFG, snap, 1.0)

    assert len(messages) == 1
    rendered = render_diagnostic(messages[0])
    assert "1.27" in rendered
    assert "2 paquetes" in rendered


def test_parser_rejections_are_reported_as_warnings() -> None:
    snap = StatsSnapshot(
        started_monotonic=0.0,
        parser_rejections={
            ParseIssue(
                "unsupported_format",
                (
                    ("game_major", 1),
                    ("game_minor", 26),
                    ("game_year", 25),
                    ("packet_format", 2024),
                    ("packet_version", 1),
                ),
            ): 3
        },
    )

    rendered = [render_diagnostic(item) for item in diagnose(CFG, snap, 1.0)]
    assert rendered == [
        "Parser: Formato o versión UDP no soportados: formato 2024, año 25, juego 1.26, "
        "paquete v1. (3 paquetes rechazados)"
    ]
