"""Evaluación del enrutador de intenciones, entidades y aclaraciones (Fase 7, Q14)."""

from __future__ import annotations

import importlib
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Protocol

from ..config import PilotConfig
from ..parser import MAX_CARS
from ..race_state import CarSnapshot, RaceSessionSnapshot, RaceStateSnapshot
from .types import AnswerKind, Intent, PilotAnswer, PilotExchange, PilotQuery, RoutedQuery

PILOT_EVAL_SCHEMA_VERSION = 1
DEFAULT_PILOT_DATASET = Path("tests/fixtures/pilot_questions")
_CAR_SNAPSHOT_FIELDS = frozenset(item.name for item in fields(CarSnapshot))


class RouterCallable(Protocol):
    """Contrato del enrutador (Q3)."""

    def __call__(
        self,
        text: str,
        *,
        language: str,
        drivers: Any,
        dialogue: Any | None,
        cfg: PilotConfig,
    ) -> RoutedQuery: ...


class DriverDirectoryFactory(Protocol):
    """Contrato del directorio de pilotos (Q4)."""

    from_state: Callable[..., Any]


class DialogueProtocol(Protocol):
    """Contrato de la memoria corta de diálogo (Q4)."""

    def record(self, exchange: PilotExchange, now: float) -> None: ...

    def pending_clarification(self, now: float) -> Any | None: ...

    def resolve_clarification(self, text: str, now: float) -> Any | None: ...


DialogueFactory = Callable[[PilotConfig], DialogueProtocol]


@dataclass(frozen=True, slots=True)
class GridCarSpec:
    """Coche sintético de parrilla para las pruebas de evaluación."""

    car_index: int
    name: str
    team: str
    position: int
    race_number: int | None = None
    result_status: str = "active"
    is_player: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.car_index, bool) or not 0 <= self.car_index < MAX_CARS:
            raise ValueError(f"car_index fuera de rango: {self.car_index}")
        if not self.name.strip():
            raise ValueError("name no puede estar vacío")
        if isinstance(self.position, bool) or self.position < 1:
            raise ValueError(f"position inválida: {self.position}")


@dataclass(frozen=True, slots=True)
class InScopeCase:
    """Frase dentro de alcance con su intención y entidad esperadas."""

    case_id: str
    text: str
    expected_intent: Intent
    expected_car_index: int | None = None
    expect_no_entity: bool = False
    grid: tuple[GridCarSpec, ...] | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("id no puede estar vacío")
        if not self.text.strip():
            raise ValueError(f"text vacío en {self.case_id}")
        if self.expected_intent is Intent.UNKNOWN:
            raise ValueError(f"{self.case_id}: in_scope no puede esperar UNKNOWN")


@dataclass(frozen=True, slots=True)
class OutOfScopeCase:
    """Frase fuera de alcance (debe producir UNKNOWN)."""

    case_id: str
    text: str
    grid: tuple[GridCarSpec, ...] | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("id no puede estar vacío")
        if not self.text.strip():
            raise ValueError(f"text vacío en {self.case_id}")


@dataclass(frozen=True, slots=True)
class AmbiguousCase:
    """Frase que debe pedir aclaración entre candidatos ordenados."""

    case_id: str
    text: str
    expected_candidate_indices: tuple[int, ...]
    expected_intent: Intent | None = None
    resolution_text: str | None = None
    expected_resolved_car_index: int | None = None
    grid: tuple[GridCarSpec, ...] | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("id no puede estar vacío")
        if not self.text.strip():
            raise ValueError(f"text vacío en {self.case_id}")
        if len(self.expected_candidate_indices) < 2:
            raise ValueError(f"{self.case_id}: se esperan al menos 2 candidatos ambiguos")


@dataclass(frozen=True, slots=True)
class DialogueTurnSpec:
    """Un turno dentro de una secuencia de diálogo."""

    text: str
    t_s: float
    expected_intent: Intent
    expected_car_index: int | None = None
    expect_clarification: bool = False
    expected_candidate_indices: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("El texto del turno no puede estar vacío")
        if not math.isfinite(self.t_s) or self.t_s < 0.0:
            raise ValueError("t_s debe ser finito y no negativo")


