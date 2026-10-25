"""Backtest historique : absence de fuite, règles d'exécution, découpage 70/30."""

from decimal import Decimal

import numpy as np
import pytest

from mcs.backtest import BacktestContext, BacktestResult, HistoricalBroker, run_backtest
from mcs.broker.base import Order, OrderStatus, Reason
from mcs.broker.fees import FeeSchedule
from mcs.indicators.signals import EmaCross, RsiConfirmation
from mcs.market_data import INTERVAL_MS
from mcs.strategy.config import (
    EntryOrder,
    ExecutionConfig,
    ExitConfig,
    PortfolioConfig,
    SignalConfig,
    Sizing,
    StopConfig,
    StopType,
    StrategyConfig,
    TakeProfitConfig,
    TakeProfitType,
    TrailingConfig,
    TrailingType,
)
from tests.backtest_data import HOUR, START, dataset

D = Decimal
BAR = INTERVAL_MS["4h"]
DAY = INTERVAL_MS["1d"]
FEES = FeeSchedule()

CONFIG = StrategyConfig(
    signals=SignalConfig(EmaCross(8, 30), 0.7, ((RsiConfirmation(14, 75.0), 0.3),), 0.7),
    stop=StopConfig(StopType.ATR, atr_multiple=D(2)),
    trailing=TrailingConfig(TrailingType.ATR, atr_multiple=D(3)),
    take_profit=TakeProfitConfig(TakeProfitType.PARTIAL, r_multiple=D(2)),
    execution=ExecutionConfig(EntryOrder.LIMIT, 2),
    exits=ExitConfig(reverse_signal=True),
    portfolio=PortfolioConfig(sizing=Sizing.RISK, max_entries=2, max_per_crypto=D("0.6")),
)


@pytest.fixture(scope="module")
def data():  # type: ignore[no-untyped-def]
    return dataset()


def fingerprint(result: BacktestResult) -> list[tuple]:
    return [
        (o.market, o.side, o.reason, o.status, o.quantity, o.price, o.created_at, o.filled_at)
        for o in result.orders
    ]


def test_backtest_trades_and_respects_200_days(data) -> None:  # type: ignore[no-untyped-def]
    start, _, end = data.split()
    result = run_backtest(CONFIG, data, start, end, FEES, keep_orders=True)
    filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
    assert result.trades >= 5 and filled
    assert min(o.created_at for o in filled) >= START + 200 * DAY  # 200 jours d'historique
    assert result.final_value > 0 and 0 <= result.max_drawdown < 1
    assert result.fees_paid > 0


def test_no_lookahead_truncated_history_gives_same_decisions(data) -> None:  # type: ignore[no-untyped-def]
    start, _, end = data.split()
    cut = start + (end - start) * 9 // 10 // BAR * BAR
    full = run_backtest(CONFIG, data, start, cut, FEES, keep_orders=True)
    truncated = dataset(end=cut)
    partial = run_backtest(CONFIG, truncated, start, cut, FEES, keep_orders=True)
    assert fingerprint(full) == fingerprint(partial)
    assert full.final_value == partial.final_value


def test_split_is_chronological_and_aligned(data) -> None:  # type: ignore[no-untyped-def]
    start, cut, end = data.split()
    assert start < cut < end and cut % BAR == 0
    assert abs((cut - start) / (end - start) - 0.7) < 0.01


def test_validation_starts_fresh(data) -> None:  # type: ignore[no-untyped-def]
    _, cut, end = data.split()
    result = run_backtest(CONFIG, data, cut, end, FEES, keep_orders=True)
    assert all(o.created_at >= cut for o in result.orders)


def test_score_rules() -> None:
    few = BacktestResult(D(1100), 0.1, 0.05, 9, 5, D(1))
    assert few.score == 0.0
    ok = BacktestResult(D(1100), 0.1, 0.05, 10, 5, D(1))
    assert ok.score == pytest.approx(2.0)
    flat = BacktestResult(D(1010), 0.01, 0.0, 12, 6, D(1))
    assert flat.score == pytest.approx(1.0)  # drawdown plancher à 1 %


