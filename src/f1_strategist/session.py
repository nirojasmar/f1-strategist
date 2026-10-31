"""Sesión de captura: receptor -> cola acotada -> consumidor (registro + estadísticas)."""

from __future__ import annotations

import platform
import queue
import threading
from datetime import datetime
from pathlib import Path

from . import __version__
from .capture import CapturedPacket, CaptureWriter
from .config import AppConfig
from .packets import parse_header
from .receiver import Stats, StatsSnapshot, UdpReceiver


class CaptureSession:
    def __init__(self, config: AppConfig, queue_size: int = 20000) -> None:
        self.config = config
        self.stats = Stats()
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

    def _consume(self) -> None:
        while not (self._stop_consumer.is_set() and self._queue.empty()):
            try:
                packet = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            self.stats.record_header(parse_header(packet.payload))
            writer = self._writer
            if writer is None:
                continue
            try:
                writer.write(packet)
            except (OSError, ValueError) as exc:
                self.stats.set_capture_error(f"Error al escribir la captura: {exc}")
                self._writer = None
                writer.close()
                continue
            self.stats.set_capture(writer.packets_written, writer.bytes_written)
        if self._writer is not None:
            self._writer.flush()
