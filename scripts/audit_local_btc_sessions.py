#!/usr/bin/env python3
"""Audit local BTC opening-drive frequency across qualified H1 windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, cast

from bot05.data.contracts import TimeRange
from bot05.data.normalizer import file_sha256
from bot05.data.reader import read_qualified_event_records
from bot05.data.report import code_sha256
from bot05.features import DriveFilter, DriveThreshold, build_opening_drive
from bot05.features.candles import aggregate_trades
from bot05.models import DatasetProvenance
from bot05.strategy import (
    ConfirmationKind,
    EntryPriceObservation,
    StrategySpec,
    TargetKind,
    advance_candle,
    initialize_strategy,
    observe_entry_price,
    register_opening_drive,
)


class SessionAuditError(ValueError):
    """Raised when qualified local session evidence is incomplete."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("window_reports", nargs="+", type=Path)
    parser.add_argument(
        "--qualification-root",
        type=Path,
        default=Path("reports/data_quality/qualifications"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SessionAuditError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise SessionAuditError(f"{name} must be an integer")
    return value


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
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
            raise SessionAuditError(
                f"refusing to overwrite immutable report: {path}"
            ) from None


def _markdown(report: dict[str, object], digest: str) -> str:
    rolling = _mapping(report["rolling_filters"], "rolling filters")
    official_h0 = _mapping(report["official_h0_parity"], "official H0 parity")
    summary = _mapping(report["summary_by_confirmation"], "confirmation summary")
    lines = [
        "# BOT05 — fréquence descriptive BTC avant le smoke historique",
        "",
        f"- SHA-256 du JSON : `{digest}`",
        f"- Fenêtres H1 qualifiées : {report['observed_window_count']}",
        f"- Candidates ouvrées : {report['weekday_candidate_count']}",
        f"- Opening Drives valides : {report['opening_drive_valid_count']}",
        f"- Parité H0 prête : `{official_h0['ready']}`",
        f"- Historique roulant prêt : `{rolling['ready']}`",
        f"- Conclusion : `{report['research_conclusion']}`",
        f"- Promotion éligible : `{report['promotion_eligible']}`",
        "",
        "## Confirmations",
        "",
    ]
    for name in sorted(summary):
        counts = _mapping(summary[name], f"confirmation {name}")
        lines.append(
            f"- `{name}` : {counts['confirmed_count']} confirmation(s), "
            f"{counts['intent_count']} intent(s)"
        )
    lines.extend(
        (
            "",
            "## Limites",
            "",
            "Le calendrier ouvré reste une heuristique sans source opérateur. Les",
            "quatre candidates sont seize sous le minimum de vingt historiques et",
            "aucune source H0 officielle checksummée n'est disponible en parité.",
            "Cet audit est descriptif ; il ne valide ni edge, ni exécution, ni",
            "promotion.",
            "",
        )
    )
    return "\n".join(lines)


def _verified_window(path: Path) -> tuple[dict[str, object], str]:
    payload = path.read_bytes()
    report = _mapping(json.loads(payload), "window report")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    parts = sidecar.read_text("ascii").strip().split()
    digest = hashlib.sha256(payload).hexdigest()
    if (
        len(parts) != 2
        or parts[0] != digest
        or parts[1] != path.name
        or report.get("kind") != "bot05_h1_window_qualification"
        or report.get("qualified") is not True
        or report.get("market") != "BTC"
    ):
        raise SessionAuditError(f"invalid qualified BTC window: {path}")
    return report, digest


def _coverage(report: dict[str, object]) -> TimeRange:
    raw = report.get("coverage")
    if not isinstance(raw, list) or len(raw) != 1:
        raise SessionAuditError("window must contain one coverage range")
    item = _mapping(raw[0], "coverage")
    return TimeRange(
        _integer(item.get("start_ms"), "coverage start"),
        _integer(item.get("end_ms"), "coverage end"),
    )


def _session(
    path: Path, qualification_root: Path
) -> tuple[dict[str, object], str, str]:
    report, report_sha256 = _verified_window(path)
    requested = _coverage(report)
    segments = report.get("segments")
    if not isinstance(segments, list) or not segments:
        raise SessionAuditError("qualified window has no segments")
    manifests = tuple(
        Path(str(_mapping(item, "segment").get("manifest_path"))) for item in segments
    )
    records = read_qualified_event_records(
        manifests,
        qualification_root=qualification_root,
        market="BTC",
        requested=requested,
    )
    if records.records_sha256 != report.get("derived_records_sha256"):
        raise SessionAuditError(f"qualified window derived checksum mismatch: {path}")
    start = datetime.fromtimestamp(requested.start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(requested.end_ms / 1000, tz=UTC)
    t0 = start.replace(hour=13, minute=30, second=0, microsecond=0)
    if not start <= t0 - timedelta(minutes=30) or end < t0 + timedelta(hours=3):
        raise SessionAuditError("window does not cover the session audit horizon")
    provenance = DatasetProvenance(
        dataset_id=f"btc-session-audit-{t0.date()}",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url=str(path.resolve()),
        raw_sha256=records.source_records_sha256,
        manifest_sha256=records.manifests_sha256,
        adapter_version="bot05-qualified-event-reader-v1",
        calendar_version="weekday_heuristic_unverified_not_for_promotion",
        code_version=f"sha256:{code_sha256()}",
        config_sha256=str(report.get("config_sha256")),
        source_timezone="UTC",
        period_start=start,
        period_end=end,
        transformations=(
            "qualified_h1_trades_only",
            "retain_earliest_consistent_market_trade_id",
            "exchange_time_ohlcv_v1",
        ),
    )
    one = aggregate_trades(
        records.trades,
        market="BTC",
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    five = aggregate_trades(
        records.trades,
        market="BTC",
        interval_seconds=300,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    if not one.complete or not five.complete:
        raise SessionAuditError("qualified window produces candle gaps")
    drive_bars = tuple(
        item
        for item in five.candles
        if t0 <= item.open_time < t0 + timedelta(minutes=15)
    )
    drive_result = build_opening_drive(
        drive_bars,
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=max(item.closed_at for item in drive_bars),
    )
    weekday = t0.weekday() < 5
    strategies: list[dict[str, object]] = []
    if drive_result.drive is not None:
        drive = drive_result.drive
        for confirmation in ConfirmationKind:
            spec = StrategySpec(
                market="BTC",
                session_id="us_cash_open",
                drive_filter=DriveFilter.NONE,
                confirmation=confirmation,
                target=TargetKind.FIXED_2R,
                config_sha256=provenance.config_sha256,
                calendar_version=provenance.calendar_version,
                code_version=provenance.code_version,
            )
            snapshot = register_opening_drive(
                initialize_strategy(
                    spec, t0=t0, source_data_sha256=records.records_sha256
                ),
                drive,
                DriveThreshold(
                    market="BTC",
                    session_id="us_cash_open",
                    filter=DriveFilter.NONE,
                    as_of=t0,
                    sample_count=0,
                    value=Decimal(0),
                    eligible=True,
                ),
            )
            for candle in five.candles:
                if not (
                    t0 + timedelta(minutes=15) <= candle.open_time
                    and candle.close_time <= t0 + timedelta(minutes=60)
                ):
                    continue
                snapshot = advance_candle(
                    snapshot, candle, observed_at=candle.closed_at
                )
                if snapshot.confirmation is not None:
                    entry_candle = next(
                        item
                        for item in one.candles
                        if item.open_time == snapshot.confirmation.confirmed_at
                    )
                    first_trade = min(
                        (
                            item
                            for item in records.trades
                            if entry_candle.open_time
                            <= item.exchange_time
                            < entry_candle.close_time
                            and item.price == entry_candle.open
                        ),
                        key=lambda item: (item.exchange_time, item.received_at),
                    )
                    snapshot = observe_entry_price(
                        snapshot,
                        EntryPriceObservation(
                            market="BTC",
                            observed_at=first_trade.received_at,
                            price=entry_candle.open,
                            source="qualified_h1_next_1m_open",
                        ),
                    )
                    break
            strategies.append(
                {
                    "confirmation": confirmation.value,
                    "state": snapshot.state.value,
                    "reason": snapshot.reason,
                    "touch": snapshot.touch is not None,
                    "confirmed": snapshot.confirmation is not None,
                    "intent": snapshot.intent is not None,
                    "confirmation_time": (
                        None
                        if snapshot.confirmation is None
                        else _iso(snapshot.confirmation.confirmed_at)
                    ),
                }
            )
        opening_drive: dict[str, object] = {
            "valid": True,
            "direction": drive.direction.value,
            "body_bps": str(drive.body_bps),
            "range_bps": str(drive.range_bps),
            "close_location": str(drive.close_location),
        }
    else:
        opening_drive = {
            "valid": False,
            "reason": drive_result.rejection_reason,
        }
    session = {
        "date": t0.date().isoformat(),
        "t0_utc": _iso(t0),
        "weekday_heuristic": weekday,
        "calendar_status": (
            "candidate_requires_operator_calendar"
            if weekday
            else "excluded_weekend_by_baseline"
        ),
        "window_report_path": str(path.resolve()),
        "window_report_sha256": report_sha256,
        "source_record_count": sum(item.record_count for item in records.sources),
        "derived_record_count": len(records.records),
        "trade_count": len(records.trades),
        "bbo_count": len(records.books),
        "trade_retransmission_count": records.duplicate_trade_count,
        "max_bbo_gap_ms": records.max_bbo_gap_ms,
        "candles_1m": len(one.candles),
        "candles_5m": len(five.candles),
        "opening_drive": opening_drive,
        "strategies_fixed_2r_drive_none": strategies,
    }
    return session, records.records_sha256, records.manifests_sha256


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths = tuple(sorted(path.resolve() for path in args.window_reports))
        sessions: list[dict[str, object]] = []
        record_hashes: list[str] = []
        manifest_hashes: list[str] = []
        for path in paths:
            session, records_sha256, manifests_sha256 = _session(
                path, args.qualification_root.resolve()
            )
            sessions.append(session)
            record_hashes.append(records_sha256)
            manifest_hashes.append(manifests_sha256)
        weekday_sessions = [item for item in sessions if item["weekday_heuristic"]]
        summary_by_confirmation = {
            confirmation.value: {
                "confirmed_count": sum(
                    any(
                        strategy["confirmation"] == confirmation.value
                        and strategy["confirmed"] is True
                        for strategy in cast(
                            list[dict[str, object]],
                            item["strategies_fixed_2r_drive_none"],
                        )
                    )
                    for item in weekday_sessions
                ),
                "intent_count": sum(
                    any(
                        strategy["confirmation"] == confirmation.value
                        and strategy["intent"] is True
                        for strategy in cast(
                            list[dict[str, object]],
                            item["strategies_fixed_2r_drive_none"],
                        )
                    )
                    for item in weekday_sessions
                ),
            }
            for confirmation in ConfirmationKind
        }
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "bot05_local_btc_session_frequency_audit",
            "generated_from_local_data_only": True,
            "network_performed": False,
            "market": "BTC",
            "session_id": "us_cash_open",
            "evidence_tier": "H1",
            "observed_window_count": len(sessions),
            "weekday_candidate_count": len(weekday_sessions),
            "operator_calendar_qualified": False,
            "opening_drive_valid_count": sum(
                _mapping(item["opening_drive"], "opening_drive").get("valid") is True
                for item in weekday_sessions
            ),
            "summary_by_confirmation": summary_by_confirmation,
            "rolling_filters": {
                "minimum_required_prior_sessions": 20,
                "locally_available_weekday_candidates": len(weekday_sessions),
                "ready": False,
                "reason": "insufficient_prior_sessions_and_no_operator_calendar",
            },
            "official_h0_parity": {
                "ready": False,
                "reason": "no_local_checksummed_h0_common_window",
            },
            "research_conclusion": "data_insufficient",
            "promotion_eligible": False,
            "code_sha256": code_sha256(),
            "script_sha256": file_sha256(Path(__file__).resolve()),
            "combined_derived_records_sha256": hashlib.sha256(
                "\n".join(record_hashes).encode("ascii")
            ).hexdigest(),
            "combined_manifests_sha256": hashlib.sha256(
                "\n".join(manifest_hashes).encode("ascii")
            ).hexdigest(),
            "sessions": sessions,
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
        _immutable_write(
            output.with_suffix(".md"), _markdown(report, digest).encode("utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(
        f"sessions={len(sessions)} weekday_candidates={len(weekday_sessions)} "
        f"report_sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
