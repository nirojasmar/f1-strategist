"""Comunicador del agente: contexto, LLM, validación y respaldo con plantillas.

El comunicador no calcula estrategia ni métricas: recibe alertas del motor de
eventos, construye el contexto en el hilo del consumidor y genera el mensaje en
su propio hilo. Un fallo del modelo nunca detiene al consumidor: se degrada a
plantilla con un motivo registrado.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from ..config import AgentConfig, LLMConfig
from ..events.types import AgentAlert, EventPriority, EventsSnapshot, Fact
from ..metrics.engine import MetricsSnapshot
from ..race_state import RaceStateSnapshot
from ..strategy.types import StrategySnapshot
from .context import ContextBuilder as DefaultContextBuilder
from .llm_client import OpenAICompatibleClient
from .prompt_registry import PromptRegistry as DefaultPromptRegistry
from .types import (
    AgentMessage,
    AgentSnapshot,
    CallOutcome,
    ContextKind,
    ContextPayload,
    FallbackReason,
    LLMCallRecord,
    LLMHealth,
    LLMMessage,
    LLMReply,
    LLMRequest,
    LLMResponse,
    LLMStatus,
    MessageSource,
    ValidationResult,
)
from .validator import ResponseValidator as DefaultResponseValidator

AgentMode = Literal["template", "recorded", "live"]


class LLMClient(Protocol):
    """Contrato de L2: cliente OpenAI-compatible o doble de prueba."""

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse: ...

    def health(self) -> LLMHealth: ...


class ContextBuilder(Protocol):
    """Contrato de L3: construye el contexto inmutable de una alerta."""

    def build_alert(
        self,
        alert: AgentAlert,
        *,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        events: EventsSnapshot,
        recent: Sequence[str],
    ) -> ContextPayload: ...


class RenderedPrompt(Protocol):
    """Contrato mínimo de L4: mensajes listos para enviar al cliente."""

    @property
    def messages(self) -> Sequence[LLMMessage]: ...

    @property
    def prompt_version(self) -> str: ...

    @property
    def response_schema(self) -> Mapping[str, Any]: ...


class PromptSet(Protocol):
    def render(self, kind: ContextKind, context: ContextPayload) -> RenderedPrompt: ...


class PromptRegistry(Protocol):
    """Contrato de L4: prompts versionados por idioma."""

    def load(self, language: str) -> PromptSet: ...


class ResponseValidator(Protocol):
    """Contrato de L5: valida la respuesta del modelo contra el contexto."""

    def validate(
        self, text: str, context: ContextPayload, alert: AgentAlert
    ) -> ValidationResult: ...


TemplateRenderer = Callable[[str, tuple[Fact, ...], str], str]


def default_template_renderer(message_key: str, facts: tuple[Fact, ...], language: str) -> str:
    """Plantilla localizada de L6; mientras no exista, usa la de Fase 5."""
    from ..console_messages import render_event_message

    return render_event_message(message_key, facts)


@dataclass(frozen=True, slots=True)
class AlertSources:
    """Instantáneas que acompañan a una alerta; solo se leen, nunca se mutan."""

    state: RaceStateSnapshot
    metrics: MetricsSnapshot
    strategy: StrategySnapshot
    events: EventsSnapshot
    observed_at: float


@dataclass(frozen=True, slots=True)
class _Pending:
    alert: AgentAlert
    sources: AlertSources
    context: ContextPayload
    cancel: threading.Event


@dataclass(frozen=True, slots=True)
class _Generation:
    text: str = ""
    error: FallbackReason | None = None
    latency_s: float = 0.0
    prompt_version: str | None = None
    response: LLMResponse | None = None
    called: bool = False
    error_text: str | None = None


class AgentCommunicator:
    """Orquesta el mensaje del piloto sin bloquear al consumidor."""

    def __init__(
        self,
        agent: AgentConfig,
        llm: LLMConfig | None = None,
        *,
        client: LLMClient | None = None,
        builder: ContextBuilder | None = None,
        registry: PromptRegistry | None = None,
        validator: ResponseValidator | None = None,
        template_renderer: TemplateRenderer | None = None,
        mode: AgentMode | None = None,
        queue_capacity: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent = agent
        self.llm = llm or LLMConfig()
        self._client = client
        self._builder = builder
        self._registry = registry
        self._validator = validator
        self._renderer = template_renderer or default_template_renderer
        self._mode: AgentMode = _resolve_mode(mode, self.llm, client)
        self._capacity = max(1, queue_capacity)
        self._clock = clock

        self._condition = threading.Condition()
        self._queue: deque[_Pending] = deque()
        self._in_flight: _Pending | None = None
        self._messages: deque[AgentMessage] = deque(maxlen=max(1, agent.recent_messages))
        self._call_records: list[LLMCallRecord] = []
        self._message_records: list[AgentMessage] = []
        self._counters: dict[str, int] = {}
        self._last_latency_s: float | None = None
        self._error: str | None = None
        self._now: float | None = None
        self._seen: set[str] = set()
        self._superseded: set[str] = set()
        self._ever_active: set[str] = set()
        self._closed_events: set[str] = set()
        self._health = self._initial_health()
        self._last_health_check: float | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    @property
    def mode(self) -> AgentMode:
        return self._mode

    def start(self) -> None:
        """Arranca el hilo de generación si el modo lo necesita."""
        if self._mode == "template":
            return
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="agent-communicator", daemon=True
            )
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Detiene el hilo; las alertas pendientes se descartan."""
        with self._condition:
            self._stopping = True
            in_flight = self._in_flight
            if in_flight is not None:
                in_flight.cancel.set()
            self._queue.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._condition:
            self._thread = None

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Espera a que no queden alertas pendientes ni generación en vuelo."""

        def idle() -> bool:
            return not self._queue and self._in_flight is None

        with self._condition:
            return self._condition.wait_for(idle, timeout=timeout)

    def drain_records(self) -> tuple[tuple[LLMCallRecord, ...], tuple[AgentMessage, ...]]:
        """Entrega las llamadas y mensajes nuevos al grabador y vacía el búfer."""
        with self._condition:
            calls = tuple(self._call_records)
            messages = tuple(self._message_records)
            self._call_records.clear()
            self._message_records.clear()
        return calls, messages

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------

    def observe(self, events: EventsSnapshot, sources: AlertSources) -> None:
        """Envía solo las alertas nuevas y cancela las que ya no proceden."""
        with self._condition:
            current = {alert.alert_id for alert in events.alerts}
            if not events.alerts:
                # El motor de eventos limpia sus alertas al reiniciar sesión o flashback.
                self._seen.clear()
            else:
                self._seen.intersection_update(current)
        for alert in events.alerts:
            with self._condition:
                if alert.alert_id in self._seen:
                    continue
                self._seen.add(alert.alert_id)
            self.submit(alert, sources)
        self.cancel_obsolete(events, sources.observed_at)

    def submit(self, alert: AgentAlert, sources: AlertSources | None = None) -> None:
        """Encola una alerta sin bloquear; nunca llama al modelo para críticas."""
        with self._condition:
            self._seen.add(alert.alert_id)
            if alert.supersedes:
                self._superseded.add(alert.supersedes)
            if sources is not None:
                self._now = sources.observed_at

        if alert.priority is EventPriority.CRITICAL and not self.agent.critical_uses_llm:
            self._emit_template(alert, sources, reason=None, latency_s=0.0)
            return
        if self._mode == "template":
            self._emit_template(alert, sources, reason=FallbackReason.LLM_DISABLED, latency_s=0.0)
            return
        context = self._build_context(alert, sources)
        if context is None:
            self._emit_template(alert, sources, reason=FallbackReason.UNAVAILABLE, latency_s=0.0)
            return
        assert sources is not None  # el contexto exige fuentes; sin ellas se degradó antes
        pending = _Pending(alert, sources, context, threading.Event())
        if self._mode == "recorded":
            # Reproducción determinista: se resuelve en el hilo llamador.
            self._process(pending)
            return
        with self._condition:
            if self._is_obsolete_locked(pending):
                self._count_locked("discarded_obsolete")
                return
            while len(self._queue) >= self._capacity:
                self._queue.popleft()
                self._count_locked("queue_overflow")
            self._queue.append(pending)
            self._condition.notify_all()

    def cancel_obsolete(self, events: EventsSnapshot, now: float) -> None:
        """Marca obsoletas las generaciones de eventos cerrados o demasiado viejas."""
        with self._condition:
            self._now = now
            active = {signal.event_id for signal in events.active_events}
            self._ever_active |= active
            self._closed_events |= self._ever_active - active
            self._closed_events -= active
            for pending in self._queue:
                if self._is_obsolete_locked(pending):
                    pending.cancel.set()
            if self._in_flight is not None and self._is_obsolete_locked(self._in_flight):
                self._in_flight.cancel.set()

    # ------------------------------------------------------------------
    # Estado publicado
    # ------------------------------------------------------------------

    def snapshot(self) -> AgentSnapshot:
        with self._condition:
            return AgentSnapshot(
                messages=tuple(self._messages),
                llm_status=self._health,
                counters=dict(self._counters),
                last_latency_s=self._last_latency_s,
                error=self._error,
            )

    def refresh_health(self) -> LLMHealth:
        """Consulta la salud del cliente (determinista para pruebas y consola)."""
        if self._mode == "template" or self._client is None:
            health = self._initial_health()
        else:
            try:
                health = self._client.health()
            except Exception as exc:
                health = LLMHealth(
                    status=LLMStatus.UNAVAILABLE,
                    model=self.llm.model or None,
                    checked_at=self._clock(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        with self._condition:
            self._health = health
            self._last_health_check = self._clock()
        return health

    def reset(self) -> None:
        """Olvida mensajes y contadores; el motor de eventos reinició el pipeline."""
        with self._condition:
            self._queue.clear()
            in_flight = self._in_flight
            if in_flight is not None:
                in_flight.cancel.set()
            self._messages.clear()
            self._call_records.clear()
            self._message_records.clear()
            self._counters.clear()
            self._seen.clear()
            self._superseded.clear()
            self._ever_active.clear()
            self._closed_events.clear()
            self._now = None
            self._last_latency_s = None
            self._error = None
            self._health = self._initial_health()
            self._last_health_check = None
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Hilo de generación
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and not self._queue:
                    wait_s = self._health_wait_locked()
                    self._condition.wait(timeout=wait_s)
                    if self._stopping or self._queue:
                        break
                    if self._health_due_locked():
                        break
                if self._stopping:
                    return
                pending = self._queue.popleft() if self._queue else None
            if pending is None:
                self.refresh_health()
                continue
            self._process(pending)

    def _health_wait_locked(self) -> float:
        if self._client is None:
            return 1.0
        if self._last_health_check is None:
            return 0.0
        elapsed = self._clock() - self._last_health_check
        return max(0.0, self.llm.health_interval_s - elapsed)

    def _health_due_locked(self) -> bool:
        if self._client is None:
            return False
        if self._last_health_check is None:
            return True
        return self._clock() - self._last_health_check >= self.llm.health_interval_s

    # ------------------------------------------------------------------
    # Generación y respaldo
    # ------------------------------------------------------------------

    def _process(self, pending: _Pending) -> None:
        with self._condition:
            if self._is_obsolete_locked(pending):
                self._count_locked("discarded_obsolete")
                return
            self._in_flight = pending
        generation: _Generation | None = None
        try:
            generation = self._generate(pending)
            if pending.cancel.is_set() or self._is_obsolete(pending):
                with self._condition:
                    self._count_locked("discarded_obsolete")
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.DISCARDED_OBSOLETE,
                    fallback_reason=FallbackReason.OBSOLETE,
                )
                return
            if generation.error is not None:
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.FALLBACK,
                    fallback_reason=generation.error,
                )
                self._degrade(pending, generation.error, generation.latency_s)
                return
            if not generation.text.strip():
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.FALLBACK,
                    fallback_reason=FallbackReason.VALIDATION_FAILED,
                )
                self._degrade(
                    pending, FallbackReason.VALIDATION_FAILED, generation.latency_s, reachable=True
                )
                return
            context = pending.context
            if self._validator is None:
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.FALLBACK,
                    fallback_reason=FallbackReason.VALIDATION_FAILED,
                )
                self._degrade(
                    pending, FallbackReason.VALIDATION_FAILED, generation.latency_s, reachable=True
                )
                return
            result = self._validator.validate(generation.text, context, pending.alert)
            if not result.valid or result.reply is None:
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.FALLBACK,
                    validation=result,
                    fallback_reason=FallbackReason.VALIDATION_FAILED,
                )
                self._degrade(
                    pending, FallbackReason.VALIDATION_FAILED, generation.latency_s, reachable=True
                )
                return
            reply = result.reply
            if not reply.should_speak and pending.alert.priority is not EventPriority.CRITICAL:
                with self._condition:
                    self._count_locked("declined")
                self._record_call(pending, generation, CallOutcome.DECLINED, validation=result)
                return
            self._record_call(pending, generation, CallOutcome.USED, validation=result)
            self._emit_reply(pending, reply, generation.latency_s)
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
            if generation is not None:
                self._record_call(
                    pending,
                    generation,
                    CallOutcome.FALLBACK,
                    fallback_reason=FallbackReason.UNAVAILABLE,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._degrade(pending, FallbackReason.UNAVAILABLE, 0.0)
        finally:
            with self._condition:
                if self._in_flight is pending:
                    self._in_flight = None
                self._condition.notify_all()

    def _generate(self, pending: _Pending) -> _Generation:
        assert self._client is not None
        started = self._clock()
        if self._registry is None:
            return _Generation(error=FallbackReason.UNAVAILABLE)
        try:
            prompt_set = self._registry.load(self.agent.language)
            rendered = prompt_set.render(ContextKind.ALERT, pending.context)
            response_format = None
            if self.llm.structured_output != "none":
                response_format = {
                    "json_schema": {
                        "name": "llm_reply",
                        "schema": rendered.response_schema,
                        "strict": True,
                    }
                }
            request = LLMRequest(
                model=self.llm.model,
                messages=tuple(rendered.messages),
                temperature=self.llm.temperature,
                top_p=self.llm.top_p,
                max_tokens=self.llm.max_tokens,
                seed=self.llm.seed,
                stream=self.llm.stream,
                response_format=response_format,
                extra_body=dict(self.llm.extra_body),
                timeout_s=self.agent.timeout_for(pending.alert.priority.value),
            )
            response = self._client.generate(request, cancel=pending.cancel)
        except Exception as exc:
            return _Generation(
                error=_reason_for_error(str(exc)),
                latency_s=self._clock() - started,
                called=True,
                error_text=f"{type(exc).__name__}: {exc}",
            )
        latency_s = self._clock() - started
        if response.error is not None:
            return _Generation(
                error=_reason_for_error(response.error),
                latency_s=latency_s,
                prompt_version=rendered.prompt_version,
                response=response,
                called=True,
                error_text=response.error,
            )
        return _Generation(
            text=response.text,
            latency_s=latency_s,
            prompt_version=rendered.prompt_version,
            response=response,
            called=True,
        )

    def _record_call(
        self,
        pending: _Pending,
        generation: _Generation,
        outcome: CallOutcome,
        *,
        validation: ValidationResult | None = None,
        fallback_reason: FallbackReason | None = None,
        error: str | None = None,
    ) -> None:
        if not generation.called:
            return
        timeout_s = self.agent.timeout_for(pending.alert.priority.value)
        parameters = {
            "temperature": self.llm.temperature,
            "top_p": self.llm.top_p,
            "max_tokens": self.llm.max_tokens,
            "seed": self.llm.seed,
            "stream": self.llm.stream,
            "timeout_s": timeout_s,
        }
        host = urlsplit(self.llm.endpoint).hostname or None
        response = generation.response
        reply = validation.reply if validation is not None else None
        record = LLMCallRecord(
            kind="alert",
            model=self.llm.model,
            outcome=outcome.value,
            alert_id=pending.alert.alert_id,
            endpoint_host=host,
            prompt_version=generation.prompt_version,
            parameters_json=json.dumps(
                parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            context_hash=pending.context.context_hash,
            context_json=pending.context.to_json() if self.agent.record_context else None,
            response_text=generation.text or None,
            reply_json=reply.to_json() if reply is not None else None,
            validation_valid=validation.valid if validation is not None else None,
            validation_errors=validation.errors if validation is not None else (),
            fallback_reason=fallback_reason.value if fallback_reason is not None else None,
            error=error if error is not None else generation.error_text,
            ttft_s=response.ttft_s if response is not None else None,
            total_s=response.total_s if response is not None else generation.latency_s,
            prompt_tokens=response.prompt_tokens if response is not None else None,
            completion_tokens=response.completion_tokens if response is not None else None,
        )
        with self._condition:
            self._call_records.append(record)

    def _emit_reply(self, pending: _Pending, reply: LLMReply, latency_s: float) -> None:
        alert = pending.alert
        message = AgentMessage(
            message_id=f"{alert.alert_id}:llm",
            alert_id=alert.alert_id,
            event_id=alert.event_id,
            priority=alert.priority,
            language=reply.language or self.agent.language,
            text=reply.message,
            source=MessageSource.LLM,
            used_facts=reply.used_facts,
            lap=alert.lap,
            emitted_at=pending.sources.observed_at,
            interrupt=alert.interrupt,
            latency_s=latency_s,
            llm_call_id=f"{alert.alert_id}:llm",
        )
        with self._condition:
            self._messages.append(message)
            self._message_records.append(message)
            self._count_locked("used")
            self._last_latency_s = latency_s
            self._health = LLMHealth(
                status=LLMStatus.AVAILABLE,
                model=self.llm.model or None,
                latency_s=latency_s,
                checked_at=self._clock(),
            )

    def _degrade(
        self,
        pending: _Pending,
        reason: FallbackReason,
        latency_s: float,
        *,
        reachable: bool = False,
    ) -> None:
        with self._condition:
            self._health = LLMHealth(
                status=LLMStatus.AVAILABLE if reachable else LLMStatus.UNAVAILABLE,
                model=self.llm.model or None,
                latency_s=latency_s,
                checked_at=self._clock(),
                error=reason.value,
            )
        self._emit_template(pending.alert, pending.sources, reason=reason, latency_s=latency_s)

    def _emit_template(
        self,
        alert: AgentAlert,
        sources: AlertSources | None,
        *,
        reason: FallbackReason | None,
        latency_s: float,
    ) -> None:
        drop = (
            reason is not None
            and self.agent.fallback_on_failure == "drop"
            and alert.priority is not EventPriority.CRITICAL
        )
        if drop:
            assert reason is not None
            with self._condition:
                self._count_locked("fallback")
                self._count_locked(f"fallback:{reason.value}")
                self._last_latency_s = latency_s
            return
        try:
            text = self._renderer(alert.message_key, alert.facts, self.agent.language)
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
            return
        message = AgentMessage(
            message_id=f"{alert.alert_id}:template",
            alert_id=alert.alert_id,
            event_id=alert.event_id,
            priority=alert.priority,
            language=self.agent.language,
            text=text,
            source=MessageSource.TEMPLATE,
            used_facts=tuple(name for name, _ in alert.facts),
            lap=alert.lap,
            emitted_at=sources.observed_at if sources is not None else alert.emitted_at,
            interrupt=alert.interrupt,
            latency_s=latency_s,
            llm_call_id=(
                f"{alert.alert_id}:llm"
                if reason is not None and reason is not FallbackReason.LLM_DISABLED
                else None
            ),
        )
        with self._condition:
            self._messages.append(message)
            self._message_records.append(message)
            if reason is not None:
                self._count_locked("fallback")
                self._count_locked(f"fallback:{reason.value}")
            self._last_latency_s = latency_s

    # ------------------------------------------------------------------
    # Obsolescencia y utilidades
    # ------------------------------------------------------------------

    def _is_obsolete(self, pending: _Pending) -> bool:
        with self._condition:
            return self._is_obsolete_locked(pending)

    def _is_obsolete_locked(self, pending: _Pending) -> bool:
        alert = pending.alert
        if alert.alert_id in self._superseded:
            return True
        if alert.event_id in self._closed_events:
            return True
        return (
            self._now is not None
            and alert.emitted_at > 0.0
            and self._now - alert.emitted_at > self.agent.max_alert_age_s
        )

    def _build_context(
        self, alert: AgentAlert, sources: AlertSources | None
    ) -> ContextPayload | None:
        if self._builder is None or sources is None:
            return None
        try:
            with self._condition:
                recent = tuple(message.text for message in self._messages)
            return self._builder.build_alert(
                alert,
                state=sources.state,
                metrics=sources.metrics,
                strategy=sources.strategy,
                events=sources.events,
                recent=recent,
            )
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
            return None

    def _count_locked(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1

    def _initial_health(self) -> LLMHealth:
        if self._mode == "template":
            return LLMHealth(status=LLMStatus.DISABLED, model=self.llm.model or None)
        return LLMHealth(status=LLMStatus.UNKNOWN, model=self.llm.model or None)


def _resolve_mode(mode: AgentMode | None, llm: LLMConfig, client: LLMClient | None) -> AgentMode:
    if client is None:
        return "template"
    if mode == "recorded":
        return mode
    if mode == "template":
        return mode
    if not llm.enabled:
        return "template"
    return "live"


def build_communicator(
    agent: AgentConfig, llm: LLMConfig, *, mode: AgentMode | None = None
) -> AgentCommunicator:
    """Compone el comunicador real respetando ``llm.enabled`` y el modo offline."""
    client = OpenAICompatibleClient(llm) if llm.enabled and mode != "recorded" else None
    return AgentCommunicator(
        agent,
        llm,
        client=client,
        builder=DefaultContextBuilder(agent),
        registry=DefaultPromptRegistry.load(agent.language),
        validator=DefaultResponseValidator(agent),
        mode=mode,
    )


def _reason_for_error(error: str) -> FallbackReason:
    lowered = error.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return FallbackReason.TIMEOUT
    if "cancel" in lowered:
        return FallbackReason.CANCELLED
    if "http" in lowered or "status" in lowered:
        return FallbackReason.HTTP_ERROR
    if "json" in lowered:
        return FallbackReason.INVALID_JSON
    return FallbackReason.UNAVAILABLE
