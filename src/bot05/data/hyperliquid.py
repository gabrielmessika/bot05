"""Pure public-data adapters for Hyperliquid H0 and shared HyperBot H1 data."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from bot05.data.normalizer import (
    NormalizationError,
    NormalizedRecord,
    SourceIntegrityError,
    SourceRow,
    decode_json_object,
)
from bot05.models import (
    AggressorSide,
    BookLevel,
    BookSnapshot,
    Candle,
    DatasetProvenance,
    Trade,
)

HYPERBOT_SEGMENT_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_INTERVAL_SECONDS = {"1m": 60, "5m": 300}


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _exact(payload: Mapping[str, object], name: str, expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        raise NormalizationError(
            "source_schema_mismatch",
            f"{name} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}",
        )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise NormalizationError("invalid_shape", f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise NormalizationError("invalid_field", f"{key} must be a non-empty string")
    return value


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NormalizationError("invalid_field", f"{key} must be an integer")
    return value


def _decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str):
        raise NormalizationError("invalid_field", f"{key} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise NormalizationError(
            "invalid_field", f"{key} must be a decimal string"
        ) from exc
    if not parsed.is_finite():
        raise NormalizationError("invalid_field", f"{key} must be finite")
    return parsed


def _utc_ms(value: int, name: str) -> datetime:
    if value < 0:
        raise NormalizationError("invalid_timestamp", f"{name} must be non-negative")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise NormalizationError(
            "invalid_timestamp", f"{name} is outside the supported UTC range"
        ) from exc


def _book_level(value: object) -> BookLevel:
    payload = _mapping(value, "book level")
    _exact(payload, "book level", {"n", "px", "sz"})
    return BookLevel(
        price=_decimal(payload, "px"),
        size=_decimal(payload, "sz"),
        order_count=_integer(payload, "n"),
    )


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise NormalizationError("invalid_shape", f"{name} must be an array")
    return cast(list[object], value)


@dataclass(slots=True)
class HyperbotH1Adapter:
    """Validate a HyperBot hash chain and adapt public market-data records."""

    provenance: DatasetProvenance
    expected_sequence: int | None = None
    expected_previous_sha256: str | None = None
    _next_sequence: int | None = None
    _previous_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.provenance.evidence_tier != "H1":
            raise ValueError("HyperBot shared data must be classified H1 in BOT05")
        if self.expected_sequence is not None and self.expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if (
            self.expected_previous_sha256 is not None
            and _SHA256.fullmatch(self.expected_previous_sha256) is None
        ):
            raise ValueError("expected_previous_sha256 must be a SHA-256 digest")
        self._next_sequence = self.expected_sequence
        self._previous_sha256 = self.expected_previous_sha256

    def _validated_payload(self, row: SourceRow) -> tuple[Mapping[str, object], str]:
        record = decode_json_object(row)
        try:
            _exact(
                record,
                "HyperBot segment record",
                {
                    "schema_version",
                    "stream",
                    "sequence",
                    "recorded_at_ms",
                    "previous_record_sha256",
                    "event_type",
                    "payload_sha256",
                    "payload",
                    "record_sha256",
                },
            )
        except NormalizationError as exc:
            raise SourceIntegrityError(
                f"HyperBot record schema mismatch at row {row.index}"
            ) from exc
        if record.get("schema_version") != HYPERBOT_SEGMENT_SCHEMA_VERSION:
            raise SourceIntegrityError(
                f"unsupported HyperBot schema at row {row.index}"
            )
        if record.get("stream") != "public-market-data":
            raise SourceIntegrityError(f"unexpected HyperBot stream at row {row.index}")
        payload = _mapping(record.get("payload"), "HyperBot payload")
        payload_sha256 = record.get("payload_sha256")
        if (
            not isinstance(payload_sha256, str)
            or hashlib.sha256(_canonical_json(payload)).hexdigest() != payload_sha256
        ):
            raise SourceIntegrityError(f"payload checksum mismatch at row {row.index}")

        record_sha256 = record.get("record_sha256")
        if (
            not isinstance(record_sha256, str)
            or _SHA256.fullmatch(record_sha256) is None
        ):
            raise SourceIntegrityError(f"invalid record checksum at row {row.index}")
        base_record = dict(record)
        del base_record["record_sha256"]
        if hashlib.sha256(_canonical_json(base_record)).hexdigest() != record_sha256:
            raise SourceIntegrityError(f"record checksum mismatch at row {row.index}")

        sequence = _integer(record, "sequence")
        if self._next_sequence is not None and sequence != self._next_sequence:
            raise SourceIntegrityError(
                f"sequence gap at row {row.index}: "
                f"expected {self._next_sequence}, got {sequence}"
            )
        previous = _string(record, "previous_record_sha256")
        if _SHA256.fullmatch(previous) is None:
            raise SourceIntegrityError(f"invalid previous checksum at row {row.index}")
        if self._previous_sha256 is not None and previous != self._previous_sha256:
            raise SourceIntegrityError(f"record chain mismatch at row {row.index}")

        recorded_at_ms = _integer(record, "recorded_at_ms")
        if recorded_at_ms < 0:
            raise SourceIntegrityError(f"negative recorded time at row {row.index}")
        self._next_sequence = sequence + 1
        self._previous_sha256 = record_sha256
        if record.get("event_type") != "PublicMarketDataEvent":
            raise NormalizationError(
                "unsupported_event_type",
                "only PublicMarketDataEvent is accepted",
            )
        return payload, record_sha256

    def __call__(self, row: SourceRow) -> NormalizedRecord:
        payload, _ = self._validated_payload(row)
        _exact(
            payload,
            "PublicMarketDataEvent",
            {
                "context",
                "channel",
                "coin",
                "exchange_ts_ms",
                "receive_ts_ms",
                "receive_monotonic_ns",
                "local_sequence",
                "payload_json",
            },
        )
        context = _mapping(payload.get("context"), "event context")
        _exact(
            context,
            "event context",
            {"code_version", "config_hash", "run_id", "time_source"},
        )
        _string(context, "code_version")
        _string(context, "run_id")
        config_hash = _string(context, "config_hash")
        if _SHA256.fullmatch(config_hash) is None:
            raise NormalizationError(
                "invalid_context", "event config_hash must be a SHA-256"
            )
        if _string(context, "time_source") != "exchange":
            raise NormalizationError(
                "non_exchange_time_source",
                "H1 event is not timestamped from the exchange clock",
            )
        for field in ("receive_monotonic_ns", "local_sequence"):
            if _integer(payload, field) < 0:
                raise NormalizationError(
                    "invalid_field", f"{field} must be non-negative"
                )
        channel = _string(payload, "channel")
        market = _string(payload, "coin")
        exchange_ms = _integer(payload, "exchange_ts_ms")
        receive_ms = _integer(payload, "receive_ts_ms")
        inner_raw = _string(payload, "payload_json")
        try:
            inner_value = json.loads(inner_raw)
        except json.JSONDecodeError as exc:
            raise NormalizationError(
                "invalid_payload_json",
                "payload_json is not valid JSON",
                market=market,
                channel=channel,
            ) from exc
        inner = _mapping(inner_value, "payload_json")
        if _string(inner, "coin") != market:
            raise NormalizationError(
                "market_mismatch",
                "wrapper and payload markets differ",
                market=market,
                channel=channel,
            )
        inner_time = _integer(inner, "time")
        if inner_time != exchange_ms:
            raise NormalizationError(
                "timestamp_mismatch",
                "wrapper and payload exchange timestamps differ",
                market=market,
                channel=channel,
            )
        exchange_time = _utc_ms(exchange_ms, "exchange_ts_ms")
        received_at = _utc_ms(receive_ms, "receive_ts_ms")

        if channel == "trades":
            _exact(
                inner,
                "trade payload",
                {"coin", "hash", "px", "side", "sz", "tid", "time", "users"},
            )
            side = _string(inner, "side")
            if side not in {"A", "B"}:
                raise NormalizationError(
                    "invalid_side",
                    "Hyperliquid side must be A or B",
                    market=market,
                    channel=channel,
                )
            trade_id = _integer(inner, "tid")
            if trade_id < 0:
                raise NormalizationError(
                    "invalid_trade_id",
                    "trade id must be non-negative",
                    market=market,
                    channel=channel,
                )
            record: Trade | BookSnapshot = Trade(
                market=market,
                trade_id=str(trade_id),
                exchange_time=exchange_time,
                received_at=received_at,
                aggressor_side=(
                    AggressorSide.BUY if side == "B" else AggressorSide.SELL
                ),
                price=_decimal(inner, "px"),
                size=_decimal(inner, "sz"),
                provenance=self.provenance,
            )
        elif channel in {"bbo", "l2Book"}:
            levels_key = "bbo" if channel == "bbo" else "levels"
            _exact(inner, f"{channel} payload", {"coin", levels_key, "time"})
            sides = _array(inner.get(levels_key), levels_key)
            if len(sides) != 2:
                raise NormalizationError(
                    "invalid_book",
                    f"{levels_key} must contain bid and ask sides",
                    market=market,
                    channel=channel,
                )
            if channel == "bbo":
                bids: tuple[BookLevel, ...] = (_book_level(sides[0]),)
                asks: tuple[BookLevel, ...] = (_book_level(sides[1]),)
                normalized_channel = "bbo"
            else:
                raw_bids = _array(sides[0], "bids")
                raw_asks = _array(sides[1], "asks")
                bids = tuple(_book_level(level) for level in raw_bids)
                asks = tuple(_book_level(level) for level in raw_asks)
                normalized_channel = "l2"
            record = BookSnapshot(
                market=market,
                exchange_time=exchange_time,
                received_at=received_at,
                bids=bids,
                asks=asks,
                provenance=self.provenance,
            )
            channel = normalized_channel
        else:
            raise NormalizationError(
                "unsupported_channel",
                f"channel {channel!r} has no D1C adapter",
                market=market,
                channel=channel,
            )
        return NormalizedRecord(row.index, row.sha256, channel, record)


@dataclass(frozen=True, slots=True)
class H0CandleAdapter:
    """Adapt already-fetched public candleSnapshot rows without network access."""

    provenance: DatasetProvenance
    observed_at: datetime
    expected_market: str | None = None

    def __post_init__(self) -> None:
        if self.provenance.evidence_tier != "H0":
            raise ValueError("candleSnapshot data must be classified H0")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("observed_at must be timezone-aware UTC")

    def __call__(self, row: SourceRow) -> NormalizedRecord:
        payload = decode_json_object(row)
        _exact(
            payload,
            "candleSnapshot row",
            {"t", "T", "s", "i", "o", "c", "h", "l", "v", "n"},
        )
        market = _string(payload, "s")
        if self.expected_market is not None and market != self.expected_market:
            raise NormalizationError(
                "market_mismatch",
                f"expected {self.expected_market}, got {market}",
                market=market,
            )
        interval = _string(payload, "i")
        interval_seconds = _INTERVAL_SECONDS.get(interval)
        if interval_seconds is None:
            raise NormalizationError(
                "unsupported_interval",
                f"interval {interval!r} is not enabled in D1C",
                market=market,
            )
        open_ms = _integer(payload, "t")
        source_close_ms = _integer(payload, "T")
        close_ms = open_ms + (interval_seconds * 1000)
        if source_close_ms not in {close_ms - 1, close_ms}:
            raise NormalizationError(
                "candle_bounds_mismatch",
                "source close does not match the declared interval",
                market=market,
                channel=f"candles_{interval}",
            )
        record = Candle(
            market=market,
            interval_seconds=interval_seconds,
            open_time=_utc_ms(open_ms, "t"),
            close_time=_utc_ms(close_ms, "T"),
            closed_at=self.observed_at,
            open=_decimal(payload, "o"),
            high=_decimal(payload, "h"),
            low=_decimal(payload, "l"),
            close=_decimal(payload, "c"),
            volume=_decimal(payload, "v"),
            trade_count=_integer(payload, "n"),
            provenance=self.provenance,
        )
        return NormalizedRecord(
            row.index,
            row.sha256,
            f"candles_{interval}",
            record,
        )
