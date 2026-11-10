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
from .metrics.types import Quality
from .store import MetricSample, SessionInfo, TyreInventorySnapshot, WeekendInfo
from .weekend_profile import (
    WeekendProfile,
    WeekendSession,
    build_weekend_profile,
    profile_from_json,
)

__all__ = [
    "WeekendProfileResult",
    "WeekendReader",
    "build_profile_for_weekend",
    "locate_weekend",
    "profiles_for_capture",
    "registered_profile",
    "resolve_weekend_profile",
]


class WeekendReader(Protocol):
    """Parte del almacén que necesita el proveedor para leer un fin de semana."""

    def get_weekend(self, weekend_id: int) -> WeekendInfo | None: ...

    def list_weekends(self) -> list[WeekendInfo]: ...

    def list_sessions(self) -> list[SessionInfo]: ...

    def find_weekend(
        self,
        *,
        weekend_link_id: int,
        season_link_id: int | None = None,
    ) -> WeekendInfo | None: ...

    def list_weekend_sessions(self, weekend_id: int) -> list[SessionInfo]: ...

    def iter_stint_summaries(self, session_id: int) -> Iterator[StintSummary]: ...

    def iter_tyre_inventory(self, session_id: int) -> Iterable[TyreInventorySnapshot]: ...

    def iter_metric_samples(
        self, session_id: int, *, key: str | None = None, lap: int | None = None
    ) -> Iterator[MetricSample]: ...


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

    Solo usa un enlace ya guardado en la sesión o los identificadores del juego.
    El circuito por sí solo no confirma que dos sesiones pertenezcan al mismo
    fin de semana.
    """
    if session.weekend_id is not None:
        return store.get_weekend(session.weekend_id)
    if session.weekend_link_id is not None:
        if session.weekend_link_id <= 0:
            return None
        weekend = store.find_weekend(
            weekend_link_id=session.weekend_link_id,
            season_link_id=session.season_link_id,
        )
        return weekend
    return None


def build_profile_for_weekend(
    store: WeekendReader,
    weekend_id: int,
    cfg: MetricsConfig,
    *,
    exclude_session_id: int | None = None,
    exclude_session_uid: int | None = None,
    inventory_override: tuple[TyreInventorySnapshot, ...] | None = None,
) -> WeekendProfile:
    """Construye el perfil de un fin de semana leyendo sus sesiones del almacén.

    Excluye la sesión en curso si se indica, para que la carrera que empieza no
    se use a sí misma como referencia. Cada sesión aporta sus resúmenes de tanda
    y las instantáneas del inventario de neumáticos. La pérdida en boxes usa la
    última muestra válida de cada sesión para no duplicar métricas retenidas.
    """
    excluded = exclude_session_id
    weekend_sessions: list[WeekendSession] = []
    for stored in store.list_weekend_sessions(weekend_id):
        if stored.id is None or stored.id == excluded or stored.session_uid == exclude_session_uid:
            continue
        pit_sample = next(
            (
                sample
                for sample in reversed(
                    tuple(store.iter_metric_samples(stored.id, key="pit.loss_estimate_s"))
                )
                if sample.value_num is not None
                and sample.sample_count > 0
                and sample.quality in {Quality.OK.value, Quality.LOW.value}
            ),
            None,
        )
        if pit_sample is None or pit_sample.value_num is None:
            pit_losses: tuple[float, ...] = ()
            pit_loss_sample_counts: tuple[int, ...] = ()
            pit_loss_qualities: tuple[Quality, ...] = ()
        else:
            pit_losses = (pit_sample.value_num,)
            pit_loss_sample_counts = (pit_sample.sample_count,)
            pit_loss_qualities = (Quality(pit_sample.quality),)
        weekend_sessions.append(
            WeekendSession(
                session_id=stored.id,
                session_type=stored.session_type,
                stint_summaries=tuple(store.iter_stint_summaries(stored.id)),
                tyre_inventory=tuple(store.iter_tyre_inventory(stored.id)),
                pit_losses_s=pit_losses,
                pit_loss_sample_counts=pit_loss_sample_counts,
                pit_loss_qualities=pit_loss_qualities,
            )
        )
    return build_weekend_profile(
        weekend_sessions,
        cfg,
        inventory_override=inventory_override,
    )


def resolve_weekend_profile(
    store: WeekendReader,
    session: SessionInfo,
    cfg: MetricsConfig,
    *,
    exclude_session_id: int | None = None,
    exclude_session_uid: int | None = None,
    inventory_override: tuple[TyreInventorySnapshot, ...] | None = None,
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
            exclude_session_uid=exclude_session_uid,
            inventory_override=inventory_override,
        )
    except Exception as exc:  # noqa: BLE001 - la captura nunca se detiene por el perfil
        return WeekendProfileResult(error=str(exc) or type(exc).__name__)
    if not profile.sessions:
        return WeekendProfileResult(weekend_id=weekend.id)
    return WeekendProfileResult(weekend_id=weekend.id, profile=profile)


def registered_profile(session: SessionInfo) -> tuple[str, str] | None:
    """Devuelve el perfil registrado de una sesión, si lo tiene.

    El par ``(profile_json, profile_hash)`` es el perfil exacto con el que arrancó
    la carrera; una reproducción debe reutilizarlo en lugar de recalcularlo.
    """
    if session.profile_json is None or session.profile_hash is None:
        return None
    return session.profile_json, session.profile_hash


def profiles_for_capture(
    store: WeekendReader,
    capture_sha256: str | None,
) -> dict[int, WeekendProfile]:
    """Carga los perfiles registrados por UID de sesión de una captura."""
    if capture_sha256 is None:
        return {}
    profiles: dict[int, WeekendProfile] = {}
    for session in store.list_sessions():
        if (
            session.capture_sha256 != capture_sha256
            or session.profile_json is None
            or session.profile_hash is None
        ):
            continue
        profile = profile_from_json(session.profile_json)
        if profile.hash != session.profile_hash:
            raise ValueError(
                f"La huella del perfil registrado para la sesión {session.session_uid} "
                "no coincide con su contenido"
            )
        previous = profiles.get(session.session_uid)
        if previous is not None and previous.hash != profile.hash:
            raise ValueError(
                f"Hay perfiles registrados distintos para la sesión {session.session_uid}"
            )
        profiles[session.session_uid] = profile
    return profiles
