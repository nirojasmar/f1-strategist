"""Reproduce capturas localmente a través de consumidores del pipeline."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .agent.communicator import AgentCommunicator, AlertSources, build_communicator
from .agent.types import AgentSnapshot
from .capture import CaptureReader
from .config import AppConfig
from .events.engine import EventEngine
from .events.types import EventsSnapshot
from .metrics.engine import MetricsEngine, MetricsSnapshot
from .metrics.history import LapHistorySnapshot
from .packets import parse_header
from .parser import (
    PacketParseError,
    ParsedPacket,
    ParseIssue,
    is_ignored_by_design,
    parse_packet,
    record_parser_rejection,
)
from .pilot.engine import PilotEngine, PilotInputs
from .pilot.types import PilotAnswer, PilotSnapshot
from .race_state import RaceState, RaceStateSnapshot
from .strategy.engine import StrategyEngine
from .strategy.types import StrategySnapshot
from .weekend_profile import WeekendProfile

_RACE_SESSION_TYPES = frozenset({"race", "race_2", "race_3"})


class PacketConsumer(Protocol):
    def apply(self, packet: ParsedPacket, observed_at: float) -> None: ...


class OfflineEventPipeline:
    """Pipeline completo reutilizable por reproducir capturas sin red.

    Reproduce el mismo orden que la sesión en vivo y el reproductor: estado,
    métricas, estrategia y eventos. El reloj siempre lo aporta el llamador, de
    modo que ``run_capture_offline`` puede usar su reloj de captura por defecto.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        profiles_by_uid: Mapping[int, WeekendProfile] | None = None,
        agent_communicator: AgentCommunicator | None = None,
    ) -> None:
        self._config = config
        self._profiles_by_uid = dict(profiles_by_uid or {})
        self._race_state = RaceState()
        self._metrics_engine = MetricsEngine(config.metrics)
        self._strategy_engine = StrategyEngine(config.strategy, config.metrics)
        self._event_engine = EventEngine(config)
        self._agent = agent_communicator or build_communicator(
            config.agent,
            config.llm,
            mode=config.agent.offline_mode,
        )
        self._observed_at = 0.0
        self._weekend_profile: WeekendProfile | None = None
        self._pilot = PilotEngine(
            config.pilot,
            snapshots=self._pilot_inputs,
            followup=getattr(self._agent, "submit_followup", None),
            note_interaction=self._note_pilot_interaction,
        )
        self._agent.start()

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self._race_state.apply(packet, observed_at)
        state = self._race_state.snapshot()
        session = state.session
        profile = (
            self._profiles_by_uid.get(session.uid)
            if session.uid is not None
            and session.session_type is not None
            and session.session_type.lower() in _RACE_SESSION_TYPES
            else None
        )
        self._observed_at = observed_at
        self._weekend_profile = profile
        self._metrics_engine.set_profile(profile)
        self._metrics_engine.apply(packet, observed_at, state)
        self._strategy_engine.apply(
            state,
            self._metrics_engine.snapshot(),
            self._metrics_engine.history.snapshot(),
            profile,
            observed_at,
        )
        self._event_engine.apply(
            state,
            self._metrics_engine.snapshot(),
            self._strategy_engine.snapshot(),
            observed_at,
        )
        events_snapshot = self._event_engine.snapshot()
        self._agent.observe(
            events_snapshot,
            AlertSources(
                state=state,
                metrics=self._metrics_engine.snapshot(),
                strategy=self._strategy_engine.snapshot(),
                events=events_snapshot,
                observed_at=observed_at,
            ),
        )
        self._agent.drain_records()

    def stop(self) -> None:
        """Detiene el hilo del comunicador si llegó a arrancarse."""
        self._agent.stop()

    close = stop

    def race_snapshot(self) -> RaceStateSnapshot:
        return self._race_state.snapshot()

    def metrics_snapshot(self) -> MetricsSnapshot:
        return self._metrics_engine.snapshot()

    def history_snapshot(self) -> LapHistorySnapshot:
        return self._metrics_engine.history.snapshot()

    def strategy_snapshot(self) -> StrategySnapshot:
        return self._strategy_engine.snapshot()

    def events_snapshot(self) -> EventsSnapshot:
        return self._event_engine.snapshot()

    def agent_snapshot(self) -> AgentSnapshot:
        """Último estado publicado por el comunicador del agente."""
        return self._agent.snapshot()

    def ask_pilot(self, text: str) -> PilotAnswer:
        """Responde con la última telemetría procesada."""
        return self._pilot.ask(text, now=self._observed_at)

    def pilot_snapshot(self) -> PilotSnapshot:
        """Intercambios recientes y aclaración pendiente."""
        return self._pilot.snapshot()

    def _pilot_inputs(self) -> PilotInputs:
        return PilotInputs(
            state=self.race_snapshot(),
            metrics=self.metrics_snapshot(),
            strategy=self.strategy_snapshot(),
            history=self.history_snapshot(),
            events=self.events_snapshot(),
            profile=self._weekend_profile,
        )

    def _note_pilot_interaction(self, now: float) -> None:
        # EventEngine aún no expone su compuerta; Q12 solo la notifica.
        gate = getattr(self._event_engine, "_gate", None)
        if gate is not None:
            gate.note_pilot_interaction(now)


