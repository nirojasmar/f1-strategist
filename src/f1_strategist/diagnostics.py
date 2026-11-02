"""Idioma-neutral connection state and diagnostic messages."""

from __future__ import annotations

from enum import Enum

from .config import TelemetryConfig
from .messages import DiagnosticMessage, message
from .receiver import StatsSnapshot


class ConnectionState(Enum):
    WAITING = "waiting"
    RECEIVING = "receiving"
    STALLED = "stalled"


def connection_state(
    cfg: TelemetryConfig, snap: StatsSnapshot, now_monotonic: float
) -> ConnectionState:
    if snap.last_packet_monotonic is None:
        return ConnectionState.WAITING
    if now_monotonic - snap.last_packet_monotonic > cfg.no_data_warning_s:
        return ConnectionState.STALLED
    return ConnectionState.RECEIVING


def diagnose(
    cfg: TelemetryConfig, snap: StatsSnapshot, now_monotonic: float
) -> list[DiagnosticMessage]:
    messages: list[DiagnosticMessage] = []
    uptime = now_monotonic - snap.started_monotonic

    if snap.received == 0:
        if snap.rejected_source:
            ips = tuple(sorted(snap.rejected_sources))
            messages.append(message("rejected_sources", ips=ips))
        elif uptime >= cfg.no_data_warning_s:
            messages.extend(
                [
                    message("no_packets_heading"),
                    message("telemetry_disabled"),
                    message(
                        "destination_mismatch",
                        address=f"{cfg.listen_address}:{cfg.listen_port}",
                    ),
                    message("firewall"),
                ]
            )
            if cfg.is_loopback_only:
                messages.append(message("loopback_only"))
    elif connection_state(cfg, snap, now_monotonic) is ConnectionState.STALLED:
        assert snap.last_packet_monotonic is not None
        messages.append(message("stalled", age_seconds=now_monotonic - snap.last_packet_monotonic))

    if snap.dropped_queue_full:
        messages.append(message("queue_dropped", count=snap.dropped_queue_full))
    if snap.capture_error:
        messages.append(snap.capture_error)
    if snap.consumer_error:
        messages.append(
            message(
                "consumer_error",
                count=snap.consumer_errors,
                error=snap.consumer_error,
            )
        )
    for issue, count in sorted(
        snap.parser_rejections.items(),
        key=lambda item: (-item[1], item[0].code, item[0].parameters),
    ):
        messages.append(message("parser_rejection", issue=issue, count=count))
    for version, count in sorted(snap.unverified_game_versions.items()):
        messages.append(message("unverified_game_version", version=version, count=count))

    sequence_issues = tuple(
        (packet_id, stats)
        for packet_id, stats in sorted(snap.sequence_by_id.items())
        if stats.frame_gaps or stats.duplicates or stats.out_of_order
    )
    if sequence_issues:
        messages.append(message("sequence_anomalies", items=sequence_issues))
        if any(stats.frame_gaps for _, stats in sequence_issues):
            messages.append(message("sequence_gap_caveat"))
    return messages