@dataclass(frozen=True, slots=True)
class DialogueSequenceCase:
    """Secuencia multi-turno para validar memoria corta y aclaraciones."""

    case_id: str
    turns: tuple[DialogueTurnSpec, ...]
    description: str = ""
    grid: tuple[GridCarSpec, ...] | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("id no puede estar vacío")
        if not self.turns:
            raise ValueError(f"{self.case_id}: la secuencia debe tener al menos un turno")


@dataclass(frozen=True, slots=True)
class PilotEvalDataset:
    """Conjuntos etiquetados de preguntas del piloto."""

    directory: Path
    language: str
    default_grid: tuple[GridCarSpec, ...]
    in_scope: tuple[InScopeCase, ...]
    out_of_scope: tuple[OutOfScopeCase, ...]
    ambiguous: tuple[AmbiguousCase, ...]
    dialogue_sequences: tuple[DialogueSequenceCase, ...]


def load_pilot_dataset(directory: Path) -> PilotEvalDataset:
    """Carga y valida los cuatro conjuntos etiquetados desde ``directory``."""
    if not directory.is_dir():
        raise ValueError(f"No existe el directorio del conjunto de evaluación: {directory}")

    grid_payload = _load_json(directory / "grid.json")
    if not isinstance(grid_payload, Mapping):
        raise ValueError("grid.json debe ser un objeto JSON")
    language = _req_str(grid_payload, "language", "grid.json")
    default_grid = _parse_grid(grid_payload.get("cars"), "grid.json")

    in_scope = _parse_in_scope(_load_json(directory / "in_scope.json"))
    out_of_scope = _parse_out_of_scope(_load_json(directory / "out_of_scope.json"))
    ambiguous = _parse_ambiguous(_load_json(directory / "ambiguous.json"))
    dialogues = _parse_dialogues(_load_json(directory / "dialogue_sequences.json"))

    return PilotEvalDataset(
        directory=directory,
        language=language,
        default_grid=default_grid,
        in_scope=in_scope,
        out_of_scope=out_of_scope,
        ambiguous=ambiguous,
        dialogue_sequences=dialogues,
    )


def build_race_state_from_grid(grid: Sequence[GridCarSpec]) -> RaceStateSnapshot:
    """Construye una instantánea inmutable de carrera a partir de la parrilla."""
    by_index = {car.car_index: car for car in grid}
    player_index = next((car.car_index for car in grid if car.is_player), None)
    if player_index is None and grid:
        player_index = grid[0].car_index

    cars: list[CarSnapshot] = []
    for index in range(MAX_CARS):
        spec = by_index.get(index)
        if spec is None:
            cars.append(CarSnapshot(car_index=index))
            continue
        kwargs: dict[str, Any] = {
            "car_index": spec.car_index,
            "name": spec.name,
            "team": spec.team,
            "grid_position": spec.position,
            "position": spec.position,
            "result_status": spec.result_status,
        }
        if "race_number" in _CAR_SNAPSHOT_FIELDS and spec.race_number is not None:
            kwargs["race_number"] = spec.race_number
        cars.append(CarSnapshot(**kwargs))

    return RaceStateSnapshot(
        session=RaceSessionSnapshot(
            uid=1,
            session_type="race",
            track="synthetic",
            total_laps=20,
            started=True,
            ended=False,
        ),
        current_lap=5,
        player_car_index=player_index,
        cars=tuple(cars),
        freshness=(),
        events=(),
    )


