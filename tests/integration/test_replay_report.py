from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bot05.features.opening_drive import DriveDirection
from bot05.replay import (
    ExitReason,
    FailureCode,
    LiquidityRole,
    OrderSide,
    ReplayModel,
    ReplayResult,
    ReplayStatus,
    SimulatedFill,
    build_report,
    write_report,
)

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def _fill(side: OrderSide, *, price: str, fee: str) -> SimulatedFill:
    return SimulatedFill(
        timestamp=NOW,
        side=side,
        role=LiquidityRole.TAKER,
        price=Decimal(price),
        quantity=Decimal(1),
        fee_rate=Decimal("0.0005"),
        fee=Decimal(fee),
        latency_ms=0,
        slippage_bps=Decimal(5),
        book_levels_consumed=0,
    )


def _closed() -> ReplayResult:
    return ReplayResult(
        run_id="1" * 64,
        intent_id="closed",
        market="BTC",
        session_id="us_cash_open",
        direction=DriveDirection.LONG,
        model=ReplayModel.OHLC_CONSERVATIVE,
        status=ReplayStatus.CLOSED,
        config_sha256="2" * 64,
        fee_schedule_sha256="3" * 64,
        signal_data_sha256="4" * 64,
        replay_data_sha256="5" * 64,
        requested_quantity=Decimal(1),
        filled_quantity=Decimal(1),
        entry=_fill(OrderSide.BUY, price="100", fee="0.05"),
        exit=_fill(OrderSide.SELL, price="110", fee="0.055"),
        exit_reason=ExitReason.TARGET,
        failure_code=None,
        same_bar_collision=False,
        target_rested=False,
        target_trade_through=False,
        gross_pnl=Decimal(10),
        funding_pnl=Decimal("-0.1"),
        net_pnl=Decimal("9.795"),
        pnl_r=Decimal("1.959"),
    )


def _failed() -> ReplayResult:
    return ReplayResult(
        run_id="6" * 64,
        intent_id="failed",
        market="BTC",
        session_id="us_cash_open",
        direction=DriveDirection.LONG,
        model=ReplayModel.TRADE_BBO_CENTRAL,
        status=ReplayStatus.FAILED_CLOSED,
        config_sha256="7" * 64,
        fee_schedule_sha256="8" * 64,
        signal_data_sha256="9" * 64,
        replay_data_sha256="a" * 64,
        requested_quantity=Decimal(1),
        filled_quantity=Decimal(1),
        entry=_fill(OrderSide.BUY, price="100", fee="0.05"),
        exit=None,
        exit_reason=None,
        failure_code=FailureCode.FEED_LOSS,
        same_bar_collision=False,
        target_rested=True,
        target_trade_through=False,
        gross_pnl=None,
        funding_pnl=None,
        net_pnl=None,
        pnl_r=None,
    )


def test_report_is_bit_exact_checksummed_and_refuses_overwrite(tmp_path) -> None:
    first = build_report("d4-synthetic", NOW, (_failed(), _closed()))
    second = build_report("d4-synthetic", NOW, (_closed(), _failed()))

    assert first == second
    assert first.json_bytes() == second.json_bytes()
    assert first.metrics.closed_count == 1
    assert first.metrics.failed_closed_count == 1
    assert first.metrics.net_pnl == Decimal("9.795")

    json_path, checksum_path, markdown_path = write_report(first, tmp_path)
    assert hashlib.sha256(json_path.read_bytes()).hexdigest() in (
        checksum_path.read_text(encoding="utf-8")
    )
    assert "ne constitue ni une preuve live" in markdown_path.read_text(
        encoding="utf-8"
    )
    assert write_report(second, tmp_path) == (
        json_path,
        checksum_path,
        markdown_path,
    )

    json_path.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_report(first, tmp_path)
