"""Proveedor del perfil del fin de semana: del almacén al perfil del motor.

Este módulo une las dos piezas puras de la fase: el almacén, que sabe leer los
resúmenes de tanda y el inventario de neumáticos, y :mod:`weekend_profile`, que
los agrega. Al empezar una carrera se localiza el fin de semana de la sesión
(por los identificadores del juego o por el enlace manual), se leen las sesiones
que lo componen y se construye el perfil que el motor usará como referencia.

La lectura nunca interrumpe la captura: cualquier fallo del almacén se devuelve
como un resultado sin perfil y con el error descrito, para que quien captura lo
registre y siga adelante. Un fin de semana sin enlazar tampoco es un error: no
hay perfil y el sistema continúa con los valores por defecto.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from .config import MetricsConfig
from .metrics.stint_summary import StintSummary
from .store import SessionInfo, TyreInventorySnapshot, WeekendInfo
from .weekend_profile import WeekendProfile, WeekendSession, build_weekend_profile

__all__ = [
    "WeekendProfileResult",
    "WeekendReader",
    "build_profile_for_weekend",
    "locate_weekend",
    "registered_profile",
    "resolve_weekend_profile",
]


class WeekendReader(Protocol):
    """Parte del almacén que necesita el proveedor para leer un fin de semana."""

    def get_weekend(self, weekend_id: int) -> WeekendInfo | None: ...

    def list_weekends(self) -> list[WeekendInfo]: ...

    def find_weekend(
        self,
        *,
        weekend_link_id: int,
        season_link_id: int | None = None,
    ) -> WeekendInfo | None: ...

    def list_weekend_sessions(self, weekend_id: int) -> list[SessionInfo]: ...

    def iter_stint_summaries(self, session_id: int) -> Iterator[StintSummary]: ...

    def iter_tyre_inventory(self, session_id: int) -> Iterable[TyreInventorySnapshot]: ...


@dataclass(frozen=True, slots=True)
class WeekendProfileResult:
    """Resultado de resolver el perfil: fin de semana, perfil y error de lectura.

    ``weekend_id`` es ``None`` cuando la sesión no está enlazada a ningún fin de
    semana. ``profile`` es ``None`` si no se pudo construir (sin datos o por un
    fallo de lectura) y en ese caso ``error`` describe el problema. Un error de
    lectura no invalida el fin de semana encontrado, solo el perfil.
    """

    weekend_id: int | None = None
    profile: WeekendProfile | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        """Indica si hay un perfil utilizable."""
        return self.profile is not None


def locate_weekend(store: WeekendReader, session: SessionInfo) -> WeekendInfo | None:
    """Localiza el fin de semana de una sesión.

    Primero usa el enlace ya guardado en la sesión, después los identificadores
    del juego y, como último recurso, un fin de semana manual del mismo circuito
    solo si es el único, para no mezclar fines de semana distintos. Devuelve
    ``None`` si no hay un fin de semana inequívoco.
    """
    if session.weekend_id is not None:
        weekend = store.get_weekend(session.weekend_id)
        if weekend is not None:
            return weekend
    if session.weekend_link_id is not None:
        weekend = store.find_weekend(
            weekend_link_id=session.weekend_link_id,
            season_link_id=session.season_link_id,
        )
        if weekend is not None:
            return weekend
    if session.track is None:
        return None
    candidates = [weekend for weekend in store.list_weekends() if weekend.track == session.track]
    return candidates[0] if len(candidates) == 1 else None


def build_profile_for_weekend(
    store: WeekendReader,
    weekend_id: int,
    cfg: MetricsConfig,
    *,
    exclude_session_id: int | None = None,
) -> WeekendProfile:
    """Construye el perfil de un fin de semana leyendo sus sesiones del almacén.

    Excluye la sesión en curso si se indica, para que la carrera que empieza no
    se use a sí misma como referencia. Cada sesión aporta sus resúmenes de tanda
    y las instantáneas del inventario de neumáticos; la pérdida en boxes no
    existe en los resúmenes, así que el perfil la deja no disponible.
    """
    excluded = exclude_session_id
    weekend_sessions: list[WeekendSession] = []
    for stored in store.list_weekend_sessions(weekend_id):
        if stored.id is None or stored.id == excluded:
            continue
        weekend_sessions.append(
            WeekendSession(
                session_id=stored.id,
                session_type=stored.session_type,
                stint_summaries=tuple(store.iter_stint_summaries(stored.id)),
                tyre_inventory=tuple(store.iter_tyre_inventory(stored.id)),
            )
        )
    return build_weekend_profile(weekend_sessions, cfg)


def resolve_weekend_profile(
    store: WeekendReader,
    session: SessionInfo,
    cfg: MetricsConfig,
    *,
    exclude_session_id: int | None = None,
) -> WeekendProfileResult:
    """Resuelve el perfil de la sesión sin lanzar nunca una excepción.

    Localiza el fin de semana y, si existe, construye su perfil. Cualquier fallo
    del almacén se captura y se devuelve en ``error``: la captura en vivo no debe
    detenerse porque no se pueda leer el perfil.
    """
    try:
        weekend = locate_weekend(store, session)
        if weekend is None:
            return WeekendProfileResult()
        profile = build_profile_for_weekend(
            store,
            weekend.id,
            cfg,
            exclude_session_id=exclude_session_id,
        )
    except Exception as exc:  # noqa: BLE001 - la captura nunca se detiene por el perfil
        return WeekendProfileResult(error=str(exc) or type(exc).__name__)
    return WeekendProfileResult(weekend_id=weekend.id, profile=profile)


def registered_profile(session: SessionInfo) -> tuple[str, str] | None:
    """Devuelve el perfil registrado de una sesión, si lo tiene.

    El par ``(profile_json, profile_hash)`` es el perfil exacto con el que arrancó
    la carrera; una reproducción debe reutilizarlo en lugar de recalcularlo.
    """
    if session.profile_json is None or session.profile_hash is None:
        return None
    return session.profile_json, session.profile_hash
