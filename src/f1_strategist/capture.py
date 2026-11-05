"""Formato de captura binaria cruda (`.f1cap`, versión 1) para replay y fixtures.

Se guardan los datagramas tal como llegaron, sin interpretarlos, para poder
reprocesarlos con parsers nuevos.

Estructura (little-endian):

    archivo   = MAGIC (6 bytes: b"F1CAP\\x01")
                uint32 longitud de los metadatos
                metadatos (JSON UTF-8: format_version, created_utc y los campos
                opcionales tool, tool_version, platform, listen_address,
                listen_port, allowed_sources y anonymized)
                registro*
    registro  = uint64 recv_time_ns (reloj de pared del PC receptor)
                uint8  longitud de la IP de origen
                IP de origen (texto ASCII)
                uint16 puerto de origen
                uint32 longitud del payload
                payload
"""

from __future__ import annotations

import gzip
import json
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

MAGIC = b"F1CAP\x01"
FORMAT_VERSION = 1
_LEN = struct.Struct("<I")
_REC_HEAD = struct.Struct("<QB")
_REC_TAIL = struct.Struct("<HI")
_MAX_IP_LEN = 45


class CaptureError(Exception):
    """Archivo de captura inválido o ilegible."""


@dataclass(frozen=True, slots=True)
class CapturedPacket:
    recv_time_ns: int
    source_ip: str
    source_port: int
    payload: bytes
    # In-process receive time for freshness; intentionally omitted from the capture format.
    recv_monotonic: float | None = None


class CaptureWriter:
    def __init__(
        self,
        path: Path,
        metadata: dict[str, Any] | None = None,
        flush_every: int = 256,
        *,
        include_created_utc: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.packets_written = 0
        self.bytes_written = 0
        self._flush_every = flush_every
        self._raw_file: BinaryIO | None = None
        if self.path.name.lower().endswith(".f1cap.gz"):
            self._raw_file = self.path.open("wb")
            self._file: BinaryIO | gzip.GzipFile | None = gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=self._raw_file,
                mtime=0,
            )
        else:
            self._file = self.path.open("wb")
        meta: dict[str, Any] = {"format_version": FORMAT_VERSION}
        if include_created_utc:
            meta["created_utc"] = datetime.now(UTC).isoformat()
        meta.update(metadata or {})
        blob = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        self._write(MAGIC + _LEN.pack(len(blob)) + blob)

    def _write(self, data: bytes) -> None:
        assert self._file is not None
        self._file.write(data)
        self.bytes_written += len(data)

    def write(self, packet: CapturedPacket) -> None:
        ip = packet.source_ip.encode("ascii")
        if len(ip) > _MAX_IP_LEN:
            raise CaptureError(f"IP de origen demasiado larga: {packet.source_ip!r}")
        self._write(
            _REC_HEAD.pack(packet.recv_time_ns, len(ip))
            + ip
            + _REC_TAIL.pack(packet.source_port, len(packet.payload))
            + packet.payload
        )
        self.packets_written += 1
        if self.packets_written % self._flush_every == 0:
            self.flush()

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._raw_file is not None:
            self._raw_file.close()
            self._raw_file = None

    def __enter__(self) -> CaptureWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class CaptureReader:
    """Lee `.f1cap` o `.f1cap.gz`; ignora un registro final incompleto y marca `truncated`."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.truncated = False
        try:
            with self._open() as handle:
                if handle.read(len(MAGIC)) != MAGIC:
                    raise CaptureError(f"{self.path} no es una captura .f1cap v1")
                raw_len = handle.read(_LEN.size)
                if len(raw_len) < _LEN.size:
                    raise CaptureError(f"{self.path}: cabecera incompleta")
                (meta_len,) = _LEN.unpack(raw_len)
                blob = handle.read(meta_len)
                if len(blob) < meta_len:
                    raise CaptureError(f"{self.path}: metadatos incompletos")
                self._data_start = handle.tell()
        except (OSError, EOFError, zlib.error) as exc:
            raise CaptureError(f"No se pudo leer {self.path}: {exc}") from exc
        try:
            self.metadata: dict[str, Any] = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureError(f"{self.path}: metadatos ilegibles") from exc

    def __iter__(self) -> Iterator[CapturedPacket]:
        self.truncated = False
        try:
            with self._open() as handle:
                handle.seek(self._data_start)
                while True:
                    head = handle.read(_REC_HEAD.size)
                    if not head:
                        return
                    if len(head) < _REC_HEAD.size:
                        self.truncated = True
                        return
                    recv_time_ns, ip_len = _REC_HEAD.unpack(head)
                    ip_raw = handle.read(ip_len)
                    tail = handle.read(_REC_TAIL.size)
                    if len(ip_raw) < ip_len or len(tail) < _REC_TAIL.size:
                        self.truncated = True
                        return
                    port, size = _REC_TAIL.unpack(tail)
                    payload = handle.read(size)
                    if len(payload) < size:
                        self.truncated = True
                        return
                    try:
                        source_ip = ip_raw.decode("ascii")
                    except UnicodeDecodeError as exc:
                        raise CaptureError(f"{self.path}: IP de origen ilegible") from exc
                    yield CapturedPacket(recv_time_ns, source_ip, port, payload)
        except CaptureError:
            raise
        except (OSError, EOFError, zlib.error) as exc:
            raise CaptureError(f"No se pudo leer {self.path}: {exc}") from exc

    def _open(self) -> BinaryIO | gzip.GzipFile:
        if self.path.name.lower().endswith(".f1cap.gz"):
            return gzip.open(self.path, "rb")
        return self.path.open("rb")
