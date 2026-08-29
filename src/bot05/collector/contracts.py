"""Strict configuration contracts for a future public-only collector transport."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from bot05.collector.health import CollectorHealthError, FeedHealthPolicy


class PublicChannel(StrEnum):
    CANDLES_1M = "candles_1m"
    CANDLES_5M = "candles_5m"
    TRADES = "trades"
    BBO = "bbo"
    L2 = "l2"
    MARKET_CONTEXT = "market_context"
    MARKET_DEFINITION = "market_definition"


class AuthenticationMode(StrEnum):
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PublicCollectorSpec:
    markets: tuple[str, ...]
    channels: tuple[PublicChannel, ...]
    authentication: AuthenticationMode
    health_policy: FeedHealthPolicy
    reconnect_backoff_ms: int
    max_reconnect_backoff_ms: int

    def __post_init__(self) -> None:
        if not self.markets or any(not item.strip() for item in self.markets):
            raise CollectorHealthError("collector markets are required")
        if len(set(self.markets)) != len(self.markets):
            raise CollectorHealthError("collector markets must be unique")
        if not self.channels or len(set(self.channels)) != len(self.channels):
            raise CollectorHealthError(
                "collector channels must be unique and non-empty"
            )
        required = {
            PublicChannel.CANDLES_1M,
            PublicChannel.CANDLES_5M,
            PublicChannel.TRADES,
            PublicChannel.BBO,
            PublicChannel.MARKET_CONTEXT,
            PublicChannel.MARKET_DEFINITION,
        }
        if not required.issubset(self.channels):
            raise CollectorHealthError("collector is missing a required public channel")
        if self.authentication is not AuthenticationMode.NONE:
            raise CollectorHealthError("collector authentication must remain disabled")
        if (
            self.reconnect_backoff_ms <= 0
            or self.max_reconnect_backoff_ms < self.reconnect_backoff_ms
        ):
            raise CollectorHealthError("collector reconnect bounds are invalid")

    @property
    def spec_id(self) -> str:
        payload = {
            "authentication": self.authentication.value,
            "channels": [item.value for item in self.channels],
            "health_policy": {
                "max_clock_offset_ms": self.health_policy.max_clock_offset_ms,
                "max_staleness_ms": self.health_policy.max_staleness_ms,
                "max_transport_delay_ms": self.health_policy.max_transport_delay_ms,
            },
            "markets": list(self.markets),
            "max_reconnect_backoff_ms": self.max_reconnect_backoff_ms,
            "reconnect_backoff_ms": self.reconnect_backoff_ms,
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
