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
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from statistics import median

from .capture import CapturedPacket
from .config import TelemetryConfig
from .messages import DiagnosticMessage
from .packets import PacketHeader
from .parser import ParseIssue

_RECV_BUFFER = 4 * 1024 * 1024
_MAX_DATAGRAM = 65535
_SEQUENCE_HISTORY_LIMIT = 256
_SEQUENCE_CADENCE_LIMIT = 21
_MAX_TRACKED_SESSIONS = 16
_MAX_TRACKED_SOURCES = 32
_OTHER_SOURCE_BUCKET = "other"
_UINT32_MASK = 0xFFFFFFFF
_UINT32_HALF = 0x80000000
_MAX_PACKET_ID = 15
_SUPPORTED_HEADER_VERSION = (2025, 25, 1, 1)
_VERIFIED_GAME_MINOR = 26
_MAX_PARSER_REJECTION_MESSAGES = 16
_OTHER_PARSER_REJECTIONS = "other_parser_rejections_limited"


class ReceiverError(Exception):
    """No se pudo abrir el socket UDP."""


@dataclass(frozen=True, slots=True)
class PacketSequenceStats:
    frame_gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0


@dataclass(slots=True)
class _SequenceCounters:
    frame_gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0


@dataclass(slots=True)
class _FrameSequence:
    last_overall_frame_identifier: int | None = None
    recent: deque[int] = field(default_factory=deque)
    seen: set[int] = field(default_factory=set)
    cadence: deque[int] = field(default_factory=lambda: deque(maxlen=_SEQUENCE_CADENCE_LIMIT))


def normalize_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).compressed
    except ValueError:
        return value


def _increment_bounded_source_counter(counter: Counter[str], source_ip: str) -> None:
    if source_ip in counter or len(counter) < _MAX_TRACKED_SOURCES:
        counter[source_ip] += 1
    else:
        counter[_OTHER_SOURCE_BUCKET] += 1


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
    sequence_by_id: dict[int, PacketSequenceStats] = field(default_factory=dict)
    last_header: PacketHeader | None = None
    parser_rejections: dict[ParseIssue, int] = field(default_factory=dict)
    ignored_packets_by_id: dict[int, int] = field(default_factory=dict)
    unverified_game_versions: dict[str, int] = field(default_factory=dict)
    capture_packets: int = 0
    capture_bytes: int = 0
    capture_error: DiagnosticMessage | None = None
    consumer_errors: int = 0
    consumer_error: DiagnosticMessage | None = None


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
        self._sequence_counters: dict[int, _SequenceCounters] = {}
        self._sequences: OrderedDict[tuple[int, int], _FrameSequence] = OrderedDict()
        self._sessions: OrderedDict[int, None] = OrderedDict()
        self._header: PacketHeader | None = None
        self._parser_rejections: Counter[ParseIssue] = Counter()
        self._ignored_packets: Counter[int] = Counter()
        self._unverified_game_versions: Counter[str] = Counter()
        self._capture_packets = 0
        self._capture_bytes = 0
        self._capture_error: DiagnosticMessage | None = None
        self._consumer_errors = 0
        self._consumer_error: DiagnosticMessage | None = None

    def record_received(self, source_ip: str, nbytes: int) -> None:
        with self._lock:
            self._received += 1
            self._bytes += nbytes
            self._last = time.monotonic()
            _increment_bounded_source_counter(self._sources, source_ip)

    def record_rejected(self, source_ip: str) -> None:
        with self._lock:
            self._rejected += 1
            _increment_bounded_source_counter(self._rejected_sources, source_ip)

    def record_dropped(self) -> None:
        with self._lock:
            self._dropped += 1

    def record_header(self, header: PacketHeader | None) -> None:
        with self._lock:
            if header is None:
                self._invalid += 1
                return
            if (
                header.packet_format,
                header.game_year,
                header.game_major_version,
                header.packet_version,
            ) != _SUPPORTED_HEADER_VERSION or not 0 <= header.packet_id <= _MAX_PACKET_ID:
                self._invalid += 1
                return
            self._by_id[header.packet_id] += 1
            self._header = header
            if header.game_minor_version != _VERIFIED_GAME_MINOR:
                version = f"{header.game_major_version}.{header.game_minor_version}"
                self._unverified_game_versions[version] += 1
            if header.session_uid not in self._sessions:
                if len(self._sessions) >= _MAX_TRACKED_SESSIONS:
                    oldest_session, _ = self._sessions.popitem(last=False)
                    for sequence_key in tuple(self._sequences):
                        if sequence_key[0] == oldest_session:
                            del self._sequences[sequence_key]
                self._sessions[header.session_uid] = None
            else:
                self._sessions.move_to_end(header.session_uid)
            counters = self._sequence_counters.setdefault(header.packet_id, _SequenceCounters())
            sequence = self._sequences.setdefault(
                (header.session_uid, header.packet_id), _FrameSequence()
            )
            frame = header.overall_frame_identifier
            if frame in sequence.seen:
                counters.duplicates += 1
                return

            last_frame = sequence.last_overall_frame_identifier
            if last_frame is None:
                sequence.last_overall_frame_identifier = frame
            else:
                delta = (frame - last_frame) & _UINT32_MASK
                if 0 < delta < _UINT32_HALF:
                    if len(sequence.cadence) >= 3:
                        typical_delta = median(sequence.cadence)
                        if delta > max(2, typical_delta * 1.75):
                            counters.frame_gaps += max(0, round(delta / typical_delta) - 1)
                    sequence.cadence.append(delta)
                    sequence.last_overall_frame_identifier = frame
                else:
                    counters.out_of_order += 1

            if len(sequence.recent) == _SEQUENCE_HISTORY_LIMIT:
                sequence.seen.remove(sequence.recent.popleft())
            sequence.recent.append(frame)
            sequence.seen.add(frame)

    def record_ignored_packet(self, packet_id: int) -> None:
        with self._lock:
            self._ignored_packets[packet_id] += 1

    def record_parser_rejection(self, issue: ParseIssue) -> None:
        with self._lock:
            if (
                issue not in self._parser_rejections
                and len(self._parser_rejections) >= _MAX_PARSER_REJECTION_MESSAGES
            ):
                issue = ParseIssue(_OTHER_PARSER_REJECTIONS)
            self._parser_rejections[issue] += 1

    def set_capture(self, packets: int, nbytes: int) -> None:
        with self._lock:
            self._capture_packets = packets
            self._capture_bytes = nbytes

    def set_capture_error(self, issue: DiagnosticMessage) -> None:
        with self._lock:
            self._capture_error = issue

    def record_consumer_error(self, issue: DiagnosticMessage) -> None:
        with self._lock:
            self._consumer_errors += 1
            self._consumer_error = issue

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
                sequence_by_id={
                    packet_id: PacketSequenceStats(
                        frame_gaps=counters.frame_gaps,
                        duplicates=counters.duplicates,
                        out_of_order=counters.out_of_order,
                    )
                    for packet_id, counters in self._sequence_counters.items()
                },
                last_header=self._header,
                parser_rejections=dict(self._parser_rejections),
                ignored_packets_by_id=dict(self._ignored_packets),
                unverified_game_versions=dict(self._unverified_game_versions),
                capture_packets=self._capture_packets,
                capture_bytes=self._capture_bytes,
                capture_error=self._capture_error,
                consumer_errors=self._consumer_errors,
                consumer_error=self._consumer_error,
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
