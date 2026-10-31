from __future__ import annotations

from pathlib import Path

import pytest

from f1_strategist.config import AppConfig, ConfigError, apply_overrides, load_config


def test_defaults_are_loopback_and_standard_port() -> None:
    cfg = AppConfig()
    assert cfg.telemetry.listen_address == "127.0.0.1"
    assert cfg.telemetry.listen_port == 20777
    assert cfg.telemetry.is_loopback_only
    assert cfg.capture.enabled


def test_load_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[telemetry]\nlisten_address = "0.0.0.0"\nallowed_sources = ["192.168.1.20"]\n'
        "[capture]\nenabled = false\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert not cfg.telemetry.is_loopback_only
    assert cfg.telemetry.allowed_sources == ["192.168.1.20"]
    assert not cfg.capture.enabled


@pytest.mark.parametrize(
    "toml",
    [
        '[telemetry]\nlisten_address = "no-es-ip"\n',
        "[telemetry]\nlisten_port = 70000\n",
        '[telemetry]\nallowed_sources = ["999.1.1.1"]\n',
        "[telemetry]\nclave_desconocida = 1\n",
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
