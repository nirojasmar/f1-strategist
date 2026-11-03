from __future__ import annotations

from pathlib import Path

import pytest

from f1_strategist.config import (
    AppConfig,
    ConfigError,
    MetricsConfig,
    apply_overrides,
    load_config,
)


def test_defaults_are_loopback_and_standard_port() -> None:
    cfg = AppConfig()
    assert cfg.telemetry.listen_address == "127.0.0.1"
    assert cfg.telemetry.listen_port == 20777
    assert cfg.telemetry.is_loopback_only
    assert cfg.capture.enabled
    assert cfg.metrics.publish_interval_s == 0.5
    assert cfg.metrics.history_max_laps == 100
    assert cfg.metrics.pace_window_laps == 5
    assert cfg.metrics.fuel_window_laps == 5
    assert cfg.metrics.min_samples_low == 3
    assert cfg.metrics.min_samples_ok == 5
    assert cfg.metrics.traffic_gap_s == 1.0
    assert cfg.metrics.traffic_lap_fraction == 0.5
    assert cfg.metrics.gap_trend_window_s == 10.0
    assert cfg.metrics.damage_change_threshold_pct == 5
    assert cfg.metrics.fuel_effect_s_per_kg is None
    assert cfg.metrics.tyre_wear_limit_pct is None
    assert cfg.metrics.ers_capacity_joules is None


def test_load_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[telemetry]\nlisten_address = "0.0.0.0"\nallowed_sources = ["192.168.1.20"]\n'
        "[capture]\nenabled = false\n"
        "[metrics]\npublish_interval_s = 0.25\nhistory_max_laps = 80\n"
        "pace_window_laps = 4\nfuel_window_laps = 3\nmin_samples_low = 2\n"
        "min_samples_ok = 4\ntraffic_gap_s = 1.2\ntraffic_lap_fraction = 0.4\n"
        "gap_trend_window_s = 8.0\ndamage_change_threshold_pct = 7\n"
        "fuel_effect_s_per_kg = 0.02\ntyre_wear_limit_pct = 75\n"
        "ers_capacity_joules = 4000000\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert not cfg.telemetry.is_loopback_only
    assert cfg.telemetry.allowed_sources == ["192.168.1.20"]
    assert not cfg.capture.enabled
    assert cfg.metrics.publish_interval_s == 0.25
    assert cfg.metrics.history_max_laps == 80
    assert cfg.metrics.pace_window_laps == 4
    assert cfg.metrics.fuel_window_laps == 3
    assert cfg.metrics.min_samples_low == 2
    assert cfg.metrics.min_samples_ok == 4
    assert cfg.metrics.traffic_gap_s == 1.2
    assert cfg.metrics.traffic_lap_fraction == 0.4
    assert cfg.metrics.gap_trend_window_s == 8.0
    assert cfg.metrics.damage_change_threshold_pct == 7
    assert cfg.metrics.fuel_effect_s_per_kg == 0.02
    assert cfg.metrics.tyre_wear_limit_pct == 75
    assert cfg.metrics.ers_capacity_joules == 4000000


def test_optional_metrics_parameters_accept_none() -> None:
    cfg = MetricsConfig(
        fuel_effect_s_per_kg=None,
        tyre_wear_limit_pct=None,
        ers_capacity_joules=None,
    )

    assert cfg.fuel_effect_s_per_kg is None
    assert cfg.tyre_wear_limit_pct is None
    assert cfg.ers_capacity_joules is None


@pytest.mark.parametrize(
    "toml",
    [
        '[telemetry]\nlisten_address = "no-es-ip"\n',
        "[telemetry]\nlisten_port = 70000\n",
        '[telemetry]\nallowed_sources = ["999.1.1.1"]\n',
        "[telemetry]\nclave_desconocida = 1\n",
        "[metrics]\nclave_desconocida = 1\n",
        "[metrics]\npublish_interval_s = 0\n",
        "[metrics]\nhistory_max_laps = 0\n",
        "[metrics]\npace_window_laps = 0\n",
        "[metrics]\nfuel_window_laps = -1\n",
        "[metrics]\nmin_samples_low = 0\n",
        "[metrics]\nmin_samples_ok = 2\nmin_samples_low = 3\n",
        "[metrics]\ntraffic_gap_s = -0.1\n",
        "[metrics]\ntraffic_lap_fraction = 1.1\n",
        "[metrics]\ngap_trend_window_s = -1\n",
        "[metrics]\ndamage_change_threshold_pct = 101\n",
        "[metrics]\nfuel_effect_s_per_kg = -0.1\n",
        "[metrics]\ntyre_wear_limit_pct = -1\n",
        "[metrics]\ners_capacity_joules = 0\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "no-existe.toml")


def test_overrides_are_validated() -> None:
    cfg = apply_overrides(AppConfig(), listen_address="0.0.0.0", listen_port=30000)
    assert cfg.telemetry.listen_port == 30000
    with pytest.raises(ConfigError):
        apply_overrides(AppConfig(), listen_address="basura")