def run_pilot_evaluation(
    dataset: PilotEvalDataset,
    *,
    config: PilotConfig | None = None,
    router: RouterCallable | None = None,
    directory_factory: DriverDirectoryFactory | None = None,
    dialogue_factory: DialogueFactory | None = None,
) -> dict[str, Any]:
    """Ejecuta los conjuntos etiquetados y devuelve métricas y fallos detallados."""
    pilot_cfg = config if config is not None else PilotConfig(language=dataset.language)
    active_router, active_dir_factory, active_dlg_factory = _resolve_components(
        router, directory_factory, dialogue_factory
    )
    default_drivers = _build_directory(
        active_dir_factory,
        build_race_state_from_grid(dataset.default_grid),
        dataset.default_grid,
    )

    failures: list[dict[str, Any]] = []
    in_scope_results = _eval_in_scope(
        dataset.in_scope,
        default_drivers,
        active_dir_factory,
        active_router,
        pilot_cfg,
        failures,
    )
    oos_fp = _eval_out_of_scope(
        dataset.out_of_scope,
        default_drivers,
        active_dir_factory,
        active_router,
        pilot_cfg,
        failures,
    )
    clarif_ok = _eval_ambiguous(
        dataset.ambiguous,
        default_drivers,
        active_dir_factory,
        active_router,
        active_dlg_factory,
        pilot_cfg,
        failures,
    )
    seq_ok, turn_ok, total_turns = _eval_dialogues(
        dataset.dialogue_sequences,
        default_drivers,
        active_dir_factory,
        active_router,
        active_dlg_factory,
        pilot_cfg,
        failures,
    )

    entity_total = in_scope_results["entity_evaluable"]
    metrics = {
        "intent_accuracy": _rate(in_scope_results["intent_ok"], len(dataset.in_scope)),
        "entity_accuracy": _rate(in_scope_results["entity_ok"], entity_total),
        "entity_evaluable": entity_total,
        "out_of_scope_false_positive_rate": _rate(oos_fp, len(dataset.out_of_scope)),
        "clarification_accuracy": _rate(clarif_ok, len(dataset.ambiguous)),
        "dialogue_sequence_accuracy": _rate(seq_ok, len(dataset.dialogue_sequences)),
        "dialogue_turn_accuracy": _rate(turn_ok, total_turns),
        "by_intent": in_scope_results["by_intent"],
    }
    return {
        "schema_version": PILOT_EVAL_SCHEMA_VERSION,
        "language": pilot_cfg.language,
        "dataset": str(dataset.directory),
        "counts": {
            "in_scope": len(dataset.in_scope),
            "out_of_scope": len(dataset.out_of_scope),
            "ambiguous": len(dataset.ambiguous),
            "dialogue_sequences": len(dataset.dialogue_sequences),
            "dialogue_turns": total_turns,
        },
        "metrics": metrics,
        "failures": failures,
    }


