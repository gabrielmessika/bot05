from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot05.data.hyperliquid import H0CandleAdapter, HyperbotH1Adapter
from bot05.data.legacy import LegacyJsonlAdapter
from bot05.data.normalizer import (
    SourceIntegrityError,
    SourceRow,
    iter_jsonl_rows,
    normalize_rows,
    source_row_from_mapping,
)
from bot05.models import (
    AggressorSide,
    BookSnapshot,
    Candle,
    DatasetProvenance,
    Trade,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _provenance(tier: str, source: str, dataset_id: str) -> DatasetProvenance:
    transformations = (
        ("received_at_assumed_exchange_time", "json_number_to_decimal_v1")
        if tier == "L"
        else ("source_schema_v2",)
    )
    return DatasetProvenance(
        dataset_id=dataset_id,
        evidence_tier=tier,
        source=source,
        source_path_or_url=f"/shared/{dataset_id}.jsonl",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version=f"bot05-{tier.lower()}-v1",
        calendar_version="test:2026:1234567890abcdef",
        code_version="0.1.0+test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=datetime(2026, 8, 16, tzinfo=UTC),
        period_end=datetime(2026, 8, 17, tzinfo=UTC),
        transformations=transformations,
    )


def _h1_row(
    index: int,
    *,
    sequence: int,
    previous: str,
    channel: str = "trades",
    trade_id: int = 42,
) -> tuple[SourceRow, str]:
    exchange_ms = 1_776_585_300_000
    if channel == "trades":
        inner: dict[str, object] = {
            "coin": "BTC",
            "hash": "0x" + ("0" * 64),
            "px": "68500.5",
            "side": "B",
            "sz": "0.0100",
            "tid": trade_id,
            "time": exchange_ms,
            "users": ["0xmaker", "0xtaker"],
        }
    else:
        inner = {
            "coin": "BTC",
            "levels": [
                [
                    {"n": 2, "px": "68500.0", "sz": "1.20"},
                    {"n": 1, "px": "68499.0", "sz": "0.50"},
                ],
                [
                    {"n": 3, "px": "68501.0", "sz": "2.10"},
                    {"n": 1, "px": "68502.0", "sz": "0.25"},
                ],
            ],
            "time": exchange_ms,
        }
    payload = {
        "context": {
            "code_version": "test",
            "config_hash": "c" * 64,
            "run_id": "fixture",
            "time_source": "exchange",
        },
        "channel": channel,
        "coin": "BTC",
        "exchange_ts_ms": exchange_ms,
        "receive_ts_ms": exchange_ms + 25,
        "receive_monotonic_ns": 123,
        "local_sequence": index - 1,
        "payload_json": _canonical(inner).decode(),
    }
    base = {
        "schema_version": 2,
        "stream": "public-market-data",
        "sequence": sequence,
        "recorded_at_ms": exchange_ms + 30,
        "previous_record_sha256": previous,
        "event_type": "PublicMarketDataEvent",
        "payload_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        "payload": payload,
    }
    record_sha256 = hashlib.sha256(_canonical(base)).hexdigest()
    return source_row_from_mapping(
        index, {**base, "record_sha256": record_sha256}
    ), record_sha256


def test_h0_candle_adapter_is_causal_and_preserves_decimal_strings() -> None:
    open_time = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
    open_ms = int(open_time.timestamp() * 1000)
    row = source_row_from_mapping(
        1,
        {
            "t": open_ms,
            "T": open_ms + 299_999,
            "s": "xyz:GOLD",
            "i": "5m",
            "o": "2500.10",
            "c": "2501.50",
            "h": "2502.00",
            "l": "2499.90",
            "v": "12.5000",
            "n": 42,
        },
    )
    adapter = H0CandleAdapter(
        _provenance("H0", "hyperliquid_public", "h0-gold"),
        observed_at=open_time + timedelta(minutes=6),
        expected_market="xyz:GOLD",
    )

    result = normalize_rows((row,), adapter)

    assert result.rejects == ()
    candle = result.domain_records[0]
    assert isinstance(candle, Candle)
    assert candle.close_time == open_time + timedelta(minutes=5)
    assert str(candle.volume) == "12.5000"
    assert result.channels == ("candles_5m",)


def test_hyperbot_h1_chain_normalizes_trade_and_book() -> None:
    first, first_hash = _h1_row(1, sequence=10, previous="0" * 64)
    second, _ = _h1_row(2, sequence=11, previous=first_hash, channel="l2Book")
    adapter = HyperbotH1Adapter(
        _provenance("H1", "hyperbot", "h1-btc"),
        expected_sequence=10,
        expected_previous_sha256="0" * 64,
    )

    result = normalize_rows((first, second), adapter)

    assert result.rejects == ()
    trade, book = result.domain_records
    assert isinstance(trade, Trade)
    assert trade.aggressor_side is AggressorSide.BUY
    assert isinstance(book, BookSnapshot)
    assert len(book.bids) == 2
    assert result.channels == ("l2", "trades")


def test_hyperbot_h1_sequence_gap_is_fatal_not_a_semantic_reject() -> None:
    row, _ = _h1_row(1, sequence=11, previous="0" * 64)
    adapter = HyperbotH1Adapter(
        _provenance("H1", "hyperbot", "h1-btc"), expected_sequence=10
    )

    with pytest.raises(SourceIntegrityError, match="sequence gap"):
        normalize_rows((row,), adapter)


def test_hyperbot_h1_payload_tampering_is_fatal() -> None:
    row, _ = _h1_row(1, sequence=10, previous="0" * 64)
    tampered = json.loads(row.raw)
    tampered["payload"]["payload_json"] = '{"coin":"BTC","time":0}'
    tampered_row = source_row_from_mapping(1, tampered)
    adapter = HyperbotH1Adapter(
        _provenance("H1", "hyperbot", "h1-btc"), expected_sequence=10
    )

    with pytest.raises(SourceIntegrityError, match="payload checksum"):
        normalize_rows((tampered_row,), adapter)


def test_exact_domain_duplicates_are_separated() -> None:
    first, first_hash = _h1_row(1, sequence=10, previous="0" * 64)
    second, _ = _h1_row(2, sequence=11, previous=first_hash)
    adapter = HyperbotH1Adapter(
        _provenance("H1", "hyperbot", "h1-btc"), expected_sequence=10
    )

    result = normalize_rows((first, second), adapter)

    assert len(result.records) == 1
    assert [item.code for item in result.rejects] == ["duplicate_domain_record"]


def test_legacy_trade_is_explicitly_limited_and_l2_is_rejected() -> None:
    provenance = _provenance("L", "trident", "legacy-btc")
    trade_row = source_row_from_mapping(
        1,
        {
            "timestamp": 1_775_037_039_202,
            "coin": "BTC",
            "price": 68600.0,
            "size": 0.00366,
            "is_buy": True,
        },
    )
    trade_result = normalize_rows(
        (trade_row,), LegacyJsonlAdapter(provenance, "trades", "BTC")
    )
    legacy_trade = trade_result.domain_records[0]
    assert isinstance(legacy_trade, Trade)
    assert legacy_trade.received_at == legacy_trade.exchange_time
    assert legacy_trade.trade_id.startswith("legacy-")

    l2_row = source_row_from_mapping(
        1,
        {
            "timestamp": 1_775_037_040_544,
            "coin": "BTC",
            "best_bid": 68610.0,
            "best_ask": 68611.0,
            "bid_depth_10bps": 3_043_053.19345,
            "ask_depth_10bps": 3_502_011.79,
            "spread_bps": 0.1457,
            "mid": 68610.5,
        },
    )
    l2_result = normalize_rows((l2_row,), LegacyJsonlAdapter(provenance, "l2", "BTC"))
    assert l2_result.records == ()
    assert l2_result.rejects[0].code == "legacy_l2_missing_top_sizes"


def test_jsonl_reader_checks_complete_source_before_streaming(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl.gz"
    with gzip.open(source, "wb") as handle:
        handle.write(b'{"coin":"BTC"}\n')
    before = source.read_bytes()
    digest = hashlib.sha256(before).hexdigest()

    rows = tuple(iter_jsonl_rows(source, expected_sha256=digest))

    assert len(rows) == 1
    assert source.read_bytes() == before
    with pytest.raises(SourceIntegrityError, match="checksum mismatch"):
        tuple(iter_jsonl_rows(source, expected_sha256="f" * 64))
