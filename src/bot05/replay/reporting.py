"""Deterministic replay metrics and immutable JSON/Markdown report output."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bot05.replay.contracts import (
    ReplayContractError,
    ReplayResult,
    ReplayStatus,
    SimulatedFill,
    utc_text,
)

REPORT_SCHEMA_VERSION = 1


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _fill_payload(fill: SimulatedFill | None) -> dict[str, object] | None:
    if fill is None:
        return None
    return {
        "benchmark_price": (
            None if fill.benchmark_price is None else str(fill.benchmark_price)
        ),
        "book_levels_consumed": fill.book_levels_consumed,
        "fee": str(fill.fee),
        "fee_rate": str(fill.fee_rate),
        "latency_ms": fill.latency_ms,
        "impact_bps": None if fill.impact_bps is None else str(fill.impact_bps),
        "price": str(fill.price),
        "quantity": str(fill.quantity),
        "role": fill.role.value,
        "side": fill.side.value,
        "slippage_bps": str(fill.slippage_bps),
        "spread_bps": None if fill.spread_bps is None else str(fill.spread_bps),
        "timestamp": utc_text(fill.timestamp),
    }


def result_payload(result: ReplayResult) -> dict[str, object]:
    return {
        "config_sha256": result.config_sha256,
        "entry": _fill_payload(result.entry),
        "exit": _fill_payload(result.exit),
        "exit_reason": (
            None if result.exit_reason is None else result.exit_reason.value
        ),
        "failure_code": (
            None if result.failure_code is None else result.failure_code.value
        ),
        "fee_schedule_sha256": result.fee_schedule_sha256,
        "filled_quantity": str(result.filled_quantity),
        "funding_pnl": _decimal(result.funding_pnl),
        "gross_pnl": _decimal(result.gross_pnl),
        "intent_id": result.intent_id,
        "market": result.market,
        "session_id": result.session_id,
        "direction": result.direction.value,
        "model": result.model.value,
        "net_pnl": _decimal(result.net_pnl),
        "pnl_r": _decimal(result.pnl_r),
        "replay_data_sha256": result.replay_data_sha256,
        "requested_quantity": str(result.requested_quantity),
        "run_id": result.run_id,
        "same_bar_collision": result.same_bar_collision,
        "signal_data_sha256": result.signal_data_sha256,
        "status": result.status.value,
        "target_rested": result.target_rested,
        "target_trade_through": result.target_trade_through,
    }


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    result_count: int
    closed_count: int
    unfilled_count: int
    failed_closed_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal | None
    gross_pnl: Decimal
    fees: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal
    mean_pnl_r: Decimal | None
    max_drawdown: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "closed_count": self.closed_count,
            "failed_closed_count": self.failed_closed_count,
            "fees": str(self.fees),
            "funding_pnl": str(self.funding_pnl),
            "gross_pnl": str(self.gross_pnl),
            "loss_count": self.loss_count,
            "max_drawdown": str(self.max_drawdown),
            "mean_pnl_r": _decimal(self.mean_pnl_r),
            "net_pnl": str(self.net_pnl),
            "result_count": self.result_count,
            "unfilled_count": self.unfilled_count,
            "win_count": self.win_count,
            "win_rate": _decimal(self.win_rate),
        }


def calculate_metrics(results: tuple[ReplayResult, ...]) -> ReplayMetrics:
    closed = tuple(item for item in results if item.status is ReplayStatus.CLOSED)
    ordered = tuple(
        sorted(
            closed,
            key=lambda item: (
                item.entry.timestamp if item.entry is not None else datetime.min,
                item.run_id,
            ),
        )
    )
    gross = sum((item.gross_pnl or Decimal(0) for item in closed), Decimal(0))
    funding = sum((item.funding_pnl or Decimal(0) for item in closed), Decimal(0))
    net = sum((item.net_pnl or Decimal(0) for item in closed), Decimal(0))
    fees = sum(
        (
            (item.entry.fee if item.entry is not None else Decimal(0))
            + (item.exit.fee if item.exit is not None else Decimal(0))
            for item in closed
        ),
        Decimal(0),
    )
    wins = sum(item.net_pnl is not None and item.net_pnl > 0 for item in closed)
    losses = sum(item.net_pnl is not None and item.net_pnl <= 0 for item in closed)
    pnl_rs = tuple(item.pnl_r for item in closed if item.pnl_r is not None)
    equity = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for result in ordered:
        equity += result.net_pnl or Decimal(0)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return ReplayMetrics(
        result_count=len(results),
        closed_count=len(closed),
        unfilled_count=sum(item.status is ReplayStatus.UNFILLED for item in results),
        failed_closed_count=sum(
            item.status is ReplayStatus.FAILED_CLOSED for item in results
        ),
        win_count=wins,
        loss_count=losses,
        win_rate=(Decimal(wins) / Decimal(len(closed)) if closed else None),
        gross_pnl=gross,
        fees=fees,
        funding_pnl=funding,
        net_pnl=net,
        mean_pnl_r=(sum(pnl_rs, Decimal(0)) / Decimal(len(pnl_rs)) if pnl_rs else None),
        max_drawdown=max_drawdown,
    )


@dataclass(frozen=True, slots=True)
class ReplayReport:
    experiment_id: str
    generated_at: datetime
    results: tuple[ReplayResult, ...]
    metrics: ReplayMetrics
    report_id: str

    def json_bytes(self) -> bytes:
        payload = {
            "experiment_id": self.experiment_id,
            "generated_at": utc_text(self.generated_at),
            "metrics": self.metrics.payload(),
            "report_id": self.report_id,
            "results": [result_payload(item) for item in self.results],
            "schema_version": REPORT_SCHEMA_VERSION,
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()

    def markdown(self) -> str:
        win_rate = (
            "n/a"
            if self.metrics.win_rate is None
            else f"{self.metrics.win_rate * Decimal(100)} %"
        )
        return "\n".join(
            (
                "# Rapport de replay BOT05",
                "",
                f"- Expérience : `{self.experiment_id}`",
                f"- Rapport JSON : `{self.report_id}`",
                f"- Généré à : `{utc_text(self.generated_at)}`",
                f"- Résultats : {self.metrics.result_count}",
                f"- Positions closes : {self.metrics.closed_count}",
                f"- Non exécutés : {self.metrics.unfilled_count}",
                f"- Fail-closed : {self.metrics.failed_closed_count}",
                f"- Taux de réussite clos : {win_rate}",
                f"- PnL brut : {self.metrics.gross_pnl}",
                f"- Frais : {self.metrics.fees}",
                f"- Funding : {self.metrics.funding_pnl}",
                f"- PnL net : {self.metrics.net_pnl}",
                f"- Drawdown maximal : {self.metrics.max_drawdown}",
                "",
                "> Ce replay de recherche ne constitue ni une preuve live "
                "ni une garantie.",
                "",
            )
        )


def build_report(
    experiment_id: str,
    generated_at: datetime,
    results: tuple[ReplayResult, ...],
) -> ReplayReport:
    if not experiment_id.strip() or not results:
        raise ReplayContractError("report experiment and results are required")
    utc_text(generated_at)
    ordered = tuple(sorted(results, key=lambda item: item.run_id))
    if len({item.run_id for item in ordered}) != len(ordered):
        raise ReplayContractError("report run ids must be unique")
    metrics = calculate_metrics(ordered)
    body = {
        "experiment_id": experiment_id,
        "generated_at": utc_text(generated_at),
        "metrics": metrics.payload(),
        "results": [result_payload(item) for item in ordered],
        "schema_version": REPORT_SCHEMA_VERSION,
    }
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    report_id = hashlib.sha256(encoded).hexdigest()
    return ReplayReport(experiment_id, generated_at, ordered, metrics, report_id)


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"refusing to overwrite published report: {path}")
        return
    path.write_bytes(content)


def write_report(
    report: ReplayReport, output_directory: Path
) -> tuple[Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"{report.report_id}.json"
    checksum_path = json_path.with_suffix(".json.sha256")
    markdown_path = output_directory / f"{report.report_id}.md"
    json_bytes = report.json_bytes()
    digest = hashlib.sha256(json_bytes).hexdigest()
    _write_immutable(json_path, json_bytes)
    _write_immutable(checksum_path, f"{digest}  {json_path.name}\n".encode())
    _write_immutable(markdown_path, report.markdown().encode())
    return json_path, checksum_path, markdown_path
