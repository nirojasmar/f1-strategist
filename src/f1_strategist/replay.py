"""Reemisión de una captura por UDP hacia una dirección y puerto configurables.

Permite probar el receptor real y las dos topologías (mismo PC y red local)
sin abrir el juego.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path

from .capture import CaptureReader


def replay_capture(
    path: Path,
    host: str = "127.0.0.1",
    port: int = 20777,
    speed: float = 1.0,
    loop: bool = False,
    on_packet: Callable[[int], None] | None = None,
) -> int:
    """Reenvía los paquetes respetando sus tiempos originales.

    `speed` multiplica la velocidad (2.0 = el doble de rápido); 0 envía sin
    pausas. Devuelve el número de paquetes enviados.
    """
    if speed < 0:
        raise ValueError("speed no puede ser negativo")
    reader = CaptureReader(path)
    info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    family, socktype, proto, _, target = info
    sent = 0
    with socket.socket(family, socktype, proto) as sock:
        while True:
            first_ns: int | None = None
            start = time.perf_counter()
            for packet in reader:
                if first_ns is None:
                    first_ns = packet.recv_time_ns
                if speed > 0:
                    due = (packet.recv_time_ns - first_ns) / 1e9 / speed
                    delay = due - (time.perf_counter() - start)
                    if delay > 0:
                        time.sleep(delay)
                sock.sendto(packet.payload, target)
                sent += 1
                if on_packet is not None:
                    on_packet(sent)
            if not loop:
                return sent
