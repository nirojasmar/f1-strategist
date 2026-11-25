"""Entrada de texto de la consola: editor puro y lectores de teclas por plataforma."""

from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

HISTORY_UP = "\x00history-up"
HISTORY_DOWN = "\x00history-down"

_ESCAPE = "\x1b"
_ENTER_KEYS = frozenset({"\r", "\n"})
_BACKSPACE_KEYS = frozenset({"\x7f", "\b"})


class KeySource(Protocol):
    """Fuente de teclas no bloqueante."""

    @property
    def enabled(self) -> bool: ...

    def read(self) -> str | None: ...


@dataclass(slots=True)
class LineEditor:
    """Editor de una línea: recibe teclas y devuelve el texto al enviar."""

    max_chars: int = 200
    history_limit: int = 100
    text: str = ""
    history: list[str] = field(default_factory=list)
    _history_index: int = -1
    _draft: str = ""

    def feed(self, key: str) -> str | None:
        if key in _ENTER_KEYS:
            return self._submit()
        if key == _ESCAPE:
            self.reset()
            return None
        if key in _BACKSPACE_KEYS:
            self.text = self.text[:-1]
            return None
        if key == HISTORY_UP:
            self._recall(1)
            return None
        if key == HISTORY_DOWN:
            self._recall(-1)
            return None
        if len(key) == 1 and key.isprintable() and len(self.text) < self.max_chars:
            self.text += key
        return None

    def type_text(self, text: str) -> str | None:
        """Procesa un pegado carácter a carácter, respetando el límite."""
        for char in text:
            submitted = self.feed(char)
            if submitted is not None:
                return submitted
        return None

    def reset(self) -> None:
        self.text = ""
        self._history_index = -1
        self._draft = ""

    def _submit(self) -> str | None:
        text = self.text.strip()
        if not text:
            return None
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            self.history = self.history[-max(1, self.history_limit) :]
        self.reset()
        return text

    def _recall(self, direction: int) -> None:
        if not self.history:
            return
        if direction > 0:
            if self._history_index == -1:
                self._draft = self.text
                self._history_index = 0
            elif self._history_index < len(self.history) - 1:
                self._history_index += 1
            self.text = self.history[-1 - self._history_index]
            return
        if self._history_index == -1:
            return
        if self._history_index == 0:
            self._history_index = -1
            self.text = self._draft
            return
        self._history_index -= 1
        self.text = self.history[-1 - self._history_index]


class TerminalKeyReader:
    """Lector no bloqueante; las flechas se traducen a tokens de historial."""

    def __init__(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._enabled = enabled
        self._windows = os.name == "nt"
        self._saved_attributes: Any | None = None
        self._fd: int | None = None
        if not enabled or self._windows:
            return
        import termios  # noqa: PLC0415
        import tty  # noqa: PLC0415

        self._fd = sys.stdin.fileno()
        self._saved_attributes = termios.tcgetattr(self._fd)  # type: ignore[reportAttributeAccessIssue]
        tty.setcbreak(self._fd)  # type: ignore[reportAttributeAccessIssue]

    @property
    def enabled(self) -> bool:
        return self._enabled

    def read(self) -> str | None:
        if not self._enabled:
            return None
        if self._windows:
            return self._read_windows()
        return self._read_posix()

    def close(self) -> None:
        if self._saved_attributes is None or self._fd is None:
            return
        import termios  # noqa: PLC0415

        termios.tcsetattr(  # type: ignore[reportAttributeAccessIssue]
            self._fd,
            termios.TCSADRAIN,  # type: ignore[reportAttributeAccessIssue]
            self._saved_attributes,
        )
        self._saved_attributes = None
        self._fd = None

    def _read_windows(self) -> str | None:
        import msvcrt  # noqa: PLC0415

        # Las stubs de msvcrt en otras plataformas no declaran estas funciones.
        kbhit = getattr(msvcrt, "kbhit", None)
        getwch = getattr(msvcrt, "getwch", None)
        if kbhit is None or getwch is None or not kbhit():
            return None
        key = getwch()
        if key in {"\x00", "\xe0"}:
            extended = getwch()
            if extended is None:
                return None
            return {"H": HISTORY_UP, "P": HISTORY_DOWN}.get(extended)
        return key if key != "\x03" else "q"

    def _read_posix(self) -> str | None:
        char = self._read_char(0.0)
        if char is None or char != _ESCAPE:
            return char
        if self._read_char(0.02) != "[":
            return _ESCAPE
        code = self._read_char(0.02)
        if code is None:
            return _ESCAPE
        return {"A": HISTORY_UP, "B": HISTORY_DOWN}.get(code, _ESCAPE)

    def _read_char(self, timeout: float) -> str | None:
        fd = self._fd
        if fd is None:
            return None
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        first = os.read(fd, 1)
        if not first:
            return None
        data = first
        while len(data) < _utf8_length(first):
            ready, _, _ = select.select([fd], [], [], 0.02)
            if not ready:
                break
            data += os.read(fd, 1)
        return data.decode("utf-8", errors="ignore") or None


def _utf8_length(first: bytes) -> int:
    lead = first[0]
    if lead < 0x80:
        return 1
    if lead >> 5 == 0b110:
        return 2
    if lead >> 4 == 0b1110:
        return 3
    if lead >> 3 == 0b11110:
        return 4
    return 1


__all__ = [
    "HISTORY_DOWN",
    "HISTORY_UP",
    "KeySource",
    "LineEditor",
    "TerminalKeyReader",
]
