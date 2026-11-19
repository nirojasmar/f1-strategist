"""Prompts versionados y esquema de respuesta del agente (Fase 6).

Los textos viven en ``agent/prompts/<idioma>`` con el número de versión en el
nombre del fichero. La versión efectiva combina nombre, versión y hash del
contenido real que se envía al modelo, de modo que cualquier cambio en el texto
queda registrado con las llamadas.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from .types import ContextKind, ContextPayload, LLMMessage, LLMReply

PROMPTS_PACKAGE = "f1_strategist.agent.prompts"

_SYSTEM_NAME = "system"
_KIND_NAMES = {
    ContextKind.ALERT: "alert",
    ContextKind.QUESTION: "question",
}


class PromptError(ValueError):
    """Prompt ausente o idioma no soportado."""


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """Mensajes listos para el cliente, su versión y el esquema de respuesta."""

    messages: tuple[LLMMessage, ...]
    prompt_version: str
    response_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))


@dataclass(frozen=True, slots=True)
class _PromptFile:
    name: str
    version: str
    text: str


class PromptRegistry:
    """Carga los prompts de un idioma y compone las peticiones al modelo."""

    def __init__(
        self,
        language: str,
        system: _PromptFile,
        prompts: Mapping[ContextKind, _PromptFile],
    ) -> None:
        self._language = language
        self._system = system
        self._prompts = dict(prompts)

    @classmethod
    def load(cls, language: str) -> PromptRegistry:
        root = files(PROMPTS_PACKAGE)
        directory = root.joinpath(language)
        if not directory.is_dir():
            available = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
            raise PromptError(
                f"Idioma de prompt no soportado: {language!r}; "
                f"disponibles: {', '.join(available) or 'ninguno'}"
            )
        system = _read(directory, _SYSTEM_NAME)
        prompts = {kind: _read(directory, name) for kind, name in _KIND_NAMES.items()}
        return cls(language, system, prompts)

    def render(self, kind: ContextKind, context: ContextPayload) -> RenderedPrompt:
        """Compone los mensajes y el esquema para un contexto concreto."""
        try:
            prompt = self._prompts[ContextKind(kind)]
        except (KeyError, ValueError) as exc:
            raise PromptError(f"Tipo de prompt no soportado: {kind!r}") from exc
        schema = LLMReply.model_json_schema()
        system_text = f"{self._system.text}\n\n## Esquema JSON de la respuesta\n{_dumps(schema)}"
        user_text = f"{prompt.text}\n\n## Contexto\n{_dumps(context.to_dict())}"
        return RenderedPrompt(
            messages=(
                LLMMessage("system", system_text),
                LLMMessage("user", user_text),
            ),
            prompt_version=prompt_version(
                f"{self._language}/{prompt.name}", prompt.version, system_text + prompt.text
            ),
            response_schema=schema,
        )


def prompt_version(name: str, version: str, content: str) -> str:
    """Identificador estable: nombre, versión y hash del contenido."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{name}.{version}+{digest}"


def _read(directory: Traversable, name: str) -> _PromptFile:
    candidates = sorted(
        entry.name for entry in directory.iterdir() if entry.name.startswith(f"{name}.")
    )
    if not candidates:
        raise PromptError(f"Falta el prompt {name}.v* en {directory}")
    file_name = candidates[-1]
    text = (directory / file_name).read_text(encoding="utf-8")
    return _PromptFile(name=name, version=file_name[len(name) + 1 :], text=text)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
