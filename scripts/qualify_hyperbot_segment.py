#!/usr/bin/env python3
"""Qualify one bounded local HyperBot H1 segment without network access."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from bot05.config import load_config
from bot05.data.contracts import TimeRange
from bot05.data.qualification import (
    QualificationError,
    audit_hyperbot_segment,
    build_qualification_report,
    load_hyperbot_segment_spec,
    provenance_for_hyperbot_segment,
    write_qualification_report,
)
from bot05.data.report import code_sha256
from bot05.data.store import AppendOnlyStore, QualificationEvidence


def _utc_ms(value: str) -> int:
    if not value.endswith("Z"):
        raise argparse.ArgumentTypeError("timestamp must have a UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 UTC") from exc
    return int(parsed.timestamp() * 1000)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/research.toml"))
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument(
        "--channels", nargs="+", default=["trades", "bbo"], choices=["trades", "bbo"]
    )
    parser.add_argument("--start-utc", type=_utc_ms)
    parser.add_argument("--end-utc", type=_utc_ms)
    parser.add_argument("--max-heartbeat-gap-ms", type=int, default=15_000)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path("reports/data_quality/qualifications"),
    )
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_config(args.config)
        spec = load_hyperbot_segment_spec(args.source_manifest, args.segment)
        if (args.start_utc is None) != (args.end_utc is None):
            raise QualificationError("start and end must be supplied together")
        window = (
            spec.source_period
            if args.start_utc is None
            else TimeRange(args.start_utc, args.end_utc)
        )
        channels = tuple(args.channels)
        dataset_id = (
            f"h1-{args.market.lower()}-{spec.storage_sha256[:12]}-"
            f"{window.start_ms}-{window.end_ms}"
        )
        provenance = provenance_for_hyperbot_segment(
            spec,
            dataset_id=dataset_id,
            code_version=f"sha256:{code_sha256()}",
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
        store_root = (
            args.store_root
            if args.store_root is not None
            else loaded.config.data.local_data_dir / "normalized"
        ).resolve()
        stored = AppendOnlyStore(store_root).append(
            provenance.dataset_id, provenance, audit.result
        )
        report = build_qualification_report(audit, stored)
        report_root = args.report_root.resolve()
        report_path = report_root / f"{dataset_id}-{stored.segment_id[:16]}.json"
        report_sha256 = write_qualification_report(report_path, report)
        if audit.qualified:
            stored.as_data_asset(
                QualificationEvidence(
                    report_path=report_path,
                    report_sha256=report_sha256,
                    coverage=(window,),
                )
            )
    except (OSError, ValueError) as exc:
        _fail(str(exc))
    print(
        f"qualified={audit.qualified} records={len(audit.result.records)} "
        f"rejects={len(audit.result.rejects)} report_sha256={report_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
