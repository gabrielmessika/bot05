"""Immutable, versioned market-data contracts used by replay and shadow paths."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DOMAIN_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ModelValidationError(ValueError):
    """Raised when a domain record violates a causal or market invariant."""


class SerializationError(ValueError):
    """Raised when a versioned domain envelope cannot be decoded safely."""


class AggressorSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketStatus(StrEnum):
    ACTIVE = "active"
    HALTED = "halted"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class MarginMode(StrEnum):
    CROSS = "cross"
    ISOLATED = "isolated"
    UNKNOWN = "unknown"


class ExternalPriceState(StrEnum):
    EXTERNAL = "external"
    INTERNAL = "internal"
    UNAVAILABLE = "unavailable"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ModelValidationError(f"{name} must be timezone-aware UTC")


def _require_sha256(value: str, name: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ModelValidationError(f"{name} must be a lowercase SHA-256 digest")


def _positive(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ModelValidationError(f"{name} must be finite and {qualifier}")


@dataclass(frozen=True, slots=True)
class DatasetProvenance:
    """Complete identity of one raw or derived dataset used by a record."""

    dataset_id: str
    evidence_tier: str
    source: str
    source_path_or_url: str
    raw_sha256: str
    manifest_sha256: str
    adapter_version: str
    calendar_version: str
    code_version: str
    config_sha256: str
    source_timezone: str
    period_start: datetime
    period_end: datetime
    transformations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("evidence_tier", self.evidence_tier),
            ("source", self.source),
            ("source_path_or_url", self.source_path_or_url),
            ("adapter_version", self.adapter_version),
            ("calendar_version", self.calendar_version),
            ("code_version", self.code_version),
        ):
            if not value.strip():
                raise ModelValidationError(f"{name} must not be blank")
        for name, value in (
            ("raw_sha256", self.raw_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("config_sha256", self.config_sha256),
        ):
            _require_sha256(value, name)
        _require_utc(self.period_start, "period_start")
        _require_utc(self.period_end, "period_end")
        if self.period_start >= self.period_end:
            raise ModelValidationError("provenance period must be non-empty")
        try:
            ZoneInfo(self.source_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ModelValidationError(
                "source_timezone must be an IANA timezone"
            ) from exc
        if len(set(self.transformations)) != len(self.transformations):
            raise ModelValidationError("transformations must be unique and ordered")


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    SCHEMA_VERSION: ClassVar[int] = DOMAIN_SCHEMA_VERSION

    market: str
    dex: str
    asset_id: int
    sz_decimals: int
    price_tick: Decimal
    size_step: Decimal
    max_leverage: Decimal
    margin_mode: MarginMode
    growth_mode: bool
    deployer_fee_scale: Decimal
    status: MarketStatus
    observed_at: datetime
    provenance: DatasetProvenance

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.dex.strip():
            raise ModelValidationError("market and dex must not be blank")
        if self.asset_id < 0 or self.sz_decimals < 0:
            raise ModelValidationError("asset_id and sz_decimals must be non-negative")
        _positive(self.price_tick, "price_tick")
        _positive(self.size_step, "size_step")
        _positive(self.max_leverage, "max_leverage")
        _positive(self.deployer_fee_scale, "deployer_fee_scale", allow_zero=True)
        _require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class Candle:
    SCHEMA_VERSION: ClassVar[int] = DOMAIN_SCHEMA_VERSION

    market: str
    interval_seconds: int
    open_time: datetime
    close_time: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None
    provenance: DatasetProvenance

    def __post_init__(self) -> None:
        if not self.market.strip() or self.interval_seconds <= 0:
            raise ModelValidationError(
                "market and positive interval_seconds are required"
            )
        for name, timestamp in (
            ("open_time", self.open_time),
            ("close_time", self.close_time),
            ("closed_at", self.closed_at),
        ):
            _require_utc(timestamp, name)
        if self.close_time - self.open_time != timedelta(seconds=self.interval_seconds):
            raise ModelValidationError("candle bounds must match interval_seconds")
        if self.closed_at < self.close_time:
            raise ModelValidationError("a candle cannot be observed before it closes")
        for name, price in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            _positive(price, name)
        _positive(self.volume, "volume", allow_zero=True)
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ModelValidationError("candle OHLC values are inconsistent")
        if self.low > self.high:
            raise ModelValidationError("candle low cannot exceed high")
        if self.trade_count is not None and self.trade_count < 0:
            raise ModelValidationError("trade_count must be non-negative")


@dataclass(frozen=True, slots=True)
class Trade:
    SCHEMA_VERSION: ClassVar[int] = DOMAIN_SCHEMA_VERSION

    market: str
    trade_id: str
    exchange_time: datetime
    received_at: datetime
    aggressor_side: AggressorSide
    price: Decimal
    size: Decimal
    provenance: DatasetProvenance

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.trade_id.strip():
            raise ModelValidationError("market and trade_id must not be blank")
        _require_utc(self.exchange_time, "exchange_time")
        _require_utc(self.received_at, "received_at")
        _positive(self.price, "price")
        _positive(self.size, "size")


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal
    order_count: int | None = None

    def __post_init__(self) -> None:
        _positive(self.price, "price")
        _positive(self.size, "size")
        if self.order_count is not None and self.order_count <= 0:
            raise ModelValidationError("order_count must be positive when present")


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    SCHEMA_VERSION: ClassVar[int] = DOMAIN_SCHEMA_VERSION

    market: str
    exchange_time: datetime
    received_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    provenance: DatasetProvenance

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ModelValidationError("market must not be blank")
        _require_utc(self.exchange_time, "exchange_time")
        _require_utc(self.received_at, "received_at")
        if not self.bids or not self.asks:
            raise ModelValidationError("book must contain bids and asks")
        if any(
            left.price <= right.price
            for left, right in zip(self.bids, self.bids[1:], strict=False)
        ):
            raise ModelValidationError("bids must be strictly descending")
        if any(
            left.price >= right.price
            for left, right in zip(self.asks, self.asks[1:], strict=False)
        ):
            raise ModelValidationError("asks must be strictly ascending")
        if self.bids[0].price >= self.asks[0].price:
            raise ModelValidationError("book must not be locked or crossed")


@dataclass(frozen=True, slots=True)
class MarketContext:
    SCHEMA_VERSION: ClassVar[int] = DOMAIN_SCHEMA_VERSION

    market: str
    observed_at: datetime
    mark_price: Decimal
    oracle_price: Decimal
    external_price: Decimal | None
    external_price_state: ExternalPriceState
    funding_rate: Decimal
    open_interest: Decimal
    day_volume: Decimal
    status: MarketStatus
    provenance: DatasetProvenance

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ModelValidationError("market must not be blank")
        _require_utc(self.observed_at, "observed_at")
        _positive(self.mark_price, "mark_price")
        _positive(self.oracle_price, "oracle_price")
        if self.external_price is not None:
            _positive(self.external_price, "external_price")
        if (
            self.external_price_state is ExternalPriceState.EXTERNAL
            and self.external_price is None
        ):
            raise ModelValidationError("external state requires an external price")
        if not self.funding_rate.is_finite():
            raise ModelValidationError("funding_rate must be finite")
        _positive(self.open_interest, "open_interest", allow_zero=True)
        _positive(self.day_volume, "day_volume", allow_zero=True)


DomainRecord: TypeAlias = (
    MarketDefinition | Candle | Trade | BookSnapshot | MarketContext
)


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        _require_utc(value, "serialized datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    raise SerializationError(f"unsupported value for canonical JSON: {type(value)!r}")


def domain_payload(record: DomainRecord) -> dict[str, JsonValue]:
    payload = _json_value(record)
    if not isinstance(payload, dict):
        raise SerializationError("domain payload must be an object")
    return payload


def encode_domain_record(record: DomainRecord) -> bytes:
    envelope: dict[str, JsonValue] = {
        "schema_version": DOMAIN_SCHEMA_VERSION,
        "record_type": type(record).__name__,
        "payload": domain_payload(record),
    }
    return (
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def domain_record_sha256(record: DomainRecord) -> str:
    return hashlib.sha256(encode_domain_record(record)).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SerializationError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _exact(payload: Mapping[str, object], name: str, expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        raise SerializationError(
            f"{name} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise SerializationError(f"{key} must be a string")
    return value


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SerializationError(f"{key} must be an integer")
    return value


def _boolean(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise SerializationError(f"{key} must be a boolean")
    return value


def _decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = _string(payload, key)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise SerializationError(f"{key} must be a decimal string") from exc


def _optional_decimal(payload: Mapping[str, object], key: str) -> Decimal | None:
    if payload.get(key) is None:
        return None
    return _decimal(payload, key)


def _datetime(payload: Mapping[str, object], key: str) -> datetime:
    value = _string(payload, key)
    if not value.endswith("Z"):
        raise SerializationError(f"{key} must have an explicit UTC Z suffix")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise SerializationError(f"{key} must be an ISO-8601 timestamp") from exc


_PROVENANCE_KEYS = {
    "dataset_id",
    "evidence_tier",
    "source",
    "source_path_or_url",
    "raw_sha256",
    "manifest_sha256",
    "adapter_version",
    "calendar_version",
    "code_version",
    "config_sha256",
    "source_timezone",
    "period_start",
    "period_end",
    "transformations",
}


def _provenance(value: object) -> DatasetProvenance:
    payload = _mapping(value, "provenance")
    _exact(payload, "provenance", _PROVENANCE_KEYS)
    transformations = payload.get("transformations")
    if not isinstance(transformations, list) or not all(
        isinstance(item, str) for item in transformations
    ):
        raise SerializationError("transformations must be an array of strings")
    return DatasetProvenance(
        dataset_id=_string(payload, "dataset_id"),
        evidence_tier=_string(payload, "evidence_tier"),
        source=_string(payload, "source"),
        source_path_or_url=_string(payload, "source_path_or_url"),
        raw_sha256=_string(payload, "raw_sha256"),
        manifest_sha256=_string(payload, "manifest_sha256"),
        adapter_version=_string(payload, "adapter_version"),
        calendar_version=_string(payload, "calendar_version"),
        code_version=_string(payload, "code_version"),
        config_sha256=_string(payload, "config_sha256"),
        source_timezone=_string(payload, "source_timezone"),
        period_start=_datetime(payload, "period_start"),
        period_end=_datetime(payload, "period_end"),
        transformations=tuple(cast(list[str], transformations)),
    )


def _decode_market_definition(payload: Mapping[str, object]) -> MarketDefinition:
    _exact(
        payload,
        "MarketDefinition",
        {
            "market",
            "dex",
            "asset_id",
            "sz_decimals",
            "price_tick",
            "size_step",
            "max_leverage",
            "margin_mode",
            "growth_mode",
            "deployer_fee_scale",
            "status",
            "observed_at",
            "provenance",
        },
    )
    return MarketDefinition(
        market=_string(payload, "market"),
        dex=_string(payload, "dex"),
        asset_id=_integer(payload, "asset_id"),
        sz_decimals=_integer(payload, "sz_decimals"),
        price_tick=_decimal(payload, "price_tick"),
        size_step=_decimal(payload, "size_step"),
        max_leverage=_decimal(payload, "max_leverage"),
        margin_mode=MarginMode(_string(payload, "margin_mode")),
        growth_mode=_boolean(payload, "growth_mode"),
        deployer_fee_scale=_decimal(payload, "deployer_fee_scale"),
        status=MarketStatus(_string(payload, "status")),
        observed_at=_datetime(payload, "observed_at"),
        provenance=_provenance(payload.get("provenance")),
    )


def _decode_candle(payload: Mapping[str, object]) -> Candle:
    _exact(
        payload,
        "Candle",
        {
            "market",
            "interval_seconds",
            "open_time",
            "close_time",
            "closed_at",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "provenance",
        },
    )
    trade_count = payload.get("trade_count")
    if trade_count is not None and (
        isinstance(trade_count, bool) or not isinstance(trade_count, int)
    ):
        raise SerializationError("trade_count must be an integer or null")
    return Candle(
        market=_string(payload, "market"),
        interval_seconds=_integer(payload, "interval_seconds"),
        open_time=_datetime(payload, "open_time"),
        close_time=_datetime(payload, "close_time"),
        closed_at=_datetime(payload, "closed_at"),
        open=_decimal(payload, "open"),
        high=_decimal(payload, "high"),
        low=_decimal(payload, "low"),
        close=_decimal(payload, "close"),
        volume=_decimal(payload, "volume"),
        trade_count=trade_count,
        provenance=_provenance(payload.get("provenance")),
    )


def _decode_trade(payload: Mapping[str, object]) -> Trade:
    _exact(
        payload,
        "Trade",
        {
            "market",
            "trade_id",
            "exchange_time",
            "received_at",
            "aggressor_side",
            "price",
            "size",
            "provenance",
        },
    )
    return Trade(
        market=_string(payload, "market"),
        trade_id=_string(payload, "trade_id"),
        exchange_time=_datetime(payload, "exchange_time"),
        received_at=_datetime(payload, "received_at"),
        aggressor_side=AggressorSide(_string(payload, "aggressor_side")),
        price=_decimal(payload, "price"),
        size=_decimal(payload, "size"),
        provenance=_provenance(payload.get("provenance")),
    )


def _book_levels(value: object, side: str) -> tuple[BookLevel, ...]:
    if not isinstance(value, list):
        raise SerializationError(f"{side} must be an array")
    result: list[BookLevel] = []
    for raw_level in value:
        payload = _mapping(raw_level, f"{side} level")
        _exact(payload, f"{side} level", {"price", "size", "order_count"})
        order_count = payload.get("order_count")
        if order_count is not None and (
            isinstance(order_count, bool) or not isinstance(order_count, int)
        ):
            raise SerializationError("order_count must be an integer or null")
        result.append(
            BookLevel(
                price=_decimal(payload, "price"),
                size=_decimal(payload, "size"),
                order_count=order_count,
            )
        )
    return tuple(result)


def _decode_book(payload: Mapping[str, object]) -> BookSnapshot:
    _exact(
        payload,
        "BookSnapshot",
        {"market", "exchange_time", "received_at", "bids", "asks", "provenance"},
    )
    return BookSnapshot(
        market=_string(payload, "market"),
        exchange_time=_datetime(payload, "exchange_time"),
        received_at=_datetime(payload, "received_at"),
        bids=_book_levels(payload.get("bids"), "bids"),
        asks=_book_levels(payload.get("asks"), "asks"),
        provenance=_provenance(payload.get("provenance")),
    )


def _decode_context(payload: Mapping[str, object]) -> MarketContext:
    _exact(
        payload,
        "MarketContext",
        {
            "market",
            "observed_at",
            "mark_price",
            "oracle_price",
            "external_price",
            "external_price_state",
            "funding_rate",
            "open_interest",
            "day_volume",
            "status",
            "provenance",
        },
    )
    return MarketContext(
        market=_string(payload, "market"),
        observed_at=_datetime(payload, "observed_at"),
        mark_price=_decimal(payload, "mark_price"),
        oracle_price=_decimal(payload, "oracle_price"),
        external_price=_optional_decimal(payload, "external_price"),
        external_price_state=ExternalPriceState(
            _string(payload, "external_price_state")
        ),
        funding_rate=_decimal(payload, "funding_rate"),
        open_interest=_decimal(payload, "open_interest"),
        day_volume=_decimal(payload, "day_volume"),
        status=MarketStatus(_string(payload, "status")),
        provenance=_provenance(payload.get("provenance")),
    )


def decode_domain_record(raw: bytes) -> DomainRecord:
    try:
        envelope = _mapping(json.loads(raw), "envelope")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SerializationError("invalid domain JSON") from exc
    _exact(envelope, "envelope", {"schema_version", "record_type", "payload"})
    if _integer(envelope, "schema_version") != DOMAIN_SCHEMA_VERSION:
        raise SerializationError("unsupported domain schema version")
    record_type = _string(envelope, "record_type")
    payload = _mapping(envelope.get("payload"), "payload")
    decoders: dict[str, Callable[[Mapping[str, object]], DomainRecord]] = {
        "MarketDefinition": _decode_market_definition,
        "Candle": _decode_candle,
        "Trade": _decode_trade,
        "BookSnapshot": _decode_book,
        "MarketContext": _decode_context,
    }
    decoder = decoders.get(record_type)
    if decoder is None:
        raise SerializationError(f"unsupported record type: {record_type}")
    try:
        return decoder(payload)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, SerializationError):
            raise
        raise SerializationError(f"invalid {record_type} payload") from exc
