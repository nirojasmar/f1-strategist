"""Resumen de tandas por compuesto para el perfil del fin de semana.

Este módulo es una función pura: recibe las vueltas registradas y las tandas
declaradas por el juego, y devuelve un resumen por tanda. No accede al estado
de la sesión ni a SQLite; la persistencia corresponde al grabador (P4) y la
agregación por fin de semana al perfil (P5).

Los límites de ``SessionHistory`` pueden cerrar una tanda antes de la vuelta de
entrada a boxes: el juego asigna esa vuelta al compuesto nuevo, pero al cerrarla
el coche aún lleva el compuesto viejo. Por eso la asignación primaria usa el
compuesto y la edad observados en cada vuelta; los límites declarados solo
sirven de respaldo cuando falta el dato observado. Así la in-lap permanece en la
tanda que correspondía y no contamina las vueltas de la tanda siguiente.

El desgaste medido es la señal principal de degradación. El ritmo se informa en
bruto y, solo cuando la configuración define ``fuel_effect_s_per_kg``, también
corregido por el combustible consumido: sin ese parámetro no se inventa la
corrección.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import StatisticsError, fmean, linear_regression

from ..config import MetricsConfig
from .comparability import classify_lap, is_clean
from .history import LapRecord, TyreStint
from .types import Quality

#: Vueltas que nunca cuentan como limpias aunque la clasificación las acepte.
_NON_CLEAN_FLAGS = frozenset({"partial_lap", "incomplete", "telemetry_partial", "retired"})

#: Puntos mínimos para estimar una tendencia mediante regresión lineal.
_MIN_TREND_POINTS = 3


@dataclass(frozen=True, slots=True)
class StintSummary:
    """Resumen inmutable de una tanda de un coche en una sesión."""

    car_index: int
    stint_index: int
    start_lap: int
    end_lap: int | None
    laps: int
    compound: str | None
    visual_compound: str | None
    clean_laps: int
    tyre_age_start: int | None
    tyre_age_end: int | None
    fuel_start_kg: float | None
    fuel_end_kg: float | None
    pace_mean_s: float | None
    pace_mean_fuel_corrected_s: float | None
    pace_trend_s_per_lap: float | None
    pace_trend_fuel_corrected_s_per_lap: float | None
    wear_start_pct: float | None
    wear_end_pct: float | None
    wear_rate_pct_per_lap: float | None
    track_temperature_mean_c: float | None
    weather: tuple[str, ...]
    representative: bool
    quality: Quality

    def __post_init__(self) -> None:
        if self.car_index < 0:
            raise ValueError("car_index no puede ser negativo")
        if self.stint_index < 0:
            raise ValueError("stint_index no puede ser negativo")
        if self.start_lap < 1:
            raise ValueError("start_lap debe ser mayor o igual que 1")
        if self.end_lap is not None and self.end_lap < self.start_lap:
            raise ValueError("end_lap no puede ser anterior a start_lap")
        if self.laps < 0:
            raise ValueError("laps no puede ser negativo")
        if not 0 <= self.clean_laps <= self.laps:
            raise ValueError("clean_laps debe estar entre 0 y laps")
        if not isinstance(self.weather, tuple):
            object.__setattr__(self, "weather", tuple(self.weather))


def summarize_stints(
    laps: Iterable[LapRecord],
    stints: Iterable[TyreStint],
    config: MetricsConfig,
) -> tuple[StintSummary, ...]:
    """Resume las tandas de un coche a partir de sus vueltas.

    Las vueltas deben pertenecer a un único coche; las tandas se ordenan por
    vuelta inicial y las vueltas fuera de la cobertura declarada se ignoran. Las
    tandas sin vueltas observadas no generan resumen. La función es pura y
    determinista: no modifica sus entradas y el resultado no depende del orden
    en que se reciban.

    La tanda se delimita con el compuesto y la edad observados al cerrar cada
    vuelta. Un cambio de compuesto, un reinicio de edad, una salida de boxes o
    el inicio de una tanda declarada cuando falta el dato observado abren una
    tanda nueva. La entrada a boxes no abre tanda: la in-lap sigue perteneciendo
    al compuesto con el que se cruzó la meta.
    """
    records = tuple(sorted(laps, key=lambda record: record.lap_number))
    ordered = tuple(sorted(stints, key=lambda stint: stint.start_lap))
    if not records:
        return ()
    car_indexes = sorted({record.car_index for record in records})
    if len(car_indexes) > 1:
        raise ValueError(
            "summarize_stints espera vueltas de un solo coche; "
            f"recibió los índices {', '.join(str(index) for index in car_indexes)}"
        )
    car_index = car_indexes[0]
    periods = _stint_periods(records, ordered)
    if not periods:
        return ()
    last_covered_lap = periods[-1][-1].lap_number
    return tuple(
        _summarize(
            car_index,
            index,
            _summary_stint(period, ordered, last_covered_lap),
            period,
            config,
            declared=bool(ordered),
        )
        for index, period in enumerate(periods)
    )


def _stint_periods(
    records: Sequence[LapRecord],
    stints: Sequence[TyreStint],
) -> tuple[tuple[LapRecord, ...], ...]:
    """Agrupa vueltas por compuesto y edad observados, con respaldo declarado."""
    if stints:
        first_lap = stints[0].start_lap
        last_lap = stints[-1].end_lap
        covered = tuple(
            record
            for record in records
            if record.lap_number >= first_lap
            and (last_lap is None or record.lap_number <= last_lap)
        )
    else:
        # Sin tandas declaradas las observaciones son la fuente primaria: se
        # cubren todas las vueltas recibidas.
        covered = tuple(records)
    if not covered:
        return ()
    declared_starts = {stint.start_lap for stint in stints}
    periods: list[list[LapRecord]] = []
    for record in covered:
        previous = periods[-1][-1] if periods else None
        starts = previous is None or _starts_observed_stint(record, previous)
        if (
            not starts
            and previous is not None
            and record.lap_number in declared_starts
            and not _observed_continuation(previous, record)
        ):
            starts = True
        if starts:
            periods.append([record])
        else:
            periods[-1].append(record)
    return tuple(tuple(period) for period in periods)


def _starts_observed_stint(record: LapRecord, previous: LapRecord) -> bool:
    """Indica si la vuelta abre una tanda según el estado observado."""
    if record.pit_exit:
        return True
    if (
        record.compound is not None
        and previous.compound is not None
        and record.compound != previous.compound
    ):
        return True
    if record.tyre_age_start == 0:
        return True
    return (
        record.tyre_age_end is not None
        and previous.tyre_age_end is not None
        and record.tyre_age_end < previous.tyre_age_end
    )


def _observed_continuation(previous: LapRecord, record: LapRecord) -> bool:
    """Indica si la edad observada confirma que la tanda continúa.

    Un compuesto igual no basta: si falta la edad, un límite declarado puede ser
    la única señal disponible de que hubo un cambio de neumáticos.
    """
    if previous.tyre_age_end is not None and record.tyre_age_end is not None:
        return record.tyre_age_end > previous.tyre_age_end
    if previous.tyre_age_start is not None and record.tyre_age_start is not None:
        return record.tyre_age_start > previous.tyre_age_start
    return False


def _summary_stint(
    period: Sequence[LapRecord],
    stints: Sequence[TyreStint],
    last_covered_lap: int,
) -> TyreStint:
    """Construye la tanda efectiva de un grupo de vueltas."""
    declared = _best_overlap(period, stints)
    compound = _first_value(record.compound for record in period)
    if compound is None and declared is not None:
        compound = declared.compound
    visual = declared.visual_compound if declared is not None else None
    open_tail = (not stints or stints[-1].end_lap is None) and (
        period[-1].lap_number == last_covered_lap
    )
    end_lap = None if open_tail else period[-1].lap_number
    return TyreStint(
        compound=compound,
        visual_compound=visual or compound,
        start_lap=period[0].lap_number,
        end_lap=end_lap,
    )


def _best_overlap(
    period: Sequence[LapRecord],
    stints: Sequence[TyreStint],
) -> TyreStint | None:
    """Tanda declarada con más vueltas en común con el periodo observado."""
    best: TyreStint | None = None
    best_overlap = 0
    for stint in stints:
        overlap = sum(
            1
            for record in period
            if record.lap_number >= stint.start_lap
            and (stint.end_lap is None or record.lap_number <= stint.end_lap)
        )
        if overlap > best_overlap:
            best = stint
            best_overlap = overlap
    return best


def _summarize(
    car_index: int,
    stint_index: int,
    stint: TyreStint,
    records: Sequence[LapRecord],
    config: MetricsConfig,
    *,
    declared: bool = True,
) -> StintSummary:
    clean = tuple(record for record in records if _is_clean(record, config))
    return StintSummary(
        car_index=car_index,
        stint_index=stint_index,
        start_lap=stint.start_lap,
        end_lap=stint.end_lap,
        laps=len(records),
        compound=stint.compound or _last_value(record.compound for record in records),
        visual_compound=stint.visual_compound or _last_value(record.compound for record in records),
        clean_laps=len(clean),
        tyre_age_start=_first_value(record.tyre_age_start for record in records),
        tyre_age_end=_last_value(record.tyre_age_end for record in records),
        fuel_start_kg=_fuel_start(records),
        fuel_end_kg=_fuel_end(records),
        pace_mean_s=_pace_mean(clean),
        pace_mean_fuel_corrected_s=_pace_mean_corrected(clean, config),
        pace_trend_s_per_lap=_pace_trend(clean),
        pace_trend_fuel_corrected_s_per_lap=_pace_trend_corrected(clean, config),
        wear_start_pct=_wear_start(records),
        wear_end_pct=_wear_end(records),
        wear_rate_pct_per_lap=_wear_rate(clean),
        track_temperature_mean_c=_mean_optional(record.track_temperature_c for record in records),
        weather=tuple(sorted({weather for record in records for weather in record.weather_seen})),
        representative=len(clean) >= config.min_representative_laps,
        quality=_quality(len(clean), config, declared=declared),
    )


def _is_clean(record: LapRecord, config: MetricsConfig) -> bool:
    if not record.closed:
        return False
    if _NON_CLEAN_FLAGS & record.flags:
        return False
    return is_clean(classify_lap(record, config))


def _quality(clean_laps: int, config: MetricsConfig, *, declared: bool = True) -> Quality:
    if clean_laps == 0:
        return Quality.INSUFFICIENT
    quality = Quality.OK if clean_laps >= config.min_representative_laps else Quality.LOW
    if not declared and quality is Quality.OK:
        # Sin tandas declaradas la delimitación depende solo de las
        # observaciones, así que no se alcanza la calidad plena.
        return Quality.LOW
    return quality


def _first_value[ValueT](values: Iterable[ValueT | None]) -> ValueT | None:
    for value in values:
        if value is not None:
            return value
    return None


def _last_value[ValueT](values: Iterable[ValueT | None]) -> ValueT | None:
    result: ValueT | None = None
    for value in values:
        if value is not None:
            result = value
    return result


def _fuel_start(records: Sequence[LapRecord]) -> float | None:
    """Combustible al inicio de la tanda según la primera vuelta con dato."""
    for record in records:
        value = record.fuel_start_kg if record.fuel_start_kg is not None else record.fuel_end_kg
        if value is not None:
            return value
    return None


def _fuel_end(records: Sequence[LapRecord]) -> float | None:
    """Combustible al final de la tanda según la última vuelta con dato."""
    for record in reversed(records):
        value = record.fuel_end_kg if record.fuel_end_kg is not None else record.fuel_start_kg
        if value is not None:
            return value
    return None


def _mean_optional(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return fmean(present)


def _mean_wear(values: tuple[float, ...] | None) -> float | None:
    if not values:
        return None
    return fmean(values)


def _wear_start(records: Sequence[LapRecord]) -> float | None:
    for record in records:
        value = _mean_wear(record.tyre_wear_start_pct)
        if value is not None:
            return value
    for record in records:
        value = _mean_wear(record.tyre_wear_end_pct)
        if value is not None:
            return value
    return None


def _wear_end(records: Sequence[LapRecord]) -> float | None:
    for record in reversed(records):
        value = _mean_wear(record.tyre_wear_end_pct)
        if value is not None:
            return value
    for record in reversed(records):
        value = _mean_wear(record.tyre_wear_start_pct)
        if value is not None:
            return value
    return None


def _wear_rate(clean: Sequence[LapRecord]) -> float | None:
    """Pendiente de desgaste medio (``%/vuelta``) frente a la edad del neumático."""
    points = [
        (float(record.tyre_age_end), mean)
        for record in clean
        if record.tyre_age_end is not None
        and (mean := _mean_wear(record.tyre_wear_end_pct)) is not None
    ]
    if len(points) < 2:
        return None
    try:
        return linear_regression([age for age, _ in points], [wear for _, wear in points]).slope
    except StatisticsError:
        return None


def _pace_mean(clean: Sequence[LapRecord]) -> float | None:
    times = [record.lap_time_s for record in clean if record.lap_time_s is not None]
    if not times:
        return None
    return fmean(times)


def _pace_mean_corrected(clean: Sequence[LapRecord], config: MetricsConfig) -> float | None:
    effect = config.fuel_effect_s_per_kg
    if effect is None:
        return None
    values = [
        record.lap_time_s - record.fuel_start_kg * effect
        for record in clean
        if record.lap_time_s is not None and record.fuel_start_kg is not None
    ]
    if not values:
        return None
    return fmean(values)


def _pace_trend(clean: Sequence[LapRecord]) -> float | None:
    points = [
        (float(record.lap_number), record.lap_time_s)
        for record in clean
        if record.lap_time_s is not None
    ]
    return _slope(points)


def _pace_trend_corrected(clean: Sequence[LapRecord], config: MetricsConfig) -> float | None:
    effect = config.fuel_effect_s_per_kg
    if effect is None:
        return None
    points = [
        (float(record.lap_number), record.lap_time_s - record.fuel_start_kg * effect)
        for record in clean
        if record.lap_time_s is not None and record.fuel_start_kg is not None
    ]
    return _slope(points)


def _slope(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < _MIN_TREND_POINTS:
        return None
    try:
        return linear_regression([x for x, _ in points], [y for _, y in points]).slope
    except StatisticsError:
        return None
