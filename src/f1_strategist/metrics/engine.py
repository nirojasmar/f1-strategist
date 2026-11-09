"""Orquestación del histórico, los módulos y la publicación de métricas."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..config import MetricsConfig
from ..parser import ParsedPacket, SessionHistoryPacket
from ..race_state import RaceStateSnapshot
from ..weekend_profile import WeekendProfile
from .history import LapHistory, MetricInputs, MetricModule
from .history_ingest import ingest_session_history
from .lap_tracker import LapTracker
from .registry import create_metric_modules
from .types import Metric, Quality, Window

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Último conjunto publicado de métricas, inmutable y consultable por clave."""

    metrics: Mapping[str, Metric] = field(default_factory=dict)
    computed_at_monotonic: float | None = None
    session_uid: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def get(self, key: str, default: Metric | None = None) -> Metric | None:
        return self.metrics.get(key, default)

    def __getitem__(self, key: str) -> Metric:
        return self.metrics[key]

    def __contains__(self, key: object) -> bool:
        return key in self.metrics

    def __iter__(self) -> Iterator[str]:
        return iter(self.metrics)

    def __len__(self) -> int:
        return len(self.metrics)

    def keys(self) -> Iterable[str]:
        return self.metrics.keys()

    def values(self) -> Iterable[Metric]:
        return self.metrics.values()

    def items(self) -> Iterable[tuple[str, Metric]]:
        return self.metrics.items()

    @property
    def updated_at_monotonic(self) -> float | None:
        """Alias del instante de publicación para consumidores externos."""
        return self.computed_at_monotonic


class MetricsEngine:
    """Alimenta el histórico y publica métricas con una cadencia acotada."""

    def __init__(
        self,
        config: MetricsConfig | None = None,
        modules: Iterable[MetricModule] | None = None,
        tracker: LapTracker | None = None,
        *,
        profile: WeekendProfile | None = None,
    ) -> None:
        if config is not None and not isinstance(config, MetricsConfig):
            if modules is not None:
                raise TypeError("config debe ser una MetricsConfig")
            modules = config
            config = None
        self._tracker = tracker or LapTracker(config=config)
        self._config = config or self._tracker.config
        self._modules = create_metric_modules() if modules is None else tuple(modules)
        self._profile = profile
        self._snapshot = MetricsSnapshot()
        self._last_publish_at: float | None = None
        self._last_signature: tuple[object, ...] | None = None
        self._reported_history_anomalies: set[tuple[int, int, str]] = set()

    @property
    def config(self) -> MetricsConfig:
        return self._config

    @property
    def modules(self) -> tuple[MetricModule, ...]:
        return self._modules

    @property
    def tracker(self) -> LapTracker:
        return self._tracker

    @property
    def history(self) -> LapHistory:
        return self._tracker.history

    @property
    def profile(self) -> WeekendProfile | None:
        """Perfil del fin de semana que reciben los módulos de métricas."""
        return self._profile

    def set_profile(self, profile: WeekendProfile | None) -> None:
        """Fija el perfil del fin de semana usado como prior.

        Se aplica en la siguiente publicación; no fuerza una publicación por sí
        mismo, porque el perfil se fija al arrancar la carrera y la primera
        publicación llega con el primer paquete.
        """
        self._profile = profile

    def apply(
        self,
        packet: ParsedPacket,
        observed_at: float,
        state_snapshot: RaceStateSnapshot,
    ) -> None:
        """Procesa un paquete y publica métricas si la política lo permite."""
        if not math.isfinite(observed_at):
            raise ValueError("observed_at debe ser finito")

        self._tracker.observe(state_snapshot, observed_at)
        history_updated = isinstance(packet, SessionHistoryPacket)
        if history_updated:
            anomaly = ingest_session_history(packet, self._tracker.history)
            if anomaly is not None:
                key = (packet.header.session_uid, packet.car_index, anomaly)
                if key not in self._reported_history_anomalies:
                    self._reported_history_anomalies.add(key)
                    _LOGGER.warning(
                        "Ignoring invalid tyre stint history: "
                        "session_uid=%s car_index=%s reason=%s",
                        *key,
                    )

        signature = self._state_signature(state_snapshot)
        changed = signature != self._last_signature
        due = (
            self._last_publish_at is None
            or observed_at - self._last_publish_at >= self._config.publish_interval_s
        )
        if history_updated or changed or due:
            self._publish(state_snapshot, observed_at)
        self._last_signature = signature

    def finalize(self, state_snapshot: RaceStateSnapshot, observed_at: float) -> None:
        """Publica el estado final, por ejemplo al terminar una captura offline."""
        if not math.isfinite(observed_at):
            raise ValueError("observed_at debe ser finito")
        self._publish(state_snapshot, observed_at)
        self._last_signature = self._state_signature(state_snapshot)

    def snapshot(self) -> MetricsSnapshot:
        return self._snapshot

    def _publish(self, state: RaceStateSnapshot, observed_at: float) -> None:
        inputs = MetricInputs(
            state=state,
            history=self._tracker.history.snapshot(),
            config=self._config,
            now=observed_at,
            car_index=_player_car_index(state),
            profile=self._profile,
        )
        metrics: dict[str, Metric] = {}
        for module in self._modules:
            try:
                produced = module.compute(inputs)
                for metric in produced:
                    metrics[metric.key] = metric
            except Exception as exc:
                failure = _module_failure(module.name, observed_at, exc)
                metrics[failure.key] = failure
        self._snapshot = MetricsSnapshot(
            metrics=metrics,
            computed_at_monotonic=observed_at,
            session_uid=state.session.uid,
        )
        self._last_publish_at = observed_at

    @staticmethod
    def _state_signature(state: RaceStateSnapshot) -> tuple[object, ...]:
        cars = tuple((car.current_lap, car.result_status) for car in state.cars)
        return (
            state.session.uid,
            state.session.ended,
            state.player_car_index,
            state.current_lap,
            cars,
        )


def _player_car_index(state: RaceStateSnapshot) -> int:
    index = state.player_car_index
    if index is not None and 0 <= index < len(state.cars):
        return index
    return -1


def _module_failure(name: str, now: float, error: Exception) -> Metric:
    return Metric(
        key=f"{name}.error",
        value=None,
        unit="",
        window=Window("instant"),
        quality=Quality.UNAVAILABLE,
        sample_count=0,
        computed_at_monotonic=now,
        sources=(name,),
        notes=("metric_module_failed", type(error).__name__),
    )
