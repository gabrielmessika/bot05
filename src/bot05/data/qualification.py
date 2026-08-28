"""Deterministic event-level qualification for bounded HyperBot H1 segments."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bot05.data.contracts import TimeRange
from bot05.data.hyperliquid import HyperbotH1Adapter
from bot05.data.normalizer import (
    NormalizationResult,
    file_sha256,
    iter_jsonl_rows,
    normalize_rows,
)
from bot05.data.store import StoredSegment
from bot05.models import BookSnapshot, DatasetProvenance, Trade

QUALIFICATION_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class QualificationError(ValueError):
    """Raised when source metadata or qualification output is inconsistent."""


class QualificationReportExistsError(QualificationError):
    """Raised when a different report already occupies an immutable path."""


@dataclass(frozen=True, slots=True)
class HyperbotSegmentSpec:
    manifest_path: Path
    manifest_sha256: str
    source_path: Path
    storage_sha256: str
    content_sha256: str
    begin_recorded_at_ms: int
    end_recorded_at_ms: int
    record_count: int
    first_sequence: int
    last_sequence: int
    expected_previous_record_sha256: str
    first_record_sha256: str
    last_record_sha256: str

    @property
    def source_period(self) -> TimeRange:
        return TimeRange(self.begin_recorded_at_ms, self.end_recorded_at_ms + 1)


@dataclass(frozen=True, slots=True)
class ChannelAudit:
    channel: str
    record_count: int
    first_exchange_ms: int | None
    last_exchange_ms: int | None
    max_inter_event_gap_ms: int | None


@dataclass(frozen=True, slots=True)
class SegmentAudit:
    spec: HyperbotSegmentSpec
    market: str
    channels: tuple[str, ...]
    window: TimeRange
    max_heartbeat_gap_ms: int
    result: NormalizationResult
    channel_audits: tuple[ChannelAudit, ...]
    critical_gap_count: int
    duplicate_count: int

    @property
    def qualified(self) -> bool:
        return (
            self.critical_gap_count == 0
            and self.duplicate_count == 0
            and not self.result.rejects
        )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise QualificationError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualificationError(f"{key} must be an integer")
    return value


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{key} must be a non-empty string")
    return value


def _sha256(payload: Mapping[str, object], key: str) -> str:
    value = _string(payload, key)
    if _SHA256.fullmatch(value) is None:
        raise QualificationError(f"{key} must be a SHA-256 digest")
    return value


def load_hyperbot_segment_spec(
    manifest_path: Path, segment_name: str
) -> HyperbotSegmentSpec:
    """Resolve one physical segment and its cross-segment chain expectations."""

    resolved_manifest = manifest_path.resolve()
    try:
        document = json.loads(resolved_manifest.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"invalid source manifest: {manifest_path}") from exc
    root = _mapping(document, "source manifest")
    if root.get("manifest_schema_version") != 1:
        raise QualificationError("unsupported HyperBot manifest schema")
    if root.get("stream") != "public-market-data":
        raise QualificationError("source manifest is not public-market-data")
    config = _mapping(root.get("config"), "source manifest config")
    if config.get("record_schema_version") != 2:
        raise QualificationError("unsupported HyperBot record schema")
    raw_segments = root.get("segments")
    if not isinstance(raw_segments, list):
        raise QualificationError("source manifest segments must be an array")
    segments = [
        _mapping(item, "source segment") for item in cast(list[object], raw_segments)
    ]
    matches = [item for item in segments if item.get("path") == segment_name]
    if len(matches) != 1:
        raise QualificationError(
            f"expected exactly one source segment named {segment_name!r}"
        )
    target = matches[0]
    first_sequence = _integer(target, "first_sequence")
    last_sequence = _integer(target, "last_sequence")
    record_count = _integer(target, "record_count")
    if first_sequence < 0 or last_sequence < first_sequence:
        raise QualificationError("source sequence bounds are invalid")
    if last_sequence - first_sequence + 1 != record_count:
        raise QualificationError("source record count disagrees with sequence bounds")

    if first_sequence == 0:
        expected_previous = "0" * 64
    else:
        previous_matches = [
            item for item in segments if item.get("last_sequence") == first_sequence - 1
        ]
        if len(previous_matches) != 1:
            raise QualificationError("previous source segment cannot be resolved")
        previous = previous_matches[0]
        expected_previous = _sha256(previous, "last_record_sha256")
        if target.get("previous_segment_sha256") != previous.get("content_sha256"):
            raise QualificationError("source segment content chain is inconsistent")

    source_path = (resolved_manifest.parent / segment_name).resolve()
    try:
        source_path.relative_to(resolved_manifest.parent)
    except ValueError as exc:
        raise QualificationError(
            "source segment escapes its manifest directory"
        ) from exc
    if not source_path.is_file():
        raise QualificationError(f"source segment is missing: {source_path}")
    begin_recorded_at_ms = _integer(target, "begin_recorded_at_ms")
    end_recorded_at_ms = _integer(target, "end_recorded_at_ms")
    if begin_recorded_at_ms < 0 or begin_recorded_at_ms > end_recorded_at_ms:
        raise QualificationError("source recorded-time bounds are invalid")
    return HyperbotSegmentSpec(
        manifest_path=resolved_manifest,
        manifest_sha256=file_sha256(resolved_manifest),
        source_path=source_path,
        storage_sha256=_sha256(target, "storage_sha256"),
        content_sha256=_sha256(target, "content_sha256"),
        begin_recorded_at_ms=begin_recorded_at_ms,
        end_recorded_at_ms=end_recorded_at_ms,
        record_count=record_count,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        expected_previous_record_sha256=expected_previous,
        first_record_sha256=_sha256(target, "first_record_sha256"),
        last_record_sha256=_sha256(target, "last_record_sha256"),
    )


def provenance_for_hyperbot_segment(
    spec: HyperbotSegmentSpec,
    *,
    dataset_id: str,
    code_version: str,
    config_sha256: str,
    market: str,
    channels: tuple[str, ...],
) -> DatasetProvenance:
    start = datetime.fromtimestamp(spec.begin_recorded_at_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp((spec.end_recorded_at_ms + 1) / 1000, tz=UTC)
    return DatasetProvenance(
        dataset_id=dataset_id,
        evidence_tier="H1",
        source="hyperbot",
        source_path_or_url=str(spec.source_path),
        raw_sha256=spec.storage_sha256,
        manifest_sha256=spec.manifest_sha256,
        adapter_version="bot05-hyperbot-h1-v1",
        calendar_version="not_applicable:data_qualification:v1",
        code_version=code_version,
        config_sha256=config_sha256,
        source_timezone="UTC",
        period_start=start,
        period_end=end,
        transformations=(
            "hyperbot_segment_schema_v2",
            f"filter_market_{market}",
            f"filter_channels_{'_'.join(channels)}",
        ),
    )


def _record_exchange_ms(record: Trade | BookSnapshot) -> int:
    return int(record.exchange_time.timestamp() * 1000)


def _channel_audit(result: NormalizationResult, channel: str) -> ChannelAudit:
    times = sorted(
        _record_exchange_ms(item.record)
        for item in result.records
        if item.source_channel == channel
        and isinstance(item.record, Trade | BookSnapshot)
    )
    gaps = [right - left for left, right in zip(times, times[1:], strict=False)]
    return ChannelAudit(
        channel=channel,
        record_count=len(times),
        first_exchange_ms=times[0] if times else None,
        last_exchange_ms=times[-1] if times else None,
        max_inter_event_gap_ms=max(gaps) if gaps else None,
    )


def audit_hyperbot_segment(
    spec: HyperbotSegmentSpec,
    provenance: DatasetProvenance,
    *,
    market: str,
    channels: tuple[str, ...],
    window: TimeRange,
    max_heartbeat_gap_ms: int = 15_000,
) -> SegmentAudit:
    """Validate every source row and retain only the preregistered scope."""

    if not market.strip() or not channels or len(set(channels)) != len(channels):
        raise QualificationError("market and unique channels are required")
    if "bbo" not in channels:
        raise QualificationError("bbo is required as the collector heartbeat")
    if any(channel not in {"trades", "bbo", "l2Book"} for channel in channels):
        raise QualificationError("unsupported H1 qualification channel")
    if max_heartbeat_gap_ms <= 0:
        raise QualificationError("max heartbeat gap must be positive")
    if (
        window.start_ms < spec.source_period.start_ms
        or window.end_ms > spec.source_period.end_ms
    ):
        raise QualificationError("qualification window escapes source segment")
    if (
        provenance.raw_sha256 != spec.storage_sha256
        or provenance.manifest_sha256 != spec.manifest_sha256
        or Path(provenance.source_path_or_url).resolve() != spec.source_path
    ):
        raise QualificationError("provenance does not match the source segment")

    adapter = HyperbotH1Adapter(
        provenance,
        expected_sequence=spec.first_sequence,
        expected_previous_sha256=spec.expected_previous_record_sha256,
        allowed_markets=frozenset({market}),
        allowed_channels=frozenset(channels),
        start_exchange_ms=window.start_ms,
        end_exchange_ms=window.end_ms,
    )
    result = normalize_rows(
        iter_jsonl_rows(
            spec.source_path,
            expected_sha256=spec.storage_sha256,
            expected_content_sha256=spec.content_sha256,
        ),
        adapter,
    )
    if (
        adapter.validated_count != spec.record_count
        or adapter.next_sequence != spec.last_sequence + 1
        or adapter.first_record_sha256 != spec.first_record_sha256
        or adapter.last_record_sha256 != spec.last_record_sha256
    ):
        raise QualificationError("validated source facts disagree with its manifest")

    channel_audits = tuple(_channel_audit(result, channel) for channel in channels)
    heartbeat = next(item for item in channel_audits if item.channel == "bbo")
    critical_gap_count = 0
    if heartbeat.first_exchange_ms is None or heartbeat.last_exchange_ms is None:
        critical_gap_count += 1
    else:
        if heartbeat.first_exchange_ms > window.start_ms + max_heartbeat_gap_ms:
            critical_gap_count += 1
        if heartbeat.last_exchange_ms < window.end_ms - max_heartbeat_gap_ms:
            critical_gap_count += 1
        heartbeat_times = sorted(
            _record_exchange_ms(item.record)
            for item in result.records
            if item.source_channel == "bbo" and isinstance(item.record, BookSnapshot)
        )
        critical_gap_count += sum(
            right - left > max_heartbeat_gap_ms
            for left, right in zip(heartbeat_times, heartbeat_times[1:], strict=False)
        )
    critical_gap_count += sum(
        item.record_count == 0 for item in channel_audits if item.channel != "bbo"
    )
    duplicate_count = sum(
        item.code == "duplicate_domain_record" for item in result.rejects
    )
    return SegmentAudit(
        spec=spec,
        market=market,
        channels=channels,
        window=window,
        max_heartbeat_gap_ms=max_heartbeat_gap_ms,
        result=result,
        channel_audits=channel_audits,
        critical_gap_count=critical_gap_count,
        duplicate_count=duplicate_count,
    )


def build_qualification_report(
    audit: SegmentAudit, stored: StoredSegment
) -> dict[str, object]:
    if stored.source_raw_sha256 != audit.spec.storage_sha256:
        raise QualificationError("stored segment does not match audited source")
    if stored.record_count != len(audit.result.records):
        raise QualificationError("stored record count does not match audit")
    if stored.reject_count != len(audit.result.rejects):
        raise QualificationError("stored reject count does not match audit")
    coverage = (
        [
            {
                "start_ms": audit.window.start_ms,
                "end_ms": audit.window.end_ms,
            }
        ]
        if audit.qualified
        else []
    )
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "kind": "bot05_dataset_qualification",
        "dataset_id": stored.dataset_id,
        "segment_id": stored.segment_id,
        "source_raw_sha256": stored.source_raw_sha256,
        "records_sha256": stored.records_sha256,
        "qualified": audit.qualified,
        "critical_gap_count": audit.critical_gap_count,
        "duplicate_count": audit.duplicate_count,
        "reject_count": len(audit.result.rejects),
        "coverage": coverage,
        "audit": {
            "source_path": str(audit.spec.source_path),
            "source_manifest_path": str(audit.spec.manifest_path),
            "source_manifest_sha256": audit.spec.manifest_sha256,
            "source_content_sha256": audit.spec.content_sha256,
            "source_record_count": audit.spec.record_count,
            "source_first_sequence": audit.spec.first_sequence,
            "source_last_sequence": audit.spec.last_sequence,
            "source_first_record_sha256": audit.spec.first_record_sha256,
            "source_last_record_sha256": audit.spec.last_record_sha256,
            "validated_record_count": audit.spec.record_count,
            "market": audit.market,
            "channels": list(audit.channels),
            "coverage_basis": "continuous_hash_chain_plus_bbo_heartbeat",
            "max_heartbeat_gap_ms": audit.max_heartbeat_gap_ms,
            "channel_metrics": {
                item.channel: {
                    "record_count": item.record_count,
                    "first_exchange_ms": item.first_exchange_ms,
                    "last_exchange_ms": item.last_exchange_ms,
                    "max_inter_event_gap_ms": item.max_inter_event_gap_ms,
                }
                for item in audit.channel_audits
            },
        },
    }


def canonical_qualification_bytes(report: Mapping[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise QualificationReportExistsError(
                f"refusing to overwrite qualification report: {path}"
            ) from None


def write_qualification_report(path: Path, report: Mapping[str, object]) -> str:
    payload = canonical_qualification_bytes(report)
    digest = hashlib.sha256(payload).hexdigest()
    _immutable_write(path, payload)
    _immutable_write(
        path.with_suffix(path.suffix + ".sha256"),
        f"{digest}  {path.name}\n".encode(),
    )
    return digest
