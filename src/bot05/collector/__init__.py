"""Transport-neutral public collector health contracts."""

from bot05.collector.contracts import (
    AuthenticationMode,
    PublicChannel,
    PublicCollectorSpec,
)
from bot05.collector.health import (
    CollectorHealthError,
    FeedHealthCode,
    FeedHealthDecision,
    FeedHealthPolicy,
    FeedHealthState,
    GapRepairEvidence,
    connect_feed,
    disconnect_feed,
    evaluate_feed_health,
    initial_feed_health,
    observe_clock_offset,
    observe_public_record,
    repair_sequence_gap,
)

__all__ = [
    "CollectorHealthError",
    "AuthenticationMode",
    "FeedHealthCode",
    "FeedHealthDecision",
    "FeedHealthPolicy",
    "FeedHealthState",
    "GapRepairEvidence",
    "PublicChannel",
    "PublicCollectorSpec",
    "connect_feed",
    "disconnect_feed",
    "evaluate_feed_health",
    "initial_feed_health",
    "observe_clock_offset",
    "observe_public_record",
    "repair_sequence_gap",
]
