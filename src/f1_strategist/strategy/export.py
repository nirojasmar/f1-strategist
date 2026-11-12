"""Exportación de evaluaciones guardadas de estrategia."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import Any

from ..store.sqlite_store import StoredStrategyEvaluation
from .types import StrategyEvaluation

STRATEGY_EXPORT_FORMATS = ("json", "csv")

_CSV_COLUMNS = (
    "seq",
    "t_sim_s",
    "revision",
    "session_uid",
    "lap",
    "laps_remaining",
    "baseline",
    "action",
    "recommended_key",
    "pit_lap",
    "compound",
    "recommended_set_index",
    "gain_vs_baseline_s",
    "gain_interval_s",
    "confidence",
    "data_quality",
    "forecast_car_index",
    "forecast_compound",
    "projected_lap_times_s",
    "assumptions",
    "risks",
    "sensitive_to",
    "alternatives",
    "inputs_hash",
    "profile_hash",
)


def _export_row(item: StoredStrategyEvaluation) -> dict[str, Any]:
    evaluation = StrategyEvaluation.from_json(item.evaluation_json)
    payload = evaluation.to_dict()
    payload.update(
        seq=item.seq,
        t_sim_s=item.t_sim_s,
        forecast_car_index=item.forecast_car_index,
        forecast_compound=item.forecast_compound,
        projected_lap_times_s=(
            None if item.projected_lap_times_s is None else list(item.projected_lap_times_s)
        ),
    )
    return payload


def render_strategy_export(
    session_id: int,
    evaluations: Iterable[StoredStrategyEvaluation],
    export_format: str,
) -> str:
    """Serializa las evaluaciones en JSON o CSV."""
    normalized_format = export_format.lower()
    if normalized_format not in STRATEGY_EXPORT_FORMATS:
        valid = ", ".join(STRATEGY_EXPORT_FORMATS)
        raise ValueError(f"Formato de exportación no válido: {export_format!r}; usa {valid}")
    rows = [_export_row(item) for item in evaluations]
    if normalized_format == "json":
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "evaluations": rows,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        recommendation = row["recommendation"]
        csv_row = {column: row.get(column) for column in _CSV_COLUMNS}
        csv_row.update(
            action=recommendation["action"],
            recommended_key=recommendation["key"],
            pit_lap=recommendation["pit_lap"],
            compound=recommendation["compound"],
            recommended_set_index=recommendation["set_index"],
            gain_vs_baseline_s=recommendation["gain_vs_baseline_s"],
            gain_interval_s=recommendation["gain_interval_s"],
            confidence=recommendation["confidence"],
        )
        writer.writerow(
            {
                column: (
                    json.dumps(csv_row[column], ensure_ascii=False, separators=(",", ":"))
                    if isinstance(csv_row[column], (list, dict))
                    else ""
                    if csv_row[column] is None
                    else csv_row[column]
                )
                for column in _CSV_COLUMNS
            }
        )
    return output.getvalue()


__all__ = ["STRATEGY_EXPORT_FORMATS", "render_strategy_export"]
