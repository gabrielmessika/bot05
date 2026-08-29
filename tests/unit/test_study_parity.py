from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.data.contracts import EvidenceTier, Qualification
from bot05.models import Candle, DatasetProvenance
from bot05.replay import ReplayModel
from bot05.studies import (
    MFE_HORIZONS_MINUTES,
    ExperimentSpec,
    ParityStatus,
    StudyContractError,
    StudyDataset,
    build_candle_parity_report,
    write_candle_parity_report,
)

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def _spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="d5-parity",
        preregistered_at=T0 - timedelta(days=1),
        universe=("xyz:GOLD",),
        sessions=("us_cash_open",),
        strategy_spec_ids=("1" * 64,),
        replay_models=tuple(ReplayModel),
        excursion_horizons_minutes=MFE_HORIZONS_MINUTES,
        selector_enabled=False,
        calendar_versions=("us_cash_2026:test",),
        config_sha256="2" * 64,
        code_version="d5-test",
    )


def _dataset(tier: EvidenceTier, instrument: str) -> StudyDataset:
    return StudyDataset(
        dataset_id=f"parity-{tier.value}",
        canonical_market="xyz:GOLD",
        source_instrument=instrument,
        tier=tier,
        qualification=Qualification.QUALIFIED,
        channels=("candles_5m",),
        period_start=T0,
        period_end=T0 + timedelta(hours=1),
        record_count=12,
        critical_gap_count=0,
        raw_sha256="3" * 64,
        manifest_sha256="4" * 64,
        derived_sha256="5" * 64,
        adapter_version="fixture-v1",
    )


def _provenance(market: str, tier: EvidenceTier) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id=f"parity-{tier.value}-{market}",
        evidence_tier=tier.value,
        source="synthetic",
        source_path_or_url="tests/unit/test_study_parity.py",
        raw_sha256="6" * 64,
        manifest_sha256="7" * 64,
        adapter_version="fixture-v1",
        calendar_version="test",
        code_version="test",
        config_sha256="8" * 64,
        source_timezone="UTC",
        period_start=T0,
        period_end=T0 + timedelta(hours=1),
    )


def _candle(
    market: str,
    tier: EvidenceTier,
    offset: int,
    prices: tuple[str, str, str, str],
) -> Candle:
    open_time = T0 + timedelta(minutes=5 * offset)
    return Candle(
        market=market,
        interval_seconds=300,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=5),
        closed_at=open_time + timedelta(minutes=5),
        open=Decimal(prices[0]),
        high=Decimal(prices[1]),
        low=Decimal(prices[2]),
        close=Decimal(prices[3]),
        volume=Decimal(1),
        trade_count=1,
        provenance=_provenance(market, tier),
    )


def test_parity_is_explicit_checksummed_and_never_merges_pnl(tmp_path) -> None:
    underlying_dataset = _dataset(EvidenceTier.UNDERLYING, "XAU/USD")
    hyperliquid_dataset = _dataset(EvidenceTier.HYPERLIQUID_CANDLES, "xyz:GOLD")
    underlying = (
        _candle("XAU/USD", EvidenceTier.UNDERLYING, 0, ("100", "102", "99", "101")),
    )
    hyperliquid = (
        _candle(
            "xyz:GOLD",
            EvidenceTier.HYPERLIQUID_CANDLES,
            0,
            ("101", "103.02", "99.99", "102.01"),
        ),
    )

    report = build_candle_parity_report(
        _spec(),
        canonical_market="xyz:GOLD",
        source_instrument="XAU/USD",
        session_id="us_cash_open",
        underlying_dataset=underlying_dataset,
        hyperliquid_dataset=hyperliquid_dataset,
        underlying_candles=underlying,
        hyperliquid_candles=hyperliquid,
        generated_at=T0 + timedelta(hours=1),
    )

    assert report.status is ParityStatus.COMMON_WINDOW_COMPLETE
    assert report.metrics.matched_count == 1
    assert report.metrics.mean_absolute_close_bps == Decimal(100)
    assert report.metrics.max_absolute_field_bps == Decimal(100)
    assert "PnL" in report.markdown()
    json_path, checksum_path, _ = write_candle_parity_report(report, tmp_path)
    assert hashlib.sha256(json_path.read_bytes()).hexdigest() in (
        checksum_path.read_text(encoding="utf-8")
    )


def test_parity_exposes_missing_buckets_and_rejects_legacy_target() -> None:
    underlying_dataset = _dataset(EvidenceTier.UNDERLYING, "XAU/USD")
    hyperliquid_dataset = _dataset(EvidenceTier.HYPERLIQUID_ARCHIVE, "xyz:GOLD")
    underlying = (
        _candle("XAU/USD", EvidenceTier.UNDERLYING, 0, ("100", "102", "99", "101")),
        _candle("XAU/USD", EvidenceTier.UNDERLYING, 1, ("101", "103", "100", "102")),
    )
    hyperliquid = (
        _candle(
            "xyz:GOLD",
            EvidenceTier.HYPERLIQUID_ARCHIVE,
            0,
            ("100", "102", "99", "101"),
        ),
    )

    report = build_candle_parity_report(
        _spec(),
        canonical_market="xyz:GOLD",
        source_instrument="XAU/USD",
        session_id="us_cash_open",
        underlying_dataset=underlying_dataset,
        hyperliquid_dataset=hyperliquid_dataset,
        underlying_candles=underlying,
        hyperliquid_candles=hyperliquid,
        generated_at=T0 + timedelta(hours=1),
    )

    assert report.status is ParityStatus.INCOMPLETE_COVERAGE
    assert report.metrics.missing_hyperliquid_count == 1

    with pytest.raises(StudyContractError, match="Hyperliquid evidence tier"):
        build_candle_parity_report(
            _spec(),
            canonical_market="xyz:GOLD",
            source_instrument="XAU/USD",
            session_id="us_cash_open",
            underlying_dataset=underlying_dataset,
            hyperliquid_dataset=replace(hyperliquid_dataset, tier=EvidenceTier.LEGACY),
            underlying_candles=underlying,
            hyperliquid_candles=hyperliquid,
            generated_at=T0 + timedelta(hours=1),
        )
