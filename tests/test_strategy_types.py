"""Tipos, configuración y esqueleto del motor de estrategia (S1)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.cli import main
from f1_strategist.config import (
    AppConfig,
    ConfigError,
    StrategyConfig,
    StrategyTrackOverride,
    load_config,
)
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.metrics.types import Quality
from f1_strategist.playback import PlaybackSession
from f1_strategist.session import CaptureSession
from f1_strategist.strategy.types import (
    ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED,
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    BASELINE_STAY_OUT,
    RISK_TRAFFIC_NOT_PRICED,
    STRATEGY_SCHEMA_VERSION,
    AlternativeResult,
    Perturbation,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
    compute_inputs_hash,
)


def _evaluation(**changes: object) -> StrategyEvaluation:
    evaluation = StrategyEvaluation(
        revision=3,
        session_uid="uid-1",
        lap=12,
        laps_remaining=8.0,
        baseline=BASELINE_STAY_OUT,
        recommendation=StrategyRecommendation(
            action=StrategyAction.PIT_NOW,
            key="pit_now:hard",
            pit_lap=12,
            compound="hard",
            gain_vs_baseline_s=3.5,
            gain_interval_s=(1.5, 5.5),
            confidence=0.7,
        ),
        alternatives=(
            StrategyAlternative(
                key="stay_out",
                remaining_time_s=1799.0,
                gain_vs_baseline_s=0.0,
                rejoin_position=None,
            ),
            StrategyAlternative(
                key="pit_now:hard",
                remaining_time_s=1795.5,
                gain_vs_baseline_s=3.5,
                rejoin_position=4,
                traffic_risk=False,
            ),
        ),
        assumptions=(ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED, "profile_prior_used"),
        risks=(RISK_TRAFFIC_NOT_PRICED, "tyre_damage_not_modelled"),
        sensitive_to=(ASSUMPTION_RIVALS_DO_NOT_PIT,),
        data_quality=Quality.OK,
        inputs_hash=compute_inputs_hash({"lap": 12, "compound": "medium"}),
        schema_version=STRATEGY_SCHEMA_VERSION,
        profile_hash="abc123",
    )
    return replace(evaluation, **changes)  # type: ignore[arg-type]


def test_evaluation_round_trips_through_json() -> None:
    evaluation = _evaluation()

    restored = json.loads(evaluation.to_json())

    assert restored["schema_version"] == STRATEGY_SCHEMA_VERSION
    assert restored["revision"] == 3
    assert restored["lap"] == 12
    assert restored["laps_remaining"] == 8.0
    assert restored["baseline"] == BASELINE_STAY_OUT
    assert restored["recommendation"]["action"] == "PIT_NOW"
    assert restored["recommendation"]["key"] == "pit_now:hard"
    assert restored["recommendation"]["gain_interval_s"] == [1.5, 5.5]
    assert restored["recommendation"]["confidence"] == 0.7
    assert [item["key"] for item in restored["alternatives"]] == ["pit_now:hard", "stay_out"]
    assert restored["data_quality"] == "ok"
    assert restored["profile_hash"] == "abc123"


def test_serialization_is_order_independent_and_canonical() -> None:
    first = _evaluation()
    second = _evaluation(
        alternatives=tuple(reversed(first.alternatives)),
        assumptions=tuple(reversed(first.assumptions)),
        risks=tuple(reversed(first.risks)),
        sensitive_to=tuple(reversed(first.sensitive_to)),
    )

    assert first.to_json() == first.to_json()
    assert first.to_json() == second.to_json()
    assert first.to_dict()["alternatives"][0]["key"] == "pit_now:hard"
    assert list(first.assumptions) == sorted(first.assumptions)
    assert list(first.risks) == sorted(first.risks)


def test_evaluation_round_trips_through_the_typed_contract() -> None:
    evaluation = _evaluation()

    restored = StrategyEvaluation.from_json(evaluation.to_json())

    assert restored == evaluation
    assert restored.to_json() == evaluation.to_json()


def test_evaluation_round_trips_projected_laps_and_player_identity() -> None:
    evaluation = _evaluation(
        forecast_car_index=2,
        forecast_compound="soft",
        projected_lap_times_s=(91.25, 91.5),
    )

    restored = StrategyEvaluation.from_json(evaluation.to_json())

    assert restored.forecast_car_index == 2
    assert restored.forecast_compound == "soft"
    assert restored.projected_lap_times_s == (91.25, 91.5)


def test_evaluation_reads_legacy_schema_v1_without_forecasts() -> None:
    payload = _evaluation().to_dict()
    payload["schema_version"] = 1
    payload.pop("forecast_car_index")
    payload.pop("forecast_compound")
    payload.pop("projected_lap_times_s")

    restored = StrategyEvaluation.from_dict(payload)

    assert restored.schema_version == 1
    assert restored.projected_lap_times_s is None


def test_evaluation_rejects_duplicate_alternative_keys() -> None:
    with pytest.raises(ValueError, match="duplicadas"):
        _evaluation(
            alternatives=(
                StrategyAlternative(
                    key="stay_out", remaining_time_s=1795.5, gain_vs_baseline_s=0.0
                ),
                StrategyAlternative(
                    key="stay_out", remaining_time_s=1795.5, gain_vs_baseline_s=1.0
                ),
            )
        )


def test_inputs_hash_is_order_independent_and_type_sensitive() -> None:
    payload = {"b": 2, "a": 1.0}
    reordered = {"a": 1.0, "b": 2}

    assert compute_inputs_hash(payload) == compute_inputs_hash(reordered)
    assert compute_inputs_hash(payload) != compute_inputs_hash({"b": 2, "a": 1.5})
    assert len(compute_inputs_hash(payload)) == 64


def test_inputs_hash_accepts_dataclasses_and_enums() -> None:
    payload = {"perturbation": Perturbation(slope_scale=1.1), "action": StrategyAction.PIT_NOW}

    hashed = compute_inputs_hash(payload)

    assert hashed == compute_inputs_hash(
        {"perturbation": {"slope_scale": 1.1, "pit_loss_delta_s": 0.0}, "action": "PIT_NOW"}
    )


def test_alternative_result_serializes_all_optional_fields() -> None:
    result = AlternativeResult(
        key="pit_in:3:soft",
        action=StrategyAction.PIT_IN,
        remaining_time_s=1801.25,
        gain_vs_baseline_s=-1.5,
        quality=Quality.LOW,
        pit_lap=15,
        compound="soft",
        set_index=2,
        projected_laps_s=1781.25,
        pit_loss_s=20.0,
        traffic_cost_s=None,
        rejoin_position=5,
        traffic_risk=True,
        notes=("low_samples",),
    )

    data = result.to_dict()

    assert data["action"] == "PIT_IN"
    assert data["quality"] == "low"
    assert data["notes"] == ["low_samples"]
    assert data["traffic_cost_s"] is None
    assert data["pit_lap"] == 15


@pytest.mark.parametrize(
    "changes",
    [
        {"slope_scale": 0.0},
        {"slope_scale": -1.0},
        {"pit_loss_delta_s": float("nan")},
    ],
)
def test_perturbation_rejects_invalid_values(changes: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        Perturbation(**changes)


def test_evaluation_rejects_inconsistent_data() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _evaluation(schema_version=99)
    with pytest.raises(ValueError, match="schema_version"):
        _evaluation(schema_version=True)
    with pytest.raises(ValueError, match="lap"):
        _evaluation(lap=0)
    with pytest.raises(ValueError, match="laps_remaining"):
        _evaluation(laps_remaining=-1.0)
    with pytest.raises(ValueError, match="confidence"):
        StrategyRecommendation(
            action=StrategyAction.NO_CHANGE,
            key="stay_out",
            confidence=1.5,
        )
    with pytest.raises(ValueError, match="gain_interval_s"):
        StrategyRecommendation(
            action=StrategyAction.NO_CHANGE,
            key="stay_out",
            gain_interval_s=(2.0, 1.0),
        )


@pytest.mark.parametrize("version", [None, True, 1.0, 1.5, 0, 3, 99])
def test_deserialization_rejects_invalid_schema_versions(version: object) -> None:
    payload = _evaluation().to_dict()
    payload["schema_version"] = version

    with pytest.raises(ValueError, match="schema_version"):
        StrategyEvaluation.from_dict(payload)


def test_deserialization_requires_schema_version() -> None:
    payload = _evaluation().to_dict()
    del payload["schema_version"]

    with pytest.raises(ValueError, match="schema_version"):
        StrategyEvaluation.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("revision", 3.5, "revision"),
        ("revision", True, "revision"),
        ("session_uid", 123, "session_uid"),
    ],
)
def test_deserialization_rejects_coerced_evaluation_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _evaluation().to_dict()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        StrategyEvaluation.from_dict(payload)


def test_strategy_snapshot_starts_empty() -> None:
    snapshot = StrategySnapshot()

    assert snapshot.evaluation is None
    assert not snapshot.available
    assert snapshot.error is None
    assert snapshot.updated_at_monotonic == 0.0

    filled = StrategySnapshot(evaluation=_evaluation(), updated_at_monotonic=12.5)
    assert filled.available
    assert filled.evaluation is not None


def test_strategy_config_defaults_match_the_plan() -> None:
    cfg = StrategyConfig()

    assert cfg.enabled is True
    assert cfg.evaluation_min_interval_s == 1.0
    assert cfg.min_gain_s == 1.0
    assert cfg.wear_slope_band_pct == 15.0
    assert cfg.pit_loss_band_s == 2.0
    assert cfg.prior_strength_k == 3.0
    assert cfg.max_pit_in_laps == 10
    assert cfg.require_two_dry_compounds is True
    assert cfg.traffic_cost_s_per_lap is None
    assert cfg.cliff_penalty_s_per_lap is None
    assert cfg.pit_loss_override_s is None
    assert cfg.tracks == {}


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("[strategy]\nmin_gain_s = -1.0\n", "strategy.min_gain_s"),
        ("[strategy]\nevaluation_min_interval_s = 0\n", "strategy.evaluation_min_interval_s"),
        ("[strategy]\nwear_slope_band_pct = 0\n", "strategy.wear_slope_band_pct"),
        ("[strategy]\nmax_pit_in_laps = 0\n", "strategy.max_pit_in_laps"),
        ("[strategy]\nprior_strength_k = -1\n", "strategy.prior_strength_k"),
        ("[strategy]\nunknown = 1\n", "strategy.unknown"),
    ],
)
def test_invalid_strategy_config_is_rejected(tmp_path: Path, section: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(section, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_strategy_track_overrides_are_applied_without_mutating_global_config() -> None:
    cfg = StrategyConfig(
        min_gain_s=1.0,
        tracks={
            "monza": StrategyTrackOverride(min_gain_s=1.5, pit_loss_override_s=20.5),
            "Suzuka": StrategyTrackOverride(),
        },
    )

    monza = cfg.for_track("MONZA")
    suzuka = cfg.for_track("suzuka")
    unknown = cfg.for_track("spa")
    none = cfg.for_track(None)

    assert monza.min_gain_s == 1.5
    assert monza.pit_loss_override_s == 20.5
    assert cfg.min_gain_s == 1.0
    assert cfg.pit_loss_override_s is None
    assert suzuka.min_gain_s == 1.0
    assert unknown is cfg
    assert none is cfg


def test_duplicate_track_names_after_normalization_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.toml"
    path.write_text(
        "[strategy.tracks.Monza]\nmin_gain_s = 1.0\n[strategy.tracks.monza]\nmin_gain_s = 2.0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Circuito duplicado"):
        load_config(path)


def test_example_config_loads_strategy_section() -> None:
    path = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"

    cfg = load_config(path)

    assert cfg.strategy.enabled is True
    assert cfg.strategy.wear_slope_band_pct == 15.0
    assert cfg.strategy.require_two_dry_compounds is True


def test_capture_session_exposes_an_empty_strategy_snapshot() -> None:
    session = CaptureSession(
        AppConfig(capture={"enabled": False}, recording={"enabled": False})  # type: ignore[arg-type]
    )

    snapshot = session.strategy_snapshot()

    assert isinstance(snapshot, StrategySnapshot)
    assert snapshot.evaluation is None
    assert not snapshot.available


def test_playback_session_exposes_an_empty_strategy_snapshot(tmp_path: Path) -> None:
    from f1_strategist.capture import CapturedPacket, CaptureWriter
    from tests.conftest import make_packet

    path = tmp_path / "capture.f1cap"
    with CaptureWriter(path) as writer:
        writer.write(
            CapturedPacket(
                recv_time_ns=0,
                source_ip="127.0.0.1",
                source_port=4000,
                payload=make_packet(1),
            )
        )
    session = PlaybackSession(path)

    snapshot = session.strategy_snapshot()

    assert isinstance(snapshot, StrategySnapshot)
    assert snapshot.evaluation is None


def test_cli_strategy_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["strategy"])

    assert exc_info.value.code == 2
    assert "required: strategy_command" in capsys.readouterr().err


def test_alternative_result_accepts_the_zero_set_index() -> None:
    result = AlternativeResult(
        key="pit_now:soft",
        action=StrategyAction.PIT_NOW,
        remaining_time_s=100.0,
        gain_vs_baseline_s=1.0,
        quality=Quality.OK,
        set_index=0,
    )

    assert result.set_index == 0
    with pytest.raises(ValueError, match="negativo"):
        replace(result, set_index=-1)


def test_inputs_hash_is_order_independent_for_read_only_mappings() -> None:
    from types import MappingProxyType

    first = LapHistorySnapshot(records={0: (), 1: ()})
    second = LapHistorySnapshot(records={1: (), 0: ()})

    assert first == second
    assert compute_inputs_hash(first) == compute_inputs_hash(second)
    assert compute_inputs_hash({"m": MappingProxyType({"b": 1, "a": 2})}) == compute_inputs_hash(
        {"m": MappingProxyType({"a": 2, "b": 1})}
    )