def evaluation_report_json(report: Mapping[str, Any]) -> str:
    """Serializa el informe de evaluación a JSON determinista."""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _eval_in_scope(
    cases: Sequence[InScopeCase],
    default_drivers: Any,
    dir_factory: DriverDirectoryFactory,
    router: RouterCallable,
    cfg: PilotConfig,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    intent_ok = 0
    entity_ok = 0
    entity_evaluable = 0
    by_intent_counts: dict[str, list[int]] = {}

    for case in cases:
        drivers = _drivers_for_case(case.grid, default_drivers, dir_factory)
        routed = router(
            case.text,
            language=cfg.language,
            drivers=drivers,
            dialogue=None,
            cfg=cfg,
        )
        bucket = by_intent_counts.setdefault(case.expected_intent.value, [0, 0])
        bucket[1] += 1

        reasons: list[str] = []
        if routed.intent is case.expected_intent:
            intent_ok += 1
            bucket[0] += 1
        else:
            reasons.append(
                f"intent_mismatch: esperado '{case.expected_intent.value}', "
                f"obtenido '{routed.intent.value}' (confianza {routed.confidence:.2f})"
            )

        if case.expected_car_index is not None:
            entity_evaluable += 1
            actual_indices = [
                entity.car_index for entity in routed.entities if entity.car_index is not None
            ]
            if routed.clarification is not None:
                reasons.append("unexpected_clarification: pidió aclaración en un caso unívoco")
            elif case.expected_car_index in actual_indices:
                entity_ok += 1
            else:
                reasons.append(
                    f"entity_mismatch: esperado coche {case.expected_car_index}, "
                    f"obtenido {actual_indices or 'ninguno'}"
                )
        elif case.expect_no_entity:
            entity_evaluable += 1
            actual_indices = [
                entity.car_index for entity in routed.entities if entity.car_index is not None
            ]
            if not actual_indices and routed.clarification is None:
                entity_ok += 1
            else:
                reasons.append(f"unexpected_entity: no se esperaba coche y obtuvo {actual_indices}")

        if reasons:
            failures.append(
                {
                    "split": "in_scope",
                    "id": case.case_id,
                    "text": case.text,
                    "reason": "; ".join(reasons),
                    "expected": {
                        "intent": case.expected_intent.value,
                        "car_index": case.expected_car_index,
                    },
                    "actual": {
                        "intent": routed.intent.value,
                        "confidence": round(routed.confidence, 4),
                        "car_indices": [entity.car_index for entity in routed.entities],
                        "clarification": routed.clarification is not None,
                    },
                }
            )

    by_intent = {
        intent_name: {
            "passed": passed,
            "total": total,
            "accuracy": _rate(passed, total),
        }
        for intent_name, (passed, total) in sorted(by_intent_counts.items())
    }
    return {
        "intent_ok": intent_ok,
        "entity_ok": entity_ok,
        "entity_evaluable": entity_evaluable,
        "by_intent": by_intent,
    }


def _eval_out_of_scope(
    cases: Sequence[OutOfScopeCase],
    default_drivers: Any,
    dir_factory: DriverDirectoryFactory,
    router: RouterCallable,
    cfg: PilotConfig,
    failures: list[dict[str, Any]],
) -> int:
    false_positives = 0
    for case in cases:
        drivers = _drivers_for_case(case.grid, default_drivers, dir_factory)
        routed = router(
            case.text,
            language=cfg.language,
            drivers=drivers,
            dialogue=None,
            cfg=cfg,
        )
        if routed.intent is not Intent.UNKNOWN:
            false_positives += 1
            failures.append(
                {
                    "split": "out_of_scope",
                    "id": case.case_id,
                    "text": case.text,
                    "reason": (
                        f"false_positive: esperado 'unknown', obtenido '{routed.intent.value}' "
                        f"(confianza {routed.confidence:.2f})"
                    ),
                    "expected": {"intent": Intent.UNKNOWN.value},
                    "actual": {
                        "intent": routed.intent.value,
                        "confidence": round(routed.confidence, 4),
                    },
                }
            )
    return false_positives


def _eval_ambiguous(
    cases: Sequence[AmbiguousCase],
    default_drivers: Any,
    dir_factory: DriverDirectoryFactory,
    router: RouterCallable,
    dialogue_factory: DialogueFactory,
    cfg: PilotConfig,
    failures: list[dict[str, Any]],
) -> int:
    passed = 0
    for case in cases:
        drivers = _drivers_for_case(case.grid, default_drivers, dir_factory)
        dialogue = dialogue_factory(cfg)
        routed = _call_router(
            router,
            case.text,
            language=cfg.language,
            drivers=drivers,
            dialogue=dialogue,
            cfg=cfg,
            now=10.0,
        )
        reasons: list[str] = []
        if case.expected_intent is not None and routed.intent is not case.expected_intent:
            reasons.append(
                f"intent_mismatch: esperado '{case.expected_intent.value}', "
                f"obtenido '{routed.intent.value}'"
            )

        actual_candidates: tuple[int, ...] = ()
        if routed.clarification is None:
            reasons.append("missing_clarification: no generó aclaración ante ambigüedad")
        else:
            actual_candidates = tuple(opt.car_index for opt in routed.clarification.options)
            if actual_candidates != case.expected_candidate_indices:
                reasons.append(
                    f"candidate_mismatch: esperados {list(case.expected_candidate_indices)}, "
                    f"obtenidos {list(actual_candidates)}"
                )
            elif case.resolution_text is not None:
                now = 10.0
                query = replace(routed.query, received_at=now)
                start = getattr(dialogue, "start_clarification", None)
                if callable(start):
                    start(
                        routed.clarification.options,
                        now,
                        message_key=routed.clarification.message_key,
                    )
                answer = PilotAnswer(
                    query_id=query.query_id,
                    intent=routed.intent,
                    kind=AnswerKind.CLARIFICATION,
                    text="",
                    message_key=routed.clarification.message_key,
                )
                dialogue.record(PilotExchange(query=query, answer=answer), now)
                resolved = dialogue.resolve_clarification(case.resolution_text, now + 2.0)
                resolved_car = _extract_car_index(resolved)
                if resolved_car != case.expected_resolved_car_index:
                    reasons.append(
                        f"resolution_mismatch: '{case.resolution_text}' resolvió coche "
                        f"{resolved_car}, esperado {case.expected_resolved_car_index}"
                    )

        if not reasons:
            passed += 1
        else:
            failures.append(
                {
                    "split": "ambiguous",
                    "id": case.case_id,
                    "text": case.text,
                    "reason": "; ".join(reasons),
                    "expected": {
                        "intent": None
                        if case.expected_intent is None
                        else case.expected_intent.value,
                        "candidates": list(case.expected_candidate_indices),
                        "resolved_car_index": case.expected_resolved_car_index,
                    },
                    "actual": {
                        "intent": routed.intent.value,
                        "candidates": list(actual_candidates),
                    },
                }
            )
    return passed


def _eval_dialogues(
    sequences: Sequence[DialogueSequenceCase],
    default_drivers: Any,
    dir_factory: DriverDirectoryFactory,
    router: RouterCallable,
    dialogue_factory: DialogueFactory,
    cfg: PilotConfig,
    failures: list[dict[str, Any]],
) -> tuple[int, int, int]:
    seq_passed = 0
    turn_passed = 0
    total_turns = 0

    for seq in sequences:
        drivers = _drivers_for_case(seq.grid, default_drivers, dir_factory)
        dialogue = dialogue_factory(cfg)
        seq_ok = True
        last_intent: Intent | None = None

        for turn_idx, turn in enumerate(seq.turns, start=1):
            total_turns += 1
            reasons: list[str] = []
            resolved_car: int | None = None
            pending = dialogue.pending_clarification(turn.t_s)
            routed: RoutedQuery | None = None

            if pending is not None:
                resolved = dialogue.resolve_clarification(turn.text, turn.t_s)
                resolved_car = _extract_car_index(resolved)

            if resolved_car is not None and last_intent is not None:
                actual_intent = last_intent
                actual_clarification = False
                actual_candidates: tuple[int, ...] = ()
                actual_car_indices = (resolved_car,)
                query = PilotQuery(
                    query_id=f"{seq.case_id}-{turn_idx}",
                    text=turn.text,
                    language=cfg.language,
                    received_at=turn.t_s,
                    source="eval",
                )
                answer = PilotAnswer(
                    query_id=query.query_id,
                    intent=actual_intent,
                    kind=AnswerKind.ANSWER,
                    text="",
                    message_key="eval",
                )
            else:
                routed = _call_router(
                    router,
                    turn.text,
                    language=cfg.language,
                    drivers=drivers,
                    dialogue=dialogue,
                    cfg=cfg,
                    now=turn.t_s,
                )
                if routed.clarification is not None:
                    start = getattr(dialogue, "start_clarification", None)
                    if callable(start):
                        start(
                            routed.clarification.options,
                            turn.t_s,
                            message_key=routed.clarification.message_key,
                        )
                actual_intent = routed.intent
                actual_clarification = routed.clarification is not None
                actual_candidates = (
                    tuple(opt.car_index for opt in routed.clarification.options)
                    if routed.clarification is not None
                    else ()
                )
                actual_car_indices = tuple(
                    entity.car_index for entity in routed.entities if entity.car_index is not None
                )
                query = replace(
                    routed.query,
                    query_id=f"{seq.case_id}-{turn_idx}",
                    received_at=turn.t_s,
                )
                kind = (
                    AnswerKind.CLARIFICATION
                    if routed.clarification is not None
                    else (
                        AnswerKind.UNKNOWN if routed.intent is Intent.UNKNOWN else AnswerKind.ANSWER
                    )
                )
                answer = PilotAnswer(
                    query_id=query.query_id,
                    intent=routed.intent,
                    kind=kind,
                    text="",
                    message_key=(
                        routed.clarification.message_key
                        if routed.clarification is not None
                        else "eval"
                    ),
                )

            if actual_intent is not turn.expected_intent:
                reasons.append(
                    f"intent_mismatch: esperado '{turn.expected_intent.value}', "
                    f"obtenido '{actual_intent.value}'"
                )
            if actual_clarification != turn.expect_clarification:
                reasons.append(
                    f"clarification_mismatch: esperado {turn.expect_clarification}, "
                    f"obtenido {actual_clarification}"
                )
            if (
                turn.expected_candidate_indices is not None
                and actual_candidates != turn.expected_candidate_indices
            ):
                reasons.append(
                    f"candidate_mismatch: esperados {list(turn.expected_candidate_indices)}, "
                    f"obtenidos {list(actual_candidates)}"
                )
            if (
                turn.expected_car_index is not None
                and turn.expected_car_index not in actual_car_indices
            ):
                reasons.append(
                    f"entity_mismatch: esperado coche {turn.expected_car_index}, "
                    f"obtenido {list(actual_car_indices) or 'ninguno'}"
                )

            if not reasons:
                turn_passed += 1
            else:
                seq_ok = False
                failures.append(
                    {
                        "split": "dialogue",
                        "id": f"{seq.case_id}#turn-{turn_idx}",
                        "text": turn.text,
                        "reason": "; ".join(reasons),
                        "expected": {
                            "intent": turn.expected_intent.value,
                            "car_index": turn.expected_car_index,
                            "clarification": turn.expect_clarification,
                        },
                        "actual": {
                            "intent": actual_intent.value,
                            "car_indices": list(actual_car_indices),
                            "clarification": actual_clarification,
                            "candidates": list(actual_candidates),
                        },
                    }
                )

            dialogue.record(PilotExchange(query=query, answer=answer), turn.t_s)
            if actual_intent is not Intent.UNKNOWN:
                last_intent = actual_intent

        if seq_ok:
            seq_passed += 1

    return seq_passed, turn_passed, total_turns


def _call_router(
    router: RouterCallable,
    text: str,
    *,
    language: str,
    drivers: Any,
    dialogue: Any | None,
    cfg: PilotConfig,
    now: float,
) -> RoutedQuery:
    if "now" in inspect.signature(router).parameters:
        return router(
            text,
            language=language,
            drivers=drivers,
            dialogue=dialogue,
            cfg=cfg,
            now=now,  # type: ignore[call-arg]
        )
    return router(
        text,
        language=language,
        drivers=drivers,
        dialogue=dialogue,
        cfg=cfg,
    )


def _drivers_for_case(
    grid: tuple[GridCarSpec, ...] | None,
    default_drivers: Any,
    dir_factory: DriverDirectoryFactory,
) -> Any:
    if grid is None:
        return default_drivers
    return _build_directory(
        dir_factory,
        build_race_state_from_grid(grid),
        grid,
    )


def _build_directory(
    dir_factory: DriverDirectoryFactory,
    state: Any,
    grid: Sequence[GridCarSpec],
) -> Any:
    numbers = {car.car_index: car.race_number for car in grid if car.race_number is not None}
    if not numbers:
        return dir_factory.from_state(state)
    try:
        return dir_factory.from_state(state, race_numbers=numbers)
    except TypeError:
        # Un doble de prueba puede no aceptar números; el evaluador sigue siendo válido.
        return dir_factory.from_state(state)


def _extract_car_index(resolved: Any) -> int | None:
    if resolved is None:
        return None
    if isinstance(resolved, int) and not isinstance(resolved, bool):
        return resolved
    car_index = getattr(resolved, "car_index", None)
    if isinstance(car_index, int) and not isinstance(car_index, bool):
        return car_index
    driver = getattr(resolved, "driver", None)
    car_index = getattr(driver, "car_index", None)
    if isinstance(car_index, int) and not isinstance(car_index, bool):
        return car_index
    return None


def _resolve_components(
    router: RouterCallable | None,
    directory_factory: DriverDirectoryFactory | None,
    dialogue_factory: DialogueFactory | None,
) -> tuple[RouterCallable, DriverDirectoryFactory, DialogueFactory]:
    if router is not None and directory_factory is not None and dialogue_factory is not None:
        return router, directory_factory, dialogue_factory
    try:
        router_mod = importlib.import_module(".router", package=__package__)
        entities_mod = importlib.import_module(".entities", package=__package__)
    except ImportError as exc:
        raise ValueError(
            "El evaluador requiere el enrutador y el resolvedor de entidades (Q3 y Q4)."
        ) from exc

    resolved_router = router if router is not None else router_mod.route
    resolved_dir = (
        directory_factory if directory_factory is not None else entities_mod.DriverDirectory
    )
    resolved_dlg: DialogueFactory = (
        dialogue_factory
        if dialogue_factory is not None
        else (lambda cfg: _instantiate_dialogue(entities_mod.DialogueState, cfg))
    )
    return resolved_router, resolved_dir, resolved_dlg


def _instantiate_dialogue(dialogue_cls: Any, cfg: PilotConfig) -> Any:
    try:
        return dialogue_cls(cfg)
    except TypeError:
        try:
            return dialogue_cls(
                max_turns=cfg.dialogue_turns,
                dialogue_ttl_s=cfg.dialogue_ttl_s,
                clarification_ttl_s=cfg.clarification_ttl_s,
            )
        except TypeError:
            return dialogue_cls()


def _rate(passed: int, total: int) -> float | None:
    return None if total == 0 else round(passed / total, 4)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Archivo de evaluación inválido ({path.name}): {exc}") from exc


def _req_str(data: Mapping[str, Any], key: str, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: '{key}' debe ser un texto no vacío")
    return value


def _opt_int(value: object, key: str, source: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{source}: '{key}' debe ser entero o null")
    return value


def _int_tuple(value: object, key: str, source: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ValueError(f"{source}: '{key}' debe ser una lista de enteros")
    return tuple(value)


def _str_tuple(value: object, key: str, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{source}: '{key}' debe ser una lista de textos")
    return tuple(value)


def _parse_grid(raw: object, source: str) -> tuple[GridCarSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{source}: 'cars' debe ser una lista no vacía")
    cars: list[GridCarSpec] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"{source}: cada coche debe ser un objeto JSON")
        car_index = _opt_int(item.get("car_index"), "car_index", source)
        position = _opt_int(item.get("position"), "position", source)
        if car_index is None or position is None:
            raise ValueError(f"{source}: cada coche requiere 'car_index' y 'position'")
        cars.append(
            GridCarSpec(
                car_index=car_index,
                name=_req_str(item, "name", source),
                team=_req_str(item, "team", source),
                position=position,
                race_number=_opt_int(item.get("race_number"), "race_number", source),
                result_status=str(item.get("result_status") or "active"),
                is_player=bool(item.get("is_player", False)),
            )
        )
    return tuple(cars)


def _parse_in_scope(raw: object) -> tuple[InScopeCase, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("in_scope.json debe ser una lista no vacía")
    seen: set[str] = set()
    cases: list[InScopeCase] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("in_scope.json: cada caso debe ser un objeto JSON")
        case_id = _req_str(item, "id", "in_scope.json")
        if case_id in seen:
            raise ValueError(f"in_scope.json: id duplicado '{case_id}'")
        seen.add(case_id)
        intent = Intent(_req_str(item, "expected_intent", "in_scope.json"))
        grid_raw = item.get("grid")
        cases.append(
            InScopeCase(
                case_id=case_id,
                text=_req_str(item, "text", "in_scope.json"),
                expected_intent=intent,
                expected_car_index=_opt_int(
                    item.get("expected_car_index"), "expected_car_index", "in_scope.json"
                ),
                expect_no_entity=bool(item.get("expect_no_entity", False)),
                grid=None if grid_raw is None else _parse_grid(grid_raw, "in_scope.json"),
                tags=_str_tuple(item.get("tags"), "tags", "in_scope.json"),
            )
        )
    return tuple(cases)


def _parse_out_of_scope(raw: object) -> tuple[OutOfScopeCase, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("out_of_scope.json debe ser una lista no vacía")
    seen: set[str] = set()
    cases: list[OutOfScopeCase] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("out_of_scope.json: cada caso debe ser un objeto JSON")
        case_id = _req_str(item, "id", "out_of_scope.json")
        if case_id in seen:
            raise ValueError(f"out_of_scope.json: id duplicado '{case_id}'")
        seen.add(case_id)
        grid_raw = item.get("grid")
        cases.append(
            OutOfScopeCase(
                case_id=case_id,
                text=_req_str(item, "text", "out_of_scope.json"),
                grid=None if grid_raw is None else _parse_grid(grid_raw, "out_of_scope.json"),
                tags=_str_tuple(item.get("tags"), "tags", "out_of_scope.json"),
            )
        )
    return tuple(cases)


def _parse_ambiguous(raw: object) -> tuple[AmbiguousCase, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("ambiguous.json debe ser una lista no vacía")
    seen: set[str] = set()
    cases: list[AmbiguousCase] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("ambiguous.json: cada caso debe ser un objeto JSON")
        case_id = _req_str(item, "id", "ambiguous.json")
        if case_id in seen:
            raise ValueError(f"ambiguous.json: id duplicado '{case_id}'")
        seen.add(case_id)
        intent_raw = item.get("expected_intent")
        intent = None if intent_raw is None else Intent(str(intent_raw))
        res_raw = item.get("resolution_text")
        if res_raw is not None and (not isinstance(res_raw, str) or not res_raw.strip()):
            raise ValueError(f"ambiguous.json: resolution_text inválido en {case_id}")
        grid_raw = item.get("grid")
        cases.append(
            AmbiguousCase(
                case_id=case_id,
                text=_req_str(item, "text", "ambiguous.json"),
                expected_candidate_indices=_int_tuple(
                    item.get("expected_candidate_indices"),
                    "expected_candidate_indices",
                    "ambiguous.json",
                ),
                expected_intent=intent,
                resolution_text=res_raw,
                expected_resolved_car_index=_opt_int(
                    item.get("expected_resolved_car_index"),
                    "expected_resolved_car_index",
                    "ambiguous.json",
                ),
                grid=None if grid_raw is None else _parse_grid(grid_raw, "ambiguous.json"),
            )
        )
    return tuple(cases)


def _parse_dialogues(raw: object) -> tuple[DialogueSequenceCase, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("dialogue_sequences.json debe ser una lista no vacía")
    seen: set[str] = set()
    sequences: list[DialogueSequenceCase] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("dialogue_sequences.json: cada secuencia debe ser un objeto JSON")
        case_id = _req_str(item, "id", "dialogue_sequences.json")
        if case_id in seen:
            raise ValueError(f"dialogue_sequences.json: id duplicado '{case_id}'")
        seen.add(case_id)
        turns_raw = item.get("turns")
        if not isinstance(turns_raw, list) or not turns_raw:
            raise ValueError(f"dialogue_sequences.json: '{case_id}' sin turnos")
        turns: list[DialogueTurnSpec] = []
        for turn in turns_raw:
            if not isinstance(turn, Mapping):
                raise ValueError(f"dialogue_sequences.json: turno inválido en '{case_id}'")
            t_s = turn.get("t_s")
            if isinstance(t_s, bool) or not isinstance(t_s, (int, float)):
                raise ValueError(f"dialogue_sequences.json: t_s inválido en '{case_id}'")
            cand_raw = turn.get("expected_candidate_indices")
            turns.append(
                DialogueTurnSpec(
                    text=_req_str(turn, "text", "dialogue_sequences.json"),
                    t_s=float(t_s),
                    expected_intent=Intent(
                        _req_str(turn, "expected_intent", "dialogue_sequences.json")
                    ),
                    expected_car_index=_opt_int(
                        turn.get("expected_car_index"),
                        "expected_car_index",
                        "dialogue_sequences.json",
                    ),
                    expect_clarification=bool(turn.get("expect_clarification", False)),
                    expected_candidate_indices=(
                        None
                        if cand_raw is None
                        else _int_tuple(
                            cand_raw, "expected_candidate_indices", "dialogue_sequences.json"
                        )
                    ),
                )
            )
        grid_raw = item.get("grid")
        sequences.append(
            DialogueSequenceCase(
                case_id=case_id,
                turns=tuple(turns),
                description=str(item.get("description") or ""),
                grid=None if grid_raw is None else _parse_grid(grid_raw, "dialogue_sequences.json"),
            )
        )
    return tuple(sequences)


__all__ = [
    "AmbiguousCase",
    "DEFAULT_PILOT_DATASET",
    "DialogueFactory",
    "DialogueProtocol",
    "DialogueSequenceCase",
    "DialogueTurnSpec",
    "DriverDirectoryFactory",
    "GridCarSpec",
    "InScopeCase",
    "OutOfScopeCase",
    "PILOT_EVAL_SCHEMA_VERSION",
    "PilotEvalDataset",
    "RouterCallable",
    "build_race_state_from_grid",
    "evaluation_report_json",
    "load_pilot_dataset",
    "run_pilot_evaluation",
]
