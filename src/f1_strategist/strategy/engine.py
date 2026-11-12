"""Motor de estrategia: orquestación, cadencia y publicación (S7).

El motor es la única pieza con estado del paquete ``strategy``: recuerda la
última evaluación, la revisión publicada y el instante del último cálculo. El
cálculo en sí sigue siendo determinista: las mismas entradas producen la misma
evaluación, y la revisión solo avanza cuando cambia la recomendación.

La cadencia sigue la sección 5.9 del plan: se evalúa al cambiar de vuelta, de
sesión o de compuesto montado, ante eventos relevantes (coche de seguridad,
clima, parada propia) y como máximo una vez por ``evaluation_min_interval_s``.
Nunca por paquete. Un fallo del motor se registra con su causa y no interrumpe
la captura.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace

from ..config import MetricsConfig, StrategyConfig
from ..metrics.engine import MetricsSnapshot
from ..metrics.history import LapHistorySnapshot, MetricInputs
from ..metrics.types import Quality
from ..race_state import CarSnapshot, RaceStateSnapshot
from ..weekend_profile import WeekendProfile
from .decision import decide
from .evaluator import evaluate_alternatives
from .pace_model import (
    CompoundModel,
    CompoundModels,
    build_compound_models,
    canonical_compound,
    project_lap_times,
)
from .pit_model import PitLossEstimate, estimate_pit_loss
from .types import (
    ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED,
    ASSUMPTION_PROFILE_PRIOR_USED,
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    BASELINE_STAY_OUT,
    RISK_DAMAGE_NOT_MODELLED,
    RISK_TRAFFIC_NOT_PRICED,
    RISK_TYRE_DAMAGE_NOT_MODELLED,
    RISK_TYRE_LIFE_EXCEEDED,
    RISK_WET_CONDITIONS,
    STRATEGY_SCHEMA_VERSION,
    AlternativeResult,
    DataQuality,
    Perturbation,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
    compute_inputs_hash,
)
from .tyre_options import TyreOptions, tyre_options

NOTE_ENGINE_ERROR = "strategy_engine_error"
"""Clave de la causa publicada en ``StrategySnapshot.error`` cuando falla el motor."""

_LOGGER = logging.getLogger(__name__)
_RACE_SESSION_TYPES = frozenset({"race", "race_2", "race_3"})

#: Orden de severidad de las calidades; el peor valor es el que se publica.
_QUALITY_ORDER: dict[Quality, int] = {
    Quality.OK: 0,
    Quality.LOW: 1,
    Quality.STALE: 2,
    Quality.INSUFFICIENT: 3,
    Quality.UNAVAILABLE: 4,
}


def _worst(*qualities: Quality) -> Quality:
    return max(qualities, key=lambda quality: _QUALITY_ORDER[quality])


def _player_car_index(state: RaceStateSnapshot) -> int:
    index = state.player_car_index
    return index if index is not None and 0 <= index < len(state.cars) else -1


def _mounted_compound(car: CarSnapshot | None) -> str | None:
    """Compuesto montado según el juego, con la instantánea ajustada por delante."""
    if car is None:
        return None
    fitted = car.fitted_tyre
    raw = (
        fitted.actual_tyre_compound
        if fitted is not None and fitted.actual_tyre_compound
        else car.actual_tyre_compound
    )
    return canonical_compound(raw)


def _lap(state: RaceStateSnapshot) -> int:
    """Vuelta actual del jugador; el contrato exige un entero positivo."""
    car = state.player_car
    if car is not None and car.current_lap is not None:
        return max(1, car.current_lap)
    if state.current_lap is not None:
        return max(1, state.current_lap)
    return 1


def _session_uid(state: RaceStateSnapshot) -> str:
    uid = state.session.uid
    return str(uid) if uid is not None else "unknown"


def _laps_remaining(state: RaceStateSnapshot) -> float | None:
    """Vueltas restantes con la fracción recorrida de la vuelta actual."""
    session = state.session
    if session.total_laps is None:
        return None
    if session.ended is True:
        return 0.0
    if session.safety_car_status == "formation_lap":
        return float(session.total_laps)
    car = state.player_car
    if car is None or car.current_lap is None:
        return None
    distance = car.lap_distance_m
    if distance is None or session.track_length_m is None or session.track_length_m <= 0:
        return None
    fraction = min(max(distance / session.track_length_m, 0.0), 1.0)
    return max(0.0, session.total_laps - (car.current_lap - 1) - fraction)


def _is_stale(state: RaceStateSnapshot, now: float) -> bool:
    """True si los grupos que alimentan el ritmo están obsoletos."""
    for group in ("lap_data", "session_history"):
        try:
            freshness = state.freshness_for(group)
        except KeyError:
            continue
        if freshness.is_stale(now):
            return True
    return False


def _pace_model(models: CompoundModels, mounted: str | None) -> CompoundModel | None:
    """Modelo del compuesto montado; si no hay, el de referencia."""
    if mounted is not None:
        model = models.get(mounted)
        if model is not None:
            return model
    if models.reference_compound is not None:
        model = models.get(models.reference_compound)
        if model is not None:
            return model
    return models.models[0] if models.models else None


def _prior_weight(model: CompoundModel | None, cfg: StrategyConfig) -> float:
    """Proporción de prior dentro del modelo, para descontar confianza."""
    if model is None or model.source == "race":
        return 0.0
    if model.source == "blend":
        total = cfg.prior_strength_k + model.n_race_laps
        return cfg.prior_strength_k / total if total > 0 else 0.0
    return 1.0


def _data_quality(
    inputs: MetricInputs,
    models: CompoundModels,
    pit: PitLossEstimate,
    cfg: StrategyConfig,
) -> DataQuality:
    model = _pace_model(models, _mounted_compound(inputs.car))
    session = inputs.state.session
    session_type = (session.session_type or "").lower()
    return DataQuality(
        pace=model.quality if model is not None else Quality.UNAVAILABLE,
        pit_loss=pit.quality,
        session_is_race=session_type in _RACE_SESSION_TYPES,
        laps_remaining=_laps_remaining(inputs.state),
        stale=_is_stale(inputs.state, inputs.now),
        pace_samples=model.n_race_laps if model is not None else 0,
        prior_weight=_prior_weight(model, cfg),
    )


def _projected_lap_forecast(
    inputs: MetricInputs,
    models: CompoundModels,
    options: TyreOptions,
    quality: DataQuality,
) -> tuple[float, ...] | None:
    """Proyecta vueltas completas después de la vuelta evaluada."""
    state = inputs.state
    total_laps = state.session.total_laps
    lap = _lap(state)
    if (
        not quality.session_is_race
        or inputs.car_index < 0
        or total_laps is None
        or total_laps <= lap
    ):
        return None
    compound = options.current_compound
    model = models.get(compound) if compound is not None else None
    if model is None or not model.available or model.initial_age_laps is None:
        return None
    return project_lap_times(
        model,
        model.initial_age_laps + 1.0,
        float(total_laps - lap),
    )


def _assumptions(
    models: CompoundModels,
    pit: PitLossEstimate,
    alternatives: tuple[AlternativeResult, ...],
) -> tuple[str, ...]:
    notes: list[str] = [ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED]
    if any(item.source in {"profile", "blend"} for item in models.models) or (
        pit.source == "profile"
    ):
        notes.append(ASSUMPTION_PROFILE_PRIOR_USED)
    if any(item.rejoin_position is not None for item in alternatives):
        notes.append(ASSUMPTION_RIVALS_DO_NOT_PIT)
    return tuple(notes)


def _risks(
    options: TyreOptions,
    alternatives: tuple[AlternativeResult, ...],
) -> tuple[str, ...]:
    notes: list[str] = [RISK_DAMAGE_NOT_MODELLED, RISK_TYRE_DAMAGE_NOT_MODELLED]
    used = {note for item in alternatives for note in item.notes}
    if RISK_TRAFFIC_NOT_PRICED in used:
        notes.append(RISK_TRAFFIC_NOT_PRICED)
    if RISK_TYRE_LIFE_EXCEEDED in used:
        notes.append(RISK_TYRE_LIFE_EXCEEDED)
    if options.wet_conditions:
        notes.append(RISK_WET_CONDITIONS)
    return tuple(notes)


def _recommendation_signature(
    recommendation: StrategyRecommendation,
) -> tuple[object, ...]:
    """Identidad de la recomendación: solo esto incrementa la revisión."""
    return (
        recommendation.action,
        recommendation.key,
        recommendation.pit_lap,
        recommendation.compound,
    )


def _failure_evaluation(
    state: RaceStateSnapshot,
    revision: int,
) -> StrategyEvaluation | None:
    """Evaluación mínima que declara ``INSUFFICIENT_DATA`` ante un fallo interno."""
    lap = _lap(state)
    uid = _session_uid(state)
    return StrategyEvaluation(
        revision=revision,
        session_uid=uid,
        lap=lap,
        laps_remaining=_laps_remaining(state),
        baseline=BASELINE_STAY_OUT,
        recommendation=StrategyRecommendation(
            action=StrategyAction.INSUFFICIENT_DATA,
            key=BASELINE_STAY_OUT,
        ),
        alternatives=(),
        assumptions=(),
        risks=(),
        sensitive_to=(),
        data_quality=Quality.INSUFFICIENT,
        inputs_hash=compute_inputs_hash({"lap": lap, "session_uid": uid}),
        schema_version=STRATEGY_SCHEMA_VERSION,
    )


def evaluate_once(
    inputs: MetricInputs,
    cfg: StrategyConfig,
    *,
    revision: int = 0,
) -> StrategyEvaluation:
    """Evalúa las alternativas bajo demanda y devuelve la evaluación completa.

    Es la función pura que usan el motor y las consultas offline (Fase 7); no
    guarda estado ni usa reloj: todas las entradas llegan por ``inputs``.
    """
    models = build_compound_models(inputs, cfg)
    pit = estimate_pit_loss(inputs, cfg)
    options = tyre_options(inputs, cfg)
    quality = _data_quality(inputs, models, pit, cfg)
    projected_lap_times = _projected_lap_forecast(inputs, models, options, quality)

    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return evaluate_alternatives(models, pit, options, inputs, cfg, perturbation)

    decision = decide(evaluate, quality, cfg)
    alternatives = evaluate(Perturbation())
    baseline = alternatives[0].key if alternatives else BASELINE_STAY_OUT

    return StrategyEvaluation(
        revision=revision,
        session_uid=_session_uid(inputs.state),
        lap=_lap(inputs.state),
        laps_remaining=_laps_remaining(inputs.state),
        baseline=baseline,
        recommendation=StrategyRecommendation(
            action=decision.action,
            key=decision.key or baseline,
            pit_lap=decision.pit_lap,
            compound=decision.compound,
            gain_vs_baseline_s=decision.gain_vs_baseline_s,
            gain_interval_s=decision.gain_interval_s,
            confidence=decision.confidence,
        ),
        alternatives=tuple(
            StrategyAlternative(
                key=item.key,
                remaining_time_s=item.remaining_time_s,
                gain_vs_baseline_s=item.gain_vs_baseline_s,
                rejoin_position=item.rejoin_position,
                traffic_risk=item.traffic_risk,
            )
            for item in alternatives
        ),
        assumptions=_assumptions(models, pit, alternatives),
        risks=_risks(options, alternatives),
        sensitive_to=decision.sensitive_to,
        data_quality=_worst(
            quality.pace,
            quality.pit_loss,
            options.quality,
        ),
        inputs_hash=compute_inputs_hash(
            {
                "state": inputs.state,
                "history": inputs.history,
                "profile": inputs.profile,
                "car_index": inputs.car_index,
            }
        ),
        profile_hash=inputs.profile.hash if inputs.profile is not None else None,
        schema_version=STRATEGY_SCHEMA_VERSION,
        forecast_car_index=inputs.car_index if projected_lap_times is not None else None,
        forecast_compound=options.current_compound if projected_lap_times is not None else None,
        projected_lap_times_s=projected_lap_times,
    )


class StrategyEngine:
    """Orquesta S2–S6 con la cadencia de la sección 5.9 y publica el snapshot."""

    def __init__(
        self,
        config: StrategyConfig | None = None,
        metrics_config: MetricsConfig | None = None,
    ) -> None:
        self._base_config = config if config is not None else StrategyConfig()
        self._config = self._base_config
        self._metrics_config = metrics_config if metrics_config is not None else MetricsConfig()
        self._profile: WeekendProfile | None = None
        self._snapshot = StrategySnapshot()
        self._revision = 0
        self._last_signature: tuple[object, ...] | None = None
        self._last_evaluated_at: float | None = None
        self._last_recommendation: tuple[object, ...] | None = None
        self._last_session_uid: str | None = None

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def metrics_config(self) -> MetricsConfig:
        return self._metrics_config

    @property
    def profile(self) -> WeekendProfile | None:
        return self._profile

    @property
    def revision(self) -> int:
        """Revisión publicada; solo avanza cuando cambia la recomendación."""
        return self._revision

    def set_profile(self, profile: WeekendProfile | None) -> None:
        """Fija el perfil del fin de semana; no fuerza una evaluación."""
        self._profile = profile

    def snapshot(self) -> StrategySnapshot:
        return self._snapshot

    # ------------------------------------------------------------------
    # Ciclo de evaluación
    # ------------------------------------------------------------------

    def apply(
        self,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot | None,
        history: LapHistorySnapshot,
        profile: WeekendProfile | None,
        observed_at: float,
    ) -> None:
        """Evalúa si la política lo permite y publica el resultado.

        ``metrics`` se recibe para que el motor comparta la instantánea de
        métricas con el resto del pipeline; el cálculo de estrategia usa el
        histórico de vueltas y el estado, no los valores ya agregados.
        """
        del metrics  # el cálculo parte del estado y del histórico de vueltas
        if not math.isfinite(observed_at):
            raise ValueError("observed_at debe ser finito")
        self._profile = profile
        self._sync_session(state)
        self._config = self._base_config.for_track(state.session.track)
        if not self._config.enabled:
            return
        try:
            signature = self._state_signature(state) + (
                self._profile.hash if self._profile is not None else None,
            )
            if signature == self._last_signature and self._snapshot.evaluation is not None:
                return
            if (
                self._last_evaluated_at is not None
                and observed_at - self._last_evaluated_at < self._config.evaluation_min_interval_s
            ):
                return

            self._last_signature = signature
            self._last_evaluated_at = observed_at
            inputs = MetricInputs(
                state=state,
                history=history,
                config=self._metrics_config,
                now=observed_at,
                car_index=_player_car_index(state),
                profile=self._profile,
            )
            evaluation = evaluate_once(inputs, self._config, revision=self._revision)
        except Exception as exc:  # noqa: BLE001 - un fallo no debe parar la captura
            _LOGGER.exception("Strategy evaluation failed")
            self._snapshot = StrategySnapshot(
                evaluation=_failure_evaluation(state, self._revision),
                updated_at_monotonic=observed_at,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        signature_recommendation = _recommendation_signature(evaluation.recommendation)
        if (
            self._last_recommendation is not None
            and signature_recommendation != self._last_recommendation
        ):
            self._revision += 1
            evaluation = replace(evaluation, revision=self._revision)
        self._last_recommendation = signature_recommendation
        self._snapshot = StrategySnapshot(
            evaluation=evaluation,
            updated_at_monotonic=observed_at,
            error=None,
        )

    def _sync_session(self, state: RaceStateSnapshot) -> None:
        """Reinicia revisión y cadencia al cambiar de sesión."""
        uid = _session_uid(state)
        if uid == self._last_session_uid:
            return
        self._last_session_uid = uid
        self._revision = 0
        self._last_recommendation = None
        self._last_evaluated_at = None
        self._last_signature = None
        self._snapshot = StrategySnapshot()

    @staticmethod
    def _state_signature(state: RaceStateSnapshot) -> tuple[object, ...]:
        """Eventos que justifican una nueva evaluación (sección 5.9)."""
        session = state.session
        car = state.player_car
        return (
            session.uid,
            session.session_type,
            session.track,
            session.ended,
            session.total_laps,
            session.track_length_m,
            session.weather,
            session.track_temperature_c,
            session.air_temperature_c,
            session.forecast,
            session.safety_car_status,
            session.safety_car_periods,
            session.virtual_safety_car_periods,
            session.red_flag_periods,
            session.pit_window_ideal_lap,
            session.pit_window_latest_lap,
            state.current_lap,
            car.current_lap if car is not None else None,
            _mounted_compound(car),
            car.pit_status if car is not None else None,
            car.result_status if car is not None else None,
        )


__all__ = [
    "NOTE_ENGINE_ERROR",
    "StrategyEngine",
    "evaluate_once",
]
