"""Stratégie et gestion du risque : entrées, dimensionnement, stops, trailing, take profit,
renforcement, pause, circuit breaker, filtre de fond, persistance des positions."""

import sqlite3
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from mcs.broker.base import OrderStatus, Reason
from mcs.risk.exits import initial_stop, take_profit_level, trailing_level
from mcs.risk.position import Position
from mcs.risk.sizing import SizingInputs, entry_amount
from mcs.storage.orders import load_orders
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
    TrailingActivation,
    TrailingConfig,
    TrailingType,
    TrendAction,
    TrendFilterConfig,
)
from mcs.strategy.context import FramesContext
from mcs.strategy.simulation import PendingEntry
from tests.strategy_harness import BAR, MARKET, Harness, Switch

D = Decimal


def make_config(main: Switch, **overrides: Any) -> StrategyConfig:
    defaults: dict[str, Any] = {
        "signals": SignalConfig(main, 1.0, threshold=0.5),  # type: ignore[arg-type]
        "stop": StopConfig(StopType.FIXED_PCT, pct=D("0.05")),
        "execution": ExecutionConfig(EntryOrder.MARKET),
        "exits": ExitConfig(reverse_signal=False),
        "portfolio": PortfolioConfig(sizing=Sizing.FIXED_PCT, max_entry=D("0.25")),
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def position(h: Harness) -> Position:
    return h.sim.positions[MARKET]


# --- Dimensionnement ----------------------------------------------------------------------


def inputs(**kw: Any) -> SizingInputs:
    base = {
        "capital": D(1000),
        "available_cash": D(1000),
        "exposure": D(0),
        "market_exposure": D(0),
        "stop_distance": D("0.04"),
    }
    base.update(kw)
    return SizingInputs(**base)


def test_sizing_fixed_and_risk_based() -> None:
    fixed = PortfolioConfig(sizing=Sizing.FIXED_PCT, max_entry=D("0.25"))
    assert entry_amount(fixed, inputs()) == D(250)
    risk = PortfolioConfig(
        sizing=Sizing.RISK, risk_per_trade=D("0.01"), max_entry=D("0.5"), max_per_crypto=D(1)
    )
    assert entry_amount(risk, inputs()) == D(250)  # 1 000 × 1 % / 4 %
    assert entry_amount(risk, inputs(stop_distance=D("0.01"))) == D(500)  # plafond 50 %


def test_sizing_caps() -> None:
    config = PortfolioConfig(
        sizing=Sizing.FIXED_PCT, max_entry=D("0.5"), max_per_crypto=D("0.4"), max_exposure=D("0.6")
    )
    assert entry_amount(config, inputs()) == D(400)
    assert entry_amount(config, inputs(market_exposure=D(300))) == D(100)
    assert entry_amount(config, inputs(exposure=D(500))) == D(100)
    assert entry_amount(config, inputs(available_cash=D(42))) == D(42)
    assert entry_amount(config, inputs(exposure=D(700))) == D(0)


# --- Niveaux de sortie --------------------------------------------------------------------


def frame(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    close, high, low = zip(*rows, strict=True)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1.0},
        index=pd.Index(range(len(rows))),
    )


def test_initial_stop_types() -> None:
    bars = FramesContext({MARKET: frame([(100.0, 101.0, 99.0)] * 20 + [(100.0, 101.0, 95.0)])}, {})
    pct = make_config(Switch(), stop=StopConfig(StopType.FIXED_PCT, pct=D("0.05")))
    assert initial_stop(pct, D(100), bars, MARKET) == D(95)
    atr = make_config(Switch(), stop=StopConfig(StopType.ATR, atr_multiple=D(2)))
    level = initial_stop(atr, D(100), bars, MARKET)
    assert level is not None and D(95) < level < D(96)  # ATR ≈ 2,2
    lowest = make_config(Switch(), stop=StopConfig(StopType.LOWEST_LOW, lowest_n=5))
    assert initial_stop(lowest, D(100), bars, MARKET) == D(95)
    assert initial_stop(lowest, D(94), bars, MARKET) is None  # stop au-dessus de l'entrée : refusé


