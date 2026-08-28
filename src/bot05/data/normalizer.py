"""Checksum-first, deterministic normalization primitives for local datasets."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from bot05.models import DomainRecord, ModelValidationError, encode_domain_record

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SourceIntegrityError(ValueError):
    """Raised when source bytes or their append-only chain cannot be trusted."""


class OutOfScopeRecord(Exception):
    """An intact source record excluded by a preregistered import scope."""


class NormalizationError(ValueError):
    """A source record is intact but cannot enter the BOT05 domain."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        market: str | None = None,
        channel: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.market = market
        self.channel = channel


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One exact source line and its stable location within the source file."""

    index: int
    raw: bytes
    sha256: str

    def __post_init__(self) -> None:
        if self.index <= 0:
            raise ValueError("source row index must be positive")
        if not self.raw:
            raise ValueError("source row bytes must not be empty")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("source row sha256 must be a lowercase digest")
        if hashlib.sha256(self.raw).hexdigest() != self.sha256:
            raise ValueError("source row sha256 does not match its bytes")


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    source_index: int
    source_record_sha256: str
    source_channel: str
    record: DomainRecord


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    source_index: int
    source_record_sha256: str
    code: str
    detail: str
    market: str | None = None
    channel: str | None = None

    def __post_init__(self) -> None:
        if self.source_index <= 0:
            raise ValueError("rejected source index must be positive")
        if _SHA256.fullmatch(self.source_record_sha256) is None:
            raise ValueError("rejected source checksum must be a lowercase SHA-256")
        if not self.code.strip() or not self.detail.strip():
            raise ValueError("rejection code and detail must not be blank")


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    records: tuple[NormalizedRecord, ...]
    rejects: tuple[RejectedRecord, ...]

    @property
    def domain_records(self) -> tuple[DomainRecord, ...]:
        return tuple(item.record for item in self.records)

    @property
    def channels(self) -> tuple[str, ...]:
        values = {item.source_channel for item in self.records}
        values.update(item.channel for item in self.rejects if item.channel is not None)
        return tuple(sorted(values))

    @property
    def markets(self) -> tuple[str, ...]:
        values = {record_market(item.record) for item in self.records}
        values.update(item.market for item in self.rejects if item.market is not None)
        return tuple(sorted(values))


class RecordAdapter(Protocol):
    def __call__(self, row: SourceRow) -> NormalizedRecord: ...


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iter_jsonl_rows(
    path: Path,
    *,
    expected_sha256: str,
    expected_content_sha256: str | None = None,
    max_record_bytes: int = 16 * 1024 * 1024,
) -> Iterator[SourceRow]:
    """Verify the complete immutable source, then stream exact JSONL records."""

    if _SHA256.fullmatch(expected_sha256) is None:
        raise SourceIntegrityError("expected source checksum is invalid")
    if (
        expected_content_sha256 is not None
        and _SHA256.fullmatch(expected_content_sha256) is None
    ):
        raise SourceIntegrityError("expected content checksum is invalid")
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        raise SourceIntegrityError(
            f"source checksum mismatch for {path}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    if max_record_bytes <= 0:
        raise ValueError("max_record_bytes must be positive")

    opener = gzip.open if path.suffix == ".gz" else Path.open
    content_digest = hashlib.sha256()
    try:
        with opener(path, "rb") as handle:
            for index, raw in enumerate(handle, start=1):
                if len(raw) > max_record_bytes:
                    raise SourceIntegrityError(
                        f"source record exceeds {max_record_bytes} bytes at "
                        f"{path}:{index}"
                    )
                content_digest.update(raw)
                yield SourceRow(index, raw, hashlib.sha256(raw).hexdigest())
    except (gzip.BadGzipFile, EOFError) as exc:
        raise SourceIntegrityError(f"invalid gzip source: {path}") from exc
    if (
        expected_content_sha256 is not None
        and content_digest.hexdigest() != expected_content_sha256
    ):
        raise SourceIntegrityError(f"content checksum mismatch for {path}")


def source_row_from_mapping(index: int, payload: Mapping[str, object]) -> SourceRow:
    """Build a canonical source row for fixtures or already-decoded H0 responses."""

    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    return SourceRow(index=index, raw=raw, sha256=hashlib.sha256(raw).hexdigest())


def decode_json_object(row: SourceRow) -> Mapping[str, object]:
    try:
        decoded = json.loads(row.raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(
            "invalid_json", "record is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise NormalizationError("invalid_shape", "record must be a JSON object")
    return cast(Mapping[str, object], decoded)


def record_market(record: DomainRecord) -> str:
    return record.market


def normalize_rows(
    rows: Iterable[SourceRow], adapter: RecordAdapter
) -> NormalizationResult:
    """Normalize intact rows, separating semantic rejects and exact duplicates."""

    normalized: list[NormalizedRecord] = []
    rejects: list[RejectedRecord] = []
    seen_records: set[bytes] = set()
    for row in rows:
        try:
            item = adapter(row)
            encoded = encode_domain_record(item.record)
            if encoded in seen_records:
                rejects.append(
                    RejectedRecord(
                        source_index=row.index,
                        source_record_sha256=row.sha256,
                        code="duplicate_domain_record",
                        detail=(
                            "record is bit-identical to an earlier normalized record"
                        ),
                        market=record_market(item.record),
                        channel=item.source_channel,
                    )
                )
                continue
            seen_records.add(encoded)
            normalized.append(item)
        except OutOfScopeRecord:
            continue
        except NormalizationError as exc:
            rejects.append(
                RejectedRecord(
                    source_index=row.index,
                    source_record_sha256=row.sha256,
                    code=exc.code,
                    detail=exc.detail,
                    market=exc.market,
                    channel=exc.channel,
                )
            )
        except ModelValidationError as exc:
            rejects.append(
                RejectedRecord(
                    source_index=row.index,
                    source_record_sha256=row.sha256,
                    code="domain_validation",
                    detail=str(exc),
                )
            )
    return NormalizationResult(tuple(normalized), tuple(rejects))
