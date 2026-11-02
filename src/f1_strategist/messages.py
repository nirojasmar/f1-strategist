"""Idioma-neutral message values passed from the core to presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiagnosticMessage:
    key: str
    parameters: tuple[tuple[str, object], ...] = ()

    def get(self, name: str) -> object:
        for key, value in self.parameters:
            if key == name:
                return value
        raise KeyError(name)


def message(key: str, **parameters: object) -> DiagnosticMessage:
    return DiagnosticMessage(key, tuple(sorted(parameters.items())))
