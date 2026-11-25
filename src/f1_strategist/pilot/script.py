"""Preguntas guiadas sobre una captura offline (Fase 7, Q14)."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ..config import AppConfig
from ..metrics.engine import MetricsEngine
from ..offline import OfflineResult, run_capture_offline
from ..parser import ParsedPacket
from ..race_state import RaceState, RaceStateSnapshot
from ..strategy.engine import StrategyEngine
from ..weekend_profile import WeekendProfile
from . import TOOLS
from .types import (
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    RoutedQuery,
    ToolInputs,
    ToolResult,
)

SCRIPT_SCHEMA_VERSION = 1
_RACE_SESSION_TYPES = frozenset({"race", "race_2", "race_3"})
_ALLOWED_STEP_KEYS = frozenset({"at_lap", "at_session_time_s", "text"})


class ScriptError(ValueError):
    """Error de lectura o validación de un guion JSONL de preguntas."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        self.detail = message
        super().__init__(f"Línea {line}: {message}" if line is not None else message)


@dataclass(frozen=True, slots=True)
class ScriptStep:
    """Una línea válida de un guion de preguntas."""

    line: int
    text: str
    at_lap: int | None = None
    at_session_time_s: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1:
            raise ScriptError("El número de línea debe ser positivo", line=None)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ScriptError("'text' debe ser un texto no vacío.", line=self.line)
        has_lap = self.at_lap is not None
        has_time = self.at_session_time_s is not None
        if has_lap == has_time:
            raise ScriptError(
                "Indica exactamente uno de 'at_lap' o 'at_session_time_s'.",
                line=self.line,
            )
        if self.at_lap is not None and (
            isinstance(self.at_lap, bool)
            or not isinstance(self.at_lap, int)
            or not 1 <= self.at_lap <= 255
        ):
            raise ScriptError(
                "'at_lap' debe ser un entero entre 1 y 255.",
                line=self.line,
            )
        if self.at_session_time_s is not None:
            if (
                isinstance(self.at_session_time_s, bool)
                or not isinstance(self.at_session_time_s, (int, float))
                or not math.isfinite(self.at_session_time_s)
                or self.at_session_time_s < 0.0
            ):
                raise ScriptError(
                    "'at_session_time_s' debe ser un número finito no negativo.",
                    line=self.line,
                )
            object.__setattr__(self, "at_session_time_s", float(self.at_session_time_s))

    @property
    def trigger_label(self) -> str:
        if self.at_lap is not None:
            return f"lap:{self.at_lap}"
        return f"session_time_s:{self.at_session_time_s}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"line": self.line, "text": self.text}
        if self.at_lap is not None:
            payload["at_lap"] = self.at_lap
        if self.at_session_time_s is not None:
            payload["at_session_time_s"] = self.at_session_time_s
        return payload


