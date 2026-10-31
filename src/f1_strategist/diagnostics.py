"""Estado de conexión y diagnóstico de ausencia de datos (RF-10, RF-11)."""

from __future__ import annotations

from enum import Enum

from .config import TelemetryConfig
from .receiver import StatsSnapshot


class ConnectionState(Enum):
    WAITING = "esperando datos"
    RECEIVING = "recibiendo"
    STALLED = "sin datos"


def connection_state(
    cfg: TelemetryConfig, snap: StatsSnapshot, now_monotonic: float
) -> ConnectionState:
    if snap.last_packet_monotonic is None:
        return ConnectionState.WAITING
    if now_monotonic - snap.last_packet_monotonic > cfg.no_data_warning_s:
        return ConnectionState.STALLED
    return ConnectionState.RECEIVING


def diagnose(cfg: TelemetryConfig, snap: StatsSnapshot, now_monotonic: float) -> list[str]:
    """Mensajes en español con las causas probables cuando no llegan datos."""
    messages: list[str] = []
    uptime = now_monotonic - snap.started_monotonic

    if snap.received == 0:
        if snap.rejected_source:
            ips = ", ".join(sorted(snap.rejected_sources))
            messages.append(
                f"Llegan paquetes de orígenes no permitidos ({ips}). "
                "Revisa 'allowed_sources' en la configuración."
            )
        elif uptime >= cfg.no_data_warning_s:
            messages.append("No ha llegado ningún paquete desde el arranque. Causas probables:")
            messages.append("  - La telemetría UDP está desactivada en el juego.")
            messages.append(
                "  - La IP o el puerto de destino configurados en el juego no coinciden "
                f"con {cfg.listen_address}:{cfg.listen_port}."
            )
            messages.append("  - Un firewall bloquea el puerto UDP de entrada.")
            if cfg.is_loopback_only:
                messages.append(
                    "  - La aplicación escucha solo en loopback: si el juego corre en "
                    'otro PC, usa listen_address = "0.0.0.0".'
                )
    elif connection_state(cfg, snap, now_monotonic) is ConnectionState.STALLED:
        assert snap.last_packet_monotonic is not None
        age = now_monotonic - snap.last_packet_monotonic
        messages.append(
            f"Sin datos desde hace {age:.0f} s. ¿El juego está en pausa, en menús "
            "o con la telemetría desactivada?"
        )

    if snap.dropped_queue_full:
        messages.append(
            f"Se descartaron {snap.dropped_queue_full} paquetes por cola llena "
            "(el consumidor no da abasto)."
        )
    if snap.capture_error:
        messages.append(snap.capture_error)
    return messages
