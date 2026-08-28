"""Read-only adapters for explicitly limited TRIDENT legacy research data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from bot05.data.normalizer import (
    NormalizationError,
    NormalizedRecord,
    SourceRow,
    decode_json_object,
)
from bot05.models import AggressorSide, DatasetProvenance, Trade


def _exact(payload: Mapping[str, object], expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        raise NormalizationError(
            "source_schema_mismatch",
            f"legacy keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}",
        )


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


def _boolean(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise NormalizationError("invalid_field", f"{key} must be a boolean")
    return value


def _decimal_number(payload: Mapping[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise NormalizationError(
            "invalid_field", f"{key} must be a JSON number or decimal string"
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise NormalizationError(
            "invalid_field", f"{key} must be a finite decimal"
        ) from exc
    if not parsed.is_finite():
        raise NormalizationError("invalid_field", f"{key} must be finite")
    return parsed


@dataclass(frozen=True, slots=True)
class LegacyJsonlAdapter:
    """Adapt one checksummed TRIDENT JSONL channel without runtime imports."""

    provenance: DatasetProvenance
    channel: str
    expected_market: str

    def __post_init__(self) -> None:
        if self.provenance.evidence_tier != "L":
            raise ValueError("TRIDENT shared data must remain classified L")
        if self.channel not in {"trades", "l2"}:
            raise ValueError("legacy D1C adapter supports only trades and l2")
        if not self.expected_market.strip():
            raise ValueError("expected_market must not be blank")

    def __call__(self, row: SourceRow) -> NormalizedRecord:
        payload = decode_json_object(row)
        market_value = payload.get("coin")
        market = market_value if isinstance(market_value, str) else None
        if self.channel == "l2":
            raise NormalizationError(
                "legacy_l2_missing_top_sizes",
                "legacy rows expose best prices and 10 bps depth, but no top-level "
                "sizes; BookSnapshot cannot be constructed without fabrication",
                market=market,
                channel="l2",
            )

        _exact(payload, {"timestamp", "coin", "price", "size", "is_buy"})
        normalized_market = _string(payload, "coin")
        if normalized_market != self.expected_market:
            raise NormalizationError(
                "market_mismatch",
                f"expected {self.expected_market}, got {normalized_market}",
                market=normalized_market,
                channel="trades",
            )
        timestamp_ms = _integer(payload, "timestamp")
        if timestamp_ms < 0:
            raise NormalizationError(
                "invalid_timestamp",
                "timestamp must be non-negative",
                market=normalized_market,
                channel="trades",
            )
        try:
            exchange_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise NormalizationError(
                "invalid_timestamp",
                "timestamp is outside the supported UTC range",
                market=normalized_market,
                channel="trades",
            ) from exc
        record = Trade(
            market=normalized_market,
            trade_id=f"legacy-{row.sha256[:24]}",
            exchange_time=exchange_time,
            received_at=exchange_time,
            aggressor_side=(
                AggressorSide.BUY if _boolean(payload, "is_buy") else AggressorSide.SELL
            ),
            price=_decimal_number(payload, "price"),
            size=_decimal_number(payload, "size"),
            provenance=self.provenance,
        )
        return NormalizedRecord(row.index, row.sha256, "trades", record)