@dataclass(frozen=True, slots=True)
class OfflineResult:
    packets_read: int
    packets_parsed: int
    parser_rejections: Mapping[ParseIssue, int]
    ignored_packets_by_id: Mapping[int, int]
    truncated: bool
    last_observed_at: float = 0.0

    def integrity_problems(self) -> tuple[str, ...]:
        """Diagnósticos que impiden certificar una reproducción como completa."""
        problems: list[str] = []
        if self.truncated:
            problems.append("la captura termina con un registro incompleto")
        if self.packets_parsed == 0:
            problems.append("la captura no contiene paquetes válidos")
        rejected = sum(self.parser_rejections.values())
        if rejected:
            problems.append(f"{rejected} paquete(s) rechazado(s) por el parser")
        return tuple(problems)


def run_capture_offline(
    path: Path,
    consumers: Iterable[PacketConsumer],
    *,
    clock_scale: float | None = None,
    stop_when: Callable[[], bool] | None = None,
) -> OfflineResult:
    """Procesa una captura sin red, hilos ni esperas.

    ``clock_scale`` escala el reloj simulado: 2.0 comprime el tiempo a la mitad.
    No usar en comprobaciones de determinismo: ``play`` y ``replay`` aceptan
    ``--speed``, que solo escala las esperas y conserva el tiempo de la captura.
    El valor por defecto (``None``) usa el tiempo transcurrido de la captura.
    Cero se rechaza porque colapsa el reloj y cambia los datos registrados.
    Las excepciones inesperadas de consumidores y parser se propagan; solo
    `PacketParseError` se cuenta como rechazo, para no ocultar fallos offline.
    """
    scale = 1.0 if clock_scale is None else clock_scale
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("clock_scale debe ser un número finito mayor que cero")

    reader = CaptureReader(path)
    packet_consumers = tuple(consumers)
    rejections: Counter[ParseIssue] = Counter()
    ignored: Counter[int] = Counter()
    packets_read = 0
    packets_parsed = 0
    first_recv_time_ns: int | None = None
    last_observed_at = 0.0

    for captured in reader:
        packets_read += 1
        if first_recv_time_ns is None:
            first_recv_time_ns = captured.recv_time_ns

        elapsed = (captured.recv_time_ns - first_recv_time_ns) / 1_000_000_000
        observed_at = max(last_observed_at, elapsed / scale)
        last_observed_at = observed_at

        header = parse_header(captured.payload)
        if header is not None and is_ignored_by_design(header):
            ignored[header.packet_id] += 1
            continue

        try:
            packet = parse_packet(captured.payload)
        except PacketParseError as exc:
            record_parser_rejection(rejections, exc.issue)
            continue

        packets_parsed += 1
        for consumer in packet_consumers:
            consumer.apply(packet, observed_at)
        if stop_when is not None and stop_when():
            break

    return OfflineResult(
        packets_read=packets_read,
        packets_parsed=packets_parsed,
        parser_rejections=MappingProxyType(dict(rejections)),
        ignored_packets_by_id=MappingProxyType(dict(ignored)),
        truncated=reader.truncated,
        last_observed_at=last_observed_at,
    )
