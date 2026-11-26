from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import f1_strategist.pilot.evaluation as eval_module
from f1_strategist.cli import main
from f1_strategist.config import PilotConfig
from f1_strategist.pilot.entities import normalize_text
from f1_strategist.pilot.evaluation import (
    DEFAULT_PILOT_DATASET,
    PilotEvalDataset,
    build_race_state_from_grid,
    load_pilot_dataset,
    run_pilot_evaluation,
)
from f1_strategist.pilot.types import (
    Clarification,
    DriverCandidate,
    EntityRef,
    Intent,
    PilotExchange,
    PilotQuery,
    RoutedQuery,
)
from f1_strategist.race_state import RaceStateSnapshot


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", without_marks)
    return " ".join(cleaned.split())


def _dataset_texts(dataset: PilotEvalDataset) -> set[str]:
    texts = [case.text for case in dataset.in_scope]
    texts.extend(case.text for case in dataset.out_of_scope)
    texts.extend(case.text for case in dataset.ambiguous)
    texts.extend(turn.text for sequence in dataset.dialogue_sequences for turn in sequence.turns)
    return {normalize_text(text) for text in texts}


@dataclass(frozen=True, slots=True)
class _FakeDriverDir:
    cars: tuple[DriverCandidate, ...]
    teams: dict[int, str]
    player_car_index: int | None

    @classmethod
    def from_state(cls, state: RaceStateSnapshot) -> _FakeDriverDir:
        cars: list[DriverCandidate] = []
        teams: dict[int, str] = {}
        for car in state.cars:
            if car.name is None:
                continue
            cars.append(
                DriverCandidate(
                    car_index=car.car_index,
                    name=car.name,
                    position=car.position,
                    team=car.team,
                )
            )
            if car.team:
                teams[car.car_index] = car.team
        cars.sort(key=lambda item: (item.position or 999, item.car_index))
        return cls(tuple(cars), teams, state.player_car_index)


class _FakeDialogue:
    def __init__(self, cfg: PilotConfig) -> None:
        self._cfg = cfg
        self._last_exchange: PilotExchange | None = None
        self._pending: Clarification | None = None

    def record(self, exchange: PilotExchange, now: float) -> None:
        self._last_exchange = exchange
        if exchange.answer.kind.value != "clarification":
            self._pending = None

    def set_pending(self, clarification: Clarification | None) -> None:
        self._pending = clarification

    def pending_clarification(self, now: float) -> Clarification | None:
        if self._pending is not None and now <= self._pending.expires_at:
            return self._pending
        return None

    def resolve_clarification(self, text: str, now: float) -> DriverCandidate | None:
        pending = self.pending_clarification(now)
        if pending is None:
            return None
        norm = _normalize(text)
        if norm in {"el primero", "primero", "1"}:
            return pending.options[0]
        if norm in {"el segundo", "segundo", "2"} and len(pending.options) > 1:
            return pending.options[1]
        for option in pending.options:
            if any(part == norm for part in _normalize(option.name).split()):
                return option
        number_map = {"4": 4, "5": 13, "16": 5, "25": 16, "55": 1, "81": 7, "2": 14}
        tokens = norm.split()
        if tokens and tokens[-1] in number_map:
            target = number_map[tokens[-1]]
            for option in pending.options:
                if option.car_index == target:
                    return option
        return None

    def last_active_intent(self, now: float) -> Intent | None:
        if self._last_exchange is None:
            return None
        if now - self._last_exchange.query.received_at > self._cfg.dialogue_ttl_s:
            return None
        if self._last_exchange.answer.intent is Intent.UNKNOWN:
            return None
        return self._last_exchange.answer.intent


_STOP_TOKENS = frozenset(
    {
        "acercando",
        "coche",
        "combustible",
        "como",
        "comparame",
        "con",
        "contra",
        "cuanto",
        "diferencia",
        "distancia",
        "esta",
        "estrategia",
        "frente",
        "funciona",
        "ganariamos",
        "hacer",
        "hueco",
        "intentar",
        "intervalo",
        "lleva",
        "neumaticos",
        "para",
        "paradas",
        "pasar",
        "podemos",
        "por",
        "que",
        "queda",
        "recorta",
        "respecto",
        "ritmo",
        "sale",
        "segundos",
        "tengo",
        "undercut",
        "viable",
        "voy",
    }
)