# --- Règles d'exécution du broker historique -----------------------------------------------


@pytest.fixture
def broker(data):  # type: ignore[no-untyped-def]
    ctx = BacktestContext(data)
    ctx.set_now(START + 250 * DAY)
    broker = HistoricalBroker(ctx, FEES)
    broker.open_account(1, D(1000))
    return broker


async def test_market_buy_pays_half_spread(broker) -> None:  # type: ignore[no-untyped-def]
    last = broker.ctx.last_price("AAA-EUR")
    order = await broker.buy_market(1, "AAA-EUR", D(100), Reason.SIGNAL, broker.ctx.now)
    assert isinstance(order, Order)
    assert order.price == last * D("1.0005")
    assert order.notional + order.fee <= D(100)


async def test_stop_fills_at_open_when_gapped(broker) -> None:  # type: ignore[no-untyped-def]
    buy = await broker.buy_market(1, "AAA-EUR", D(100), Reason.SIGNAL, broker.ctx.now)
    broker.current_bar["AAA-EUR"] = np.array([90.0, 91.0, 89.0, 90.5])
    sell = await broker.sell_market(
        1, "AAA-EUR", buy.quantity, Reason.STOP, broker.ctx.now, trigger_price=D(95)
    )
    assert sell.price == D("90.0") * D("0.999")  # ouverture sous le stop, moins un spread
    broker.current_bar["AAA-EUR"] = np.array([99.0, 100.0, 94.0, 96.0])
    buy = await broker.buy_market(1, "AAA-EUR", D(100), Reason.SIGNAL, broker.ctx.now)
    sell = await broker.sell_market(
        1, "AAA-EUR", buy.quantity, Reason.TRAILING, broker.ctx.now, trigger_price=D(95)
    )
    assert sell.price == D(95) * D("0.999")


async def test_limit_orders_need_strict_cross(broker) -> None:  # type: ignore[no-untyped-def]
    now = broker.ctx.now
    order = await broker.place_limit_buy(1, "AAA-EUR", D(50), D(100), now + 2 * HOUR,
                                         Reason.SIGNAL, now)  # fmt: skip
    assert broker.on_bar("AAA-EUR", now, np.array([51.0, 52.0, 50.0, 51.0])) == []
    assert broker.on_bar("AAA-EUR", now - HOUR, np.array([49.0, 52.0, 49.0, 51.0])) == []
    (filled,) = broker.on_bar("AAA-EUR", now + HOUR, np.array([51.0, 52.0, 49.99, 51.0]))
    assert filled.id == order.id and filled.price == D(50) and filled.liquidity == "maker"
    assert broker.holdings(1) == {"AAA-EUR": order.quantity}


def test_stop_checked_before_take_profit_in_same_hour(data) -> None:  # type: ignore[no-untyped-def]
    config = StrategyConfig(
        signals=SignalConfig(EmaCross(8, 30), 1.0),
        stop=StopConfig(StopType.FIXED_PCT, pct=D("0.01")),
        take_profit=TakeProfitConfig(TakeProfitType.FIXED_PCT, pct=D("0.01")),
        execution=ExecutionConfig(EntryOrder.MARKET),
        exits=ExitConfig(reverse_signal=False),
        portfolio=PortfolioConfig(sizing=Sizing.FIXED_PCT),
    )
    start, _, end = data.split()
    result = run_backtest(config, data, start, end, FEES, keep_orders=True)
    filled = sorted(
        (o for o in result.orders if o.status is OrderStatus.FILLED), key=lambda o: o.filled_at
    )
    entries: dict[str, Order] = {}
    take_profits = stops = 0
    for order in filled:
        if order.side == "buy":
            entries[order.market] = order
            continue
        entry = entries.pop(order.market)
        stop_level = entry.price * D("0.99")
        bar = data.markets[order.market].bar(order.filled_at)
        if order.reason is Reason.TAKE_PROFIT:
            take_profits += 1
            # Un TP n'est jamais pris dans une heure où le stop était aussi atteint.
            assert D(repr(float(bar[2]))) > stop_level
        elif order.reason is Reason.STOP:
            stops += 1
    assert take_profits and stops