@dataclass(frozen=True, slots=True)
class ScriptExchange:
    """Respuesta producida al activarse un paso del guion o una consulta puntual."""

    line: int
    trigger: str
    t_sim_s: float
    lap: int | None
    query_text: str
    answer: PilotAnswer

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "trigger": self.trigger,
            "t_sim_s": round(self.t_sim_s, 9),
            "lap": self.lap,
            "query_text": self.query_text,
            "answer": self.answer.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """Resultado completo y huella determinista de ejecutar un guion."""

    exchanges: tuple[ScriptExchange, ...]
    untriggered: tuple[ScriptStep, ...]
    offline: OfflineResult
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCRIPT_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "exchanges": [item.to_dict() for item in self.exchanges],
            "untriggered": [step.to_dict() for step in self.untriggered],
            "offline": {
                "packets_read": self.offline.packets_read,
                "packets_parsed": self.offline.packets_parsed,
                "truncated": self.offline.truncated,
                "last_observed_at": round(self.offline.last_observed_at, 9),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class ScriptResponder(Protocol):
    """Puerto para responder preguntas en modo plantilla durante el replay."""

    def ask(self, text: str, *, inputs: ToolInputs, source: str = "script") -> PilotAnswer: ...

    def reset(self) -> None: ...


def load_script(path: Path) -> tuple[ScriptStep, ...]:
    """Carga y valida un guion JSONL informando la línea ante cualquier error."""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScriptError(f"No se pudo leer el guion {path}: {exc}") from exc

    steps: list[ScriptStep] = []
    for line_no, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ScriptError(f"JSON inválido ({exc.msg}).", line=line_no) from exc
        if not isinstance(data, Mapping):
            raise ScriptError("Se esperaba un objeto JSON.", line=line_no)
        unknown = sorted(set(str(key) for key in data) - _ALLOWED_STEP_KEYS)
        if unknown:
            raise ScriptError(
                f"Campos desconocidos en el guion: {', '.join(unknown)}.",
                line=line_no,
            )
        if "text" not in data:
            raise ScriptError("Falta el campo obligatorio 'text'.", line=line_no)
        steps.append(
            ScriptStep(
                line=line_no,
                text=data["text"],
                at_lap=data.get("at_lap"),
                at_session_time_s=data.get("at_session_time_s"),
            )
        )
    if not steps:
        raise ScriptError("El guion no contiene preguntas.")
    return tuple(steps)


class DefaultScriptResponder:
    """Respondedor determinista en modo plantilla sobre los contratos Q3/Q4/TOOLS/Q10."""

    def __init__(self, config: AppConfig) -> None:
        self._config = template_mode_config(config)
        self._route, self._directory_cls, self._dialogue_cls, self._compose = (
            _load_pilot_pipeline_components()
        )
        self._dialogue = _build_dialogue(self._dialogue_cls, self._config)
        self._seq = 0

    def reset(self) -> None:
        self._dialogue = _build_dialogue(self._dialogue_cls, self._config)
        self._seq = 0

    def ask(self, text: str, *, inputs: ToolInputs, source: str = "script") -> PilotAnswer:
        self._seq += 1
        query_id = f"{source}-{self._seq}"
        pilot_cfg = self._config.pilot
        drivers = self._directory_cls.from_state(inputs.state)
        routed: RoutedQuery = self._route(
            text,
            language=pilot_cfg.language,
            drivers=drivers,
            dialogue=self._dialogue,
            cfg=pilot_cfg,
        )
        normalized_query = PilotQuery(
            query_id=query_id,
            text=text,
            language=pilot_cfg.language,
            received_at=inputs.now,
            source=source,
        )
        routed = replace(routed, query=normalized_query)

        result: ToolResult | None = None
        if routed.clarification is None and routed.intent in TOOLS:
            result = TOOLS[routed.intent](routed, inputs)
        answer: PilotAnswer = self._compose(
            routed,
            result,
            language=pilot_cfg.language,
            cfg=pilot_cfg,
        )
        answer = replace(answer, query_id=query_id, latency_s=None, followup_pending=False)
        self._dialogue.record(PilotExchange(query=normalized_query, answer=answer), inputs.now)
        return answer


def template_mode_config(config: AppConfig | None) -> AppConfig:
    """Devuelve una copia de la configuración forzada a modo plantilla sin LLM."""
    base = config if config is not None else AppConfig()
    return base.model_copy(
        update={
            "llm": base.llm.model_copy(update={"enabled": False}),
            "agent": base.agent.model_copy(update={"offline_mode": "template"}),
            "pilot": base.pilot.model_copy(update={"llm_followup": False}),
        }
    )


def compute_script_fingerprint(
    exchanges: Sequence[ScriptExchange],
    untriggered: Sequence[ScriptStep] = (),
) -> str:
    """Calcula la huella determinista de las respuestas del guion (sin rutas ni latencias)."""
    payload = {
        "schema_version": SCRIPT_SCHEMA_VERSION,
        "exchanges": [
            {
                "line": item.line,
                "trigger": item.trigger,
                "t_sim_s": round(item.t_sim_s, 9),
                "lap": item.lap,
                "query_text": item.query_text,
                "answer": _canonical_answer(item.answer),
            }
            for item in exchanges
        ],
        "untriggered": [step.to_dict() for step in untriggered],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_pilot_script(
    capture_path: Path,
    script_path: Path,
    config: AppConfig | None = None,
    *,
    profiles_by_uid: Mapping[int, WeekendProfile] | None = None,
    responder: ScriptResponder | None = None,
) -> ScriptResult:
    """Reproduce ``capture_path`` offline y lanza las preguntas de ``script_path``."""
    effective_cfg = template_mode_config(config)
    steps = load_script(script_path)
    active_responder = responder if responder is not None else DefaultScriptResponder(effective_cfg)
    active_responder.reset()

    collector = _ScriptCollector(
        effective_cfg,
        steps,
        active_responder,
        profiles_by_uid=profiles_by_uid,
    )
    offline = run_capture_offline(capture_path, [collector])
    exchanges = tuple(collector.exchanges)
    untriggered = tuple(collector.pending)
    return ScriptResult(
        exchanges=exchanges,
        untriggered=untriggered,
        offline=offline,
        fingerprint=compute_script_fingerprint(exchanges, untriggered),
    )


def ask_capture_at_lap(
    capture_path: Path,
    lap: int,
    question: str,
    config: AppConfig | None = None,
    *,
    profiles_by_uid: Mapping[int, WeekendProfile] | None = None,
    responder: ScriptResponder | None = None,
) -> tuple[ScriptExchange, OfflineResult]:
    """Lanza una pregunta puntual al alcanzar la vuelta ``lap`` de una captura."""
    if isinstance(lap, bool) or not isinstance(lap, int) or lap < 1:
        raise ValueError("La vuelta (--lap) debe ser un entero positivo.")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("La pregunta no puede estar vacía.")

    effective_cfg = template_mode_config(config)
    active_responder = responder if responder is not None else DefaultScriptResponder(effective_cfg)
    active_responder.reset()

    step = ScriptStep(line=1, text=question, at_lap=lap)
    collector = _ScriptCollector(
        effective_cfg,
        (step,),
        active_responder,
        profiles_by_uid=profiles_by_uid,
        source="ask",
    )
    offline = run_capture_offline(capture_path, [collector], stop_when=collector.all_triggered)
    if not collector.exchanges:
        raise ValueError(f"No se encontró la vuelta {lap} en la captura.")
    return collector.exchanges[0], offline


class _ScriptCollector:
    """Consumidor offline que avanza el pipeline y dispara preguntas al cumplirse su condición."""

    def __init__(
        self,
        config: AppConfig,
        steps: Sequence[ScriptStep],
        responder: ScriptResponder,
        *,
        profiles_by_uid: Mapping[int, WeekendProfile] | None = None,
        source: str = "script",
    ) -> None:
        self._config = config
        self._profiles_by_uid = dict(profiles_by_uid or {})
        self._responder = responder
        self._source = source
        self._race = RaceState()
        self._metrics = MetricsEngine(config.metrics)
        self._strategy = StrategyEngine(config.strategy, config.metrics)
        self.pending: deque[ScriptStep] = deque(steps)
        self.exchanges: list[ScriptExchange] = []
        self._last_session_uid: int | None = None

    def all_triggered(self) -> bool:
        return not self.pending

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self._race.apply(packet, observed_at)
        state = self._race.snapshot()
        session = state.session
        if session.uid != self._last_session_uid:
            self._last_session_uid = session.uid
        profile = (
            self._profiles_by_uid.get(session.uid)
            if session.uid is not None
            and session.session_type is not None
            and session.session_type.lower() in _RACE_SESSION_TYPES
            else None
        )
        self._metrics.set_profile(profile)
        self._metrics.apply(packet, observed_at, state)
        metrics = self._metrics.snapshot()
        history = self._metrics.history.snapshot()
        self._strategy.apply(state, metrics, history, profile, observed_at)
        strategy = self._strategy.snapshot()

        current_lap = _current_lap(state)
        while self.pending and _step_matches(self.pending[0], current_lap, observed_at):
            step = self.pending.popleft()
            inputs = ToolInputs(
                state=state,
                metrics=metrics,
                strategy=strategy,
                history=history,
                config=self._config.pilot,
                now=observed_at,
                profile=profile,
            )
            answer = self._responder.ask(step.text, inputs=inputs, source=self._source)
            answer = replace(answer, latency_s=None, followup_pending=False)
            self.exchanges.append(
                ScriptExchange(
                    line=step.line,
                    trigger=step.trigger_label,
                    t_sim_s=observed_at,
                    lap=current_lap,
                    query_text=step.text,
                    answer=answer,
                )
            )


def _current_lap(state: RaceStateSnapshot) -> int | None:
    player = state.player_car
    if player is not None and player.current_lap is not None:
        return player.current_lap
    return state.current_lap


def _step_matches(step: ScriptStep, current_lap: int | None, observed_at: float) -> bool:
    if step.at_lap is not None:
        return current_lap is not None and current_lap >= step.at_lap
    assert step.at_session_time_s is not None
    return observed_at >= step.at_session_time_s


def _canonical_answer(answer: PilotAnswer) -> dict[str, Any]:
    payload = answer.to_dict()
    payload["latency_s"] = None
    return _round_floats(payload)


def _round_floats(value: Any, digits: int = 9) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, Mapping):
        return {str(key): _round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_floats(item, digits) for item in value]
    return value


def _load_pilot_pipeline_components() -> tuple[Any, Any, Any, Any]:
    try:
        router_mod = importlib.import_module(".router", package=__package__)
        entities_mod = importlib.import_module(".entities", package=__package__)
        answers_mod = importlib.import_module(".answers", package=__package__)
    except ImportError as exc:
        raise ValueError(
            "El motor de preguntas requiere los módulos del enrutador, entidades y respuestas "
            "(Q3, Q4 y Q10)."
        ) from exc
    return (
        router_mod.route,
        entities_mod.DriverDirectory,
        entities_mod.DialogueState,
        answers_mod.compose_answer,
    )


def _build_dialogue(dialogue_cls: Any, config: AppConfig) -> Any:
    pilot_cfg = config.pilot
    try:
        return dialogue_cls(pilot_cfg)
    except TypeError:
        try:
            return dialogue_cls(
                max_turns=pilot_cfg.dialogue_turns,
                dialogue_ttl_s=pilot_cfg.dialogue_ttl_s,
                clarification_ttl_s=pilot_cfg.clarification_ttl_s,
            )
        except TypeError:
            return dialogue_cls()


__all__ = [
    "DefaultScriptResponder",
    "SCRIPT_SCHEMA_VERSION",
    "ScriptError",
    "ScriptExchange",
    "ScriptResponder",
    "ScriptResult",
    "ScriptStep",
    "ask_capture_at_lap",
    "compute_script_fingerprint",
    "load_script",
    "run_pilot_script",
    "template_mode_config",
]
