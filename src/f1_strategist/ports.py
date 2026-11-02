"""Stable interfaces consumed by presentation adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .receiver import StatsSnapshot


class SessionPort(Protocol):
    @property
    def bound_port(self) -> int: ...

    @property
    def receiver_error(self) -> str | None: ...

    @property
    def capture_path(self) -> Path | None: ...

    def snapshot(self) -> StatsSnapshot: ...
