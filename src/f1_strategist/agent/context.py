"""Construcción del contexto mínimo que ve el modelo (Fase 6, L3).

El contexto es puro y determinista: solo copia datos ya publicados por los
snapshots, los sanea y los recorta para respetar el presupuesto de tokens. No
consulta relojes, red ni estado global, de modo que la misma carrera produce el
mismo ``context_hash`` a cualquier velocidad de reproducción.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..config import AgentConfig, AppConfig
from ..events.types import AgentAlert, EventsSnapshot
from ..metrics.engine import MetricsSnapshot
from ..metrics.types import Quality
from ..race_state import RaceStateSnapshot
from ..strategy.types import StrategySnapshot
from .types import AgentMessage, ContextKind, ContextPayload

# Longitud máxima de un valor de texto procedente del juego.
_MAX_VALUE_CHARS = 80
# Longitud máxima de un mensaje reciente reutilizado para dar coherencia.
_MAX_MESSAGE_CHARS = 200
# Métricas incluidas como máximo antes de aplicar el presupuesto.
_MAX_METRICS = 6

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")

# Unidades por nombre de hecho: ``Fact`` no transporta unidad.
_FACT_UNITS: dict[str, str] = {
    "age_laps": "laps",
    "age_s": "s",
    "air_temperature_c": "celsius",
    "confidence": "ratio",
    "consumption_per_lap": "kg/lap",
    "damage_pct": "pct",
    "delta_pct": "pct",
    "expected_rate_pct_per_lap": "pct/lap",
    "gain_high_s": "s",
    "gain_low_s": "s",
    "gain_s": "s",
    "gap_s": "s",
    "horizon_min": "min",
    "ideal_lap": "lap",
    "lap": "lap",
    "laps_left": "laps",
    "laps_remaining": "laps",
    "laps_to_ideal": "laps",
    "latest_lap": "lap",
    "margin_kg": "kg",
    "margin_laps": "laps",
    "pace_trend_s_per_lap": "s/lap",
    "pit_lap": "lap",
    "position": "position",
    "probability_pct": "pct",
    "ratio": "ratio",
    "track_temperature_c": "celsius",
    "wear_mean_pct": "pct",
    "wear_rate_pct_per_lap": "pct/lap",
}

# Prefijos de métricas relevantes por categoría de alerta.
_CATEGORY_METRIC_PREFIXES: dict[str, tuple[str, ...]] = {
    "damage": ("damage.",),
    "rivals": ("gap.", "traffic.", "rivals."),
    "fuel": ("fuel.", "consumption."),
    "tyres": ("tyre.", "tyres.", "wear.", "pace."),
    "strategy": ("strategy.", "pit.", "tyre.", "tyres.", "fuel."),
    "track": ("track.", "weather.", "safety."),
    "weather": ("weather.", "track."),
    "data": ("metrics.",),
}


def _sanitize(value: str, limit: int) -> str:
    """Quita controles y saltos de línea, colapsa espacios y acota la longitud."""
    cleaned = _CONTROL_CHARS.sub(" ", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned[:limit]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize(value, _MAX_VALUE_CHARS)
    if isinstance(value, (tuple, list)):
        return [_sanitize_value(item) for item in value]
    return value


def _fact_value(name: str, value: Any) -> Any:
    """Publica car_index en la convención 1-based que se muestra al piloto."""
    if name == "car_index" and isinstance(value, int) and not isinstance(value, bool):
        return value + 1
    return _sanitize_value(value)


def _estimate_tokens(payload: ContextPayload) -> int:
    """Estimación determinista y barata: un token cada cuatro caracteres."""
    return max(1, (len(payload.to_json()) + 3) // 4)


def _lap(state: RaceStateSnapshot, alert: AgentAlert | None) -> int | None:
    if state.current_lap is not None:
        return state.current_lap
    if alert is not None and alert.lap > 0:
        return alert.lap
    return None


def _situation(state: RaceStateSnapshot, lap: int | None) -> dict[str, Any]:
    total_laps = state.session.total_laps
    laps_remaining = (
        max(0, total_laps - lap) if total_laps is not None and lap is not None else None
    )
    car = state.player_car
    compound = car.actual_tyre_compound if car is not None else None
    return {
        "lap": lap,
        "laps_remaining": laps_remaining,
        "position": car.position if car is not None else None,
        "compound": _sanitize(compound, _MAX_VALUE_CHARS) if compound else None,
        "tyre_age_laps": car.tyre_age_laps if car is not None else None,
    }


def _missing_uncertainties(block: dict[str, Any]) -> list[str]:
    return [f"missing:{key}" for key, value in block.items() if value is None]


def _strategy_block(
    strategy: StrategySnapshot | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    evaluation = strategy.evaluation if strategy is not None else None
    if evaluation is None:
        return None, ["strategy_unavailable"]
    recommendation = evaluation.recommendation
    low, high = recommendation.gain_interval_s or (None, None)
    block: dict[str, Any] = {
        "action": recommendation.action.value,
        "key": recommendation.key,
        "pit_lap": recommendation.pit_lap,
        "compound": (
            _sanitize(recommendation.compound, _MAX_VALUE_CHARS)
            if recommendation.compound
            else None
        ),
        "gain_vs_baseline_s": recommendation.gain_vs_baseline_s,
        "gain_low_s": low,
        "gain_high_s": high,
        "confidence": recommendation.confidence,
        "assumptions": list(evaluation.assumptions),
        "risks": list(evaluation.risks),
        "lap": evaluation.lap,
        "laps_remaining": evaluation.laps_remaining,
        "data_quality": evaluation.data_quality.value,
    }
    uncertainties: list[str] = []
    if evaluation.data_quality == Quality.STALE:
        uncertainties.append("stale:strategy")
    elif evaluation.data_quality in (Quality.LOW, Quality.INSUFFICIENT, Quality.UNAVAILABLE):
        uncertainties.append(f"{evaluation.data_quality.value}:strategy")
    return block, uncertainties


def _relevant_metrics(
    metrics: MetricsSnapshot | None, category: str | None
) -> tuple[dict[str, Any], list[str]]:
    if metrics is None or not len(metrics):
        return {}, []
    prefixes = _CATEGORY_METRIC_PREFIXES.get(category or "", ())
    keys = sorted(metrics.keys())
    preferred = [key for key in keys if any(key.startswith(p) for p in prefixes)]
    selected = preferred + [key for key in keys if key not in preferred]
    selected = selected[:_MAX_METRICS]
    block: dict[str, Any] = {}
    uncertainties: list[str] = []
    for key in selected:
        metric = metrics[key]
        block[key] = {
            "value": _sanitize_value(metric.value),
            "unit": metric.unit,
            "quality": metric.quality.value,
        }
        if metric.quality != Quality.OK:
            uncertainties.append(f"{metric.quality.value}:{key}")
    return block, uncertainties


def _stale_uncertainties(alert: AgentAlert | None, events: EventsSnapshot | None) -> list[str]:
    alerts = () if events is None else events.alerts
    signals = (alert,) if alert is not None else ()
    if any(item.message_key == "event.data_stale" for item in (*alerts, *signals)):
        return ["stale:telemetry"]
    return []


def _recent_texts(recent: Sequence[AgentMessage | str], limit: int) -> list[str]:
    if limit <= 0:
        return []
    texts: list[str] = []
    for item in recent:
        raw = item.text if isinstance(item, AgentMessage) else str(item)
        cleaned = _sanitize(raw, _MAX_MESSAGE_CHARS)
        if cleaned:
            texts.append(cleaned)
    return texts[-limit:]


class ContextBuilder:
    """Compone el ``ContextPayload`` de una alerta o de una pregunta del piloto."""

    def __init__(self, config: AgentConfig | AppConfig) -> None:
        self._config = config.agent if isinstance(config, AppConfig) else config

    def build_alert(
        self,
        alert: AgentAlert,
        *,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot | None = None,
        strategy: StrategySnapshot | None = None,
        events: EventsSnapshot | None = None,
        recent: Sequence[AgentMessage | str] = (),
    ) -> ContextPayload:
        """Contexto de una alerta; los hechos de la alerta nunca se recortan."""
        lap = _lap(state, alert)
        situation = _situation(state, lap)
        strategy_block, strategy_uncertainties = _strategy_block(strategy)
        metric_block, metric_uncertainties = _relevant_metrics(metrics, alert.category)
        uncertainties = [
            *_missing_uncertainties(situation),
            *strategy_uncertainties,
            *metric_uncertainties,
            *_stale_uncertainties(alert, events),
        ]
        alert_block = {
            "message_key": alert.message_key,
            "priority": alert.priority.value,
            "category": alert.category,
            "lap": lap,
            "facts": [
                {"name": name, "value": _fact_value(name, value), "unit": _FACT_UNITS.get(name, "")}
                for name, value in alert.facts
            ],
        }
        return self._fit(
            kind=ContextKind.ALERT,
            alert=alert_block,
            situation=situation,
            strategy=strategy_block,
            metrics=metric_block,
            uncertainties=uncertainties,
            recent=_recent_texts(recent, self._config.recent_messages),
            constraints=self._constraints(),
        )

    def build_question(
        self,
        question: str,
        *,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot | None = None,
        strategy: StrategySnapshot | None = None,
        events: EventsSnapshot | None = None,
        recent: Sequence[AgentMessage | str] = (),
    ) -> ContextPayload:
        """Contexto de una pregunta del piloto (contrato reservado a la Fase 7)."""
        lap = _lap(state, None)
        situation = _situation(state, lap)
        strategy_block, strategy_uncertainties = _strategy_block(strategy)
        metric_block, metric_uncertainties = _relevant_metrics(metrics, None)
        uncertainties = [
            *_missing_uncertainties(situation),
            *strategy_uncertainties,
            *metric_uncertainties,
            *_stale_uncertainties(None, events),
        ]
        constraints = {**self._constraints(), "question": _sanitize(question, _MAX_MESSAGE_CHARS)}
        return self._fit(
            kind=ContextKind.QUESTION,
            alert=None,
            situation=situation,
            strategy=strategy_block,
            metrics=metric_block,
            uncertainties=uncertainties,
            recent=_recent_texts(recent, self._config.recent_messages),
            constraints=constraints,
        )

    def _constraints(self) -> dict[str, Any]:
        return {
            "max_words": self._config.max_words,
            "max_chars": self._config.max_chars,
            "tone": "radio",
        }

    def _fit(
        self,
        *,
        kind: ContextKind,
        alert: dict[str, Any] | None,
        situation: dict[str, Any],
        strategy: dict[str, Any] | None,
        metrics: dict[str, Any],
        uncertainties: list[str],
        recent: list[str],
        constraints: dict[str, Any],
    ) -> ContextPayload:
        """Recorta métricas (de menor a mayor relevancia) y luego mensajes viejos."""
        metric_items = list(metrics.items())
        recent_items = list(recent)
        ordered_uncertainties = tuple(sorted(dict.fromkeys(uncertainties)))
        while True:
            payload = ContextPayload(
                kind=kind,
                language=self._config.language,
                alert=alert,
                situation=situation,
                strategy=strategy,
                metrics=dict(metric_items),
                uncertainties=ordered_uncertainties,
                recent_messages=tuple(recent_items),
                constraints=constraints,
            )
            if _estimate_tokens(payload) <= self._config.context_token_budget:
                return payload
            if metric_items:
                metric_items.pop()
            elif recent_items:
                recent_items.pop(0)
            else:
                # Los hechos de la alerta son intocables: el mínimo puede exceder el presupuesto.
                return payload
