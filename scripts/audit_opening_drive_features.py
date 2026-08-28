#!/usr/bin/env python3
"""Build a checksummed causal-feature audit from qualified local BOT05 data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

from bot05.config import load_config
from bot05.data.contracts import Qualification, SourceProject, TimeRange
from bot05.data.inventory import discover_local_inventory
from bot05.data.normalizer import file_sha256
from bot05.data.report import code_sha256
from bot05.features.candles import (
    CandleBuildResult,
    aggregate_candles,
    aggregate_trades,
)
from bot05.features.opening_drive import build_opening_drive
from bot05.models import (
    Candle,
    DatasetProvenance,
    Trade,
    decode_domain_record,
    encode_domain_record,
)


class FeatureAuditError(ValueError):
    """Raised when qualified inputs or derived outputs are inconsistent."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/research_btc_open_qualified.toml")
    )
    parser.add_argument("--market", default="BTC")
    parser.add_argument("--session-id", default="us_cash_open")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/features/btc_us_open_2026-08-21.json"),
    )
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _combined_sha256(values: tuple[str, ...]) -> str:
    return _sha256_bytes("\n".join(values).encode("ascii"))


def _overlaps(left: TimeRange, right: TimeRange) -> bool:
    return left.start_ms < right.end_ms and right.start_ms < left.end_ms


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise FeatureAuditError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _verified_trades(
    manifest_paths: tuple[Path, ...], market: str, qualification_root: Path
) -> tuple[tuple[Trade, ...], tuple[dict[str, object], ...]]:
    trades: list[Trade] = []
    sources: list[dict[str, object]] = []
    for manifest_path in manifest_paths:
        manifest = _mapping(json.loads(manifest_path.read_bytes()), "store manifest")
        dataset_id = manifest.get("dataset_id")
        segment_id = manifest.get("segment_id")
        records_file = manifest.get("records_file")
        records_sha256 = manifest.get("records_sha256")
        if not all(
            isinstance(value, str) and value
            for value in (dataset_id, segment_id, records_file, records_sha256)
        ):
            raise FeatureAuditError("store manifest identity is invalid")
        dataset_root = manifest_path.parent.parent
        records_path = (dataset_root / cast(str, records_file)).resolve()
        records_path.relative_to(dataset_root.resolve())
        if file_sha256(records_path) != records_sha256:
            raise FeatureAuditError("normalized records checksum mismatch")
        qualification_matches = tuple(
            qualification_root.glob(f"*-{cast(str, segment_id)[:16]}.json")
        )
        if len(qualification_matches) != 1:
            raise FeatureAuditError("qualification report cannot be resolved uniquely")
        qualification_path = qualification_matches[0].resolve()
        qualification = _mapping(
            json.loads(qualification_path.read_bytes()), "qualification report"
        )
        sidecar_path = qualification_path.with_suffix(
            qualification_path.suffix + ".sha256"
        )
        sidecar_parts = sidecar_path.read_text("ascii").strip().split()
        if (
            qualification.get("segment_id") != segment_id
            or len(sidecar_parts) != 2
            or sidecar_parts[1] != qualification_path.name
            or file_sha256(qualification_path) != sidecar_parts[0]
        ):
            raise FeatureAuditError("qualification report checksum mismatch")
        source_trade_count = 0
        with records_path.open("rb") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    record = decode_domain_record(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise FeatureAuditError(
                        f"invalid normalized record at line {line_number}"
                    ) from exc
                if isinstance(record, Trade) and record.market == market:
                    trades.append(record)
                    source_trade_count += 1
        sources.append(
            {
                "dataset_id": dataset_id,
                "segment_id": segment_id,
                "manifest_path": str(manifest_path),
                "manifest_sha256": file_sha256(manifest_path),
                "records_path": str(records_path),
                "records_sha256": records_sha256,
                "qualification_report_path": str(qualification_path),
                "qualification_report_sha256": sidecar_parts[0],
                "trade_count": source_trade_count,
            }
        )
    return tuple(trades), tuple(sources)


def _candle_digest(candles: tuple[Candle, ...]) -> str:
    return _sha256_bytes(b"".join(encode_domain_record(item) for item in candles))


def _aggregation_summary(result: CandleBuildResult) -> dict[str, object]:
    return {
        "candle_count": len(result.candles),
        "gap_count": len(result.gaps),
        "gaps": [
            {
                "start": _iso(item.start),
                "end": _iso(item.end),
                "reason": item.reason.value,
            }
            for item in result.gaps
        ],
        "records_sha256": _candle_digest(result.candles),
    }


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise FeatureAuditError(
                f"refusing to overwrite immutable report: {path}"
            ) from None


def _render_markdown(report: Mapping[str, object], digest: str) -> str:
    aggregation = _mapping(report["aggregation"], "aggregation")
    one_minute = _mapping(aggregation["candles_1m"], "candles_1m")
    five_minute = _mapping(aggregation["candles_5m_direct"], "candles_5m_direct")
    drive = report["opening_drive"]
    drive_status = (
        "accepté par le filtre de quartile externe"
        if isinstance(drive, dict) and drive.get("status") == "valid"
        else f"rejeté : {drive.get('reason') if isinstance(drive, dict) else 'inconnu'}"
    )
    return "\n".join(
        (
            "# BOT05 — audit causal de l’opening drive BTC",
            "",
            "Ce rapport dérive localement les bougies depuis les trades H1 qualifiés.",
            "Aucun appel réseau ni donnée non qualifiée n’a été utilisé.",
            "",
            f"- SHA-256 du JSON : `{digest}`",
            f"- Fenêtre : `{report['start_utc']}` → `{report['end_utc']}`",
            f"- Trades : {aggregation['trade_count']}",
            (
                f"- Bougies 1m : {one_minute['candle_count']} "
                f"(gaps : {one_minute['gap_count']})"
            ),
            (
                f"- Bougies 5m : {five_minute['candle_count']} "
                f"(gaps : {five_minute['gap_count']})"
            ),
            f"- Parité 5m directe / rollup 1m : `{aggregation['internal_5m_parity']}`",
            f"- Opening drive : {drive_status}",
            "",
            "## Limites",
            "",
            "- La parité H0 reste en attente : aucune source officielle checksummée",
            "  n’est présente sur cette fenêtre.",
            "- Une session ne suffit pas aux filtres roulants q50/q75 ;",
            "  vingt sessions comparables antérieures sont requises.",
            "- Le niveau de preuve reste H1 partagé et ne devient pas H2 BOT05.",
            "",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_config(args.config)
        config = loaded.config
        if config.universe.markets != (args.market,):
            raise FeatureAuditError(
                "audit config must contain exactly the requested market"
            )
        start = config.coverage.start_utc
        end = config.coverage.end_utc
        requested = TimeRange(_ms(start), _ms(end))
        qualification_root = Path("reports/data_quality/qualifications").resolve()
        inventory = discover_local_inventory(
            config.data.hyperbot_root,
            config.data.trident_root,
            bot05_data_root=config.data.local_data_dir,
            qualification_root=qualification_root,
        )
        if inventory.issues:
            raise FeatureAuditError("local inventory contains qualification issues")
        assets = tuple(
            item
            for item in inventory.assets
            if item.source_project is SourceProject.BOT05
            and item.qualification is Qualification.QUALIFIED
            and item.markets == (args.market,)
            and "trades" in item.channels
            and any(_overlaps(span, requested) for span in item.coverage)
        )
        assets = tuple(sorted(assets, key=lambda item: item.coverage[0]))
        coverage = tuple(span for item in assets for span in item.coverage)
        manifest_paths = tuple(item.path for item in assets)
        trades, sources = _verified_trades(
            manifest_paths, args.market, qualification_root
        )
        records_hashes = tuple(cast(str, item["records_sha256"]) for item in sources)
        manifest_hashes = tuple(cast(str, item["manifest_sha256"]) for item in sources)
        provenance = DatasetProvenance(
            dataset_id=f"derived-{args.market.lower()}-{requested.start_ms}-{requested.end_ms}",
            evidence_tier="H1",
            source="bot05",
            source_path_or_url=";".join(str(item) for item in manifest_paths),
            raw_sha256=_combined_sha256(records_hashes),
            manifest_sha256=_combined_sha256(manifest_hashes),
            adapter_version="bot05-causal-candles-v1",
            calendar_version=(
                f"session_id:{args.session_id}:calendar_resolution_external"
            ),
            code_version=f"sha256:{code_sha256()}",
            config_sha256=loaded.sha256,
            source_timezone="UTC",
            period_start=start,
            period_end=end,
            transformations=(
                "qualified_h1_trades_only",
                "exchange_time_ohlcv_v1",
                "explicit_gap_policy_v1",
            ),
        )
        one_minute = aggregate_trades(
            trades,
            market=args.market,
            interval_seconds=60,
            requested=requested,
            qualified_coverage=coverage,
            provenance=provenance,
        )
        five_minute = aggregate_trades(
            trades,
            market=args.market,
            interval_seconds=300,
            requested=requested,
            qualified_coverage=coverage,
            provenance=provenance,
        )
        five_from_one = aggregate_candles(
            one_minute.candles,
            market=args.market,
            interval_seconds=300,
            requested=requested,
            provenance=provenance,
        )
        parity = five_minute.candles == five_from_one.candles
        if len(five_minute.candles) != 3 or five_minute.gaps:
            raise FeatureAuditError("opening drive requires three gap-free 5m candles")
        drive_observed_at = max(item.closed_at for item in five_minute.candles)
        drive_result = build_opening_drive(
            five_minute.candles,
            market=args.market,
            session_id=args.session_id,
            t0=start,
            observed_at=drive_observed_at,
        )
        if drive_result.drive is None:
            opening_drive: dict[str, object] = {
                "status": "rejected",
                "reason": drive_result.rejection_reason,
            }
        else:
            drive = drive_result.drive
            opening_drive = {
                "status": "valid",
                "observed_at": _iso(drive.observed_at),
                "direction": drive.direction.value,
                "open": str(drive.open),
                "high": str(drive.high),
                "low": str(drive.low),
                "close": str(drive.close),
                "body_bps": str(drive.body_bps),
                "range_bps": str(drive.range_bps),
                "close_location": str(drive.close_location),
                "midpoint": str(drive.midpoint),
            }
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "bot05_causal_opening_drive_feature_audit",
            "market": args.market,
            "session_id": args.session_id,
            "start_utc": _iso(start),
            "end_utc": _iso(end),
            "network_performed": False,
            "evidence_tier": "H1",
            "config_sha256": loaded.sha256,
            "code_sha256": code_sha256(),
            "derived_provenance": {
                "dataset_id": provenance.dataset_id,
                "source_records_sha256": provenance.raw_sha256,
                "source_manifests_sha256": provenance.manifest_sha256,
                "adapter_version": provenance.adapter_version,
                "code_version": provenance.code_version,
                "transformations": list(provenance.transformations),
            },
            "sources": list(sources),
            "aggregation": {
                "trade_count": len(
                    [
                        item
                        for item in trades
                        if requested.start_ms
                        <= _ms(item.exchange_time)
                        < requested.end_ms
                    ]
                ),
                "candles_1m": _aggregation_summary(one_minute),
                "candles_5m_direct": _aggregation_summary(five_minute),
                "candles_5m_from_1m": _aggregation_summary(five_from_one),
                "internal_5m_parity": parity,
            },
            "opening_drive": opening_drive,
            "rolling_filters": {
                "drive_none": "available",
                "drive_q50": "unavailable_requires_20_prior_comparable_sessions",
                "drive_q75": "unavailable_requires_20_prior_comparable_sessions",
                "current_session_excluded_by_contract": True,
            },
            "official_candle_parity": {
                "status": "pending",
                "reason": "no_checksummed_h0_official_candles_for_common_window",
            },
        }
        payload = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = _sha256_bytes(payload)
        output = args.output.resolve()
        _immutable_write(output, payload)
        _immutable_write(
            output.with_suffix(output.suffix + ".sha256"),
            f"{digest}  {output.name}\n".encode("ascii"),
        )
        _immutable_write(
            output.with_suffix(".md"), _render_markdown(report, digest).encode("utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(
        f"trades={report['aggregation']['trade_count']} "
        f"internal_5m_parity={parity} report_sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
