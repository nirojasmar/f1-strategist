"""Métricas del perfil del fin de semana.

Estas métricas son un **prior**: describen lo que se espera del fin de semana
antes de que la carrera en vivo aporte datos propios. Por eso su calidad nunca
supera ``low`` y llevan siempre la nota ``profile_prior``: quien las consuma
debe saber que se sustituyen o mezclan con lo medido en la sesión actual.

Sin perfil (sin sesiones previas enlazadas) todas las métricas quedan
``unavailable`` y el motor sigue funcionando con normalidad.
"""

from __future__ import annotations

from ..race_state import CarSnapshot
from ..weekend_profile import CompoundProfile, ProfileValue, WeekendProfile
from .history import MetricInputs
from .types import Metric, Quality, Window, unavailable

#: Nota común de todas las métricas: son un prior, no una medición.
PRIOR_NOTE = "profile_prior"

#: Confianza base según la calidad del perfil. Nunca llega a 1: es un prior.
_QUALITY_CONFIDENCE: dict[Quality, float] = {
    Quality.OK: 0.8,
    Quality.LOW: 0.55,
    Quality.STALE: 0.4,
    Quality.INSUFFICIENT: 0.2,
    Quality.UNAVAILABLE: 0.0,
}

#: Tope de confianza: un prior no puede presentarse como certeza.
_CONFIDENCE_MAX = 0.9

#: Fuente declarada por todas las métricas del módulo.
_SOURCES = ("profile",)


class ProfileMetrics:
    """Publica el perfil del fin de semana como métricas ``profile.*``."""

    name = "profile"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        profile = inputs.profile
        now = inputs.now
        if profile is None:
            return tuple(
                unavailable(key, unit, "profile_unavailable", now, sources=_SOURCES)
                for key, unit in _UNAVAILABLE_KEYS
            )
        compound = _compound_for(profile, inputs.car)
        return (
            _compound_metric(
                "profile.wear_rate_pct_per_lap",
                "%/lap",
                compound,
                "wear_rate_pct_per_lap",
                now,
            ),
            _compound_metric(
                "profile.pace_mean_s",
                "s",
                compound,
                "pace_mean_s",
                now,
            ),
            _compound_metric(
                "profile.pace_delta_vs_reference_s",
                "s",
                compound,
                "pace_delta_vs_reference_s",
                now,
            ),
            _compound_metric(
                "profile.fuel_consumption_kg_per_lap",
                "kg/lap",
                compound,
                "fuel_consumption_kg_per_lap",
                now,
            ),
            _profile_metric(
                "profile.pit_loss_s",
                "s",
                profile.pit_loss_s,
                now,
                "profile_pit_loss_unavailable",
            ),
            _sets_available(profile, now),
            _confidence(profile, now),
        )


def _compound_for(
    profile: WeekendProfile,
    car: CarSnapshot | None,
) -> CompoundProfile | None:
    """Localiza el compuesto montado y, si no lo hay, el de referencia."""
    if car is not None:
        actual = car.actual_tyre_compound
        if actual is None and car.fitted_tyre is not None:
            actual = car.fitted_tyre.actual_tyre_compound
        if actual is not None:
            found = profile.compound(actual)
            if found is not None:
                return found
        visual = car.visual_tyre_compound
        if visual is None and car.fitted_tyre is not None:
            visual = car.fitted_tyre.visual_tyre_compound
        if visual is not None:
            for entry in profile.compounds:
                if visual in entry.visual_compounds:
                    return entry
    if profile.reference_compound is not None:
        return profile.compound(profile.reference_compound)
    return None


def _compound_metric(
    key: str,
    unit: str,
    compound: CompoundProfile | None,
    attribute: str,
    now: float,
) -> Metric:
    if compound is None:
        return unavailable(key, unit, "profile_compound_unavailable", now, sources=_SOURCES)
    value: ProfileValue = getattr(compound, attribute)
    return _profile_metric(
        key,
        unit,
        value,
        now,
        f"{attribute}_unavailable",
        notes=(f"compound:{compound.compound}",),
    )


def _profile_metric(
    key: str,
    unit: str,
    value: ProfileValue,
    now: float,
    reason_key: str,
    *,
    notes: tuple[str, ...] = (),
) -> Metric:
    if value.value is None:
        return unavailable(key, unit, reason_key, now, sources=_SOURCES)
    return Metric(
        key=key,
        value=value.value,
        unit=unit,
        window=Window("instant"),
        quality=Quality.LOW,
        sample_count=max(1, value.samples),
        computed_at_monotonic=now,
        sources=_SOURCES,
        notes=(PRIOR_NOTE, *notes),
    )


def _sets_available(profile: WeekendProfile, now: float) -> Metric:
    key = "profile.sets_available"
    inventory = profile.inventory
    if not inventory:
        return unavailable(key, "sets", "profile_inventory_empty", now, sources=_SOURCES)
    known = [snapshot.available for snapshot in inventory if snapshot.available is not None]
    if not known:
        return unavailable(
            key,
            "sets",
            "profile_inventory_unknown",
            now,
            sample_count=len(inventory),
            sources=_SOURCES,
        )
    return Metric(
        key=key,
        value=sum(1 for available in known if available),
        unit="sets",
        window=Window("instant"),
        quality=Quality.LOW,
        sample_count=len(known),
        computed_at_monotonic=now,
        sources=_SOURCES,
        notes=(PRIOR_NOTE,),
    )


def _confidence(profile: WeekendProfile, now: float) -> Metric:
    key = "profile.confidence"
    base = _QUALITY_CONFIDENCE[profile.quality]
    if base <= 0.0:
        return unavailable(key, "", "profile_confidence_unavailable", now, sources=_SOURCES)
    # Más sesiones en el fin de semana dan algo más de confianza, sin llegar nunca
    # a la certeza: la muestra sigue siendo de sesiones anteriores a esta carrera.
    factor = min(1.0, 0.6 + 0.2 * min(len(profile.sessions), 3))
    value = round(min(_CONFIDENCE_MAX, base * factor), 2)
    return Metric(
        key=key,
        value=value,
        unit="",
        window=Window("instant"),
        quality=Quality.LOW,
        sample_count=max(1, len(profile.sessions)),
        computed_at_monotonic=now,
        sources=_SOURCES,
        notes=(PRIOR_NOTE, f"quality:{profile.quality.value}"),
    )


#: Claves que se declaran no disponibles cuando no hay perfil.
_UNAVAILABLE_KEYS: tuple[tuple[str, str], ...] = (
    ("profile.wear_rate_pct_per_lap", "%/lap"),
    ("profile.pace_mean_s", "s"),
    ("profile.pace_delta_vs_reference_s", "s"),
    ("profile.fuel_consumption_kg_per_lap", "kg/lap"),
    ("profile.pit_loss_s", "s"),
    ("profile.sets_available", "sets"),
    ("profile.confidence", ""),
)

__all__ = ["PRIOR_NOTE", "ProfileMetrics"]
