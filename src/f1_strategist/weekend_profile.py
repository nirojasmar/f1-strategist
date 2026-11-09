"""Perfil del fin de semana: agregados por compuesto y mezcla con el prior.

Este módulo es puro: recibe en memoria los resúmenes de tanda y las
instantáneas del inventario de neumáticos ya leídos del almacén, y devuelve un
perfil inmutable. No abre SQLite ni conoce la sesión en vivo; esa lectura
corresponde al proveedor del fin de semana y la publicación de métricas a la
fase de perfil.

Cada valor agregado conserva su procedencia (sesiones que lo aportan), el
número de muestras y una calidad, de modo que quien lo consuma pueda decidir si
lo usa tal cual, lo mezcla con un prior o lo declara no disponible. Los valores
se combinan con una mediana ponderada por vueltas limpias: es robusta frente a
tandas anómalas y no depende del orden en que lleguen las sesiones.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .config import MetricsConfig
from .metrics.stint_summary import StintSummary
from .metrics.types import Quality
from .store import TyreInventorySnapshot

#: Versión del documento JSON que produce :func:`profile_to_dict`.
PROFILE_SCHEMA_VERSION = 1

#: Fuerza del prior propuesta para la mezcla (decisión abierta B2).
DEFAULT_PRIOR_STRENGTH = 3.0

#: Firmeza relativa de cada compuesto; mayor significa más blando. Los
#: compuestos de lluvia quedan en cero y nunca se eligen como referencia de
#: seco. Los fixtures del juego mezclan nombres reales (``c3``) y visuales
#: (``soft``), así que se aceptan ambos vocabularios.
_COMPOUND_SOFTNESS: dict[str, int] = {
    "c0": 1,
    "c1": 2,
    "c2": 3,
    "c3": 4,
    "c4": 5,
    "c5": 6,
    "c6": 7,
    "hard": 2,
    "medium": 4,
    "soft": 6,
    "supersoft": 7,
    "ultrasoft": 8,
    "hypersoft": 9,
    "intermediate": 0,
    "wet": 0,
}

#: Muestras a partir de las cuales una pérdida en boxes se considera firme.
_PIT_LOSS_OK_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class ProfileValue:
    """Valor agregado con su procedencia, su número de muestras y su calidad.

    ``UNAVAILABLE`` exige ``value=None``: si no hay datos no se inventa un
    número. ``INSUFFICIENT`` sí puede llevar un valor parcial, porque un perfil
    incompleto es preferible a un hueco siempre que su calidad lo delate.
    """

    value: float | None
    samples: int = 0
    quality: Quality = Quality.UNAVAILABLE
    sessions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.samples < 0:
            raise ValueError("samples no puede ser negativo")
        if self.quality is Quality.UNAVAILABLE and self.value is not None:
            raise ValueError("Un valor no disponible no puede tener número")
        if self.quality in {Quality.OK, Quality.LOW, Quality.STALE} and self.value is None:
            raise ValueError(f"Un valor {self.quality.value!r} necesita un número")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("El valor del perfil debe ser finito")
        object.__setattr__(self, "sessions", tuple(sorted(set(self.sessions))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "samples": self.samples,
            "quality": self.quality.value,
            "sessions": list(self.sessions),
        }


@dataclass(frozen=True, slots=True)
class CompoundProfile:
    """Agregado de un compuesto real en el fin de semana."""

    compound: str
    visual_compounds: tuple[str, ...]
    stints: int
    representative_stints: int
    laps: int
    clean_laps: int
    wear_rate_pct_per_lap: ProfileValue
    pace_mean_s: ProfileValue
    pace_delta_vs_reference_s: ProfileValue
    fuel_consumption_kg_per_lap: ProfileValue
    pit_loss_s: ProfileValue
    track_temperature_c: ProfileValue
    fuel_range_kg: tuple[float, float] | None
    weather: tuple[str, ...]
    sessions: tuple[int, ...]
    quality: Quality

    def __post_init__(self) -> None:
        if not self.compound:
            raise ValueError("El compuesto no puede estar vacío")
        if self.stints < 0 or self.representative_stints < 0:
            raise ValueError("Los contadores de tandas no pueden ser negativos")
        if not 0 <= self.representative_stints <= self.stints:
            raise ValueError("Las tandas representativas no pueden superar el total")
        if self.laps < 0 or not 0 <= self.clean_laps <= self.laps:
            raise ValueError("Las vueltas limpias deben estar entre 0 y las vueltas totales")
        object.__setattr__(self, "visual_compounds", tuple(self.visual_compounds))
        object.__setattr__(self, "weather", tuple(self.weather))
        object.__setattr__(self, "sessions", tuple(sorted(set(self.sessions))))
        if self.fuel_range_kg is not None:
            low, high = self.fuel_range_kg
            if low > high:
                raise ValueError("El rango de combustible debe ir de menor a mayor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "compound": self.compound,
            "visual_compounds": list(self.visual_compounds),
            "stints": self.stints,
            "representative_stints": self.representative_stints,
            "laps": self.laps,
            "clean_laps": self.clean_laps,
            "wear_rate_pct_per_lap": self.wear_rate_pct_per_lap.to_dict(),
            "pace_mean_s": self.pace_mean_s.to_dict(),
            "pace_delta_vs_reference_s": self.pace_delta_vs_reference_s.to_dict(),
            "fuel_consumption_kg_per_lap": self.fuel_consumption_kg_per_lap.to_dict(),
            "pit_loss_s": self.pit_loss_s.to_dict(),
            "track_temperature_c": self.track_temperature_c.to_dict(),
            "fuel_range_kg": (list(self.fuel_range_kg) if self.fuel_range_kg is not None else None),
            "weather": list(self.weather),
            "sessions": list(self.sessions),
            "quality": self.quality.value,
        }


@dataclass(frozen=True, slots=True)
class WeekendProfile:
    """Perfil inmutable de un fin de semana, con esquema versionado.

    Un perfil sin datos es válido y queda en ``UNAVAILABLE``: construir el
    perfil nunca falla por falta de sesiones. La lista de compuestos y el
    inventario se normalizan por orden, de modo que el hash no dependa del
    orden en que lleguen las entradas.
    """

    schema_version: int = PROFILE_SCHEMA_VERSION
    compounds: tuple[CompoundProfile, ...] = ()
    reference_compound: str | None = None
    pit_loss_s: ProfileValue = ProfileValue(None)
    inventory: tuple[TyreInventorySnapshot, ...] = ()
    sessions: tuple[int, ...] = ()
    quality: Quality = Quality.UNAVAILABLE

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("schema_version debe ser mayor o igual que 1")
        object.__setattr__(
            self,
            "compounds",
            tuple(sorted(self.compounds, key=lambda entry: entry.compound)),
        )
        object.__setattr__(
            self,
            "inventory",
            tuple(sorted(self.inventory, key=lambda entry: (entry.kind, entry.set_index))),
        )
        object.__setattr__(self, "sessions", tuple(sorted(set(self.sessions))))

    def compound(self, name: str) -> CompoundProfile | None:
        """Devuelve el agregado del compuesto real indicado, si existe."""
        for entry in self.compounds:
            if entry.compound == name:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return profile_to_dict(self)

    def to_json(self) -> str:
        return profile_to_json(self)

    @property
    def hash(self) -> str:
        return profile_hash(self)


@dataclass(frozen=True, slots=True)
class WeekendSession:
    """Datos de una sesión del fin de semana, ya leídos del almacén.

    ``pit_losses_s`` es opcional: los resúmenes de tanda no miden la parada, así
    que la pérdida en boxes solo existe si el proveedor la aporta. Sin ella el
    perfil la deja no disponible en lugar de estimarla.
    """

    session_id: int
    session_type: str | None = None
    stint_summaries: tuple[StintSummary, ...] = ()
    tyre_inventory: tuple[TyreInventorySnapshot, ...] = ()
    pit_losses_s: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.session_id < 1:
            raise ValueError("session_id debe ser mayor o igual que 1")
        object.__setattr__(self, "stint_summaries", tuple(self.stint_summaries))
        object.__setattr__(self, "tyre_inventory", tuple(self.tyre_inventory))
        object.__setattr__(self, "pit_losses_s", tuple(self.pit_losses_s))


def build_weekend_profile(
    sessions: Iterable[WeekendSession],
    cfg: MetricsConfig,
    *,
    reference_compound: str | None = None,
) -> WeekendProfile:
    """Agrega las sesiones de un fin de semana en un perfil inmutable.

    El perfil agrupa por compuesto real y usa solo tandas representativas (las
    que alcanzan ``cfg.min_representative_laps`` vueltas limpias) cuando las hay.
    Si no hay ninguna representativa, devuelve un perfil parcial marcado con
    calidad baja o insuficiente, nunca un valor inventado.

    ``reference_compound`` fija el compuesto de referencia del ritmo relativo
    (decisión abierta B6). Sin él se elige el más blando con tandas
    representativas; si el compuesto indicado no aparece en el fin de semana se
    aplica la misma elección automática.
    """
    ordered = tuple(sorted(sessions, key=lambda session: session.session_id))
    grouped: dict[str, list[tuple[int, StintSummary]]] = {}
    for session in ordered:
        for summary in session.stint_summaries:
            compound = summary.compound
            if compound is None:
                continue
            grouped.setdefault(compound, []).append((session.session_id, summary))

    pit_loss = _aggregate_pit_loss(ordered)
    compounds = tuple(
        _build_compound(compound, entries, pit_loss)
        for compound, entries in sorted(grouped.items())
    )
    reference = _select_reference(compounds, reference_compound)
    reference_pace = None if reference is None else _compound_pace(compounds, reference)
    if reference is not None:
        compounds = tuple(
            replace(
                entry,
                pace_delta_vs_reference_s=_pace_delta(entry, reference, reference_pace),
            )
            for entry in compounds
        )

    return WeekendProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        compounds=compounds,
        reference_compound=reference,
        pit_loss_s=pit_loss,
        inventory=_select_inventory(ordered),
        sessions=tuple(session.session_id for session in ordered),
        quality=_profile_quality(compounds),
    )


def blend_with_prior(
    prior: float | None,
    race_estimate: float | None,
    n_race: int,
    k: float = DEFAULT_PRIOR_STRENGTH,
) -> float | None:
    """Mezcla un prior con la estimación de la carrera: ``(k·prior + n·carrera)/(k+n)``.

    ``n_race`` son las vueltas propias válidas y ``k`` la fuerza del prior
    (propuesta 3, decisión abierta B2). Sin vueltas de carrera devuelve el
    prior; sin prior devuelve la estimación de la carrera. Aquí solo se ofrece
    la función: la fase de perfil decide cuándo usarla.
    """
    if n_race < 0:
        raise ValueError("n_race no puede ser negativo")
    if not math.isfinite(k) or k < 0:
        raise ValueError("k debe ser finito y no negativo")
    if prior is None:
        return race_estimate
    if race_estimate is None or n_race == 0:
        return prior
    if k == 0:
        return race_estimate
    return (k * prior + n_race * race_estimate) / (k + n_race)


def profile_to_dict(profile: WeekendProfile) -> dict[str, Any]:
    """Convierte el perfil en un documento JSON canónico y ordenado."""
    return {
        "schema_version": profile.schema_version,
        "quality": profile.quality.value,
        "reference_compound": profile.reference_compound,
        "sessions": list(profile.sessions),
        "pit_loss_s": profile.pit_loss_s.to_dict(),
        "compounds": [entry.to_dict() for entry in profile.compounds],
        "inventory": [_inventory_to_dict(entry) for entry in profile.inventory],
    }


def profile_to_json(profile: WeekendProfile) -> str:
    """Serializa el perfil con claves ordenadas y sin espacios innecesarios."""
    return json.dumps(
        profile_to_dict(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def profile_hash(profile: WeekendProfile) -> str:
    """Huella SHA-256 del perfil serializado; estable e independiente del orden."""
    return hashlib.sha256(profile_to_json(profile).encode("utf-8")).hexdigest()


def _build_compound(
    compound: str,
    entries: Sequence[tuple[int, StintSummary]],
    pit_loss: ProfileValue,
) -> CompoundProfile:
    representative = [summary for _, summary in entries if summary.representative]
    used = representative or [summary for _, summary in entries]
    representative_count = len(representative)

    wear_rows: list[tuple[float, int, int]] = []
    pace_rows: list[tuple[float, int, int]] = []
    consumption_rows: list[tuple[float, int, int]] = []
    temperature_rows: list[tuple[float, int, int]] = []
    fuel_starts: list[float] = []
    weather: set[str] = set()
    visual: set[str] = set()
    laps = 0
    clean_laps = 0

    for _, summary in entries:
        laps += summary.laps
        clean_laps += summary.clean_laps
        if summary.visual_compound is not None:
            visual.add(summary.visual_compound)
        weather.update(summary.weather)
        if summary.fuel_start_kg is not None:
            fuel_starts.append(summary.fuel_start_kg)

    for session_id, summary in entries:
        if representative and not summary.representative:
            continue
        weight = summary.clean_laps if summary.clean_laps > 0 else max(summary.laps, 1)
        if summary.wear_rate_pct_per_lap is not None:
            wear_rows.append((summary.wear_rate_pct_per_lap, weight, session_id))
        pace = _summary_pace(summary)
        if pace is not None:
            pace_rows.append((pace, weight, session_id))
        consumption = _fuel_consumption(summary)
        if consumption is not None:
            consumption_rows.append((consumption, weight, session_id))
        if summary.track_temperature_mean_c is not None:
            temperature_rows.append((summary.track_temperature_mean_c, weight, session_id))

    sessions = {session_id for session_id, _ in entries}
    fuel_range: tuple[float, float] | None = None
    if fuel_starts:
        fuel_range = (min(fuel_starts), max(fuel_starts))

    return CompoundProfile(
        compound=compound,
        visual_compounds=tuple(sorted(visual)),
        stints=len(entries),
        representative_stints=representative_count,
        laps=laps,
        clean_laps=clean_laps,
        wear_rate_pct_per_lap=_profile_value(wear_rows, representative_count),
        pace_mean_s=_profile_value(pace_rows, representative_count),
        pace_delta_vs_reference_s=ProfileValue(None),
        fuel_consumption_kg_per_lap=_profile_value(consumption_rows, representative_count),
        pit_loss_s=pit_loss,
        track_temperature_c=_profile_value(temperature_rows, representative_count),
        fuel_range_kg=fuel_range,
        weather=tuple(sorted(weather)),
        sessions=tuple(sessions),
        quality=_compound_quality(representative_count, used),
    )


def _summary_pace(summary: StintSummary) -> float | None:
    """Ritmo comparable: el corregido por combustible si existe, si no el bruto."""
    if summary.pace_mean_fuel_corrected_s is not None:
        return summary.pace_mean_fuel_corrected_s
    return summary.pace_mean_s


def _fuel_consumption(summary: StintSummary) -> float | None:
    if summary.fuel_start_kg is None or summary.fuel_end_kg is None or summary.laps <= 0:
        return None
    consumed = summary.fuel_start_kg - summary.fuel_end_kg
    if consumed < 0:
        return None
    return consumed / summary.laps


def _profile_value(
    rows: Sequence[tuple[float, int, int]],
    representative_count: int,
) -> ProfileValue:
    """Construye un valor agregado a partir de ``(muestra, peso, sesión)``."""
    if not rows:
        return ProfileValue(None)
    value = _weighted_median([(sample, weight) for sample, weight, _ in rows])
    if value is None:
        return ProfileValue(None)
    return ProfileValue(
        value=value,
        samples=len(rows),
        quality=_value_quality(len(rows), representative_count),
        sessions=tuple({session_id for _, _, session_id in rows}),
    )


def _weighted_median(samples: Sequence[tuple[float, int]]) -> float | None:
    """Mediana ponderada; con peso total par promedia los dos valores centrales."""
    data = sorted(
        (float(value), int(weight))
        for value, weight in samples
        if weight > 0 and math.isfinite(value)
    )
    if not data:
        return None
    total = sum(weight for _, weight in data)
    if total <= 0:
        return None
    cumulative = 0
    for index, (value, weight) in enumerate(data):
        cumulative += weight
        if cumulative * 2 > total:
            return value
        if cumulative * 2 == total:
            following = data[index + 1][0] if index + 1 < len(data) else value
            return (value + following) / 2.0
    return data[-1][0]


def _aggregate_pit_loss(sessions: Sequence[WeekendSession]) -> ProfileValue:
    rows = [
        (loss, 1, session.session_id)
        for session in sessions
        for loss in session.pit_losses_s
        if math.isfinite(loss)
    ]
    if not rows:
        return ProfileValue(None)
    value = _weighted_median([(sample, weight) for sample, weight, _ in rows])
    if value is None:
        return ProfileValue(None)
    return ProfileValue(
        value=value,
        samples=len(rows),
        quality=(Quality.OK if len(rows) >= _PIT_LOSS_OK_SAMPLES else Quality.LOW),
        sessions=tuple({session_id for _, _, session_id in rows}),
    )


def _select_inventory(
    sessions: Sequence[WeekendSession],
) -> tuple[TyreInventorySnapshot, ...]:
    """Inventario al empezar la carrera; si no lo hay, el último fin de sesión."""
    for kind in ("race_start", "session_end"):
        candidates = [
            session
            for session in sessions
            if any(snapshot.kind == kind for snapshot in session.tyre_inventory)
        ]
        if not candidates:
            continue
        chosen = max(candidates, key=lambda session: session.session_id)
        return tuple(
            sorted(
                (snapshot for snapshot in chosen.tyre_inventory if snapshot.kind == kind),
                key=lambda snapshot: snapshot.set_index,
            )
        )
    return ()


def _select_reference(
    compounds: Sequence[CompoundProfile],
    configured: str | None,
) -> str | None:
    if configured is not None and any(entry.compound == configured for entry in compounds):
        return configured
    candidates = [entry for entry in compounds if entry.representative_stints > 0]
    if not candidates:
        candidates = [entry for entry in compounds if entry.quality is not Quality.UNAVAILABLE]
    dry = [entry for entry in candidates if _COMPOUND_SOFTNESS.get(entry.compound.lower(), 0) > 0]
    if not dry:
        return None
    return min(
        dry,
        key=lambda entry: (
            -_COMPOUND_SOFTNESS.get(entry.compound.lower(), 0),
            entry.compound,
        ),
    ).compound


def _compound_pace(
    compounds: Sequence[CompoundProfile],
    compound: str,
) -> ProfileValue | None:
    for entry in compounds:
        if entry.compound == compound:
            return entry.pace_mean_s
    return None


def _pace_delta(
    entry: CompoundProfile,
    reference: str,
    reference_pace: ProfileValue | None,
) -> ProfileValue:
    if entry.compound == reference:
        pace = entry.pace_mean_s
        if pace.value is None:
            return ProfileValue(None)
        return ProfileValue(
            value=0.0,
            samples=pace.samples,
            quality=pace.quality,
            sessions=pace.sessions,
        )
    if reference_pace is None or reference_pace.value is None or entry.pace_mean_s.value is None:
        return ProfileValue(None)
    combined_quality = _worst_quality(entry.pace_mean_s.quality, reference_pace.quality)
    return ProfileValue(
        value=entry.pace_mean_s.value - reference_pace.value,
        samples=entry.pace_mean_s.samples,
        quality=combined_quality,
        sessions=entry.pace_mean_s.sessions + reference_pace.sessions,
    )


def _value_quality(samples: int, representative_count: int) -> Quality:
    if samples == 0:
        return Quality.UNAVAILABLE
    if representative_count > 0:
        return Quality.OK if samples >= 2 else Quality.LOW
    return Quality.LOW if samples >= 2 else Quality.INSUFFICIENT


def _compound_quality(representative_count: int, used: Sequence[StintSummary]) -> Quality:
    if representative_count >= 2:
        return Quality.OK
    if representative_count == 1:
        return Quality.LOW
    if used:
        return Quality.INSUFFICIENT
    return Quality.UNAVAILABLE


def _profile_quality(compounds: Sequence[CompoundProfile]) -> Quality:
    if not compounds:
        return Quality.UNAVAILABLE
    qualities = {entry.quality for entry in compounds}
    if Quality.OK in qualities:
        return Quality.OK
    if qualities == {Quality.INSUFFICIENT}:
        return Quality.INSUFFICIENT
    return Quality.LOW


def _worst_quality(left: Quality, right: Quality) -> Quality:
    order = {
        Quality.OK: 0,
        Quality.LOW: 1,
        Quality.STALE: 2,
        Quality.INSUFFICIENT: 3,
        Quality.UNAVAILABLE: 4,
    }
    return left if order[left] >= order[right] else right


def _inventory_to_dict(snapshot: TyreInventorySnapshot) -> dict[str, Any]:
    return {
        "kind": snapshot.kind,
        "set_index": snapshot.set_index,
        "actual_tyre_compound": snapshot.actual_tyre_compound,
        "visual_tyre_compound": snapshot.visual_tyre_compound,
        "wear_pct": snapshot.wear_pct,
        "available": snapshot.available,
        "lifespan_laps": snapshot.lifespan_laps,
        "usable_life_laps": snapshot.usable_life_laps,
        "lap_delta_time_ms": snapshot.lap_delta_time_ms,
        "recommended_session": snapshot.recommended_session,
        "fitted": snapshot.fitted,
    }


__all__ = [
    "DEFAULT_PRIOR_STRENGTH",
    "PROFILE_SCHEMA_VERSION",
    "CompoundProfile",
    "ProfileValue",
    "WeekendProfile",
    "WeekendSession",
    "blend_with_prior",
    "build_weekend_profile",
    "profile_hash",
    "profile_to_dict",
    "profile_to_json",
]
