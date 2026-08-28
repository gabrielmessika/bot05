"""Content-addressed append-only store for normalized BOT05 domain records."""

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

from bot05.data.contracts import (
    DataAsset,
    EvidenceTier,
    Qualification,
    SourceProject,
    TimeRange,
)
from bot05.data.normalizer import NormalizationResult, RejectedRecord
from bot05.models import (
    BookSnapshot,
    Candle,
    DatasetProvenance,
    DomainRecord,
    MarketContext,
    MarketDefinition,
    Trade,
    decode_domain_record,
    encode_domain_record,
)

STORE_SCHEMA_VERSION = 1
_DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class AppendOnlyStoreError(ValueError):
    """Raised when immutable store contents are invalid or would be replaced."""


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    """Checksummed audit that alone can promote normalized coverage for reuse."""

    report_path: Path
    report_sha256: str
    coverage: tuple[TimeRange, ...]

    def __post_init__(self) -> None:
        if not self.report_path.is_absolute():
            raise ValueError("qualification report path must be absolute")
        if _SHA256.fullmatch(self.report_sha256) is None:
            raise ValueError("qualification report checksum must be a SHA-256")
        if not self.coverage:
            raise ValueError("qualification evidence must declare coverage")
        if tuple(sorted(self.coverage)) != self.coverage:
            raise ValueError("qualification coverage must be sorted")
        if any(
            previous.end_ms > current.start_ms
            for previous, current in zip(self.coverage, self.coverage[1:], strict=False)
        ):
            raise ValueError("qualification coverage must not overlap")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _timestamp_ms(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86_400) + delta.seconds) * 1000 + (delta.microseconds // 1000)


def _record_bounds(record: DomainRecord) -> TimeRange:
    if isinstance(record, Candle):
        return TimeRange(
            _timestamp_ms(record.open_time), _timestamp_ms(record.close_time)
        )
    if isinstance(record, Trade | BookSnapshot):
        timestamp = _timestamp_ms(record.exchange_time)
    elif isinstance(record, MarketDefinition | MarketContext):
        timestamp = _timestamp_ms(record.observed_at)
    else:
        raise TypeError(f"unsupported domain record: {type(record)!r}")
    return TimeRange(timestamp, timestamp + 1)


def _reject_bytes(reject: RejectedRecord) -> bytes:
    return _canonical_json(
        {
            "schema_version": STORE_SCHEMA_VERSION,
            "kind": "bot05_normalization_reject",
            "source_index": reject.source_index,
            "source_record_sha256": reject.source_record_sha256,
            "code": reject.code,
            "detail": reject.detail,
            "market": reject.market,
            "channel": reject.channel,
        }
    )


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise AppendOnlyStoreError(
                f"refusing to replace immutable store file: {path}"
            ) from None


@dataclass(frozen=True, slots=True)
class StoredSegment:
    dataset_id: str
    segment_id: str
    manifest_path: Path
    manifest_sha256: str
    records_path: Path
    records_sha256: str
    rejects_path: Path
    rejects_sha256: str
    record_count: int
    reject_count: int
    markets: tuple[str, ...]
    channels: tuple[str, ...]
    coverage: TimeRange | None
    source: str
    evidence_tier: str
    source_raw_sha256: str
    source_manifest_sha256: str
    source_period: TimeRange

    def as_data_asset(self, evidence: QualificationEvidence | None = None) -> DataAsset:
        """Expose usable normalized coverage to the local-first planner."""

        if self.coverage is None or self.record_count == 0:
            raise AppendOnlyStoreError("a reject-only segment has no reusable coverage")
        qualification = Qualification.CANDIDATE
        coverage: tuple[TimeRange, ...] = (self.coverage,)
        flags = [
            "bot05_normalized_append_only",
            f"source_{self.source}",
            f"source_tier_{self.evidence_tier}",
        ]
        if evidence is None:
            flags.append("requires_event_level_qualification")
            if self.reject_count:
                flags.append("requires_reject_review")
        else:
            if self.reject_count:
                raise AppendOnlyStoreError(
                    "a segment with rejects cannot be promoted as qualified"
                )
            if not evidence.report_path.is_file():
                raise AppendOnlyStoreError("qualification report is missing")
            report_payload = evidence.report_path.read_bytes()
            if _sha256(report_payload) != evidence.report_sha256:
                raise AppendOnlyStoreError("qualification report checksum mismatch")
            try:
                raw_report = json.loads(report_payload)
            except json.JSONDecodeError as exc:
                raise AppendOnlyStoreError(
                    "qualification report is not valid JSON"
                ) from exc
            if not isinstance(raw_report, dict):
                raise AppendOnlyStoreError("qualification report must be a JSON object")
            report = cast(Mapping[str, object], raw_report)
            expected_keys = {
                "schema_version",
                "kind",
                "dataset_id",
                "segment_id",
                "source_raw_sha256",
                "records_sha256",
                "qualified",
                "critical_gap_count",
                "duplicate_count",
                "reject_count",
                "coverage",
                "audit",
            }
            if set(report) != expected_keys:
                raise AppendOnlyStoreError("qualification report keys mismatch")
            schema_version = report.get("schema_version")
            zero_counts = all(
                type(report.get(key)) is int and report.get(key) == 0
                for key in (
                    "critical_gap_count",
                    "duplicate_count",
                    "reject_count",
                )
            )
            if (
                type(schema_version) is not int
                or schema_version != STORE_SCHEMA_VERSION
                or report.get("kind") != "bot05_dataset_qualification"
                or report.get("dataset_id") != self.dataset_id
                or report.get("segment_id") != self.segment_id
                or report.get("source_raw_sha256") != self.source_raw_sha256
                or report.get("records_sha256") != self.records_sha256
                or report.get("qualified") is not True
                or not zero_counts
            ):
                raise AppendOnlyStoreError(
                    "qualification report does not prove a clean segment"
                )
            report_coverage = report.get("coverage")
            audit = report.get("audit")
            if (
                not isinstance(audit, dict)
                or audit.get("source_manifest_sha256") != self.source_manifest_sha256
            ):
                raise AppendOnlyStoreError(
                    "qualification audit does not match source manifest"
                )
            expected_coverage = [
                {"start_ms": item.start_ms, "end_ms": item.end_ms}
                for item in evidence.coverage
            ]
            coverage_shape_is_valid = isinstance(report_coverage, list) and all(
                isinstance(item, dict)
                and set(item) == {"start_ms", "end_ms"}
                and type(item.get("start_ms")) is int
                and type(item.get("end_ms")) is int
                for item in report_coverage
            )
            if not coverage_shape_is_valid or report_coverage != expected_coverage:
                raise AppendOnlyStoreError(
                    "qualification evidence coverage does not match its report"
                )
            if any(
                item.start_ms < self.source_period.start_ms
                or item.end_ms > self.source_period.end_ms
                for item in evidence.coverage
            ):
                raise AppendOnlyStoreError(
                    "qualified coverage escapes source provenance bounds"
                )
            qualification = Qualification.QUALIFIED
            coverage = evidence.coverage
            flags.extend(
                (
                    "qualified_by_checksummed_report",
                    f"qualification_report_sha256_{evidence.report_sha256}",
                )
            )
        return DataAsset(
            dataset_id=f"bot05-normalized-{self.segment_id[:16]}",
            source_project=SourceProject.BOT05,
            tier=EvidenceTier(self.evidence_tier),
            path=self.manifest_path.resolve(),
            markets=self.markets,
            channels=self.channels,
            coverage=coverage,
            provenance_sha256=self.manifest_sha256,
            qualification=qualification,
            quality_flags=tuple(flags),
        )


class AppendOnlyStore:
    """Write immutable content-addressed files; identical retries are idempotent."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def append(
        self,
        dataset_id: str,
        provenance: DatasetProvenance,
        result: NormalizationResult,
    ) -> StoredSegment:
        if _DATASET_ID.fullmatch(dataset_id) is None:
            raise AppendOnlyStoreError("dataset_id is not a safe store directory name")
        if dataset_id != provenance.dataset_id:
            raise AppendOnlyStoreError("store dataset_id must match provenance")
        if not result.records and not result.rejects:
            raise AppendOnlyStoreError("cannot append an empty normalization result")
        if any(item.record.provenance != provenance for item in result.records):
            raise AppendOnlyStoreError(
                "all normalized records must share the supplied provenance"
            )
        source_indices = [item.source_index for item in result.records]
        source_indices.extend(item.source_index for item in result.rejects)
        if len(source_indices) != len(set(source_indices)):
            raise AppendOnlyStoreError("one source row cannot be stored twice")

        records_payload = b"".join(
            encode_domain_record(item.record) for item in result.records
        )
        rejects_payload = b"".join(_reject_bytes(item) for item in result.rejects)
        records_sha256 = _sha256(records_payload)
        rejects_sha256 = _sha256(rejects_payload)
        bounds = [_record_bounds(item.record) for item in result.records]
        coverage = (
            TimeRange(
                min(item.start_ms for item in bounds),
                max(item.end_ms for item in bounds),
            )
            if bounds
            else None
        )
        markets = result.markets
        channels = result.channels
        if not markets or not channels:
            raise AppendOnlyStoreError(
                "normalization result must identify at least one market and channel"
            )

        basis: dict[str, object] = {
            "schema_version": STORE_SCHEMA_VERSION,
            "kind": "bot05_normalized_segment",
            "dataset_id": dataset_id,
            "source": provenance.source,
            "evidence_tier": provenance.evidence_tier,
            "source_raw_sha256": provenance.raw_sha256,
            "source_manifest_sha256": provenance.manifest_sha256,
            "source_period": {
                "start_ms": _timestamp_ms(provenance.period_start),
                "end_ms": _timestamp_ms(provenance.period_end),
            },
            "adapter_version": provenance.adapter_version,
            "code_version": provenance.code_version,
            "config_sha256": provenance.config_sha256,
            "records_sha256": records_sha256,
            "rejects_sha256": rejects_sha256,
            "record_count": len(result.records),
            "reject_count": len(result.rejects),
            "markets": list(markets),
            "channels": list(channels),
            "record_types": sorted(
                {type(item.record).__name__ for item in result.records}
            ),
            "coverage": (
                None
                if coverage is None
                else {"start_ms": coverage.start_ms, "end_ms": coverage.end_ms}
            ),
        }
        segment_id = _sha256(_canonical_json(basis))
        dataset_root = self.root / dataset_id
        records_path = dataset_root / "records" / f"{segment_id}.jsonl"
        rejects_path = dataset_root / "rejects" / f"{segment_id}.jsonl"
        manifest_path = dataset_root / "manifests" / f"{segment_id}.json"
        manifest = {
            **basis,
            "segment_id": segment_id,
            "records_file": str(records_path.relative_to(dataset_root)),
            "rejects_file": str(rejects_path.relative_to(dataset_root)),
        }
        manifest_payload = _canonical_json(manifest)

        _immutable_write(records_path, records_payload)
        _immutable_write(rejects_path, rejects_payload)
        _immutable_write(manifest_path, manifest_payload)
        return StoredSegment(
            dataset_id=dataset_id,
            segment_id=segment_id,
            manifest_path=manifest_path,
            manifest_sha256=_sha256(manifest_payload),
            records_path=records_path,
            records_sha256=records_sha256,
            rejects_path=rejects_path,
            rejects_sha256=rejects_sha256,
            record_count=len(result.records),
            reject_count=len(result.rejects),
            markets=markets,
            channels=channels,
            coverage=coverage,
            source=provenance.source,
            evidence_tier=provenance.evidence_tier,
            source_raw_sha256=provenance.raw_sha256,
            source_manifest_sha256=provenance.manifest_sha256,
            source_period=TimeRange(
                _timestamp_ms(provenance.period_start),
                _timestamp_ms(provenance.period_end),
            ),
        )

    def read_records(self, segment: StoredSegment) -> tuple[DomainRecord, ...]:
        """Verify immutable files and decode a previously stored segment."""

        if _sha256(segment.manifest_path.read_bytes()) != segment.manifest_sha256:
            raise AppendOnlyStoreError("stored manifest checksum mismatch")
        records_payload = segment.records_path.read_bytes()
        if _sha256(records_payload) != segment.records_sha256:
            raise AppendOnlyStoreError("stored records checksum mismatch")
        rejects_payload = segment.rejects_path.read_bytes()
        if _sha256(rejects_payload) != segment.rejects_sha256:
            raise AppendOnlyStoreError("stored rejects checksum mismatch")

        records: list[DomainRecord] = []
        lines = records_payload.splitlines(keepends=True)
        for line_number, line in enumerate(lines, 1):
            try:
                records.append(decode_domain_record(line))
            except (ValueError, json.JSONDecodeError) as exc:
                raise AppendOnlyStoreError(
                    f"invalid normalized record at line {line_number}"
                ) from exc
        if len(records) != segment.record_count:
            raise AppendOnlyStoreError("stored record count mismatch")
        if len(rejects_payload.splitlines()) != segment.reject_count:
            raise AppendOnlyStoreError("stored reject count mismatch")
        return tuple(records)

    def discover_manifests(self, dataset_id: str) -> tuple[Path, ...]:
        if _DATASET_ID.fullmatch(dataset_id) is None:
            raise AppendOnlyStoreError("dataset_id is not a safe store directory name")
        manifest_root = self.root / dataset_id / "manifests"
        if not manifest_root.is_dir():
            return ()
        return tuple(sorted(manifest_root.glob("*.json")))


def parse_store_manifest(path: Path) -> Mapping[str, object]:
    """Read a store manifest as an object for audit/report tooling."""

    try:
        decoded = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise AppendOnlyStoreError(f"invalid store manifest: {path}") from exc
    if not isinstance(decoded, dict):
        raise AppendOnlyStoreError("store manifest must be a JSON object")
    return cast(Mapping[str, object], decoded)
