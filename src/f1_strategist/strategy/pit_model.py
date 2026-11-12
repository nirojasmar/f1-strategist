"""Pérdida en boxes y reincorporación estimadas.

El módulo es puro y determinista: recibe el estado, el histórico, el perfil del
fin de semana y la configuración, y devuelve estimaciones inmutables. No usa
reloj, no hace E/S y no inventa valores: cuando no hay ninguna fuente, la
estimación queda no disponible (nunca vale cero).

La pérdida se toma de la fuente más específica disponible, en este orden:
anulación explícita de la configuración, mediana del campo en esta carrera,
prior del fin de semana, tiempo de carril de boxes del juego y, si no hay
ninguna, ``none``. La reincorporación se calcula con los intervalos al líder y
**supone que los rivales no paran**: es un supuesto declarado, no un dato.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Literal

from ..config import StrategyConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.history import LapRecord, MetricInputs
from ..metrics.types import Quality
from ..race_state import CarSnapshot

#: Origen de la pérdida en boxes estimada.
PitLossSource = Literal["override", "field", "profile", "lane", "none"]

PIT_LOSS_SOURCES: frozenset[str] = frozenset({"override", "field", "profile", "lane", "none"})

NOTE_OVERRIDE = "pit_loss_override"
NOTE_FIELD_DISPERSION = "field_dispersion"
NOTE_FIXED_BAND = "fixed_band"
NOTE_LANE_TIME_ONLY = "lane_time_only"
NOTE_NO_PIT_LOSS = "no_pit_loss_source"
NOTE_PROFILE_PRIOR = "profile_prior_used"
NOTE_RIVALS_DO_NOT_PIT = "rivals_do_not_pit"
NOTE_LAPPED_CARS_IGNORED = "lapped_cars_ignored"
NOTE_RIVAL_GAP_UNAVAILABLE = "rival_gap_unavailable"
NOTE_LEADER_GAP_UNAVAILABLE = "leader_gap_unavailable"
NOTE_GAME_REJOIN_MISMATCH = "game_rejoin_mismatch"
NOTE_PLAYER_UNAVAILABLE = "player_car_unavailable"

#: Estados que excluyen a un coche del cálculo de reincorporación.
_RETIRED_STATUSES = frozenset({"retired", "inactive", "invalid"})

#: Vueltas de referencia a cada lado de una parada para estimar su pérdida.
_REFERENCE_WINDOW = 3

#: Paradas mínimas del campo para considerar representativa la mediana.
_MIN_FIELD_STOPS = 3

#: Factor de consistencia de la desviación absoluta mediana (MAD).
_MAD_FACTOR = 1.4826


def _check_finite(value: float, name: str, *, minimum: float | None = None) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} no puede ser menor que {minimum}")
    return value


def _active(car: CarSnapshot) -> bool:
    return car.result_status not in _RETIRED_STATUSES and car.driver_status not in _RETIRED_STATUSES


def _freshness_is_stale(inputs: MetricInputs) -> bool:
    for group in ("lap_data", "session"):
        try:
            freshness = inputs.state.freshness_for(group)
        except KeyError:
            continue
        if freshness.is_stale(inputs.now):
            return True
    return False


@dataclass(frozen=True, slots=True)
class PitLossEstimate:
    """Pérdida en boxes estimada, con su banda de incertidumbre."""

    value_s: float | None
    band_s: float | None
    dispersion_s: float | None
    source: PitLossSource
    sample_count: int
    quality: Quality
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source not in PIT_LOSS_SOURCES:
            raise ValueError(f"Origen de pérdida desconocido: {self.source}")
        if self.sample_count < 0:
            raise ValueError("sample_count no puede ser negativo")
        if self.value_s is not None:
            _check_finite(self.value_s, "value_s", minimum=0.0)
        if self.band_s is not None:
            _check_finite(self.band_s, "band_s", minimum=0.0)
        if self.dispersion_s is not None:
            _check_finite(self.dispersion_s, "dispersion_s", minimum=0.0)
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def available(self) -> bool:
        """True si hay un valor de pérdida utilizable."""
        return self.value_s is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "value_s": self.value_s,
            "band_s": self.band_s,
            "dispersion_s": self.dispersion_s,
            "source": self.source,
            "sample_count": self.sample_count,
            "quality": self.quality.value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RejoinEstimate:
    """Posición y tráfico esperados al salir de boxes."""

    position: int | None
    car_ahead_index: int | None = None
    car_behind_index: int | None = None
    gap_ahead_s: float | None = None
    gap_behind_s: float | None = None
    traffic_risk: bool = False
    game_rejoin_position: int | None = None
    game_delta_positions: int | None = None
    quality: Quality = Quality.UNAVAILABLE
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.position is not None and self.position < 1:
            raise ValueError("position debe ser positiva")
        if self.car_ahead_index is not None and self.car_ahead_index < 0:
            raise ValueError("car_ahead_index no puede ser negativo")
        if self.car_behind_index is not None and self.car_behind_index < 0:
            raise ValueError("car_behind_index no puede ser negativo")
        for name in ("gap_ahead_s", "gap_behind_s"):
            value = getattr(self, name)
            if value is not None:
                _check_finite(value, name, minimum=0.0)
        if self.game_rejoin_position is not None and self.game_rejoin_position < 1:
            raise ValueError("game_rejoin_position debe ser positiva")
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def available(self) -> bool:
        """True si se pudo calcular una posición de reincorporación."""
        return self.position is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "car_ahead_index": self.car_ahead_index,
            "car_behind_index": self.car_behind_index,
            "gap_ahead_s": self.gap_ahead_s,
            "gap_behind_s": self.gap_behind_s,
            "traffic_risk": self.traffic_risk,
            "game_rejoin_position": self.game_rejoin_position,
            "game_delta_positions": self.game_delta_positions,
            "quality": self.quality.value,
            "notes": list(self.notes),
        }


def _stop_pairs(inputs: MetricInputs) -> tuple[tuple[LapRecord, LapRecord], ...]:
    """Empareja cada entrada a boxes con su salida posterior, como en ``metrics.pit``."""
    safety = frozenset({"safety_car", "virtual_safety_car", "red_flag"})
    ineligible = frozenset(
        {"incomplete", "partial_lap", "telemetry_partial", "retired", "invalid", "flashback"}
    )
    pairs: list[tuple[LapRecord, LapRecord]] = []
    for car_index in inputs.history.records:
        records = tuple(record for record in inputs.history.records_for(car_index) if record.closed)
        entries = tuple(record for record in records if record.pit_entry)
        exits = tuple(record for record in records if record.pit_exit)
        for entry in entries:
            exit_record = next(
                (
                    record
                    for record in exits
                    if record.lap_number > entry.lap_number
                    and not any(
                        entry.lap_number < other.lap_number < record.lap_number for other in entries
                    )
                ),
                None,
            )
            if exit_record is None:
                continue
            entry_flags = classify_lap(entry, inputs.config)
            exit_flags = classify_lap(exit_record, inputs.config)
            if safety.intersection(entry_flags | exit_flags):
                continue
            if ineligible.intersection(entry_flags | exit_flags):
                continue
            if entry.valid is False or exit_record.valid is False:
                continue
            if entry.lap_time_s is None or exit_record.lap_time_s is None:
                continue
            pairs.append((entry, exit_record))
    return tuple(pairs)


def _loss_samples(inputs: MetricInputs) -> tuple[float, ...]:
    """Pérdida observada de cada parada: entrada + salida − 2 · ritmo de referencia."""
    losses: list[float] = []
    for entry, exit_record in _stop_pairs(inputs):
        records = inputs.history.records_for(entry.car_index)
        reference = tuple(
            record.lap_time_s
            for record in records
            if record.closed
            and record.lap_time_s is not None
            and entry.lap_number - _REFERENCE_WINDOW
            <= record.lap_number
            <= exit_record.lap_number + _REFERENCE_WINDOW
            and record.lap_number not in (entry.lap_number, exit_record.lap_number)
            and record.valid is not False
            and is_clean(classify_lap(record, inputs.config))
        )
        if not reference or entry.lap_time_s is None or exit_record.lap_time_s is None:
            continue
        losses.append(entry.lap_time_s + exit_record.lap_time_s - 2 * median(reference))
    return tuple(losses)


def _robust_dispersion(samples: tuple[float, ...]) -> float | None:
    """Desviación absoluta mediana escalada; ``None`` con menos de dos muestras."""
    if len(samples) < 2:
        return None
    center = median(samples)
    return _MAD_FACTOR * median([abs(sample - center) for sample in samples])


def _field_loss(inputs: MetricInputs, cfg: StrategyConfig, stale: bool) -> PitLossEstimate | None:
    samples = _loss_samples(inputs)
    if not samples:
        return None
    value = median(samples)
    dispersion = _robust_dispersion(samples)
    if dispersion is not None and dispersion > 0:
        band = dispersion
        notes = (NOTE_FIELD_DISPERSION,)
    else:
        band = cfg.pit_loss_band_s
        notes = (NOTE_FIXED_BAND,)
    _, ok = inputs.config.thresholds("pit")
    enough = len(samples) >= ok and len(samples) >= _MIN_FIELD_STOPS
    quality = Quality.OK if enough else Quality.LOW
    if stale:
        quality = Quality.STALE
    return PitLossEstimate(
        value_s=value,
        band_s=band,
        dispersion_s=dispersion,
        source="field",
        sample_count=len(samples),
        quality=quality,
        notes=notes,
    )


def _profile_loss(inputs: MetricInputs, cfg: StrategyConfig, stale: bool) -> PitLossEstimate | None:
    profile = inputs.profile
    if profile is None:
        return None
    entry = profile.pit_loss_s
    if entry.value is None:
        return None
    quality = Quality.STALE if stale else entry.quality
    return PitLossEstimate(
        value_s=entry.value,
        band_s=cfg.pit_loss_band_s,
        dispersion_s=None,
        source="profile",
        sample_count=entry.samples,
        quality=quality,
        notes=(NOTE_PROFILE_PRIOR, NOTE_FIXED_BAND),
    )


def _lane_loss(inputs: MetricInputs, cfg: StrategyConfig, stale: bool) -> PitLossEstimate | None:
    records = tuple(
        record
        for record in inputs.history.records_for(inputs.car_index)
        if record.closed and record.pit_entry and record.pit_lane_time_s is not None
    )
    if not records:
        return None
    lane_time = records[-1].pit_lane_time_s
    if lane_time is None:
        return None
    return PitLossEstimate(
        value_s=lane_time,
        band_s=cfg.pit_loss_band_s,
        dispersion_s=None,
        source="lane",
        sample_count=1,
        quality=Quality.STALE if stale else Quality.LOW,
        notes=(NOTE_LANE_TIME_ONLY, NOTE_FIXED_BAND),
    )


def estimate_pit_loss(inputs: MetricInputs, cfg: StrategyConfig) -> PitLossEstimate:
    """Estima la pérdida en boxes con la fuente más específica disponible."""
    stale = _freshness_is_stale(inputs)
    if cfg.pit_loss_override_s is not None:
        return PitLossEstimate(
            value_s=cfg.pit_loss_override_s,
            band_s=cfg.pit_loss_band_s,
            dispersion_s=None,
            source="override",
            sample_count=0,
            quality=Quality.OK,
            notes=(NOTE_OVERRIDE,),
        )
    candidates = (
        _field_loss(inputs, cfg, stale),
        _profile_loss(inputs, cfg, stale),
        _lane_loss(inputs, cfg, stale),
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return PitLossEstimate(
        value_s=None,
        band_s=None,
        dispersion_s=None,
        source="none",
        sample_count=0,
        quality=Quality.UNAVAILABLE,
        notes=(NOTE_NO_PIT_LOSS,),
    )


def _leader_gap(car: CarSnapshot) -> float | None:
    if car.position == 1:
        return 0.0
    if car.delta_to_leader_ms is None:
        return None
    return car.delta_to_leader_ms / 1000.0


def estimate_rejoin(
    inputs: MetricInputs,
    pit_loss_s: float,
    cfg: StrategyConfig,
) -> RejoinEstimate:
    """Calcula la posición esperada al salir de boxes y el tráfico inmediato.

    Se supone que los rivales no paran: solo se desplaza al piloto. Los coches
    retirados, inactivos o sin intervalo conocido no cuentan; los doblados se
    ignoran y se declara en las notas.
    """
    _check_finite(pit_loss_s, "pit_loss_s", minimum=0.0)
    stale = _freshness_is_stale(inputs)
    player = inputs.car
    if player is None:
        return RejoinEstimate(
            position=None,
            quality=Quality.UNAVAILABLE,
            notes=(NOTE_PLAYER_UNAVAILABLE,),
        )
    own_gap = _leader_gap(player)
    if own_gap is None:
        return RejoinEstimate(
            position=None,
            quality=Quality.INSUFFICIENT,
            notes=(NOTE_LEADER_GAP_UNAVAILABLE,),
        )

    notes: list[str] = [NOTE_RIVALS_DO_NOT_PIT]
    rivals: list[tuple[float, int]] = []
    lapped = False
    missing_gap = False
    for car in inputs.state.cars:
        if car.car_index == player.car_index or not _active(car):
            continue
        if car.position is None:
            continue
        if (
            car.current_lap is not None
            and player.current_lap is not None
            and car.current_lap < player.current_lap
        ):
            lapped = True
            continue
        if car.position == 1:
            rivals.append((0.0, car.car_index))
            continue
        if car.delta_to_leader_ms is None:
            missing_gap = True
            continue
        rivals.append((car.delta_to_leader_ms / 1000.0, car.car_index))

    if lapped:
        notes.append(NOTE_LAPPED_CARS_IGNORED)
    if missing_gap:
        notes.append(NOTE_RIVAL_GAP_UNAVAILABLE)
    if not rivals:
        return RejoinEstimate(
            position=None,
            quality=Quality.INSUFFICIENT,
            notes=tuple(notes),
        )

    projected = own_gap + pit_loss_s
    position = 1 + sum(1 for gap, _ in rivals if gap < projected)
    ahead = [(gap, index) for gap, index in rivals if gap < projected]
    behind = [(gap, index) for gap, index in rivals if gap >= projected]
    gap_ahead: float | None = None
    ahead_index: int | None = None
    if ahead:
        gap, ahead_index = max(ahead)
        gap_ahead = max(0.0, projected - gap)
    gap_behind: float | None = None
    behind_index: int | None = None
    if behind:
        gap, behind_index = min(behind)
        gap_behind = max(0.0, gap - projected)

    traffic_risk = gap_ahead is not None and gap_ahead < inputs.config.traffic_gap_s

    game_position = inputs.state.session.pit_rejoin_position
    game_delta = position - game_position if game_position is not None else None
    if game_delta not in (None, 0):
        notes.append(NOTE_GAME_REJOIN_MISMATCH)

    quality = Quality.OK if player.position is not None and len(rivals) >= 3 else Quality.LOW
    if stale:
        quality = Quality.STALE

    return RejoinEstimate(
        position=position,
        car_ahead_index=ahead_index,
        car_behind_index=behind_index,
        gap_ahead_s=gap_ahead,
        gap_behind_s=gap_behind,
        traffic_risk=traffic_risk,
        game_rejoin_position=game_position,
        game_delta_positions=game_delta,
        quality=quality,
        notes=tuple(notes),
    )


__all__ = [
    "NOTE_LANE_TIME_ONLY",
    "NOTE_LAPPED_CARS_IGNORED",
    "NOTE_LEADER_GAP_UNAVAILABLE",
    "NOTE_NO_PIT_LOSS",
    "NOTE_OVERRIDE",
    "NOTE_PLAYER_UNAVAILABLE",
    "NOTE_PROFILE_PRIOR",
    "NOTE_RIVALS_DO_NOT_PIT",
    "NOTE_RIVAL_GAP_UNAVAILABLE",
    "PIT_LOSS_SOURCES",
    "PitLossEstimate",
    "PitLossSource",
    "RejoinEstimate",
    "estimate_pit_loss",
    "estimate_rejoin",
]
