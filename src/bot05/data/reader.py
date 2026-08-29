"""Checksum-first reader for BOT05 datasets promoted by qualification reports."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bot05.data.contracts import TimeRange
from bot05.data.normalizer import file_sha256
from bot05.models import (
    BookSnapshot,
    DomainRecord,
    Trade,
    decode_domain_record,
    encode_domain_record,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class QualifiedStoreError(ValueError):
    """Raised when qualified local records cannot be proven bit-exact."""


@dataclass(frozen=True, slots=True)
class QualifiedSource:
    """Identity and declared coverage of one verified normalized segment."""

    dataset_id: str
    segment_id: str
    manifest_path: Path
    manifest_sha256: str
    records_path: Path
    records_sha256: str
    qualification_path: Path
    qualification_sha256: str
    coverage: tuple[TimeRange, ...]
    record_count: int


@dataclass(frozen=True, slots=True)
class QualifiedRecordSet:
    """Verified records plus the exact source identities used to derive them."""

    market: str
    channels: tuple[str, ...]
    requested: TimeRange
    records: tuple[DomainRecord, ...]
    sources: tuple[QualifiedSource, ...]
    records_sha256: str
    source_records_sha256: str
    manifests_sha256: str
    max_bbo_gap_ms: int
    duplicate_trade_count: int

    @property
    def trades(self) -> tuple[Trade, ...]:
        return tuple(item for item in self.records if isinstance(item, Trade))

    @property
    def books(self) -> tuple[BookSnapshot, ...]:
        return tuple(item for item in self.records if isinstance(item, BookSnapshot))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise QualifiedStoreError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualifiedStoreError(f"{name} must be a non-empty string")
    return value


def _digest(value: object, name: str) -> str:
    result = _string(value, name)
    if _SHA256.fullmatch(result) is None:
        raise QualifiedStoreError(f"{name} must be a SHA-256 digest")
    return result


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise QualifiedStoreError(f"{name} must be an integer")
    return value


def _coverage(value: object) -> tuple[TimeRange, ...]:
    if not isinstance(value, list) or not value:
        raise QualifiedStoreError("qualification coverage must be a non-empty array")
    result: list[TimeRange] = []
    for raw in cast(list[object], value):
        item = _mapping(raw, "qualification coverage entry")
        if set(item) != {"start_ms", "end_ms"}:
            raise QualifiedStoreError("qualification coverage entry keys mismatch")
        result.append(
            TimeRange(
                _integer(item.get("start_ms"), "coverage start"),
                _integer(item.get("end_ms"), "coverage end"),
            )
        )
    ordered = tuple(sorted(result))
    if ordered != tuple(result) or any(
        left.end_ms > right.start_ms
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        raise QualifiedStoreError("qualification coverage must be ordered and disjoint")
    return ordered


def _resolve_child(root: Path, relative: object, name: str) -> Path:
    path = (root / _string(relative, f"{name} file")).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise QualifiedStoreError(f"{name} path escapes its dataset root") from exc
    return path


def _qualification_for(
    qualification_root: Path, segment_id: str
) -> tuple[Path, Mapping[str, object], str]:
    matches: list[tuple[Path, Mapping[str, object], str]] = []
    for path in sorted(qualification_root.glob("*.json")):
        try:
            report = _mapping(json.loads(path.read_bytes()), "qualification report")
        except (OSError, json.JSONDecodeError, QualifiedStoreError):
            continue
        if report.get("segment_id") != segment_id:
            continue
        sidecar = path.with_suffix(path.suffix + ".sha256")
        try:
            parts = sidecar.read_text("ascii").strip().split()
        except OSError as exc:
            raise QualifiedStoreError("qualification sidecar is missing") from exc
        if len(parts) != 2 or parts[1] != path.name:
            raise QualifiedStoreError("qualification sidecar shape mismatch")
        digest = _digest(parts[0], "qualification sidecar digest")
        if file_sha256(path) != digest:
            raise QualifiedStoreError("qualification report checksum mismatch")
        matches.append((path.resolve(), report, digest))
    if len(matches) != 1:
        raise QualifiedStoreError(
            "qualification report cannot be resolved uniquely for segment"
        )
    return matches[0]


def _verified_source(
    manifest_path: Path, qualification_root: Path
) -> tuple[QualifiedSource, bytes, tuple[DomainRecord, ...]]:
    resolved_manifest = manifest_path.resolve()
    try:
        manifest_payload = resolved_manifest.read_bytes()
        manifest = _mapping(json.loads(manifest_payload), "store manifest")
    except (OSError, json.JSONDecodeError) as exc:
        raise QualifiedStoreError(f"invalid store manifest: {manifest_path}") from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "bot05_normalized_segment"
    ):
        raise QualifiedStoreError("unsupported normalized store manifest")
    dataset_id = _string(manifest.get("dataset_id"), "dataset_id")
    segment_id = _digest(manifest.get("segment_id"), "segment_id")
    records_sha256 = _digest(manifest.get("records_sha256"), "records_sha256")
    record_count = _integer(manifest.get("record_count"), "record_count")
    reject_count = _integer(manifest.get("reject_count"), "reject_count")
    if record_count <= 0 or reject_count != 0:
        raise QualifiedStoreError("qualified store must contain records and no rejects")
    dataset_root = resolved_manifest.parent.parent
    records_path = _resolve_child(dataset_root, manifest.get("records_file"), "records")
    rejects_path = _resolve_child(dataset_root, manifest.get("rejects_file"), "rejects")
    try:
        records_payload = records_path.read_bytes()
    except OSError as exc:
        raise QualifiedStoreError("normalized records are missing") from exc
    if hashlib.sha256(records_payload).hexdigest() != records_sha256:
        raise QualifiedStoreError("normalized records checksum mismatch")
    rejects_sha256 = _digest(manifest.get("rejects_sha256"), "rejects_sha256")
    try:
        rejects_payload = rejects_path.read_bytes()
    except OSError as exc:
        raise QualifiedStoreError("normalized rejects are missing") from exc
    if hashlib.sha256(rejects_payload).hexdigest() != rejects_sha256:
        raise QualifiedStoreError("normalized rejects checksum mismatch")
    if rejects_payload:
        raise QualifiedStoreError("qualified rejects file must be empty")

    qualification_path, qualification, qualification_sha256 = _qualification_for(
        qualification_root, segment_id
    )
    expected_report_keys = {
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
    if set(qualification) != expected_report_keys:
        raise QualifiedStoreError("qualification report keys mismatch")
    if (
        qualification.get("schema_version") != 1
        or qualification.get("kind") != "bot05_dataset_qualification"
        or qualification.get("dataset_id") != dataset_id
        or qualification.get("segment_id") != segment_id
        or qualification.get("records_sha256") != records_sha256
        or qualification.get("source_raw_sha256") != manifest.get("source_raw_sha256")
        or qualification.get("qualified") is not True
        or any(
            qualification.get(name) != 0
            for name in ("critical_gap_count", "duplicate_count", "reject_count")
        )
    ):
        raise QualifiedStoreError("qualification does not prove a clean store")
    coverage = _coverage(qualification.get("coverage"))
    audit = _mapping(qualification.get("audit"), "qualification audit")
    if audit.get("source_manifest_sha256") != manifest.get("source_manifest_sha256"):
        raise QualifiedStoreError("qualification source manifest mismatch")

    records: list[DomainRecord] = []
    for line_number, line in enumerate(records_payload.splitlines(keepends=True), 1):
        try:
            records.append(decode_domain_record(line))
        except (ValueError, json.JSONDecodeError) as exc:
            raise QualifiedStoreError(
                f"invalid normalized record at line {line_number}"
            ) from exc
    if len(records) != record_count:
        raise QualifiedStoreError("normalized record count mismatch")
    source = QualifiedSource(
        dataset_id=dataset_id,
        segment_id=segment_id,
        manifest_path=resolved_manifest,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        records_path=records_path,
        records_sha256=records_sha256,
        qualification_path=qualification_path,
        qualification_sha256=qualification_sha256,
        coverage=coverage,
        record_count=record_count,
    )
    return source, records_payload, tuple(records)


def _record_ms(record: DomainRecord) -> int:
    if isinstance(record, Trade | BookSnapshot):
        return int(record.exchange_time.timestamp() * 1000)
    raise QualifiedStoreError(
        "qualified event reader accepts only trade and book snapshot records"
    )


def _event_key(record: DomainRecord) -> tuple[datetime, datetime, str]:
    if isinstance(record, Trade | BookSnapshot):
        return (record.exchange_time, record.received_at, type(record).__name__)
    raise QualifiedStoreError("qualified event ordering received a non-event record")


def read_qualified_event_records(
    manifest_paths: tuple[Path, ...],
    *,
    qualification_root: Path,
    market: str,
    requested: TimeRange,
    max_bbo_gap_ms: int = 15_000,
) -> QualifiedRecordSet:
    """Load qualified event stores and re-audit their combined BBO heartbeat."""

    if not manifest_paths or not market.strip():
        raise QualifiedStoreError("manifest paths and market are required")
    if max_bbo_gap_ms <= 0:
        raise QualifiedStoreError("max BBO gap must be positive")
    loaded = tuple(
        _verified_source(path, qualification_root.resolve()) for path in manifest_paths
    )
    ordered = tuple(sorted(loaded, key=lambda item: item[0].coverage[0]))
    sources = tuple(item[0] for item in ordered)
    declared = tuple(span for source in sources for span in source.coverage)
    if any(
        left.end_ms > right.start_ms
        for left, right in zip(declared, declared[1:], strict=False)
    ):
        raise QualifiedStoreError("qualified source coverage overlaps")
    if (
        declared[0].start_ms > requested.start_ms
        or declared[-1].end_ms < requested.end_ms
    ):
        raise QualifiedStoreError("qualified sources do not reach requested bounds")
    if any(
        right.start_ms - left.end_ms > max_bbo_gap_ms
        for left, right in zip(declared, declared[1:], strict=False)
    ):
        raise QualifiedStoreError("qualified source coverage has a critical gap")

    selected: list[DomainRecord] = []
    trades_by_id: dict[tuple[str, str], Trade] = {}
    duplicate_trade_count = 0
    encoded_seen: set[bytes] = set()
    payloads: list[bytes] = []
    for _source, payload, records in ordered:
        payloads.append(payload)
        for record in records:
            if record.market != market:
                raise QualifiedStoreError("qualified record market mismatch")
            record_ms = _record_ms(record)
            if not requested.start_ms <= record_ms < requested.end_ms:
                continue
            if isinstance(record, Trade):
                key = (record.market, record.trade_id)
                previous = trades_by_id.get(key)
                if previous is not None:
                    previous_facts = (
                        previous.exchange_time,
                        previous.aggressor_side,
                        previous.price,
                        previous.size,
                    )
                    current_facts = (
                        record.exchange_time,
                        record.aggressor_side,
                        record.price,
                        record.size,
                    )
                    if previous_facts != current_facts:
                        raise QualifiedStoreError(
                            "duplicate trade_id has conflicting execution facts"
                        )
                    duplicate_trade_count += 1
                    if record.received_at < previous.received_at:
                        trades_by_id[key] = record
                    continue
                trades_by_id[key] = record
                continue
            # Decoded stores retain canonical envelopes. Re-encode only for a
            # cross-segment duplicate guard; source-file hashes remain primary.
            encoded = encode_domain_record(record)
            if encoded in encoded_seen:
                raise QualifiedStoreError("duplicate domain record across segments")
            encoded_seen.add(encoded)
            selected.append(record)
    selected.extend(trades_by_id.values())
    if not selected:
        raise QualifiedStoreError("qualified request contains no event records")
    selected.sort(key=_event_key)
    books = tuple(item for item in selected if isinstance(item, BookSnapshot))
    trades = tuple(item for item in selected if isinstance(item, Trade))
    if not books or not trades:
        raise QualifiedStoreError("qualified request requires trades and BBO")
    bbo_times = tuple(_record_ms(item) for item in books)
    boundary_gaps = (
        bbo_times[0] - requested.start_ms,
        requested.end_ms - bbo_times[-1],
    )
    internal_gaps = tuple(
        right - left for left, right in zip(bbo_times, bbo_times[1:], strict=False)
    )
    max_gap = max((*boundary_gaps, *internal_gaps), default=0)
    if min(boundary_gaps) < 0 or max_gap > max_bbo_gap_ms:
        raise QualifiedStoreError("combined BBO heartbeat has a critical gap")
    return QualifiedRecordSet(
        market=market,
        channels=("bbo", "trades"),
        requested=requested,
        records=tuple(selected),
        sources=sources,
        records_sha256=hashlib.sha256(
            b"".join(encode_domain_record(item) for item in selected)
        ).hexdigest(),
        source_records_sha256=hashlib.sha256(b"".join(payloads)).hexdigest(),
        manifests_sha256=hashlib.sha256(
            "\n".join(item.manifest_sha256 for item in sources).encode("ascii")
        ).hexdigest(),
        max_bbo_gap_ms=max_gap,
        duplicate_trade_count=duplicate_trade_count,
    )
