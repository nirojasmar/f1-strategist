"""Receptor UDP en un hilo dedicado y estadísticas de recepción.

El receptor usa un socket bloqueante con timeout (funciona igual en Windows y
Linux) y entrega los datagramas a una cola acotada. Nunca escribe en disco ni
interpreta paquetes: eso lo hace el consumidor (ver `session.py`).
"""

from __future__ import annotations

import contextlib
import ipaddress
import queue
import socket
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from .capture import CapturedPacket
from .config import TelemetryConfig
from .packets import PacketHeader

_RECV_BUFFER = 4 * 1024 * 1024
_MAX_DATAGRAM = 65535


class ReceiverError(Exception):
    """No se pudo abrir el socket UDP."""


def normalize_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).compressed
    except ValueError:
        return value


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    started_monotonic: float
    received: int = 0
    rejected_source: int = 0
    invalid: int = 0
    dropped_queue_full: int = 0
    bytes_received: int = 0
    last_packet_monotonic: float | None = None
    sources: dict[str, int] = field(default_factory=dict)
    rejected_sources: dict[str, int] = field(default_factory=dict)
    packets_by_id: dict[int, int] = field(default_factory=dict)
    last_header: PacketHeader | None = None
    capture_packets: int = 0
    capture_bytes: int = 0
    capture_error: str | None = None


class Stats:
    """Contadores compartidos entre el receptor, el consumidor y la consola."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._received = 0
        self._rejected = 0
        self._invalid = 0
        self._dropped = 0
        self._bytes = 0
        self._last: float | None = None
        self._sources: Counter[str] = Counter()
        self._rejected_sources: Counter[str] = Counter()
        self._by_id: Counter[int] = Counter()
        self._header: PacketHeader | None = None
        self._capture_packets = 0
        self._capture_bytes = 0
        self._capture_error: str | None = None

    def record_received(self, source_ip: str, nbytes: int) -> None:
        with self._lock:
            self._received += 1
            self._bytes += nbytes
            self._last = time.monotonic()
            self._sources[source_ip] += 1

    def record_rejected(self, source_ip: str) -> None:
        with self._lock:
            self._rejected += 1
            self._rejected_sources[source_ip] += 1

    def record_dropped(self) -> None:
        with self._lock:
            self._dropped += 1

    def record_header(self, header: PacketHeader | None) -> None:
        with self._lock:
            if header is None:
                self._invalid += 1
                return
            self._by_id[header.packet_id] += 1
            self._header = header

    def set_capture(self, packets: int, nbytes: int) -> None:
        with self._lock:
            self._capture_packets = packets
            self._capture_bytes = nbytes

    def set_capture_error(self, message: str) -> None:
        with self._lock:
            self._capture_error = message

    def snapshot(self) -> StatsSnapshot:
        with self._lock:
            return StatsSnapshot(
                started_monotonic=self._started,
                received=self._received,
                rejected_source=self._rejected,
                invalid=self._invalid,
                dropped_queue_full=self._dropped,
                bytes_received=self._bytes,
                last_packet_monotonic=self._last,
                sources=dict(self._sources),
                rejected_sources=dict(self._rejected_sources),
                packets_by_id=dict(self._by_id),
                last_header=self._header,
                capture_packets=self._capture_packets,
                capture_bytes=self._capture_bytes,
                capture_error=self._capture_error,
            )


class UdpReceiver:
    def __init__(
        self,
        config: TelemetryConfig,
        out: queue.Queue[CapturedPacket],
        stats: Stats,
    ) -> None:
        self._config = config
        self._out = out
        self._stats = stats
        self._allowed = {normalize_ip(ip) for ip in config.allowed_sources}
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.bound_port = 0
        self.error: str | None = None

    def start(self) -> None:
        family = socket.AF_INET6 if self._config.ip_version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        # El tamaño del buffer es una mejora, no un requisito.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RECV_BUFFER)
        address = (self._config.listen_address, self._config.listen_port)
        try:
            sock.bind(address)
        except OSError as exc:
            sock.close()
            raise ReceiverError(
                f"No se pudo abrir UDP {address[0]}:{address[1]} ({exc}). "
                "Comprueba que el puerto no esté en uso por otra aplicación "
                "(por ejemplo, otro programa de telemetría) y que la dirección "
                "exista en este equipo."
            ) from exc
        sock.settimeout(0.25)
        self._sock = sock
        self.bound_port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="udp-receiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _run(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(_MAX_DATAGRAM)
            except TimeoutError:
                continue
            except ConnectionResetError:
                continue  # Windows notifica ICMP "puerto inaccesible"; se ignora
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = f"Error en el socket UDP: {exc}"
                break
            recv_time_ns = time.time_ns()
            source_ip = str(addr[0])
            if self._allowed and normalize_ip(source_ip) not in self._allowed:
                self._stats.record_rejected(source_ip)
                continue
            self._stats.record_received(source_ip, len(data))
            try:
                self._out.put_nowait(CapturedPacket(recv_time_ns, source_ip, int(addr[1]), data))
            except queue.Full:
                self._stats.record_dropped()