def test_trailing_levels() -> None:
    bars = FramesContext({MARKET: frame([(100.0, 101.0, 99.0)] * 30)}, {})
    pos = Position(MARKET, 1, D(1), D(100), D(100), D(0), D(5), D(95), D(95), D(100), D(110), 0)
    pct = make_config(Switch(), trailing=TrailingConfig(TrailingType.PCT, pct=D("0.1")))
    assert trailing_level(pct, pos, bars, MARKET) == D(99)
    atr = make_config(Switch(), trailing=TrailingConfig(TrailingType.ATR, atr_multiple=D(3)))
    assert trailing_level(atr, pos, bars, MARKET) == D(110) - 3 * D("2.0")
    chandelier = make_config(
        Switch(), trailing=TrailingConfig(TrailingType.CHANDELIER, atr_multiple=D(2))
    )
    assert trailing_level(chandelier, pos, bars, MARKET) == D(101) - 2 * D("2.0")


def test_take_profit_levels() -> None:
    pos = Position(MARKET, 1, D(1), D(100), D(100), D(0), D(5), D(95), D(95), D(100), D(100), 0)
    r = make_config(
        Switch(), take_profit=TakeProfitConfig(TakeProfitType.R_MULTIPLE, r_multiple=D(2))
    )
    assert take_profit_level(r, pos) == D(110)
    pct = make_config(
        Switch(), take_profit=TakeProfitConfig(TakeProfitType.FIXED_PCT, pct=D("0.2"))
    )
    assert take_profit_level(pct, pos) == D(120)
    assert take_profit_level(make_config(Switch()), pos) is None


# --- Entrées ------------------------------------------------------------------------------


