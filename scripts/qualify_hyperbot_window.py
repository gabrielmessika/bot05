#!/usr/bin/env python3
"""Qualify a bounded H1 window across local HyperBot source segments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from bot05.config import load_config
from bot05.data.contracts import TimeRange
from bot05.data.normalizer import file_sha256
from bot05.data.qualification import (
    audit_hyperbot_segment,
    build_qualification_report,
    provenance_for_hyperbot_segment,
    select_hyperbot_segment_windows,
    write_qualification_report,
)
from bot05.data.report import code_sha256
from bot05.data.store import AppendOnlyStore, QualificationEvidence
from bot05.models import BookSnapshot, Trade, encode_domain_record


class WindowQualificationError(ValueError):
    """Raised when a multi-segment qualification cannot be proven clean."""


def _utc_ms(value: str) -> int:
    if not value.endswith("Z"):
        raise argparse.ArgumentTypeError("timestamp must have a UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 UTC") from exc
    return int(parsed.timestamp() * 1000)


def _iso_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/research.toml"))
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument(
        "--channels", nargs="+", default=["trades", "bbo"], choices=["trades", "bbo"]
    )
    parser.add_argument("--start-utc", type=_utc_ms, required=True)
    parser.add_argument("--end-utc", type=_utc_ms, required=True)
    parser.add_argument("--max-heartbeat-gap-ms", type=int, default=15_000)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument(
        "--qualification-root",
        type=Path,
        default=Path("reports/data_quality/qualifications"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise WindowQualificationError(
                f"refusing to overwrite immutable report: {path}"
            ) from None


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        requested = TimeRange(args.start_utc, args.end_utc)
        if args.max_heartbeat_gap_ms <= 0:
            raise WindowQualificationError("max heartbeat gap must be positive")
        loaded = load_config(args.config)
        if args.market not in loaded.config.universe.markets:
            raise WindowQualificationError("market is outside the configured universe")
        channels = tuple(args.channels)
        if len(set(channels)) != len(channels) or "bbo" not in channels:
            raise WindowQualificationError("unique channels including BBO are required")
        partitions = select_hyperbot_segment_windows(
            args.source_manifest,
            requested,
            max_manifest_gap_ms=args.max_heartbeat_gap_ms,
        )
        store_root = (
            args.store_root
            if args.store_root is not None
            else loaded.config.data.local_data_dir / "normalized"
        ).resolve()
        qualification_root = args.qualification_root.resolve()
        code_version = f"sha256:{code_sha256()}"
        records_payloads: list[bytes] = []
        normalized_records: list[Trade | BookSnapshot] = []
        segment_reports: list[dict[str, object]] = []
        critical_gap_count = 0
        duplicate_count = 0
        reject_count = 0
        for spec, window in partitions:
            dataset_id = (
                f"h1-{args.market.lower()}-{spec.storage_sha256[:12]}-"
                f"{window.start_ms}-{window.end_ms}"
            )
            provenance = provenance_for_hyperbot_segment(
                spec,
                dataset_id=dataset_id,
                code_version=code_version,
                config_sha256=loaded.sha256,
                market=args.market,
                channels=channels,
            )
            audit = audit_hyperbot_segment(
                spec,
                provenance,
                market=args.market,
                channels=channels,
                window=window,
                max_heartbeat_gap_ms=args.max_heartbeat_gap_ms,
            )
            stored = AppendOnlyStore(store_root).append(
                provenance.dataset_id, provenance, audit.result
            )
            qualification = build_qualification_report(audit, stored)
            qualification_path = (
                qualification_root / f"{dataset_id}-{stored.segment_id[:16]}.json"
            )
            qualification_sha256 = write_qualification_report(
                qualification_path, qualification
            )
            if audit.qualified:
                stored.as_data_asset(
                    QualificationEvidence(
                        qualification_path.resolve(),
                        qualification_sha256,
                        (window,),
                    )
                )
            critical_gap_count += audit.critical_gap_count
            duplicate_count += audit.duplicate_count
            reject_count += len(audit.result.rejects)
            payload = b"".join(
                encode_domain_record(item.record) for item in audit.result.records
            )
            records_payloads.append(payload)
            normalized_records.extend(
                item.record
                for item in audit.result.records
                if isinstance(item.record, Trade | BookSnapshot)
            )
            segment_reports.append(
                {
                    "source_path": str(spec.source_path),
                    "source_storage_sha256": spec.storage_sha256,
                    "window": {
                        "start_ms": window.start_ms,
                        "end_ms": window.end_ms,
                    },
                    "dataset_id": dataset_id,
                    "segment_id": stored.segment_id,
                    "manifest_path": str(stored.manifest_path.resolve()),
                    "manifest_sha256": stored.manifest_sha256,
                    "records_sha256": stored.records_sha256,
                    "record_count": stored.record_count,
                    "qualification_path": str(qualification_path.resolve()),
                    "qualification_sha256": qualification_sha256,
                    "qualified": audit.qualified,
                }
            )

        bbo_times = sorted(
            int(item.exchange_time.timestamp() * 1000)
            for item in normalized_records
            if isinstance(item, BookSnapshot)
        )
        trade_count = sum(isinstance(item, Trade) for item in normalized_records)
        bbo_count = len(bbo_times)
        heartbeat_gaps = (
            [bbo_times[0] - requested.start_ms]
            + [
                right - left
                for left, right in zip(bbo_times, bbo_times[1:], strict=False)
            ]
            + [requested.end_ms - bbo_times[-1]]
            if bbo_times
            else [requested.end_ms - requested.start_ms]
        )
        max_bbo_gap_ms = max(heartbeat_gaps)
        if min(heartbeat_gaps) < 0 or max_bbo_gap_ms > args.max_heartbeat_gap_ms:
            critical_gap_count += 1
        if trade_count == 0:
            critical_gap_count += 1
        trades_by_id: dict[tuple[str, str], Trade] = {}
        derived_records: list[Trade | BookSnapshot] = []
        trade_retransmission_count = 0
        for record in normalized_records:
            if not isinstance(record, Trade):
                derived_records.append(record)
                continue
            key = (record.market, record.trade_id)
            previous = trades_by_id.get(key)
            if previous is None:
                trades_by_id[key] = record
                continue
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
                duplicate_count += 1
                continue
            trade_retransmission_count += 1
            if record.received_at < previous.received_at:
                trades_by_id[key] = record
        derived_records.extend(trades_by_id.values())
        derived_records.sort(
            key=lambda item: (
                item.exchange_time,
                item.received_at,
                type(item).__name__,
            )
        )
        all_payload = b"".join(records_payloads)
        derived_payload = b"".join(
            encode_domain_record(item) for item in derived_records
        )
        qualified = (
            critical_gap_count == 0
            and duplicate_count == 0
            and reject_count == 0
            and all(item["qualified"] is True for item in segment_reports)
        )
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "bot05_h1_window_qualification",
            "qualified": qualified,
            "network_performed": False,
            "market": args.market,
            "channels": list(channels),
            "start_utc": _iso_ms(requested.start_ms),
            "end_utc": _iso_ms(requested.end_ms),
            "coverage": (
                [{"start_ms": requested.start_ms, "end_ms": requested.end_ms}]
                if qualified
                else []
            ),
            "config_sha256": loaded.sha256,
            "code_sha256": code_sha256(),
            "script_sha256": file_sha256(Path(__file__).resolve()),
            "source_manifest_path": str(args.source_manifest.resolve()),
            "source_manifest_sha256": file_sha256(args.source_manifest.resolve()),
            "max_heartbeat_gap_ms": args.max_heartbeat_gap_ms,
            "observed_max_bbo_gap_ms": max_bbo_gap_ms,
            "critical_gap_count": critical_gap_count,
            "duplicate_count": duplicate_count,
            "reject_count": reject_count,
            "record_count": sum(int(item["record_count"]) for item in segment_reports),
            "source_trade_count": trade_count,
            "derived_trade_count": len(trades_by_id),
            "bbo_count": bbo_count,
            "source_records_sha256": hashlib.sha256(all_payload).hexdigest(),
            "derived_record_count": len(derived_records),
            "derived_records_sha256": hashlib.sha256(derived_payload).hexdigest(),
            "trade_retransmission_count": trade_retransmission_count,
            "transformations": [
                "retain_earliest_received_copy_per_consistent_market_trade_id"
            ],
            "segments": segment_reports,
        }
        payload = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        output = args.output.resolve()
        _immutable_write(output, payload)
        _immutable_write(
            output.with_suffix(output.suffix + ".sha256"),
            f"{digest}  {output.name}\n".encode("ascii"),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(
        f"qualified={qualified} segments={len(segment_reports)} "
        f"records={report['record_count']} max_bbo_gap_ms={max_bbo_gap_ms} "
        f"report_sha256={digest}"
    )
    return 0 if qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
