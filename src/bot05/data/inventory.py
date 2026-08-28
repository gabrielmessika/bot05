"""Read-only discovery of reusable HyperBot and TRIDENT datasets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from bot05.data.contracts import (
    DataAsset,
    EvidenceTier,
    InventoryIssue,
    LocalInventory,
    Qualification,
    SourceProject,
    TimeRange,
)

_REPLAY_NAME = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<market>[A-Za-z0-9:_-]+)-[a-f0-9]+\.json$"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return int(parsed.timestamp() * 1000)


def _read_tail(path: Path, size: int = 131_072) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        length = handle.tell()
        handle.seek(max(0, length - size))
        return handle.read()


def _last_integer(payload: bytes, field: str) -> int | None:
    pattern = re.compile(rb'"' + field.encode("ascii") + rb'"\s*:\s*(\d+)')
    matches = pattern.findall(payload)
    return int(matches[-1]) if matches else None


def _event_coverage(first_ms: int, last_ms: int) -> TimeRange:
    """Treat near-boundary datasets as full UTC days, keep smoke windows exact."""

    milliseconds_per_day = 86_400_000
    first_day_start = (first_ms // milliseconds_per_day) * milliseconds_per_day
    last_day_end = (
        last_ms // milliseconds_per_day
    ) * milliseconds_per_day + milliseconds_per_day
    start_ms = first_day_start if first_ms - first_day_start < 60_000 else first_ms
    end_ms = last_day_end if 0 <= last_day_end - last_ms < 60_000 else last_ms + 1
    return TimeRange(start_ms, end_ms)


def _read_sidecar_digest(path: Path) -> str | None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        return None
    first = sidecar.read_text(encoding="utf-8").split()
    if not first or not re.fullmatch(r"[a-f0-9]{64}", first[0]):
        return None
    return first[0]


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(cast(list[str], value))


def _sequence_coverage(
    segments: list[tuple[int, int, int, int]],
) -> tuple[TimeRange, ...]:
    """Merge segment times only when record sequences prove continuity."""

    if not segments:
        return ()
    ordered = sorted(segments)
    first_sequence, last_sequence, start_ms, end_ms = ordered[0]
    del first_sequence
    merged: list[TimeRange] = []
    for current_first, current_last, current_start, current_end in ordered[1:]:
        if current_first <= last_sequence + 1:
            last_sequence = max(last_sequence, current_last)
            end_ms = max(end_ms, current_end)
        else:
            merged.append(_event_coverage(start_ms, end_ms))
            last_sequence = current_last
            start_ms = current_start
            end_ms = current_end
    merged.append(_event_coverage(start_ms, end_ms))
    return tuple(merged)


def _discover_hyperbot_exports(
    root: Path,
) -> tuple[list[DataAsset], list[InventoryIssue]]:
    """Discover physically present raw segments from checksummed fetch exports."""

    fetch_root = root / "data" / "server-fetches"
    assets: list[DataAsset] = []
    issues: list[InventoryIssue] = []
    if not fetch_root.is_dir():
        return assets, [InventoryIssue(str(fetch_root), "missing server-fetch root")]

    seen_digests: set[str] = set()
    for manifest in sorted(fetch_root.glob("*/export/manifest.json")):
        try:
            raw = manifest.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen_digests:
                continue
            document = cast(Mapping[str, object], json.loads(raw))
            runtime_status = document.get("runtime_status")
            if not isinstance(runtime_status, dict):
                continue
            collector = runtime_status.get("collector_status.json")
            if not isinstance(collector, dict):
                continue
            collector_status = cast(Mapping[str, object], collector)
            markets = _string_list(collector_status.get("markets"))
            raw_channels = _string_list(collector_status.get("channels"))
            channels = tuple(
                "l2" if channel == "l2Book" else channel
                for channel in raw_channels
                if channel in {"l2Book", "bbo", "trades"}
            )
            if not markets or not channels:
                continue

            files = document.get("files")
            if not isinstance(files, list):
                continue
            payload_root = manifest.parent.parent / "payload"
            present_segments: dict[str, str] = {}
            for raw_file in files:
                if not isinstance(raw_file, dict):
                    continue
                file_record = cast(Mapping[str, object], raw_file)
                relative = file_record.get("path")
                sha256 = file_record.get("sha256")
                if not isinstance(relative, str) or not isinstance(sha256, str):
                    continue
                if "/public-market-data/" not in relative:
                    continue
                physical = payload_root / relative
                if physical.is_file():
                    present_segments[physical.name] = sha256
            if not present_segments:
                continue

            segment_coverage: list[tuple[int, int, int, int]] = []
            stores = document.get("store_manifests")
            if not isinstance(stores, list):
                continue
            for raw_store in stores:
                if not isinstance(raw_store, dict):
                    continue
                store = cast(Mapping[str, object], raw_store)
                embedded = store.get("manifest")
                if not isinstance(embedded, dict):
                    continue
                embedded_manifest = cast(Mapping[str, object], embedded)
                if embedded_manifest.get("stream") != "public-market-data":
                    continue
                segments = embedded_manifest.get("segments")
                if not isinstance(segments, list):
                    continue
                for raw_segment in segments:
                    if not isinstance(raw_segment, dict):
                        continue
                    segment = cast(Mapping[str, object], raw_segment)
                    name = segment.get("path")
                    start_ms = segment.get("begin_recorded_at_ms")
                    end_ms = segment.get("end_recorded_at_ms")
                    first_sequence = segment.get("first_sequence")
                    last_sequence = segment.get("last_sequence")
                    storage_sha256 = segment.get("storage_sha256")
                    if not isinstance(name, str) or name not in present_segments:
                        continue
                    if not all(
                        isinstance(value, int)
                        for value in (
                            start_ms,
                            end_ms,
                            first_sequence,
                            last_sequence,
                        )
                    ):
                        continue
                    if (
                        isinstance(storage_sha256, str)
                        and present_segments[name] != storage_sha256
                    ):
                        issues.append(
                            InventoryIssue(
                                str(manifest),
                                f"segment checksum metadata mismatch: {name}",
                            )
                        )
                        continue
                    segment_coverage.append(
                        (
                            cast(int, first_sequence),
                            cast(int, last_sequence),
                            cast(int, start_ms),
                            cast(int, end_ms),
                        )
                    )
            merged = _sequence_coverage(segment_coverage)
            if not merged:
                issues.append(
                    InventoryIssue(
                        str(manifest), "no physical segment coverage resolved"
                    )
                )
                continue

            seen_digests.add(digest)
            assets.append(
                DataAsset(
                    dataset_id=f"hyperbot-raw-export-{digest[:16]}",
                    source_project=SourceProject.HYPERBOT,
                    tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
                    path=manifest.resolve(),
                    markets=markets,
                    channels=channels,
                    coverage=merged,
                    provenance_sha256=digest,
                    qualification=Qualification.CANDIDATE,
                    quality_flags=(
                        "shared_read_only",
                        "physical_segments_present",
                        "manifest_declared_market_subscription",
                        "recorded_time_coverage_only",
                        "shared_hyperbot_not_bot05_h2",
                        "requires_event_level_market_and_gap_qualification",
                    ),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                InventoryIssue(str(manifest), f"invalid fetch manifest: {exc}")
            )
    return assets, issues


def _discover_hyperbot_replays(
    root: Path,
) -> tuple[list[DataAsset], list[InventoryIssue]]:
    replay_root = root / "data" / "replay_datasets"
    assets: list[DataAsset] = []
    issues: list[InventoryIssue] = []
    if not replay_root.is_dir():
        return assets, [InventoryIssue(str(replay_root), "missing replay dataset root")]

    seen_digests: set[str] = set()
    for path in sorted(replay_root.rglob("*.json")):
        match = _REPLAY_NAME.match(path.name)
        if match is None:
            continue
        digest = _read_sidecar_digest(path)
        if digest is None:
            issues.append(
                InventoryIssue(str(path), "missing or invalid SHA-256 sidecar")
            )
            continue
        if digest in seen_digests:
            continue

        tail = _read_tail(path)
        first_ms = _last_integer(tail, "first_exchange_ts_ms")
        last_ms = _last_integer(tail, "last_exchange_ts_ms")
        if first_ms is None or last_ms is None or first_ms >= last_ms:
            issues.append(
                InventoryIssue(str(path), "missing exact exchange-time metrics")
            )
            continue

        channels: list[str] = []
        if (_last_integer(tail, "trade_count") or 0) > 0:
            channels.append("trades")
        if (_last_integer(tail, "bbo_count") or 0) > 0:
            channels.append("bbo")
        if (_last_integer(tail, "book_count") or 0) > 0:
            channels.append("l2")
        if not channels:
            issues.append(
                InventoryIssue(str(path), "dataset contains no usable channel")
            )
            continue

        seen_digests.add(digest)
        assets.append(
            DataAsset(
                dataset_id=f"hyperbot-replay-{digest[:16]}",
                source_project=SourceProject.HYPERBOT,
                tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
                path=path.resolve(),
                markets=(match.group("market"),),
                channels=tuple(channels),
                coverage=(_event_coverage(first_ms, last_ms),),
                provenance_sha256=digest,
                qualification=Qualification.CANDIDATE,
                quality_flags=(
                    "shared_read_only",
                    "source_declared_tier_A",
                    "shared_hyperbot_not_bot05_h2",
                    "requires_bot05_schema_and_gap_qualification",
                ),
            )
        )
    return assets, issues


def _legacy_channel_and_market(path: Path) -> tuple[str, str] | None:
    parts = path.parts
    for channel in ("l2", "trades"):
        if channel not in parts:
            continue
        index = parts.index(channel)
        if index + 1 < len(parts):
            return channel, parts[index + 1]
    return None


def _discover_legacy_manifest(
    hyperbot_root: Path, trident_root: Path
) -> tuple[list[DataAsset], list[InventoryIssue]]:
    manifest = hyperbot_root / "reports" / "legacy_inventory" / "manifest.json"
    if not manifest.is_file():
        return [], [InventoryIssue(str(manifest), "missing HyperBot legacy manifest")]
    try:
        document = cast(Mapping[str, object], json.loads(manifest.read_text("utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [InventoryIssue(str(manifest), f"invalid legacy manifest: {exc}")]
    records = document.get("files")
    if not isinstance(records, list):
        return [], [InventoryIssue(str(manifest), "legacy manifest has no files array")]

    assets: list[DataAsset] = []
    issues: list[InventoryIssue] = []
    trident_resolved = trident_root.resolve()
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        record = cast(Mapping[str, object], raw_record)
        raw_path = record.get("source_path")
        first = record.get("first_timestamp_utc")
        last = record.get("last_timestamp_utc")
        digest = record.get("sha256")
        if not all(isinstance(item, str) for item in (raw_path, first, last, digest)):
            continue
        source_path = Path(cast(str, raw_path))
        channel_market = _legacy_channel_and_market(source_path)
        if channel_market is None:
            continue
        try:
            source_path.resolve().relative_to(trident_resolved)
        except ValueError:
            issues.append(
                InventoryIssue(str(source_path), "legacy path escapes TRIDENT root")
            )
            continue
        if not source_path.is_file():
            issues.append(
                InventoryIssue(str(source_path), "legacy source file is missing")
            )
            continue
        try:
            start_ms = _parse_utc_ms(cast(str, first))
            end_ms = _parse_utc_ms(cast(str, last)) + 1
        except ValueError:
            issues.append(
                InventoryIssue(str(source_path), "invalid legacy time coverage")
            )
            continue
        channel, market = channel_market
        assets.append(
            DataAsset(
                dataset_id=f"trident-legacy-{cast(str, digest)[:16]}",
                source_project=SourceProject.TRIDENT,
                tier=EvidenceTier.LEGACY,
                path=source_path.resolve(),
                markets=(market,),
                channels=(channel,),
                coverage=(TimeRange(start_ms, end_ms),),
                provenance_sha256=cast(str, digest),
                qualification=Qualification.CANDIDATE,
                quality_flags=(
                    "legacy_research_only",
                    "shared_read_only",
                    "requires_bot05_schema_and_gap_qualification",
                ),
            )
        )
    return assets, issues


def _discover_trident_candles(
    root: Path,
) -> tuple[list[DataAsset], list[InventoryIssue]]:
    research_root = root / "data" / "research"
    assets: list[DataAsset] = []
    issues: list[InventoryIssue] = []
    if not research_root.is_dir():
        return assets, [
            InventoryIssue(str(research_root), "missing TRIDENT research root")
        ]

    for manifest in sorted(research_root.glob("*/current/manifest.json")):
        try:
            document = cast(
                Mapping[str, object], json.loads(manifest.read_text(encoding="utf-8"))
            )
            start = document.get("requested_start")
            end = document.get("requested_end")
            symbols = document.get("symbols")
            intervals = document.get("intervals")
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            if not isinstance(symbols, list) or not all(
                isinstance(item, str) for item in symbols
            ):
                continue
            if not isinstance(intervals, list) or not all(
                isinstance(item, str) for item in intervals
            ):
                continue
            supported = tuple(
                f"candles_{item}"
                for item in cast(list[str], intervals)
                if item in {"1m", "5m"}
            )
            if not supported:
                continue
            digest = _sha256_file(manifest)
            assets.append(
                DataAsset(
                    dataset_id=f"trident-candles-{digest[:16]}",
                    source_project=SourceProject.TRIDENT,
                    tier=EvidenceTier.LEGACY,
                    path=manifest.resolve(),
                    markets=tuple(cast(list[str], symbols)),
                    channels=supported,
                    coverage=(TimeRange(_parse_utc_ms(start), _parse_utc_ms(end)),),
                    provenance_sha256=digest,
                    qualification=Qualification.CANDIDATE,
                    quality_flags=(
                        "legacy_research_only",
                        "manifest_declared_coverage",
                        "requires_file_level_qualification",
                    ),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                InventoryIssue(str(manifest), f"invalid candle manifest: {exc}")
            )
    return assets, issues


def _qualification_ranges(value: object) -> tuple[TimeRange, ...]:
    if not isinstance(value, list):
        raise ValueError("qualification coverage must be an array")
    ranges: list[TimeRange] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"start_ms", "end_ms"}:
            raise ValueError("qualification coverage entry is invalid")
        start = raw.get("start_ms")
        end = raw.get("end_ms")
        if type(start) is not int or type(end) is not int:
            raise ValueError("qualification coverage bounds must be integers")
        ranges.append(TimeRange(start, end))
    if not ranges or tuple(sorted(ranges)) != tuple(ranges):
        raise ValueError("qualification coverage must be non-empty and sorted")
    return tuple(ranges)


def _resolved_child(root: Path, relative: object, name: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{name} path must be a non-empty string")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{name} path escapes its dataset root") from exc
    return path


def _discover_bot05_qualifications(
    data_root: Path,
    qualification_root: Path,
    hyperbot_root: Path,
) -> tuple[list[DataAsset], list[InventoryIssue]]:
    assets: list[DataAsset] = []
    issues: list[InventoryIssue] = []
    if not qualification_root.is_dir():
        return assets, issues
    normalized_root = (data_root / "normalized").resolve()
    hyperbot_resolved = hyperbot_root.resolve()
    for report_path in sorted(qualification_root.glob("*.json")):
        try:
            report_digest = _read_sidecar_digest(report_path)
            if report_digest is None or _sha256_file(report_path) != report_digest:
                raise ValueError("qualification report checksum is invalid")
            document = cast(
                Mapping[str, object], json.loads(report_path.read_text("utf-8"))
            )
            if (
                document.get("schema_version") != 1
                or document.get("kind") != "bot05_dataset_qualification"
                or document.get("qualified") is not True
                or document.get("critical_gap_count") != 0
                or document.get("duplicate_count") != 0
                or document.get("reject_count") != 0
            ):
                raise ValueError("qualification report is not a clean promotion")
            dataset_id = document.get("dataset_id")
            segment_id = document.get("segment_id")
            if not isinstance(dataset_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", dataset_id
            ):
                raise ValueError("qualification dataset_id is invalid")
            if not isinstance(segment_id, str) or not re.fullmatch(
                r"[a-f0-9]{64}", segment_id
            ):
                raise ValueError("qualification segment_id is invalid")
            dataset_root = (normalized_root / dataset_id).resolve()
            dataset_root.relative_to(normalized_root)
            manifest_path = (
                dataset_root / "manifests" / f"{segment_id}.json"
            ).resolve()
            manifest = cast(
                Mapping[str, object], json.loads(manifest_path.read_text("utf-8"))
            )
            if (
                manifest.get("schema_version") != 1
                or manifest.get("kind") != "bot05_normalized_segment"
                or manifest.get("dataset_id") != dataset_id
                or manifest.get("segment_id") != segment_id
                or manifest.get("record_count") in {None, 0}
                or manifest.get("reject_count") != 0
                or manifest.get("records_sha256") != document.get("records_sha256")
                or manifest.get("source_raw_sha256")
                != document.get("source_raw_sha256")
            ):
                raise ValueError("normalized manifest disagrees with qualification")
            records_path = _resolved_child(
                dataset_root, manifest.get("records_file"), "records"
            )
            rejects_path = _resolved_child(
                dataset_root, manifest.get("rejects_file"), "rejects"
            )
            if (
                not records_path.is_file()
                or _sha256_file(records_path) != manifest.get("records_sha256")
                or not rejects_path.is_file()
                or _sha256_file(rejects_path) != manifest.get("rejects_sha256")
            ):
                raise ValueError("normalized store checksum is invalid")
            audit = document.get("audit")
            if not isinstance(audit, dict):
                raise ValueError("qualification audit is missing")
            source_path_raw = audit.get("source_path")
            source_manifest_raw = audit.get("source_manifest_path")
            if not isinstance(source_path_raw, str) or not isinstance(
                source_manifest_raw, str
            ):
                raise ValueError("qualification source paths are invalid")
            source_path = Path(source_path_raw).resolve()
            source_manifest = Path(source_manifest_raw).resolve()
            source_path.relative_to(hyperbot_resolved)
            source_manifest.relative_to(hyperbot_resolved)
            if _sha256_file(source_path) != document.get(
                "source_raw_sha256"
            ) or _sha256_file(source_manifest) != audit.get("source_manifest_sha256"):
                raise ValueError("qualified shared source checksum changed")
            market = audit.get("market")
            channels = audit.get("channels")
            if not isinstance(market, str) or not market:
                raise ValueError("qualification market is invalid")
            if not isinstance(channels, list) or not all(
                isinstance(item, str) and item for item in channels
            ):
                raise ValueError("qualification channels are invalid")
            normalized_channels = tuple(
                "l2" if item == "l2Book" else item for item in cast(list[str], channels)
            )
            manifest_markets = manifest.get("markets")
            manifest_channels = manifest.get("channels")
            if manifest_markets != [market] or set(normalized_channels) != set(
                cast(list[str], manifest_channels)
                if isinstance(manifest_channels, list)
                else []
            ):
                raise ValueError("qualified scope disagrees with normalized manifest")
            assets.append(
                DataAsset(
                    dataset_id=f"bot05-qualified-{segment_id[:16]}",
                    source_project=SourceProject.BOT05,
                    tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
                    path=manifest_path,
                    markets=(market,),
                    channels=normalized_channels,
                    coverage=_qualification_ranges(document.get("coverage")),
                    provenance_sha256=_sha256_file(manifest_path),
                    qualification=Qualification.QUALIFIED,
                    quality_flags=(
                        "bot05_normalized_append_only",
                        "qualified_by_checksummed_report",
                        f"qualification_report_sha256_{report_digest}",
                        "shared_hyperbot_not_bot05_h2",
                    ),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                InventoryIssue(str(report_path), f"invalid qualification: {exc}")
            )
    return assets, issues


def discover_local_inventory(
    hyperbot_root: Path,
    trident_root: Path,
    *,
    bot05_data_root: Path | None = None,
    qualification_root: Path | None = None,
) -> LocalInventory:
    """Discover metadata without modifying or fully hashing shared large files."""

    hyperbot_assets, hyperbot_issues = _discover_hyperbot_replays(hyperbot_root)
    export_assets, export_issues = _discover_hyperbot_exports(hyperbot_root)
    legacy_assets, legacy_issues = _discover_legacy_manifest(
        hyperbot_root, trident_root
    )
    candle_assets, candle_issues = _discover_trident_candles(trident_root)
    bot05_assets: list[DataAsset] = []
    bot05_issues: list[InventoryIssue] = []
    if bot05_data_root is not None and qualification_root is not None:
        bot05_assets, bot05_issues = _discover_bot05_qualifications(
            bot05_data_root, qualification_root, hyperbot_root
        )
    assets = sorted(
        (
            *bot05_assets,
            *hyperbot_assets,
            *export_assets,
            *legacy_assets,
            *candle_assets,
        ),
        key=lambda item: item.dataset_id,
    )
    issues = sorted(
        (
            *bot05_issues,
            *hyperbot_issues,
            *export_issues,
            *legacy_issues,
            *candle_issues,
        ),
        key=lambda item: (item.source, item.reason),
    )
    return LocalInventory(assets=tuple(assets), issues=tuple(issues))
