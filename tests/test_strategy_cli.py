from __future__ import annotations

import csv
import json
from pathlib import Path

from f1_strategist.cli import main
from f1_strategist.metrics.history import LapRecord
from f1_strategist.metrics.types import Quality
from f1_strategist.store import SessionInfo, SessionStore, StoredStrategyEvaluation
from f1_strategist.strategy.types import (
    STRATEGY_SCHEMA_VERSION,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
    StrategyRecommendation,
)


def _evaluation() -> StrategyEvaluation:
    return StrategyEvaluation(
        revision=0,
        session_uid="123",
        lap=1,
        laps_remaining=3.0,
        baseline="stay_out",
        recommendation=StrategyRecommendation(
            action=StrategyAction.NO_CHANGE,
            key="stay_out",
            confidence=0.8,
        ),
        alternatives=(
            StrategyAlternative(
                key="stay_out",
                remaining_time_s=270.0,
                gain_vs_baseline_s=0.0,
            ),
        ),
        assumptions=("fuel_effect_not_separated",),
        risks=("damage_not_modelled",),
        sensitive_to=("pit_loss_s",),
        data_quality=Quality.OK,
        inputs_hash="a" * 64,
        schema_version=STRATEGY_SCHEMA_VERSION,
        forecast_car_index=0,
        forecast_compound="soft",
        projected_lap_times_s=(90.0, 91.0, 92.0),
    )


def _stored_evaluation(evaluation: StrategyEvaluation) -> StoredStrategyEvaluation:
    return StoredStrategyEvaluation(
        seq=1,
        t_sim_s=1.0,
        lap=evaluation.lap,
        action=evaluation.recommendation.action.value,
        recommended_key=evaluation.recommendation.key,
        gain_vs_baseline_s=evaluation.recommendation.gain_vs_baseline_s,
        gain_low_s=None,
        gain_high_s=None,
        confidence=evaluation.recommendation.confidence,
        data_quality=evaluation.data_quality.value,
        inputs_hash=evaluation.inputs_hash,
        profile_hash=None,
        evaluation_json=evaluation.to_json(),
        forecast_car_index=evaluation.forecast_car_index,
        forecast_compound=evaluation.forecast_compound,
        projected_lap_times_s=evaluation.projected_lap_times_s,
    )


def _database(path: Path, *, with_evaluation: bool = True) -> int:
    with SessionStore.open(path) as store:
        session_id = store.create_session(
            SessionInfo(
                session_uid=123,
                source="ingest",
                started_at_utc="2026-11-12T10:00:00Z",
            )
        )
        if with_evaluation:
            evaluation = _evaluation()
            store.add_strategy_evaluations(session_id, [_stored_evaluation(evaluation)])
            store.upsert_laps(
                session_id,
                (
                    LapRecord(
                        car_index=0,
                        lap_number=2,
                        lap_time_s=91.0,
                        valid=True,
                        compound="soft",
                        closed=True,
                    ),
                    LapRecord(
                        car_index=0,
                        lap_number=3,
                        lap_time_s=90.5,
                        valid=True,
                        compound="soft",
                        closed=True,
                    ),
                    LapRecord(
                        car_index=0,
                        lap_number=4,
                        lap_time_s=90.0,
                        valid=True,
                        compound="soft",
                        pit_entry=True,
                        closed=True,
                    ),
                ),
            )
        return session_id


def test_strategy_show_export_and_review_use_read_only_session_data(
    tmp_path: Path,
    capsys,
) -> None:
    db = tmp_path / "sessions.db"
    session_id = _database(db)

    assert main(["strategy", "show", "--session", str(session_id), "--db", str(db)]) == 0
    shown = " ".join(capsys.readouterr().out.split())
    assert "Mantener" in shown
    assert "Ninguna parada superó" in shown
    assert "Daños del coche no modelados" in shown

    assert main(["strategy", "export", "--session", str(session_id), "--db", str(db)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["evaluations"][0]["projected_lap_times_s"] == [90.0, 91.0, 92.0]

    csv_path = tmp_path / "strategy.csv"
    assert (
        main(
            [
                "strategy",
                "export",
                "--session",
                str(session_id),
                "--format",
                "csv",
                "--out",
                str(csv_path),
                "--db",
                str(db),
            ]
        )
        == 0
    )
    csv_output = csv_path.read_text(encoding="utf-8")
    assert "projected_lap_times_s" in csv_output
    assert "[90.0,91.0,92.0]" in csv_output
    with csv_path.open(encoding="utf-8", newline="") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["action"] == "NO_CHANGE"
    assert csv_row["recommended_key"] == "stay_out"

    assert main(["strategy", "review", "--session", str(session_id), "--db", str(db)]) == 0
    review = capsys.readouterr().out
    assert "Evaluaciones utilizadas: 1/1" in review
    assert "1.000 s" in review
    assert "0.500 s" in review


def test_strategy_commands_explain_missing_session_and_empty_evaluations(
    tmp_path: Path,
    capsys,
) -> None:
    db = tmp_path / "sessions.db"
    session_id = _database(db, with_evaluation=False)

    assert main(["strategy", "show", "--session", str(session_id), "--db", str(db)]) == 0
    assert "no tiene evaluaciones" in capsys.readouterr().out
    assert main(["strategy", "review", "--session", str(session_id), "--db", str(db)]) == 0
    assert "no tiene evaluaciones" in capsys.readouterr().out

    assert main(["strategy", "show", "--session", "99", "--db", str(db)]) == 2
    assert "No existe la sesión 99" in capsys.readouterr().err
