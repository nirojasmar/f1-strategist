"""Tipos inmutables del motor de estrategia y su serialización canónica.

Este módulo fija el contrato común de la Fase 4: acciones, alternativas,
evaluación y snapshot. No contiene cálculo: los modelos de ritmo, boxes,
neumáticos y decisión viven en sus propios módulos y reciben estos tipos.

La serialización es estable: las claves se ordenan y los flotantes se
redondean, de modo que dos ejecuciones equivalentes producen el mismo JSON y
el mismo ``inputs_hash``. Los textos visibles son claves de mensaje; el
español se resuelve en la capa de presentación.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from ..metrics.types import Quality

STRATEGY_SCHEMA_VERSION = 2
"""Versión del contrato serializado; se incrementa ante cambios incompatibles."""

BASELINE_STAY_OUT = "stay_out"
"""Clave de la línea base: continuar con el compuesto montado."""

ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED = "fuel_effect_not_separated"
ASSUMPTION_RIVALS_DO_NOT_PIT = "rivals_do_not_pit"
ASSUMPTION_PROFILE_PRIOR_USED = "profile_prior_used"

RISK_DAMAGE_NOT_MODELLED = "damage_not_modelled"
RISK_TYRE_DAMAGE_NOT_MODELLED = "tyre_damage_not_modelled"
RISK_TRAFFIC_NOT_PRICED = "traffic_not_priced"
RISK_TYRE_LIFE_EXCEEDED = "tyre_life_exceeded"
RISK_WET_CONDITIONS = "wet_conditions"

_FLOAT_DIGITS = 9


class StrategyAction(StrEnum):
    """Acción recomendada por el motor."""

    NO_CHANGE = "NO_CHANGE"
    PIT_NOW = "PIT_NOW"
    PIT_IN = "PIT_IN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class Perturbation:
    """Variación aplicada a las entradas para medir sensibilidad."""

    slope_scale: float = 1.0
    pit_loss_delta_s: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.slope_scale) or self.slope_scale <= 0:
            raise ValueError("slope_scale debe ser finito y positivo")
        if not math.isfinite(self.pit_loss_delta_s):
            raise ValueError("pit_loss_delta_s debe ser finito")


@dataclass(frozen=True, slots=True)
class AlternativeResult:
    """Resultado de evaluar una alternativa concreta (salida de S5)."""

    key: str
    action: StrategyAction
    remaining_time_s: float
    gain_vs_baseline_s: float
    quality: Quality
    pit_lap: int | None = None
    compound: str | None = None
    set_index: int | None = None
    projected_laps_s: float | None = None
    pit_loss_s: float | None = None
    traffic_cost_s: float | None = None
    rejoin_position: int | None = None
    traffic_risk: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la alternativa no puede estar vacía")
        _check_finite("remaining_time_s", self.remaining_time_s)
        _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.pit_lap is not None and self.pit_lap < 1:
            raise ValueError("pit_lap debe ser positivo")
        if self.set_index is not None and self.set_index < 0:
            raise ValueError("set_index no puede ser negativo")
        if self.rejoin_position is not None and self.rejoin_position < 1:
            raise ValueError("rejoin_position debe ser positivo")
        for name in ("projected_laps_s", "pit_loss_s", "traffic_cost_s"):
            value = getattr(self, name)
            if value is not None:
                _check_finite(name, value)
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action.value,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "set_index": self.set_index,
            "remaining_time_s": _round(self.remaining_time_s),
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "projected_laps_s": _round(self.projected_laps_s),
            "pit_loss_s": _round(self.pit_loss_s),
            "traffic_cost_s": _round(self.traffic_cost_s),
            "rejoin_position": self.rejoin_position,
            "traffic_risk": self.traffic_risk,
            "quality": self.quality.value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    """Recomendación publicada dentro de una evaluación."""

    action: StrategyAction
    key: str
    pit_lap: int | None = None
    compound: str | None = None
    gain_vs_baseline_s: float | None = None
    gain_interval_s: tuple[float, float] | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la recomendación no puede estar vacía")
        if self.pit_lap is not None and self.pit_lap < 1:
            raise ValueError("pit_lap debe ser positivo")
        if self.gain_vs_baseline_s is not None:
            _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.gain_interval_s is not None:
            low, high = self.gain_interval_s
            _check_finite("gain_interval_s", low)
            _check_finite("gain_interval_s", high)
            if low > high:
                raise ValueError("gain_interval_s requiere low <= high")
            object.__setattr__(self, "gain_interval_s", (low, high))
        if self.confidence is not None:
            _check_finite("confidence", self.confidence)
            if not 0 <= self.confidence <= 1:
                raise ValueError("confidence debe estar entre 0 y 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "key": self.key,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "gain_interval_s": (
                None
                if self.gain_interval_s is None
                else [_round(self.gain_interval_s[0]), _round(self.gain_interval_s[1])]
            ),
            "confidence": _round(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyRecommendation:
        """Reconstruye la recomendación desde su representación JSON validada."""
        interval = data.get("gain_interval_s")
        return cls(
            action=StrategyAction(data["action"]),
            key=str(data["key"]),
            pit_lap=data.get("pit_lap"),
            compound=data.get("compound"),
            gain_vs_baseline_s=data.get("gain_vs_baseline_s"),
            gain_interval_s=(
                None if interval is None else (float(interval[0]), float(interval[1]))
            ),
            confidence=data.get("confidence"),
        )


@dataclass(frozen=True, slots=True)
class DataQuality:
    """Disponibilidad y calidad de las entradas críticas de la decisión.

    La decisión (S6) no recibe los modelos: recibe este resumen para poder
    declarar ``INSUFFICIENT_DATA`` sin mirar por dentro de cada componente.
    """

    pace: Quality = Quality.UNAVAILABLE
    pit_loss: Quality = Quality.UNAVAILABLE
    session_is_race: bool = True
    laps_remaining: float | None = None
    stale: bool = False
    pace_samples: int = 0
    prior_weight: float = 0.0

    def __post_init__(self) -> None:
        if self.pace_samples < 0:
            raise ValueError("pace_samples no puede ser negativo")
        if self.laps_remaining is not None:
            _check_finite("laps_remaining", self.laps_remaining)
            if self.laps_remaining < 0:
                raise ValueError("laps_remaining no puede ser negativo")
        _check_finite("prior_weight", self.prior_weight)
        if not 0 <= self.prior_weight <= 1:
            raise ValueError("prior_weight debe estar entre 0 y 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pace": self.pace.value,
            "pit_loss": self.pit_loss.value,
            "session_is_race": self.session_is_race,
            "laps_remaining": _round(self.laps_remaining),
            "stale": self.stale,
            "pace_samples": self.pace_samples,
            "prior_weight": _round(self.prior_weight),
        }


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Decisión del motor: acción, alternativa elegida e incertidumbre.

    ``sensitive_to`` y ``notes`` son conjuntos de claves: se ordenan para que
    dos decisiones equivalentes se serialicen igual.
    """

    action: StrategyAction
    key: str | None = None
    pit_lap: int | None = None
    compound: str | None = None
    gain_vs_baseline_s: float | None = None
    gain_interval_s: tuple[float, float] | None = None
    confidence: float = 0.0
    sensitive_to: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action in {StrategyAction.PIT_NOW, StrategyAction.PIT_IN} and not self.key:
            raise ValueError("Una decisión de parada requiere una clave")
        if self.key is not None and not self.key:
            raise ValueError("La clave de la decisión no puede estar vacía")
        if self.pit_lap is not None and self.pit_lap < 1:
            raise ValueError("pit_lap debe ser positivo")
        if self.gain_vs_baseline_s is not None:
            _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.gain_interval_s is not None:
            low, high = self.gain_interval_s
            _check_finite("gain_interval_s", low)
            _check_finite("gain_interval_s", high)
            if low > high:
                raise ValueError("gain_interval_s requiere low <= high")
            object.__setattr__(self, "gain_interval_s", (low, high))
        _check_finite("confidence", self.confidence)
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence debe estar entre 0 y 1")
        object.__setattr__(self, "sensitive_to", tuple(sorted(set(self.sensitive_to))))
        object.__setattr__(self, "notes", tuple(dict.fromkeys(self.notes)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "key": self.key,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "gain_interval_s": (
                None
                if self.gain_interval_s is None
                else [_round(self.gain_interval_s[0]), _round(self.gain_interval_s[1])]
            ),
            "confidence": _round(self.confidence),
            "sensitive_to": list(self.sensitive_to),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyDecision:
        """Reconstruye la decisión desde su representación JSON validada."""
        interval = data.get("gain_interval_s")
        return cls(
            action=StrategyAction(data["action"]),
            key=data.get("key"),
            pit_lap=data.get("pit_lap"),
            compound=data.get("compound"),
            gain_vs_baseline_s=data.get("gain_vs_baseline_s"),
            gain_interval_s=(
                None if interval is None else (float(interval[0]), float(interval[1]))
            ),
            confidence=float(data.get("confidence", 0.0)),
            sensitive_to=tuple(data.get("sensitive_to", ())),
            notes=tuple(data.get("notes", ())),
        )


@dataclass(frozen=True, slots=True)
class StrategyAlternative:
    """Alternativa tal como se publica en la evaluación."""

    key: str
    remaining_time_s: float
    gain_vs_baseline_s: float
    rejoin_position: int | None = None
    traffic_risk: bool | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la alternativa no puede estar vacía")
        _check_finite("remaining_time_s", self.remaining_time_s)
        _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.rejoin_position is not None and self.rejoin_position < 1:
            raise ValueError("rejoin_position debe ser positivo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "remaining_time_s": _round(self.remaining_time_s),
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "rejoin_position": self.rejoin_position,
            "traffic_risk": self.traffic_risk,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyAlternative:
        """Reconstruye la alternativa desde su representación JSON validada."""
        return cls(
            key=str(data["key"]),
            remaining_time_s=float(data["remaining_time_s"]),
            gain_vs_baseline_s=float(data["gain_vs_baseline_s"]),
            rejoin_position=data.get("rejoin_position"),
            traffic_risk=data.get("traffic_risk"),
        )


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    """Evaluación completa, inmutable y serializable (sección 5.8 del plan)."""

    revision: int
    session_uid: str
    lap: int
    laps_remaining: float | None
    baseline: str
    recommendation: StrategyRecommendation
    alternatives: tuple[StrategyAlternative, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    sensitive_to: tuple[str, ...]
    data_quality: Quality
    inputs_hash: str
    schema_version: int
    profile_hash: str | None = None
    forecast_car_index: int | None = None
    forecast_compound: str | None = None
    projected_lap_times_s: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise ValueError("schema_version debe ser un entero")
        if self.schema_version not in {1, STRATEGY_SCHEMA_VERSION}:
            raise ValueError(
                f"schema_version no soportada: {self.schema_version}; "
                f"esperada 1 o {STRATEGY_SCHEMA_VERSION}"
            )
        if self.revision < 0:
            raise ValueError("revision no puede ser negativa")
        if not self.session_uid:
            raise ValueError("session_uid no puede estar vacío")
        if self.lap < 1:
            raise ValueError("lap debe ser positivo")
        if self.laps_remaining is not None:
            _check_finite("laps_remaining", self.laps_remaining)
            if self.laps_remaining < 0:
                raise ValueError("laps_remaining no puede ser negativo")
        if not self.baseline:
            raise ValueError("baseline no puede estar vacía")
        if not self.inputs_hash:
            raise ValueError("inputs_hash no puede estar vacío")
        if self.forecast_car_index is not None and self.forecast_car_index < 0:
            raise ValueError("forecast_car_index no puede ser negativo")
        if self.forecast_compound is not None and not self.forecast_compound:
            raise ValueError("forecast_compound no puede estar vacío")
        if self.projected_lap_times_s is not None:
            projected_lap_times = tuple(self.projected_lap_times_s)
            for value in projected_lap_times:
                _check_finite("projected_lap_times_s", value)
                if value <= 0.0:
                    raise ValueError("projected_lap_times_s debe contener tiempos positivos")
            if self.forecast_car_index is None or self.forecast_compound is None:
                raise ValueError("La proyección requiere coche y compuesto")
            object.__setattr__(self, "projected_lap_times_s", projected_lap_times)
        alternatives = tuple(self.alternatives)
        keys = [item.key for item in alternatives]
        if len(set(keys)) != len(keys):
            raise ValueError("Hay alternativas duplicadas en la evaluación")
        # Las alternativas se publican ordenadas por clave: así dos evaluaciones
        # equivalentes se serializan igual. Los supuestos, riesgos y
        # sensibilidades son conjuntos y se canonizan alfabéticamente.
        object.__setattr__(
            self,
            "alternatives",
            tuple(sorted(alternatives, key=lambda item: item.key)),
        )
        object.__setattr__(self, "assumptions", tuple(sorted(set(self.assumptions))))
        object.__setattr__(self, "risks", tuple(sorted(set(self.risks))))
        object.__setattr__(self, "sensitive_to", tuple(sorted(set(self.sensitive_to))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "session_uid": self.session_uid,
            "lap": self.lap,
            "laps_remaining": _round(self.laps_remaining),
            "baseline": self.baseline,
            "recommendation": self.recommendation.to_dict(),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "assumptions": list(self.assumptions),
            "risks": list(self.risks),
            "sensitive_to": list(self.sensitive_to),
            "data_quality": self.data_quality.value,
            "inputs_hash": self.inputs_hash,
            "profile_hash": self.profile_hash,
            "forecast_car_index": self.forecast_car_index,
            "forecast_compound": self.forecast_compound,
            "projected_lap_times_s": (
                None
                if self.projected_lap_times_s is None
                else [_round(value) for value in self.projected_lap_times_s]
            ),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyEvaluation:
        """Reconstruye la evaluación desde su representación JSON validada."""
        schema_version = data.get("schema_version")
        if type(schema_version) is not int:
            raise ValueError("schema_version debe estar presente y ser un entero")
        if schema_version not in {1, STRATEGY_SCHEMA_VERSION}:
            raise ValueError(
                f"schema_version no soportada: {schema_version}; "
                f"esperada 1 o {STRATEGY_SCHEMA_VERSION}"
            )
        revision = data.get("revision")
        if type(revision) is not int:
            raise ValueError("revision debe ser un entero")
        session_uid = data.get("session_uid")
        if not isinstance(session_uid, str):
            raise ValueError("session_uid debe ser una cadena")
        forecast_car_index = data.get("forecast_car_index")
        if forecast_car_index is not None and (
            type(forecast_car_index) is not int or forecast_car_index < 0
        ):
            raise ValueError("forecast_car_index debe ser un entero no negativo o None")
        forecast_compound = data.get("forecast_compound")
        if forecast_compound is not None and not isinstance(forecast_compound, str):
            raise ValueError("forecast_compound debe ser una cadena o None")
        projected_lap_times = data.get("projected_lap_times_s")
        if projected_lap_times is not None and (
            not isinstance(projected_lap_times, (list, tuple))
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in projected_lap_times
            )
        ):
            raise ValueError("projected_lap_times_s debe ser una lista de números o None")
        return cls(
            revision=revision,
            session_uid=session_uid,
            lap=int(data["lap"]),
            laps_remaining=data.get("laps_remaining"),
            baseline=str(data["baseline"]),
            recommendation=StrategyRecommendation.from_dict(data["recommendation"]),
            alternatives=tuple(
                StrategyAlternative.from_dict(item) for item in data["alternatives"]
            ),
            assumptions=tuple(data["assumptions"]),
            risks=tuple(data["risks"]),
            sensitive_to=tuple(data["sensitive_to"]),
            data_quality=Quality(data["data_quality"]),
            inputs_hash=str(data["inputs_hash"]),
            profile_hash=data.get("profile_hash"),
            schema_version=schema_version,
            forecast_car_index=forecast_car_index,
            forecast_compound=forecast_compound,
            projected_lap_times_s=(
                None
                if projected_lap_times is None
                else tuple(float(value) for value in projected_lap_times)
            ),
        )

    @classmethod
    def from_json(cls, text: str) -> StrategyEvaluation:
        """Reconstruye la evaluación desde su JSON canónico."""
        payload = json.loads(text)
        if not isinstance(payload, Mapping):
            raise ValueError("La evaluación debe ser un objeto JSON")
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    """Última evaluación publicada por el motor; vacío hasta que S7 exista."""

    evaluation: StrategyEvaluation | None = None
    updated_at_monotonic: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        _check_finite("updated_at_monotonic", self.updated_at_monotonic)
        if self.updated_at_monotonic < 0:
            raise ValueError("updated_at_monotonic no puede ser negativo")

    @property
    def available(self) -> bool:
        return self.evaluation is not None


def compute_inputs_hash(payload: object) -> str:
    """SHA-256 del JSON canónico de las entradas, independiente del orden."""
    return hashlib.sha256(_canonical_json(_normalize(payload)).encode("utf-8")).hexdigest()


def _check_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(value, _FLOAT_DIGITS)
    return 0.0 if rounded == 0 else rounded


def _normalize(value: object) -> Any:
    if isinstance(value, Enum):
        return _normalize(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _normalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        # ``Mapping`` cubre también los ``MappingProxyType`` del histórico; sin
        # esto caerían al ``str(value)`` y el orden de inserción alteraría el hash.
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted((_normalize(item) for item in value), key=_canonical_json)
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("El hash de entradas no admite valores no finitos")
        return _round(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