def _fake_resolve(
    norm: str, drivers: _FakeDriverDir
) -> tuple[list[EntityRef], Clarification | None]:
    player = next((c for c in drivers.cars if c.car_index == drivers.player_car_index), None)
    ordered = list(drivers.cars)
    if "de delante" in norm and player is not None:
        idx = ordered.index(player)
        if idx > 0:
            target = ordered[idx - 1]
            ref = EntityRef(raw="el de delante", car_index=target.car_index, candidates=(target,))
            return [ref], None
    if ("de atras" in norm or "de detras" in norm) and player is not None:
        idx = ordered.index(player)
        if idx + 1 < len(ordered):
            target = ordered[idx + 1]
            ref = EntityRef(raw="el de atras", car_index=target.car_index, candidates=(target,))
            return [ref], None
    if "lider" in norm:
        target = ordered[0]
        return [EntityRef(raw="el lider", car_index=target.car_index, candidates=(target,))], None
    if "companero" in norm and player is not None:
        mates = [c for c in ordered if c.team == player.team and c.car_index != player.car_index]
        if len(mates) == 1:
            ref = EntityRef(
                raw="mi companero", car_index=mates[0].car_index, candidates=(mates[0],)
            )
            return [ref], None
    if re.search(r"\b(?:el|al) 44\b", norm):
        match = next(c for c in ordered if c.car_index == 4)
        return [EntityRef(raw="44", car_index=4, candidates=(match,))], None
    tokens = norm.split()
    if "p2" in tokens:
        match = next(c for c in ordered if c.position == 2)
        return [EntityRef(raw="p2", car_index=match.car_index, candidates=(match,))], None
    if "p3" in tokens:
        match = next(c for c in ordered if c.position == 3)
        return [EntityRef(raw="p3", car_index=match.car_index, candidates=(match,))], None

    for team_name in ("ferrari", "mercedes", "mclaren", "red bull", "alpine"):
        if f"de {team_name}" in norm:
            matches = tuple(c for c in ordered if _normalize(c.team or "") == team_name)
            return [EntityRef(raw=team_name, candidates=matches)], Clarification(
                message_key="pilot.clarify_driver",
                options=matches,
                expires_at=30.0,
            )

    for token in tokens:
        if len(token) < 3 or token in _STOP_TOKENS:
            continue
        matches = tuple(
            c for c in ordered if any(part.startswith(token) for part in _normalize(c.name).split())
        )
        if len(matches) == 1:
            return [EntityRef(raw=token, car_index=matches[0].car_index, candidates=matches)], None
        if len(matches) > 1:
            return [EntityRef(raw=token, candidates=matches)], Clarification(
                message_key="pilot.clarify_driver",
                options=matches,
                expires_at=30.0,
            )
    return [], None


_OOS_PATTERN = re.compile(
    r"\b(?:mundial|titulos|cuesta|ayer|clasificacion|microsector|acelerador|record|"
    r"jefe de equipo|gasolinera|calle|madrid|restaurante|calendario|campeonato|"
    r"television|caballos|halo|edad|volumen|musica|londres|taller|pantalla|"
    r"barcelona|chiste|hora|hola|buenos dias|gracias)\b"
)


