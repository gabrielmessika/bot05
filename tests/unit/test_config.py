from __future__ import annotations

from pathlib import Path

import pytest

from bot05.config import ConfigurationError, UnsafeConfigurationError, load_config


def _configuration(tmp_path: Path, *, mode: str = "", data: str = "") -> Path:
    hyperbot_root = tmp_path / "hyperbot"
    trident_root = tmp_path / "trident"
    hyperbot_root.mkdir()
    trident_root.mkdir()
    path = tmp_path / "config" / "research.toml"
    path.parent.mkdir()
    path.write_text(
        f"""
[mode]
live_enabled = {"true" if mode == "live" else "false"}
shadow_only = {"false" if mode == "not_shadow" else "true"}
public_data_only = true

[data]
local_data_dir = "data"
hyperbot_root = "{hyperbot_root}"
trident_root = "{trident_root}"
local_first = {"false" if data == "remote_first" else "true"}
remote_fetch_enabled = false

[coverage]
start_utc = "2026-08-16T00:00:00Z"
end_utc = "2026-08-17T00:00:00Z"
channels = ["trades", "candles_5m"]

[universe]
markets = ["BTC", "HYPE"]

[reporting]
output_json = "reports/coverage.json"
output_markdown = "reports/coverage.md"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_baseline_configuration_is_safe_and_hashed(tmp_path: Path) -> None:
    loaded = load_config(_configuration(tmp_path))

    assert loaded.config.mode.live_enabled is False
    assert loaded.config.mode.shadow_only is True
    assert loaded.config.mode.public_data_only is True
    assert loaded.config.data.local_first is True
    assert loaded.config.reporting.output_json.is_absolute()
    assert len(loaded.sha256) == 64


@pytest.mark.parametrize("mode", ["live", "not_shadow"])
def test_unsafe_execution_modes_are_rejected(tmp_path: Path, mode: str) -> None:
    with pytest.raises(UnsafeConfigurationError):
        load_config(_configuration(tmp_path, mode=mode))


def test_remote_first_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafeConfigurationError, match="local_first"):
        load_config(_configuration(tmp_path, data="remote_first"))


def test_unknown_configuration_key_is_rejected(tmp_path: Path) -> None:
    path = _configuration(tmp_path)
    path.write_text(path.read_text() + "\nunknown = true\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="keys mismatch"):
        load_config(path)


def test_timestamp_requires_explicit_utc(tmp_path: Path) -> None:
    path = _configuration(tmp_path)
    content = path.read_text().replace("2026-08-16T00:00:00Z", "2026-08-16T00:00:00")
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="UTC Z suffix"):
        load_config(path)
