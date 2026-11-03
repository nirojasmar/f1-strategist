"""Sesión de captura: receptor -> cola acotada -> consumidor (registro + estadísticas)."""

from __future__ import annotations

import logging
import platform
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .capture import CapturedPacket, CaptureWriter
from .config import AppConfig
from .messages import message
from .packets import parse_header
from .parser import PacketParseError, is_ignored_by_design, parse_packet
from .race_state import RaceState, RaceStateSnapshot
from .receiver import Stats, StatsSnapshot, UdpReceiver

_LOGGER = logging.getLogger(__name__)


class CaptureSession:
    def __init__(self, config: AppConfig, queue_size: int = 20000) -> None:
        self.config = config
        self.stats = Stats()
        self._race_state = RaceState()
        self._queue: queue.Queue[CapturedPacket] = queue.Queue(maxsize=queue_size)
        self._receiver = UdpReceiver(config.telemetry, self._queue, self.stats)
        self._writer: CaptureWriter | None = None
        self._consumer: threading.Thread | None = None
        self._stop_consumer = threading.Event()
        self.capture_path: Path | None = None

    @property
    def bound_port(self) -> int:
        return self._receiver.bound_port

    @property
    def receiver_error(self) -> str | None:
        return self._receiver.error

    def start(self) -> None:
        # El socket se abre primero: si falla no se crea ningún archivo de captura.
        self._receiver.start()
        if self.config.capture.enabled:
            name = datetime.now().strftime("session-%Y%m%d-%H%M%S.f1cap")
            self.capture_path = self.config.capture.directory / name
            tele = self.config.telemetry
            self._writer = CaptureWriter(
                self.capture_path,
                metadata={
                    "tool": "f1-strategist",
                    "tool_version": __version__,
                    "platform": platform.platform(),
                    "listen_address": tele.listen_address,
                    "listen_port": self._receiver.bound_port,
                    "allowed_sources": tele.allowed_sources,
                },
            )
        self._consumer = threading.Thread(
            target=self._consume, name="capture-consumer", daemon=True
        )
        self._consumer.start()

    def stop(self) -> None:
        self._receiver.stop()
        self._stop_consumer.set()
        if self._consumer is not None:
            self._consumer.join(timeout=5.0)
        if self._writer is not None:
            self._writer.close()

    def snapshot(self) -> StatsSnapshot:
        return self.stats.snapshot()

    def race_snapshot(self) -> RaceStateSnapshot:
        return self._race_state.snapshot()

    def _consume(self) -> None:
        while not (self._stop_consumer.is_set() and self._queue.empty()):
            try:
                packet = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                header = parse_header(packet.payload)
                self.stats.record_header(header)
            except Exception as exc:
                _LOGGER.exception("Unexpected error while recording packet header")
                self.stats.record_consumer_error(message("consumer_header_error", error=str(exc)))
                header = None

            self._write_packet(packet)

            if header is not None and is_ignored_by_design(header):
                self.stats.record_ignored_packet(header.packet_id)
                continue
            try:
                parsed_packet = parse_packet(packet.payload)
            except PacketParseError as exc:
                self.stats.record_parser_rejection(exc.issue)
            except Exception as exc:
                _LOGGER.exception("Unexpected error while parsing UDP packet")
                self.stats.record_consumer_error(message("consumer_parse_error", error=str(exc)))
            else:
                try:
                    observed_at = packet.recv_monotonic
                    if observed_at is None:
                        observed_at = time.monotonic()
                    self._race_state.apply(parsed_packet, observed_at)
                except Exception as exc:
                    _LOGGER.exception("Unexpected error while updating race state")
                    self.stats.record_consumer_error(
                        message("race_state_update_error", error=str(exc))
                    )
        writer = self._writer
        if writer is not None:
            try:
                writer.flush()
            except Exception as exc:
                _LOGGER.exception("Unable to flush capture")
                self.stats.set_capture_error(message("capture_flush_error", error=str(exc)))

    def _write_packet(self, packet: CapturedPacket) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(packet)
        except Exception as exc:
            _LOGGER.exception("Unable to write packet to capture")
            self.stats.set_capture_error(message("capture_write_error", error=str(exc)))
            self._writer = None
            try:
                writer.close()
            except Exception as close_exc:
                _LOGGER.exception("Unable to close failed capture")
                self.stats.set_capture_error(message("capture_close_error", error=str(close_exc)))
            return
        self.stats.set_capture(writer.packets_written, writer.bytes_written)