async def test_market_entry_creates_position(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    h = Harness(conn, make_config(main))
    await h.bar(100.0)
    pos = position(h)
    assert pos.avg_entry_price == D("100.05")  # meilleur ask
    assert pos.stop == pos.initial_stop == pos.avg_entry_price * D("0.95")
    assert pos.risk_per_unit == pos.avg_entry_price - pos.stop
    assert pos.invested_cost <= D(250)
    assert h.store.load(1)[MARKET].quantity == pos.quantity


async def test_no_entry_without_main_signal(conn: sqlite3.Connection) -> None:
    main, confirmation = Switch(buy=False), Switch(buy=True)
    signals = SignalConfig(main, 0.5, ((confirmation, 0.5),), threshold=0.5)  # type: ignore[arg-type]
    h = Harness(conn, make_config(main, signals=signals))
    await h.bar(100.0)
    assert h.sim.positions == {}  # les confirmations seules ne suffisent pas


async def test_score_threshold(conn: sqlite3.Connection) -> None:
    main, confirmation = Switch(buy=True), Switch(buy=False)
    signals = SignalConfig(main, 0.6, ((confirmation, 0.4),), threshold=0.8)  # type: ignore[arg-type]
    h = Harness(conn, make_config(main, signals=signals))
    await h.bar(100.0)
    assert h.sim.positions == {}
    confirmation.buy = True
    await h.bar(100.0)
    assert MARKET in h.sim.positions


async def test_pair_without_200_days_is_excluded(conn: sqlite3.Connection) -> None:
    h = Harness(conn, make_config(Switch(buy=True)), daily_days=199)
    await h.bar(100.0)
    assert h.sim.positions == {}


async def test_trend_filter_blocks_entries(conn: sqlite3.Connection) -> None:
    main, trend = Switch(buy=True), Switch(favorable=False)
    config = make_config(main, trend=TrendFilterConfig(True, trend))  # type: ignore[arg-type]
    h = Harness(conn, config)
    await h.bar(100.0)
    assert h.sim.positions == {}
    trend.is_favorable = True
    await h.bar(100.0)
    assert MARKET in h.sim.positions


async def test_limit_entry_at_best_bid_then_filled(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    config = make_config(main, execution=ExecutionConfig(EntryOrder.LIMIT, 4))
    h = Harness(conn, config)
    await h.bar(100.0)
    (order,) = h.broker.open_orders(1)
    assert order.price == D("99.95") and MARKET in h.sim.pending
    main.buy = False
    await h.bar(100.0, low=99.9)  # traversé dans le délai (4 h)
    assert position(h).avg_entry_price == D("99.95")
    assert MARKET not in h.sim.pending


async def test_limit_entry_expires(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    h = Harness(conn, make_config(main, execution=ExecutionConfig(EntryOrder.LIMIT, 1)))
    await h.bar(100.0)
    main.buy = False
    await h.bar(101.0, low=99.95)  # limite seulement touchée : pas remplie, puis expirée
    assert h.sim.pending == {} and h.sim.positions == {}
    assert load_orders(conn, 1)[0].status is OrderStatus.EXPIRED
    assert h.broker.cash(1) == D(1000)


async def test_max_positions(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True), portfolio=PortfolioConfig(sizing=Sizing.FIXED_PCT, max_positions=1)
    )
    h = Harness(conn, config)
    h.sim.pending["XXX-EUR"] = PendingEntry(999, is_add=False)  # entrée en attente ailleurs
    await h.bar(100.0)
    assert MARKET not in h.sim.positions


# --- Stops et trailing --------------------------------------------------------------------


async def test_stop_triggers_and_pause_blocks_reentry(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    config = make_config(
        main, portfolio=PortfolioConfig(sizing=Sizing.FIXED_PCT, pause_after_stop_bars=2)
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    stop = position(h).stop
    await h.bar(97.0, low=float(stop) + 0.01)
    assert MARKET in h.sim.positions
    await h.bar(94.0, low=float(stop) - 0.5)  # stop franchi pendant la bougie
    assert MARKET not in h.sim.positions  # et pas de ré-entrée pendant la pause
    sell = load_orders(conn, 1)[-1]
    assert sell.reason is Reason.STOP
    assert sell.price < D("94") * (1 - D("0.0005"))  # pénalité d'un spread estimé
    await h.bar(94.0)
    assert MARKET not in h.sim.positions
    await h.bar(94.0)
    assert MARKET in h.sim.positions  # pause de 2 bougies écoulée


async def test_stop_never_moves_down(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True), trailing=TrailingConfig(TrailingType.ATR, atr_multiple=D(2))
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    opened_at, stops = position(h).opened_at, [position(h).stop]
    for close in (104.0, 108.0, 106.0, 103.0, 107.0, 110.0, 104.0):
        await h.bar(close, low=close - 0.5)
        if MARKET not in h.sim.positions or position(h).opened_at != opened_at:
            break
        stops.append(position(h).stop)
    assert len(stops) > 3 and stops == sorted(stops)
    assert stops[-1] > stops[0]


async def test_trailing_activates_after_one_r(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True),
        trailing=TrailingConfig(
            TrailingType.PCT, pct=D("0.03"), activation=TrailingActivation.ONE_R
        ),
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    pos = position(h)
    one_r = pos.avg_entry_price + pos.risk_per_unit  # ≈ 105,05
    await h.bar(103.0, high=104.0)
    assert not pos.trailing_active and pos.stop == pos.initial_stop
    await h.bar(106.0, high=float(one_r) + 0.5)
    assert pos.trailing_active
    assert pos.stop == pos.highest_high * D("0.97")


async def test_trailing_stop_exit_reason(conn: sqlite3.Connection) -> None:
    config = make_config(Switch(buy=True), trailing=TrailingConfig(TrailingType.PCT, pct=D("0.02")))
    h = Harness(conn, config)
    await h.bar(100.0)
    await h.bar(110.0, high=111.0, low=109.0)
    assert position(h).stop_from_trailing
    await h.bar(105.0, low=104.0)
    sells = [o for o in load_orders(conn, 1) if o.side == "sell"]
    assert sells[-1].reason is Reason.TRAILING


# --- Take profit --------------------------------------------------------------------------


async def test_take_profit_limit_sell(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True), take_profit=TakeProfitConfig(TakeProfitType.R_MULTIPLE, r_multiple=D(2))
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    pos = position(h)
    tp = pos.take_profit
    assert tp is not None and pos.tp_order_id is not None
    assert h.broker.available_quantity(1, MARKET) == 0  # quantité bloquée par le TP
    await h.bar(float(tp), high=float(tp))  # touché : pas rempli
    assert MARKET in h.sim.positions
    h.sim.config.signals.main.buy = False  # type: ignore[attr-defined]
    await h.bar(float(tp), high=float(tp) + 0.1)
    assert MARKET not in h.sim.positions
    sell = load_orders(conn, 1)[-1]
    assert sell.reason is Reason.TAKE_PROFIT and sell.liquidity == "maker"
    assert h.broker.holdings(1) == {}


async def test_partial_take_profit_then_trailing(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    config = make_config(
        main, take_profit=TakeProfitConfig(TakeProfitType.PARTIAL, r_multiple=D(1))
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    pos = position(h)
    quantity, tp = pos.quantity, pos.take_profit
    main.buy = False
    await h.bar(float(tp) + 1, high=float(tp) + 1)
    assert pos.partial_done and pos.trailing_active
    assert pos.quantity == quantity - quantity * D("0.5")
    assert pos.stop == pos.avg_entry_price  # sans trailing : stop au prix d'entrée
    assert pos.tp_order_id is None


async def test_stop_cancels_pending_take_profit(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True), take_profit=TakeProfitConfig(TakeProfitType.FIXED_PCT, pct=D("0.2"))
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    h.sim.config.signals.main.buy = False  # type: ignore[attr-defined]
    await h.bar(90.0, low=90.0)
    assert MARKET not in h.sim.positions
    statuses = [(o.side, o.status) for o in load_orders(conn, 1)]
    assert ("sell", OrderStatus.CANCELLED) in statuses and h.broker.holdings(1) == {}


# --- Renforcement -------------------------------------------------------------------------


async def test_pyramiding_after_one_r(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True),
        portfolio=PortfolioConfig(
            sizing=Sizing.FIXED_PCT, max_entry=D("0.2"), max_per_crypto=D("1"), max_entries=2
        ),
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    pos = position(h)
    one_r = pos.last_entry_price + pos.risk_per_unit
    await h.bar(float(one_r) - 1, low=float(one_r) - 2)
    assert pos.lots == 1
    await h.bar(float(one_r) + 1)
    assert pos.lots == 2
    assert pos.stop >= pos.avg_entry_price  # stop remonté au prix moyen
    await h.bar(float(one_r) * 1.2)
    assert pos.lots == 2  # maximum atteint


# --- Sorties sur signal, durée, filtre ------------------------------------------------------


async def test_reverse_signal_exit(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    h = Harness(conn, make_config(main, exits=ExitConfig(reverse_signal=True)))
    await h.bar(100.0)
    main.buy, main.sell = False, True
    await h.bar(100.0)
    assert MARKET not in h.sim.positions
    assert load_orders(conn, 1)[-1].reason is Reason.REVERSE_SIGNAL


async def test_max_holding_exit(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    h = Harness(conn, make_config(main, exits=ExitConfig(False, max_holding_bars=3)))
    await h.bar(100.0)
    main.buy = False
    for _ in range(2):
        await h.bar(100.0)
    assert MARKET in h.sim.positions
    await h.bar(100.0)
    assert load_orders(conn, 1)[-1].reason is Reason.MAX_DURATION


async def test_trend_reversal_block_and_exit(conn: sqlite3.Connection) -> None:
    main, trend = Switch(buy=True), Switch(favorable=True)
    config = make_config(main, trend=TrendFilterConfig(True, trend, TrendAction.BLOCK_AND_EXIT))  # type: ignore[arg-type]
    h = Harness(conn, config)
    await h.bar(100.0)
    trend.is_favorable = False
    await h.bar(100.0)
    assert MARKET not in h.sim.positions
    assert load_orders(conn, 1)[-1].reason is Reason.TREND_FILTER


# --- Circuit breaker ----------------------------------------------------------------------


async def test_circuit_breaker_pauses_seven_days(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    config = make_config(
        main,
        portfolio=PortfolioConfig(
            sizing=Sizing.FIXED_PCT,
            max_entry=D("1"),
            max_per_crypto=D("1"),
            circuit_breaker_drawdown=D("0.1"),
        ),
        stop=StopConfig(StopType.FIXED_PCT, pct=D("0.5")),
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    await h.bar(80.0, low=79.0)  # -20 % sur un portefeuille investi à ~100 %
    assert h.sim.breaker_until == h.now + 42 * BAR
    await h.sim.exit(MARKET, Reason.CIRCUIT_BREAKER, h.now)
    for _ in range(41):
        await h.bar(80.0)
        assert MARKET not in h.sim.positions
    await h.bar(80.0)
    assert h.sim.breaker_until is None and MARKET in h.sim.positions
    assert h.sim.peak_value < D(900)  # plus haut de référence remis à la valeur courante


# --- Persistance --------------------------------------------------------------------------


async def test_position_store_round_trip(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True),
        take_profit=TakeProfitConfig(TakeProfitType.PARTIAL, r_multiple=D(2)),
        trailing=TrailingConfig(TrailingType.PCT),
    )
    h = Harness(conn, config)
    await h.bar(100.0)
    await h.bar(103.0)
    stored = h.store.load(1)[MARKET]
    assert stored == position(h)
    await h.sim.exit(MARKET, Reason.SIGNAL, h.now)
    assert h.store.load(1) == {}


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        SignalConfig(Switch(), 0.7, ((Switch(), 0.2),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PortfolioConfig(max_exposure=D("1.5"))
    with pytest.raises(ValueError):
        TrendFilterConfig(True, None)


# --- Scénario complet sur bougies réelles -------------------------------------------------


async def test_full_scenario_on_real_btc_candles(conn: sqlite3.Connection) -> None:
    from mcs.indicators.signals import EmaCross, RsiConfirmation
    from tests.test_indicators import load_fixture

    bars = load_fixture("BTC-EUR_4h")
    config = StrategyConfig(
        signals=SignalConfig(EmaCross(8, 30), 0.7, ((RsiConfirmation(14, 75.0), 0.3),), 0.7),
        stop=StopConfig(StopType.ATR, atr_multiple=D(2)),
        trailing=TrailingConfig(
            TrailingType.ATR, atr_multiple=D(3), activation=TrailingActivation.ONE_R
        ),
        take_profit=TakeProfitConfig(TakeProfitType.PARTIAL, r_multiple=D(2)),
        execution=ExecutionConfig(EntryOrder.LIMIT, 4),
        exits=ExitConfig(reverse_signal=True, max_holding_bars=120),
        portfolio=PortfolioConfig(
            sizing=Sizing.RISK, risk_per_trade=D("0.01"), max_entries=2, max_per_crypto=D("0.8")
        ),
    )
    h = Harness(conn, config, history=0)
    h.rows = [(r.close, r.high, r.low) for r in bars.iloc[:100].itertuples()]
    stops: dict[int, list[Decimal]] = {}
    for r in bars.iloc[100:].itertuples():
        await h.bar(r.close, high=r.high, low=r.low)
        account = h.broker.account(1)
        assert account.cash_total >= 0 and account.available >= 0
        held = account.holdings.get(MARKET, D(0))
        pos = h.sim.positions.get(MARKET)
        assert held == (pos.quantity if pos else D(0))
        if pos:
            stops.setdefault(pos.opened_at, []).append(pos.stop)
    orders = load_orders(conn, 1)
    reasons = {o.reason for o in orders if o.status is OrderStatus.FILLED}
    assert len([o for o in orders if o.status is OrderStatus.FILLED]) >= 4
    assert Reason.SIGNAL in reasons and len(reasons) >= 2
    assert all(s == sorted(s) for s in stops.values())  # stops monotones par position
    assert h.broker.account(1).fees_paid > 0
    assert D(500) < h.sim.portfolio_value(h.ctx()) < D(2000)


# --- Minimum d'ordre --------------------------------------------------------------------


async def test_entry_below_minimum_is_not_sent(conn: sqlite3.Connection) -> None:
    config = make_config(
        Switch(buy=True), portfolio=PortfolioConfig(sizing=Sizing.FIXED_PCT, max_entry=D("0.004"))
    )
    h = Harness(conn, config)  # 0,4 % de 1 000 € = 4 € < minimum de 5 €
    await h.bar(100.0)
    assert h.sim.positions == {} and load_orders(conn, 1) == []


async def test_dust_position_is_not_sold_below_minimum(conn: sqlite3.Connection) -> None:
    main = Switch(buy=True)
    config = make_config(
        main,
        portfolio=PortfolioConfig(sizing=Sizing.FIXED_PCT, max_entry=D("0.006")),
        stop=StopConfig(StopType.FIXED_PCT, pct=D("0.05")),
    )
    h = Harness(conn, config)
    await h.bar(100.0)  # achat d'environ 6 €
    assert MARKET in h.sim.positions
    main.buy = False
    await h.bar(50.0, low=40.0)  # la position ne vaut plus que ~3 € : vente impossible
    assert MARKET in h.sim.positions
    assert [o.side for o in load_orders(conn, 1)] == ["buy"]  # aucune vente tentée
