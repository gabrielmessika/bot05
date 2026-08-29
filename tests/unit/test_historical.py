from __future__ import annotations

from pathlib import Path

import pytest

from bot05.replay.historical import (
    HistoricalSmokeError,
    load_historical_smoke_spec,
)
from bot05.strategy import ConfirmationKind, TargetKind


def test_historical_smoke_spec_records_every_research_assumption() -> None:
    spec = load_historical_smoke_spec(
        Path("config/historical_smoke_btc_2026-08-21.toml")
    )

    assert spec.market == "BTC"
    assert spec.confirmation is ConfirmationKind.BREAKOUT
    assert spec.target is TargetKind.FIXED_2R
    assert spec.max_position_seconds == 7_200
    assert len(spec.config_sha256) == 64
    assert "unverified" in spec.calendar_assumption


def test_historical_smoke_spec_rejects_unknown_root_key(tmp_path: Path) -> None:
    source = Path("config/historical_smoke_btc_2026-08-21.toml").read_text()
    config = tmp_path / "invalid.toml"
    config.write_text(source + "\nunknown = true\n")

    with pytest.raises(HistoricalSmokeError, match="keys mismatch"):
        load_historical_smoke_spec(config)