def _fake_router(
    text: str,
    *,
    language: str,
    drivers: _FakeDriverDir,
    dialogue: _FakeDialogue | None,
    cfg: PilotConfig,
    now: float = 0.0,
) -> RoutedQuery:
    norm = _normalize(text)
    entities, clarification = _fake_resolve(norm, drivers)
    if dialogue is not None:
        dialogue.set_pending(clarification)

    if _OOS_PATTERN.search(norm) or (
        "combustible le queda a sterling" in norm or "danos tiene el coche de verano" in norm
    ):
        intent, confidence = Intent.UNKNOWN, 0.2
    elif "undercut" in norm:
        intent, confidence = Intent.PIT_UNDERCUT, 0.95
    elif any(
        k in norm
        for k in (
            "estrategia",
            "strategia",
            "boxes",
            "paramos",
            "box",
            "si paro",
            "parar",
            "entramos ahora",
            "seguimos fuera",
            "plan de paradas",
        )
    ):
        intent, confidence = Intent.ASK_STRATEGY, 0.92
    elif any(k in norm for k in ("combustible", "gasolina", "deposito")):
        intent, confidence = Intent.FUEL_STATUS, 0.92
    elif any(
        k in norm
        for k in (
            "perdiendo tiempo",
            "pierdo tiempo",
            "tan lento",
            "diagnostico",
            "sector estoy perdiendo",
            "decimas",
            "bajamos tanto",
            "empeoraron",
            "caida de ritmo",
            "pasando con el ritmo",
        )
    ):
        intent, confidence = Intent.PACE_DIAGNOSIS, 0.90
    elif any(
        k in norm for k in ("dano", "danos", "danado", "aleron", "averia", "como esta el coche")
    ):
        intent, confidence = Intent.DAMAGE_STATUS, 0.91
    elif any(
        k in norm
        for k in (
            "llover",
            "lluvia",
            "prevision",
            "clima",
            "tiempo ahora",
            "cambiar el tiempo",
            "temperatura",
            "intermedios",
            "pronostico",
        )
    ):
        intent, confidence = Intent.WEATHER_FORECAST, 0.91
    elif any(
        k in norm
        for k in (
            "a cuanto",
            "distancia",
            "diferencia",
            "segundos esta",
            "intervalo",
            "acercando",
            "hueco",
            "recorta",
        )
    ):
        intent, confidence = Intent.GAP_STATUS, 0.90
    elif any(
        k in norm
        for k in (
            "respecto",
            "comparativa",
            "comparame",
            "mas rapido que",
            "frente a",
            "frente al",
            "neumaticos lleva",
            "paradas lleva",
            "ritmo lleva",
        )
    ):
        intent, confidence = Intent.COMPARE_DRIVER, 0.90
    elif any(
        k in norm
        for k in (
            "neumatico",
            "neumaticos",
            "ruedas",
            "gomas",
            "degradacion",
            "desgaste",
            "juego",
            "compuesto llevo",
        )
    ):
        intent, confidence = Intent.TYRE_LIFE, 0.92
    elif any(
        k in norm
        for k in (
            "como voy",
            "como vamos",
            "resumen",
            "posicion estoy",
            "situacion actual",
            "vueltas quedan",
            "vuelta estamos",
            "va la carrera",
            "estado general",
        )
    ):
        intent, confidence = Intent.GENERAL_RACE_STATE, 0.90
    elif dialogue is not None and (entities or clarification is not None):
        inherited = dialogue.last_active_intent(now)
        if inherited is not None:
            intent, confidence = inherited, 0.85
        else:
            intent, confidence = Intent.UNKNOWN, 0.2
    else:
        intent, confidence = Intent.UNKNOWN, 0.2

    query = PilotQuery(
        query_id="eval-q", text=text, language=language, received_at=now, source="eval"
    )
    return RoutedQuery(
        query=query,
        intent=intent,
        confidence=confidence,
        entities=tuple(entities),
        clarification=clarification,
        normalized_text=norm,
    )


def test_pilot_questions_dataset_meets_minimum_counts_and_covers_all_intents() -> None:
    dataset = load_pilot_dataset(DEFAULT_PILOT_DATASET)

    assert len(dataset.in_scope) >= 100
    assert len(dataset.out_of_scope) >= 30
    assert len(dataset.ambiguous) >= 15
    assert len(dataset.dialogue_sequences) >= 10

    expected_intents = {intent for intent in Intent if intent is not Intent.UNKNOWN}
    covered_intents = {case.expected_intent for case in dataset.in_scope}
    assert covered_intents == expected_intents

    state = build_race_state_from_grid(dataset.default_grid)
    assert state.player_car is not None
    assert state.player_car.position == 4


def test_run_pilot_evaluation_computes_metrics_and_reports_failing_phrases() -> None:
    dataset = load_pilot_dataset(DEFAULT_PILOT_DATASET)
    cfg = PilotConfig(language="es")

    perfect = run_pilot_evaluation(
        dataset,
        config=cfg,
        router=_fake_router,
        directory_factory=_FakeDriverDir,
        dialogue_factory=_FakeDialogue,
    )
    assert perfect["failures"] == []
    assert perfect["metrics"]["intent_accuracy"] == 1.0
    assert perfect["metrics"]["entity_accuracy"] == 1.0
    assert perfect["metrics"]["out_of_scope_false_positive_rate"] == 0.0
    assert perfect["metrics"]["clarification_accuracy"] == 1.0
    assert perfect["metrics"]["dialogue_sequence_accuracy"] == 1.0

    def faulty_router(
        text: str,
        *,
        language: str,
        drivers: Any,
        dialogue: Any,
        cfg: PilotConfig,
        now: float = 0.0,
    ) -> RoutedQuery:
        routed = _fake_router(
            text, language=language, drivers=drivers, dialogue=dialogue, cfg=cfg, now=now
        )
        if "combustible" in text.lower() and "gasolinera" in text.lower():
            return RoutedQuery(
                query=routed.query,
                intent=Intent.FUEL_STATUS,
                confidence=0.81,
                normalized_text=routed.normalized_text,
            )
        if text == "¿Cómo voy?":
            return RoutedQuery(
                query=routed.query,
                intent=Intent.UNKNOWN,
                confidence=0.3,
                normalized_text=routed.normalized_text,
            )
        return routed

    report = run_pilot_evaluation(
        dataset,
        config=cfg,
        router=faulty_router,
        directory_factory=_FakeDriverDir,
        dialogue_factory=_FakeDialogue,
    )

    metrics = report["metrics"]
    assert metrics["intent_accuracy"] < 1.0
    assert metrics["out_of_scope_false_positive_rate"] > 0.0
    assert metrics["clarification_accuracy"] == 1.0
    assert metrics["entity_accuracy"] == 1.0

    failures_by_id = {item["id"]: item for item in report["failures"]}
    assert "grs-01" in failures_by_id
    assert failures_by_id["grs-01"]["text"] == "¿Cómo voy?"
    assert "intent_mismatch" in failures_by_id["grs-01"]["reason"]
    assert "oos-17" in failures_by_id
    assert "false_positive" in failures_by_id["oos-17"]["reason"]


