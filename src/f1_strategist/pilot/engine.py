"""Motor de preguntas del piloto: acuse, enrutado, herramienta y respuesta.

El motor solo lee instantáneas inmutables, así que sigue respondiendo con la
telemetría en pausa. El acuse se publica antes de cualquier trabajo y la
respuesta determinista nunca espera al LLM ni al Gate de avisos.
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from ..config import MetricsConfig, PilotConfig, StrategyConfig
from ..events.types import EventsSnapshot
from ..metrics.engine import MetricsSnapshot
from ..metrics.history import LapHistorySnapshot
from ..race_state import PacketFreshness, RaceStateSnapshot
from ..strategy.types import StrategySnapshot
from ..weekend_profile import WeekendProfile
from . import TOOLS
from .types import (
    AnswerKind,
    Clarification,
    EntityRef,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    PilotSnapshot,
    RoutedQuery,
    Tool,
    ToolInputs,
    ToolResult,
)

_LOGGER = logging.getLogger(__name__)

RouteFn = Callable[..., RoutedQuery]
DirectoryFn = Callable[[RaceStateSnapshot], object]
ComposeFn = Callable[..., PilotAnswer]
FollowupFn = Callable[..., None]


class _Dialogue(Protocol):
    """Memoria de diálogo mínima que consume el motor (contrato de Q4)."""

    def record(self, exchange: PilotExchange, now: float) -> None: ...

    def pending_clarification(self, now: float) -> Clarification | None: ...


@dataclass(frozen=True, slots=True)
class PilotInputs:
    """Últimas instantáneas que el motor lee para responder."""

    state: RaceStateSnapshot
    metrics: MetricsSnapshot
    strategy: StrategySnapshot
    history: LapHistorySnapshot
    events: EventsSnapshot
    profile: WeekendProfile | None = None
    metrics_config: MetricsConfig | None = None
    strategy_config: StrategyConfig | None = None


def _lazy(module_name: str, attribute: str) -> Any | None:
    """Resuelve un colaborador de otra tarea de la ola si ya está fusionado."""
    qualified = f"{__package__}.{module_name}"
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except ModuleNotFoundError as exc:
        if exc.name == qualified:
            return None
        raise
    return getattr(module, attribute, None)


def _has_recent_telemetry(state: RaceStateSnapshot, now: float) -> bool:
    try:
        freshness: PacketFreshness = state.freshness_for("lap_data")
    except KeyError:
        return False
    return freshness.updated_at_monotonic is not None and not freshness.is_stale(now)


class PilotEngine:
    """Orquesta una pregunta: ACK, enrutado, herramienta, respuesta y memoria."""

    def __init__(
        self,
        config: PilotConfig | None = None,
        *,
        snapshots: Callable[[], PilotInputs],
        route: RouteFn | None = None,
        directory: DirectoryFn | None = None,
        dialogue: _Dialogue | None = None,
        tools: Mapping[Intent, Tool] | None = None,
        compose: ComposeFn | None = None,
        followup: FollowupFn | None = None,
        note_interaction: Callable[[float], None] | None = None,
        publish: Callable[[PilotAnswer], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        metrics_config: MetricsConfig | None = None,
        strategy_config: StrategyConfig | None = None,
    ) -> None:
        self._config = config or PilotConfig()
        self._snapshots = snapshots
        self._route = cast(RouteFn | None, route or _lazy("router", "route"))
        self._directory = directory
        if self._directory is None:
            directory_cls = _lazy("entities", "DriverDirectory")
            if directory_cls is not None:
                self._directory = directory_cls.from_state
        self._dialogue = dialogue
        if self._dialogue is None:
            dialogue_cls = _lazy("entities", "DialogueState")
            if dialogue_cls is not None:
                self._dialogue = dialogue_cls(self._config)
        self._tools = TOOLS if tools is None else tools
        self._compose = cast(ComposeFn | None, compose or _lazy("answers", "compose_answer"))
        self._followup = followup
        self._note_interaction = note_interaction
        self._publish = publish
        self._clock = clock
        self._metrics_config = metrics_config
        self._strategy_config = strategy_config
        self._recent: list[PilotExchange] = []
        self._counters: dict[str, int] = {}
        self._pending: Clarification | None = None

    def ask(self, text: str, *, now: float, source: str = "console") -> PilotAnswer:
        """Responde una pregunta; nunca espera al LLM ni a la compuerta."""
        query = PilotQuery(
            query_id=uuid.uuid4().hex,
            text=text,
            language=self._config.language,
            received_at=now,
            source=source,
        )
        self._emit(
            PilotAnswer(
                query_id=query.query_id,
                intent=Intent.UNKNOWN,
                kind=AnswerKind.ACK,
                text="",
                message_key="pilot_ack",
            )
        )
        started = self._clock()
        if self._note_interaction is not None:
            try:
                self._note_interaction(now)
            except Exception:
                # La respuesta no depende del Gate: un fallo solo se registra.
                _LOGGER.exception("pilot_gate_failed")
        inputs = self._snapshots()
        routed = self._route_query(query, inputs)
        answer = self._build_answer(routed, inputs, now)
        followup_pending = self._wants_followup(routed, answer)
        answer = replace(
            answer,
            latency_s=max(0.0, self._clock() - started),
            followup_pending=followup_pending,
        )
        self._remember(query, answer, routed, now)
        if followup_pending:
            self._submit_followup(answer, inputs, now)
        return answer

    def snapshot(self) -> PilotSnapshot:
        """Intercambios recientes, aclaración pendiente y contadores por tipo."""
        return PilotSnapshot(
            recent=tuple(self._recent),
            pending_clarification=self._pending,
            counters=dict(self._counters),
        )

    def reset(self) -> None:
        """Olvida el diálogo y el historial (reinicio de sesión o captura)."""
        self._recent.clear()
        self._counters.clear()
        self._pending = None
        reset = getattr(self._dialogue, "reset", None)
        if callable(reset):
            reset()

    def _route_query(self, query: PilotQuery, inputs: PilotInputs) -> RoutedQuery:
        if self._route is None or self._directory is None:
            return RoutedQuery(query=query, intent=Intent.UNKNOWN, confidence=0.0)
        resolved = self._resolve_pending(query)
        if resolved is not None:
            intent = self._last_intent(query.received_at) or Intent.UNKNOWN
            return RoutedQuery(
                query=query,
                intent=intent,
                confidence=1.0,
                entities=(EntityRef(resolved.raw, resolved.driver.car_index),),
            )
        routed = self._route(
            query.text,
            language=query.language,
            drivers=self._directory(inputs.state),
            dialogue=self._dialogue,
            cfg=self._config,
            now=query.received_at,
        )
        # El motor es la fuente autorizada de la pregunta para la trazabilidad.
        routed = replace(routed, query=query)
        if routed.clarification is not None:
            start = getattr(self._dialogue, "start_clarification", None)
            if callable(start):
                start(
                    routed.clarification.options,
                    query.received_at,
                    message_key=routed.clarification.message_key,
                )
        return routed

    def _resolve_pending(self, query: PilotQuery) -> Any | None:
        """Resuelve la aclaración pendiente antes de enrutar el texto nuevo."""
        dialogue = self._dialogue
        pending = getattr(dialogue, "pending_clarification", None)
        resolver = getattr(dialogue, "resolve_clarification", None)
        if not callable(pending) or not callable(resolver):
            return None
        if pending(query.received_at) is None:
            return None
        from .entities import Resolved

        outcome = resolver(query.text, query.received_at)
        if isinstance(outcome, Resolved):
            return outcome
        cancel = getattr(dialogue, "cancel_clarification", None)
        if callable(cancel):
            cancel()
        return None

    def _last_intent(self, now: float) -> Intent | None:
        getter = getattr(self._dialogue, "last_intent", None)
        if not callable(getter):
            return None
        intent = getter(now)
        return None if intent is None or intent is Intent.UNKNOWN else Intent(intent)

    def _build_answer(self, routed: RoutedQuery, inputs: PilotInputs, now: float) -> PilotAnswer:
        if self._compose is None:
            return self._unavailable(routed)
        if routed.clarification is not None or routed.intent is Intent.UNKNOWN:
            return self._compose(routed, None, language=routed.query.language, cfg=self._config)
        if not _has_recent_telemetry(inputs.state, now):
            return self._compose(routed, None, language=routed.query.language, cfg=self._config)
        tool = self._tools.get(routed.intent)
        if tool is None:
            return self._unavailable(routed)
        try:
            result: ToolResult = tool(
                routed,
                ToolInputs(
                    state=inputs.state,
                    metrics=inputs.metrics,
                    strategy=inputs.strategy,
                    history=inputs.history,
                    config=self._config,
                    now=now,
                    profile=inputs.profile,
                    metrics_config=inputs.metrics_config or self._metrics_config,
                    strategy_config=inputs.strategy_config or self._strategy_config,
                ),
            )
        except Exception:
            _LOGGER.exception("pilot_tool_failed intent=%s", routed.intent.value)
            return self._unavailable(routed)
        return self._compose(routed, result, language=routed.query.language, cfg=self._config)

    def _unavailable(self, routed: RoutedQuery) -> PilotAnswer:
        return PilotAnswer(
            query_id=routed.query.query_id,
            intent=routed.intent,
            kind=AnswerKind.UNAVAILABLE,
            text="",
            message_key="pilot_unavailable",
        )

    def _wants_followup(self, routed: RoutedQuery, answer: PilotAnswer) -> bool:
        return (
            self._followup is not None
            and self._config.llm_followup
            and answer.kind is AnswerKind.ANSWER
            and routed.intent.value in self._config.llm_followup_intents
        )

    def _remember(
        self, query: PilotQuery, answer: PilotAnswer, routed: RoutedQuery, now: float
    ) -> None:
        exchange = PilotExchange(query=query, answer=answer)
        self._recent.append(exchange)
        if len(self._recent) > self._config.history_size:
            del self._recent[: len(self._recent) - self._config.history_size]
        self._counters[answer.kind.value] = self._counters.get(answer.kind.value, 0) + 1
        if self._dialogue is not None:
            self._dialogue.record(exchange, now)
            self._pending = self._dialogue.pending_clarification(now) or routed.clarification
        else:
            self._pending = routed.clarification

    def _submit_followup(self, answer: PilotAnswer, inputs: PilotInputs, now: float) -> None:
        from ..agent.communicator import AlertSources

        followup = self._followup
        if followup is None:
            return
        sources = AlertSources(
            state=inputs.state,
            metrics=inputs.metrics,
            strategy=inputs.strategy,
            events=inputs.events,
            observed_at=now,
        )
        try:
            followup(answer, sources)
        except Exception:
            _LOGGER.exception("pilot_followup_failed")

    def _emit(self, answer: PilotAnswer) -> None:
        if self._publish is None:
            return
        try:
            self._publish(answer)
        except Exception:
            _LOGGER.exception("pilot_ack_failed")
