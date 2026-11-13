"""Pérdida en boxes y reincorporación estimadas.

El cálculo es determinista: recibe el estado, el histórico, el perfil del fin
de semana y la configuración, y devuelve estimaciones inmutables. La tabla
estática de circuitos se carga una sola vez desde los recursos del paquete.
No usa reloj ni inventa valores: cuando no hay ninguna fuente, la estimación
queda no disponible (nunca vale cero).

La pérdida se toma de la fuente más específica disponible, en este orden:
anulación explícita de la configuración, mediana del campo en esta carrera,
prior del fin de semana, tiempo de carril de boxes del juego, prior por
circuito y, si no hay ninguna, ``none``. La reincorporación se calcula con los
intervalos al líder y **supone que los rivales no paran**: es un supuesto
declarado, no un dato.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from io import StringIO
from statistics import median
from typing import Literal

from ..config import StrategyConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.history import LapRecord, MetricInputs
from ..metrics.types import Quality
from ..race_state import CarSnapshot

#: Origen de la pérdida en boxes estimada.
PitLossSource = Literal["override", "field", "profile", "lane", "circuit", "none"]

PIT_LOSS_SOURCES: frozenset[str] = frozenset(
    {"override", "field", "profile", "lane", "circuit", "none"}
)

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
NOTE_NEGATIVE_SAMPLES_DISCARDED = "negative_pit_loss_samples_discarded"
NOTE_CIRCUIT_PRIOR = "circuit_pit_loss_prior"
NOTE_CIRCUIT_GREEN_NOT_OFFICIAL = "circuit_pit_loss_not_official"
NOTE_CIRCUIT_VSC_ESTIMATE = "circuit_vsc_pit_loss_estimate"
NOTE_CIRCUIT_SC_ESTIMATE = "circuit_sc_pit_loss_estimate"
NOTE_CIRCUIT_SC_UNCERTAINTY = "circuit_sc_uncertainty"

#: Estados de resultado que excluyen a un coche del cálculo de reincorporación.
_RETIRED_STATUSES = frozenset(
    {
        "retired",
        "inactive",
        "invalid",
        "did_not_finish",
        "disqualified",
        "not_classified",
    }
)

#: Vueltas de referencia a cada lado de una parada para estimar su pérdida.
_REFERENCE_WINDOW = 3

#: Paradas mínimas del campo para considerar representativa la mediana.
_MIN_FIELD_STOPS = 3

#: Factor de consistencia de la desviación absoluta mediana (MAD).
_MAD_FACTOR = 1.4826

_TRACK_CIRCUIT_ALIASES = {
    "melbourne": "australia",
    "australia": "australia",
    "shanghai": "chinashanghai",
    "china": "chinashanghai",
    "chinashanghai": "chinashanghai",
    "sakhir": "bahrainsakhir",
    "bahrain": "bahrainsakhir",
    "bahrainsakhir": "bahrainsakhir",
    "catalunya": "spainbarcelonacatalunya",
    "barcelona": "spainbarcelonacatalunya",
    "spainbarcelonacatalunya": "spainbarcelonacatalunya",
    "monaco": "monaco",
    "montreal": "canadamontreal",
    "canada": "canadamontreal",
    "canadamontreal": "canadamontreal",
    "silverstone": "greatbritainsilverstone",
    "silverstonereverse": "greatbritainsilverstone",
    "greatbritain": "greatbritainsilverstone",
    "greatbritainsilverstone": "greatbritainsilverstone",
    "hungaroring": "hungaryhungaroring",
    "hungary": "hungaryhungaroring",
    "hungaryhungaroring": "hungaryhungaroring",
    "spa": "belgiumspafrancorchamps",
    "belgium": "belgiumspafrancorchamps",
    "belgiumspafrancorchamps": "belgiumspafrancorchamps",
    "monza": "italymonza",
    "italy": "italymonza",
    "italymonza": "italymonza",
    "singapore": "singaporemarinabay",
    "marinabay": "singaporemarinabay",
    "singaporemarinabay": "singaporemarinabay",
    "suzuka": "japansuzuka",
    "japan": "japansuzuka",
    "japansuzuka": "japansuzuka",
    "abudhabi": "abudhabiyasmarina",
    "yasmarina": "abudhabiyasmarina",
    "abudhabiyasmarina": "abudhabiyasmarina",
    "texas": "unitedstatesaustincota",
    "austin": "unitedstatesaustincota",
    "cota": "unitedstatesaustincota",
    "unitedstatesaustincota": "unitedstatesaustincota",
    "brazil": "saopaulointerlagos",
    "interlagos": "saopaulointerlagos",
    "saopaulointerlagos": "saopaulointerlagos",
    "austria": "austriaredbullring",
    "austriareverse": "austriaredbullring",
    "redbullring": "austriaredbullring",
    "austriaredbullring": "austriaredbullring",
    "mexico": "mexicocityhermanosrodriguez",
    "mexicocity": "mexicocityhermanosrodriguez",
    "hermanosrodriguez": "mexicocityhermanosrodriguez",
    "mexicocityhermanosrodriguez": "mexicocityhermanosrodriguez",
    "baku": "azerbaijanbaku",
    "azerbaijan": "azerbaijanbaku",
    "azerbaijanbaku": "azerbaijanbaku",
    "zandvoort": "netherlandszandvoort",
    "zandvoortreverse": "netherlandszandvoort",
    "netherlands": "netherlandszandvoort",
    "netherlandszandvoort": "netherlandszandvoort",
    "imola": "emiliaromagnaimola",
    "emiliaromagna": "emiliaromagnaimola",
    "emiliaromagnaimola": "emiliaromagnaimola",
    "jeddah": "saudiarabiajeddah",
    "saudiarabia": "saudiarabiajeddah",
    "saudiarabiajeddah": "saudiarabiajeddah",
    "miami": "miami",
    "lasvegas": "lasvegasstripcircuit",
    "lasvegasstripcircuit": "lasvegasstripcircuit",
    "losail": "qatarlusail",
    "lusail": "qatarlusail",
    "qatar": "qatarlusail",
    "qatarlusail": "qatarlusail",
    "madrid": "spainmadridmadring",
    "madring": "spainmadridmadring",
    "spainmadridmadring": "spainmadridmadring",
}


@dataclass(frozen=True, slots=True)
class _CircuitPitLoss:
    green_flag_s: float | None
    vsc_s: float | None
    sc_s: float | None
    sc_uncertainty_s: float | None
    green_flag_is_official: bool


def _normalize_circuit_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return "".join(character.lower() for character in normalized if character.isalnum())


def _csv_number(value: str | None, *, circuit: str, column: str) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"Valor inválido en {column!r} para {circuit!r}: {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Valor no válido en {column!r} para {circuit!r}: {value!r}")
    return number


@lru_cache(maxsize=1)
def _circuit_pit_losses() -> dict[str, _CircuitPitLoss]:
    resource = files("f1_strategist").joinpath("data", "F1-pit-loss-calendars-2025-2026.csv")
    reader = csv.DictReader(StringIO(resource.read_text(encoding="utf-8-sig")))
    required_columns = {
        "Circuit",
        "Green-flag pit loss (s)",
        "Estimated VSC pit loss (s)",
        "Estimated SC pit loss (s)",
        "Notes",
    }
    if reader.fieldnames is None or not required_columns.issubset(reader.fieldnames):
        raise ValueError("El CSV de pérdidas en boxes no contiene las columnas requeridas")
    results: dict[str, _CircuitPitLoss] = {}
    for row in reader:
        circuit = row["Circuit"]
        if circuit is None or not circuit.strip():
            raise ValueError("El CSV de pérdidas en boxes contiene un circuito vacío")
        key = _normalize_circuit_name(circuit)
        if key in results:
            raise ValueError(f"Circuito duplicado en el CSV de pérdidas en boxes: {circuit!r}")
        notes = row["Notes"] or ""
        uncertainty = re.search(r"SC model uncertainty approx\. ±(\d+(?:\.\d+)?) s", notes)
        results[key] = _CircuitPitLoss(
            green_flag_s=_csv_number(
                row["Green-flag pit loss (s)"],
                circuit=circuit,
                column="Green-flag pit loss (s)",
            ),
            vsc_s=_csv_number(
                row["Estimated VSC pit loss (s)"],
                circuit=circuit,
                column="Estimated VSC pit loss (s)",
            ),
            sc_s=_csv_number(
                row["Estimated SC pit loss (s)"],
                circuit=circuit,
                column="Estimated SC pit loss (s)",
            ),
            sc_uncertainty_s=(None if uncertainty is None else float(uncertainty.group(1))),
            green_flag_is_official="pit stop time loss as n/a" not in notes.casefold(),
        )
    return results


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
    position_range: tuple[int, int] | None = None

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
        if self.position_range is not None:
            low, high = self.position_range
            if low < 1 or high < low:
                raise ValueError("position_range requiere posiciones positivas y low <= high")
            object.__setattr__(self, "position_range", (low, high))
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
            "position_range": (None if self.position_range is None else list(self.position_range)),
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


def _loss_samples(inputs: MetricInputs) -> tuple[tuple[float, ...], int]:
    """Muestras válidas de pérdida y cuántas se descartaron por ser negativas.

    Una muestra negativa indica que las vueltas de referencia no son
    comparables (por ejemplo, tras un coche de seguridad); se descarta en lugar
    de convertirla en cero o dejar que rompa la validación del resultado.
    """
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
    valid = tuple(sample for sample in losses if sample >= 0.0)
    return valid, len(losses) - len(valid)


def _robust_dispersion(samples: tuple[float, ...]) -> float | None:
    """Desviación absoluta mediana escalada; ``None`` con menos de dos muestras."""
    if len(samples) < 2:
        return None
    center = median(samples)
    return _MAD_FACTOR * median([abs(sample - center) for sample in samples])


def _field_loss(inputs: MetricInputs, cfg: StrategyConfig, stale: bool) -> PitLossEstimate | None:
    samples, discarded = _loss_samples(inputs)
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
    if discarded:
        notes = (*notes, NOTE_NEGATIVE_SAMPLES_DISCARDED)
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
    if entry.value is None or entry.value < 0:
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
    # Solo una vuelta que ya ha salido del carril acumula el tiempo completo:
    # la de entrada se cierra con el tramo recorrido y daría una pérdida parcial.
    records = tuple(
        record
        for record in inputs.history.records_for(inputs.car_index)
        if record.closed and record.pit_exit and record.pit_lane_time_s is not None
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


def _circuit_loss(inputs: MetricInputs, cfg: StrategyConfig, stale: bool) -> PitLossEstimate | None:
    track = inputs.state.session.track
    if track is None:
        return None
    circuit_key = _TRACK_CIRCUIT_ALIASES.get(_normalize_circuit_name(track))
    if circuit_key is None:
        return None
    circuit = _circuit_pit_losses().get(circuit_key)
    if circuit is None:
        return None

    safety_car_status = inputs.state.session.safety_car_status
    if safety_car_status == "no_safety_car":
        value_s = circuit.green_flag_s
        band_s = cfg.pit_loss_band_s
        notes = [NOTE_CIRCUIT_PRIOR, NOTE_FIXED_BAND]
        if not circuit.green_flag_is_official:
            notes.append(NOTE_CIRCUIT_GREEN_NOT_OFFICIAL)
    elif safety_car_status == "virtual_safety_car":
        value_s = circuit.vsc_s
        band_s = cfg.pit_loss_band_s
        notes = [NOTE_CIRCUIT_PRIOR, NOTE_CIRCUIT_VSC_ESTIMATE, NOTE_FIXED_BAND]
    elif safety_car_status == "full_safety_car":
        value_s = circuit.sc_s
        band_s = circuit.sc_uncertainty_s
        notes = [NOTE_CIRCUIT_PRIOR, NOTE_CIRCUIT_SC_ESTIMATE]
        if band_s is not None:
            notes.append(NOTE_CIRCUIT_SC_UNCERTAINTY)
        else:
            band_s = cfg.pit_loss_band_s
            notes.append(NOTE_FIXED_BAND)
    else:
        return None
    if value_s is None:
        return None
    return PitLossEstimate(
        value_s=value_s,
        band_s=band_s,
        dispersion_s=None,
        source="circuit",
        sample_count=0,
        quality=Quality.STALE if stale else Quality.LOW,
        notes=tuple(notes),
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
        _circuit_loss(inputs, cfg, stale),
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
    *,
    pit_loss_band_s: float = 0.0,
) -> RejoinEstimate:
    """Calcula la posición esperada al salir de boxes y el tráfico inmediato.

    Se supone que los rivales no paran: solo se desplaza al piloto. Los coches
    retirados, inactivos o sin intervalo conocido no cuentan; los doblados se
    ignoran y se declara en las notas.
    """
    _check_finite(pit_loss_s, "pit_loss_s", minimum=0.0)
    _check_finite(pit_loss_band_s, "pit_loss_band_s", minimum=0.0)
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
    position_range: tuple[int, int] | None = None
    if pit_loss_band_s > 0.0:
        lower_projected = max(0.0, projected - pit_loss_band_s)
        upper_projected = projected + pit_loss_band_s
        lower_position = 1 + sum(1 for gap, _ in rivals if gap < lower_projected)
        upper_position = 1 + sum(1 for gap, _ in rivals if gap < upper_projected)
        position_range = (lower_position, upper_position)
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

    if missing_gap:
        quality = Quality.LOW
    elif stale:
        quality = Quality.STALE
    elif player.position is not None and len(rivals) >= 3:
        quality = Quality.OK
    else:
        quality = Quality.LOW

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
        position_range=position_range,
    )


__all__ = [
    "NOTE_LANE_TIME_ONLY",
    "NOTE_LAPPED_CARS_IGNORED",
    "NOTE_LEADER_GAP_UNAVAILABLE",
    "NOTE_NO_PIT_LOSS",
    "NOTE_OVERRIDE",
    "NOTE_PLAYER_UNAVAILABLE",
    "NOTE_NEGATIVE_SAMPLES_DISCARDED",
    "NOTE_PROFILE_PRIOR",
    "NOTE_RIVALS_DO_NOT_PIT",
    "NOTE_RIVAL_GAP_UNAVAILABLE",
    "NOTE_CIRCUIT_PRIOR",
    "NOTE_CIRCUIT_GREEN_NOT_OFFICIAL",
    "NOTE_CIRCUIT_VSC_ESTIMATE",
    "NOTE_CIRCUIT_SC_ESTIMATE",
    "NOTE_CIRCUIT_SC_UNCERTAINTY",
    "PIT_LOSS_SOURCES",
    "PitLossEstimate",
    "PitLossSource",
    "RejoinEstimate",
    "estimate_pit_loss",
    "estimate_rejoin",
]