def test_cli_pilot_eval_outputs_json_and_report_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        eval_module,
        "_resolve_components",
        lambda *_args: (_fake_router, _FakeDriverDir, _FakeDialogue),
    )
    out_file = tmp_path / "pilot_eval.json"

    assert (
        main(
            [
                "pilot",
                "eval",
                "--dataset",
                str(DEFAULT_PILOT_DATASET),
                "--out",
                str(out_file),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert out_file.read_text(encoding="utf-8") == output
    report = json.loads(output)
    assert report["counts"]["in_scope"] >= 100
    assert report["counts"]["out_of_scope"] >= 30
    assert report["counts"]["ambiguous"] >= 15
    assert report["counts"]["dialogue_sequences"] >= 10

    assert main(["pilot", "eval", "--dataset", str(DEFAULT_PILOT_DATASET)]) == 0
    table_out = capsys.readouterr().out
    assert "Exactitud de intención" in table_out
    assert "Falsos positivos fuera de alcance" in table_out


def test_cli_pilot_eval_rejects_overwriting_dataset(tmp_path: Path) -> None:
    out_inside = DEFAULT_PILOT_DATASET / "report.json"
    assert (
        main(["pilot", "eval", "--dataset", str(DEFAULT_PILOT_DATASET), "--out", str(out_inside)])
        == 2
    )
    assert main(["pilot", "eval", "--dataset", str(tmp_path / "missing")]) == 2


def test_holdout_dataset_is_separate_and_evaluable() -> None:
    holdout = load_pilot_dataset(Path(__file__).parent / "fixtures" / "pilot_questions_holdout")
    training = load_pilot_dataset(DEFAULT_PILOT_DATASET)

    assert len(holdout.in_scope) >= 30
    assert len(holdout.out_of_scope) >= 10
    assert len(holdout.ambiguous) >= 5
    assert len(holdout.dialogue_sequences) >= 3
    assert not {case.text for case in holdout.in_scope} & {case.text for case in training.in_scope}

    report = run_pilot_evaluation(holdout)
    assert report["dataset"].endswith("pilot_questions_holdout")
    assert report["counts"]["in_scope"] == len(holdout.in_scope)


def test_validation_dataset_is_separate_from_training_and_development_sets() -> None:
    validation = load_pilot_dataset(
        Path(__file__).parent / "fixtures" / "pilot_questions_validation"
    )
    development = load_pilot_dataset(Path(__file__).parent / "fixtures" / "pilot_questions_holdout")
    training = load_pilot_dataset(DEFAULT_PILOT_DATASET)

    assert len(validation.in_scope) >= 30
    assert len(validation.out_of_scope) >= 10
    assert len(validation.ambiguous) >= 5
    assert len(validation.dialogue_sequences) >= 3
    used = {case.text for case in training.in_scope} | {case.text for case in development.in_scope}
    assert not {case.text for case in validation.in_scope} & used

    report = run_pilot_evaluation(validation)
    assert report["dataset"].endswith("pilot_questions_validation")
    assert report["counts"]["in_scope"] == len(validation.in_scope)


def test_independent_dataset_is_unseen_and_meets_phase_thresholds() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    independent = load_pilot_dataset(fixtures / "pilot_questions_independent")
    other_names = (
        "pilot_questions",
        "pilot_questions_holdout",
        "pilot_questions_validation",
        "pilot_questions_final",
    )
    others = [load_pilot_dataset(fixtures / name) for name in other_names]

    assert len(independent.in_scope) >= 30
    assert len(independent.out_of_scope) >= 10
    assert len(independent.ambiguous) >= 5
    assert len(independent.dialogue_sequences) >= 3
    seen = set().union(*(_dataset_texts(dataset) for dataset in others))
    assert not _dataset_texts(independent) & seen

    report = run_pilot_evaluation(independent)
    metrics = report["metrics"]
    assert metrics["intent_accuracy"] >= 0.95
    assert metrics["out_of_scope_false_positive_rate"] <= 0.05
    assert metrics["clarification_accuracy"] >= 0.95
    assert metrics["dialogue_turn_accuracy"] >= 0.95
