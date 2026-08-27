from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.models import (
    AggressorSide,
    BookLevel,
    BookSnapshot,
    Candle,
    DatasetProvenance,
    ExternalPriceState,
    MarginMode,
    MarketContext,
    MarketDefinition,
    MarketStatus,
    ModelValidationError,
    SerializationError,
    Trade,
    decode_domain_record,
    domain_record_sha256,
    encode_domain_record,
)


def _provenance() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="fixture-h1",
        evidence_tier="H1",
        source="hyperbot",
        source_path_or_url="/shared/source.jsonl.gz",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="bot05-hyperbot-v1",
        calendar_version="test:2026:1234567890abcdef",
        code_version="0.1.0+test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=datetime(2026, 8, 16, tzinfo=UTC),
        period_end=datetime(2026, 8, 17, tzinfo=UTC),
        transformations=("source_schema_v3",),
    )


def _records() -> tuple[object, ...]:
    provenance = _provenance()
    observed = datetime(2026, 8, 16, 9, 15, tzinfo=UTC)
    return (
        MarketDefinition(
            market="xyz:GOLD",
            dex="xyz",
            asset_id=110003,
            sz_decimals=4,
            price_tick=Decimal("0.1"),
            size_step=Decimal("0.0001"),
            max_leverage=Decimal("20"),
            margin_mode=MarginMode.ISOLATED,
            growth_mode=False,
            deployer_fee_scale=Decimal("1"),
            status=MarketStatus.ACTIVE,
            observed_at=observed,
            provenance=provenance,
        ),
        Candle(
            market="xyz:GOLD",
            interval_seconds=300,
            open_time=datetime(2026, 8, 16, 9, 10, tzinfo=UTC),
            close_time=observed,
            closed_at=observed,
            open=Decimal("2500.1"),
            high=Decimal("2502.0"),
            low=Decimal("2499.9"),
            close=Decimal("2501.5"),
            volume=Decimal("12.5000"),
            trade_count=42,
            provenance=provenance,
        ),
        Trade(
            market="xyz:GOLD",
            trade_id="trade-1",
            exchange_time=observed,
            received_at=observed + timedelta(milliseconds=25),
            aggressor_side=AggressorSide.BUY,
            price=Decimal("2501.5"),
            size=Decimal("0.10"),
            provenance=provenance,
        ),
        BookSnapshot(
            market="xyz:GOLD",
            exchange_time=observed,
            received_at=observed + timedelta(milliseconds=25),
            bids=(BookLevel(Decimal("2501.4"), Decimal("2"), 2),),
            asks=(BookLevel(Decimal("2501.5"), Decimal("3"), 3),),
            provenance=provenance,
        ),
        MarketContext(
            market="xyz:GOLD",
            observed_at=observed,
            mark_price=Decimal("2501.45"),
            oracle_price=Decimal("2501.40"),
            external_price=Decimal("2501.35"),
            external_price_state=ExternalPriceState.EXTERNAL,
            funding_rate=Decimal("-0.000001"),
            open_interest=Decimal("1000"),
            day_volume=Decimal("2500000"),
            status=MarketStatus.ACTIVE,
            provenance=provenance,
        ),
    )


@pytest.mark.parametrize("record", _records())
def test_versioned_domain_records_round_trip_bit_exact(record: object) -> None:
    raw = encode_domain_record(record)  # type: ignore[arg-type]

    decoded = decode_domain_record(raw)

    assert decoded == record
    assert encode_domain_record(decoded) == raw
    assert domain_record_sha256(decoded) == domain_record_sha256(record)  # type: ignore[arg-type]
    assert json.loads(raw)["schema_version"] == 1


def test_decimal_serialization_preserves_trailing_precision() -> None:
    candle = _records()[1]
    raw = encode_domain_record(candle)  # type: ignore[arg-type]

    assert b'"volume":"12.5000"' in raw
    assert b'"open":"2500.1"' in raw


def test_candle_must_be_closed_before_it_can_enter_the_domain() -> None:
    provenance = _provenance()
    open_time = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)

    with pytest.raises(ModelValidationError, match="before it closes"):
        Candle(
            market="BTC",
            interval_seconds=300,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=5),
            closed_at=open_time + timedelta(minutes=4, seconds=59),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("1"),
            trade_count=1,
            provenance=provenance,
        )


def test_candle_rejects_inconsistent_ohlc() -> None:
    provenance = _provenance()
    open_time = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)

    with pytest.raises(ModelValidationError, match="OHLC"):
        Candle(
            market="BTC",
            interval_seconds=300,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=5),
            closed_at=open_time + timedelta(minutes=5),
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("98"),
            close=Decimal("100.5"),
            volume=Decimal("1"),
            trade_count=1,
            provenance=provenance,
        )


def test_book_rejects_crossed_or_unsorted_levels() -> None:
    provenance = _provenance()
    now = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)

    with pytest.raises(ModelValidationError, match="locked or crossed"):
        BookSnapshot(
            market="BTC",
            exchange_time=now,
            received_at=now,
            bids=(BookLevel(Decimal("101"), Decimal("1")),),
            asks=(BookLevel(Decimal("100"), Decimal("1")),),
            provenance=provenance,
        )


def test_provenance_rejects_unknown_timezone_and_bad_checksum() -> None:
    values = _provenance()
    base = {field: getattr(values, field) for field in values.__dataclass_fields__}

    with pytest.raises(ModelValidationError, match="raw_sha256"):
        DatasetProvenance(**{**base, "raw_sha256": "not-a-hash"})
    with pytest.raises(ModelValidationError, match="IANA"):
        DatasetProvenance(**{**base, "source_timezone": "Mars/Olympus"})


def test_external_market_context_requires_external_price() -> None:
    with pytest.raises(ModelValidationError, match="requires an external price"):
        MarketContext(
            market="xyz:GOLD",
            observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
            mark_price=Decimal("1"),
            oracle_price=Decimal("1"),
            external_price=None,
            external_price_state=ExternalPriceState.EXTERNAL,
            funding_rate=Decimal("0"),
            open_interest=Decimal("0"),
            day_volume=Decimal("0"),
            status=MarketStatus.ACTIVE,
            provenance=_provenance(),
        )


def test_domain_models_are_immutable() -> None:
    trade = _records()[2]

    with pytest.raises(FrozenInstanceError):
        trade.price = Decimal("1")  # type: ignore[union-attr,misc]


def test_decoder_rejects_unknown_schema_and_extra_fields() -> None:
    raw = encode_domain_record(_records()[2])  # type: ignore[arg-type]
    envelope = json.loads(raw)
    envelope["schema_version"] = 2

    with pytest.raises(SerializationError, match="unsupported"):
        decode_domain_record(json.dumps(envelope).encode())

    envelope["schema_version"] = 1
    envelope["payload"]["future"] = True
    with pytest.raises(SerializationError, match="keys mismatch"):
        decode_domain_record(json.dumps(envelope).encode())
