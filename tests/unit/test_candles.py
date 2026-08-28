from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot05.data.contracts import TimeRange
from bot05.features.candles import (
    CandleGapReason,
    CandleParityIssueKind,
    aggregate_candles,
    aggregate_trades,
    compare_candle_series,
)
from bot05.models import AggressorSide, DatasetProvenance, Trade


def _provenance(start: datetime, end: datetime) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="derived-btc-candles",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url="/tmp/qualified-records.jsonl",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="bot05-trade-candles-v1",
        calendar_version="not_applicable:test",
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=start,
        period_end=end,
        transformations=("qualified_trades_to_candles_v1",),
    )


def _trade(
    provenance: DatasetProvenance,
    trade_id: str,
    timestamp: datetime,
    price: str,
    size: str = "1",
    delay_ms: int = 0,
) -> Trade:
    return Trade(
        market="BTC",
        trade_id=trade_id,
        exchange_time=timestamp,
        received_at=timestamp + timedelta(milliseconds=delay_ms),
        aggressor_side=AggressorSide.BUY,
        price=Decimal(price),
        size=Decimal(size),
        provenance=provenance,
    )


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_trade_aggregation_is_exchange_ordered_and_receive_time_causal() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    end = t0 + timedelta(minutes=2)
    provenance = _provenance(t0, end)
    requested = TimeRange(_ms(t0), _ms(end))
    trades = (
        _trade(provenance, "late", t0 + timedelta(seconds=50), "102", "3", 20_000),
        _trade(provenance, "open", t0 + timedelta(seconds=1), "100", "1"),
        _trade(provenance, "low", t0 + timedelta(seconds=20), "99", "2"),
        _trade(provenance, "second", t0 + timedelta(minutes=1, seconds=2), "101"),
    )

    result = aggregate_trades(
        trades,
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )

    assert result.complete
    assert len(result.candles) == 2
    first = result.candles[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("100"),
        Decimal("102"),
        Decimal("99"),
        Decimal("102"),
    )
    assert first.volume == Decimal("6")
    assert first.trade_count == 3
    assert first.closed_at == t0 + timedelta(seconds=70)


def test_trade_aggregation_exposes_partial_coverage_and_empty_bucket() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    end = t0 + timedelta(minutes=3)
    provenance = _provenance(t0, end)
    requested = TimeRange(_ms(t0), _ms(end))

    result = aggregate_trades(
        (_trade(provenance, "one", t0 + timedelta(seconds=1), "100"),),
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(TimeRange(_ms(t0), _ms(t0 + timedelta(minutes=2))),),
        provenance=provenance,
    )

    assert len(result.candles) == 1
    assert [item.reason for item in result.gaps] == [
        CandleGapReason.NO_TRADES,
        CandleGapReason.PARTIAL_COVERAGE,
    ]


def test_direct_5m_and_complete_1m_rollup_are_identical() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    end = t0 + timedelta(minutes=5)
    provenance = _provenance(t0, end)
    requested = TimeRange(_ms(t0), _ms(end))
    trades = tuple(
        _trade(
            provenance,
            f"trade-{minute}",
            t0 + timedelta(minutes=minute, seconds=1),
            str(100 + minute),
            str(minute + 1),
        )
        for minute in range(5)
    )
    one_minute = aggregate_trades(
        trades,
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    direct = aggregate_trades(
        trades,
        market="BTC",
        interval_seconds=300,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    rolled = aggregate_candles(
        one_minute.candles,
        market="BTC",
        interval_seconds=300,
        requested=requested,
        provenance=provenance,
    )

    assert one_minute.complete and direct.complete and rolled.complete
    assert rolled.candles == direct.candles


def test_rollup_refuses_to_hide_a_missing_component() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    end = t0 + timedelta(minutes=5)
    provenance = _provenance(t0, end)
    requested = TimeRange(_ms(t0), _ms(end))
    trades = tuple(
        _trade(
            provenance,
            f"trade-{minute}",
            t0 + timedelta(minutes=minute, seconds=1),
            str(100 + minute),
        )
        for minute in range(4)
    )
    one_minute = aggregate_trades(
        trades,
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )

    rolled = aggregate_candles(
        one_minute.candles,
        market="BTC",
        interval_seconds=300,
        requested=requested,
        provenance=provenance,
    )

    assert not rolled.candles
    assert rolled.gaps[0].reason is CandleGapReason.MISSING_COMPONENT_CANDLE


def test_official_parity_reports_missing_buckets_and_ohlcv_mismatch() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    end = t0 + timedelta(minutes=2)
    provenance = _provenance(t0, end)
    requested = TimeRange(_ms(t0), _ms(end))
    derived = aggregate_trades(
        (
            _trade(provenance, "one", t0 + timedelta(seconds=1), "100"),
            _trade(
                provenance,
                "two",
                t0 + timedelta(minutes=1, seconds=1),
                "101",
            ),
        ),
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    ).candles
    official = (
        derived[0],
        replace(derived[1], close=Decimal("101.5"), high=Decimal("101.5")),
        replace(
            derived[1],
            open_time=end,
            close_time=end + timedelta(minutes=1),
            closed_at=end + timedelta(minutes=1),
        ),
    )

    result = compare_candle_series(
        derived,
        official,
        market="BTC",
        interval_seconds=60,
    )

    assert not result.matches
    assert result.compared_count == 2
    assert [item.kind for item in result.issues] == [
        CandleParityIssueKind.FIELD_MISMATCH,
        CandleParityIssueKind.MISSING_DERIVED,
    ]
    assert result.issues[0].fields == ("high", "close")
