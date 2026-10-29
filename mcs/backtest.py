"""Backtest historique d'une configuration, avec la même classe `Simulation` que le live.

Déroulé, heure par heure (bougies 1 h) :
1. pour chaque paire détenue ou ayant un ordre ouvert : stop vérifié sur le plus bas de
   l'heure (avant tout take profit : décision 26), puis ordres limite (achat : plus bas
   strictement sous la limite ; vente : plus haut strictement au-dessus) ;
2. expiration des ordres limite ;
3. à chaque clôture 4 h : sorties, trailings et entrées ; sinon, recalcul horaire du trailing
   pour les simulations configurées ainsi.

Exécution (décision 26) : ordre au marché au dernier cours ± un demi-spread estimé, frais
taker ; stop exécuté à son niveau ou à l'ouverture de l'heure si le cours a ouvert dessous,
moins un spread ; minimums et arrondis de chaque marché respectés.

Pas de fuite d'information : les indicateurs sont calculés une fois sur tout l'historique
(aucune valeur ne lit le futur) et lus à l'indice de la dernière bougie clôturée.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from mcs.broker.base import (
    STOP_REASONS,
    Liquidity,
    Order,
    OrderStatus,
    OrderType,
    Reason,
    Rejection,
    RejectReason,
    Side,
)
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import Account
from mcs.broker.rules import MarketRules, load_rules
from mcs.indicators import core
from mcs.indicators.frame import candles_to_frame, fill_gaps
from mcs.indicators.signals import Supertrend
from mcs.market_data import INTERVAL_MS
from mcs.metrics import calmar_ratio
from mcs.storage.candles import load_candles
from mcs.storage.markets import selected_markets
from mcs.strategy.config import ATR_PERIOD, StopRecalc, StrategyConfig
from mcs.strategy.simulation import Simulation

HOUR = INTERVAL_MS["1h"]
BAR_4H = INTERVAL_MS["4h"]
DAY = INTERVAL_MS["1d"]
DEFAULT_SPREAD = Decimal("0.001")
INITIAL_CAPITAL = Decimal(1000)
FRAME_WINDOW = 300


def _dec(value: float) -> Decimal | None:
    return None if np.isnan(value) else Decimal(repr(float(value)))


# --- Données ------------------------------------------------------------------------------


@dataclass
class MarketData:
    market: str
    rules: MarketRules
    spread: Decimal  # spread estimé (fraction)
    h1: pd.DataFrame
    h4: pd.DataFrame
    d1: pd.DataFrame
    h1_start: int = 0
    h1_values: np.ndarray = field(default_factory=lambda: np.empty((0, 4)))
    h4_end: np.ndarray = field(default_factory=lambda: np.empty(0, dtype="int64"))
    d1_end: np.ndarray = field(default_factory=lambda: np.empty(0, dtype="int64"))

    def __post_init__(self) -> None:
        if len(self.h1):
            self.h1_start = int(self.h1.index[0])
            self.h1_values = self.h1[["open", "high", "low", "close"]].to_numpy("float64")
        self.h4_end = self.h4.index.to_numpy("int64") + BAR_4H
        self.d1_end = self.d1.index.to_numpy("int64") + DAY

    def bar(self, ts: int) -> np.ndarray | None:
        """Bougie 1 h démarrant à `ts` : [open, high, low, close], ou None hors historique."""
        index = (ts - self.h1_start) // HOUR
        if 0 <= index < len(self.h1_values):
            row: np.ndarray = self.h1_values[index]
            return row
        return None


@dataclass
class Dataset:
    markets: dict[str, MarketData]

    @property
    def start(self) -> int:
        first = min(int(m.h1.index[0]) for m in self.markets.values() if len(m.h1))
        return -(-first // BAR_4H) * BAR_4H

    @property
    def end(self) -> int:
        last = max(int(m.h1.index[-1]) + HOUR for m in self.markets.values() if len(m.h1))
        return last // BAR_4H * BAR_4H

    def split(self, ratio: float = 0.7) -> tuple[int, int, int]:
        """(début, coupure, fin) : apprentissage [début, coupure), validation [coupure, fin)."""
        start, end = self.start, self.end
        cut = start + int((end - start) * ratio) // BAR_4H * BAR_4H
        return start, cut, end


def _frame(conn: sqlite3.Connection, market: str, interval: str) -> pd.DataFrame:
    return fill_gaps(candles_to_frame(load_candles(conn, market, interval)), interval)


def load_dataset(conn: sqlite3.Connection, markets: Iterable[str] | None = None) -> Dataset:
    """Bougies 1 h, 4 h et 1 j des paires sélectionnées (ou de `markets`)."""
    chosen = list(markets) if markets is not None else selected_markets(conn)
    rules = load_rules(conn)
    data = {}
    for market in chosen:
        row = conn.execute(
            "SELECT spread_estimate_pct FROM markets WHERE market = ?", (market,)
        ).fetchone()
        spread = Decimal(row[0]) / 100 if row and row[0] is not None else DEFAULT_SPREAD
        data[market] = MarketData(
            market=market,
            rules=rules[market],
            spread=spread,
            h1=_frame(conn, market, "1h"),
            h4=_frame(conn, market, "4h"),
            d1=_frame(conn, market, "1d"),
        )
    return Dataset(data)


# --- Contexte historique ------------------------------------------------------------------


class BacktestContext:
    """`MarketContext` à l'instant `now` sur un historique complet (séries précalculées)."""

    def __init__(self, data: Dataset, cache: dict[tuple[Any, ...], Any] | None = None) -> None:
        self.data = data
        self.cache = cache if cache is not None else {}
        self.now = 0
        self._counts: dict[tuple[str, str], int] = {}

    def set_now(self, now: int) -> None:
        if now != self.now:
            self.now = now
            self._counts.clear()

    def _count(self, market: str, kind: str) -> int:
        """Nombre de bougies 4 h (ou 1 j) clôturées à `now`."""
        key = (market, kind)
        if key not in self._counts:
            data = self.data.markets[market]
            ends = data.h4_end if kind == "4h" else data.d1_end
            self._counts[key] = int(np.searchsorted(ends, self.now, side="right"))
        return self._counts[key]

    def _series(self, key: tuple[Any, ...], compute: Any) -> Any:
        if key not in self.cache:
            self.cache[key] = compute()
        return self.cache[key]

    def frame_4h(self, market: str) -> pd.DataFrame:
        i = self._count(market, "4h")
        return self.data.markets[market].h4.iloc[max(0, i - FRAME_WINDOW) : i]

    def bars_4h(self, market: str) -> int:
        return self._count(market, "4h")

    def daily_history_days(self, market: str) -> int:
        return self._count(market, "1d")

    def last_price(self, market: str) -> Decimal | None:
        data = self.data.markets[market]
        index = min((self.now - data.h1_start) // HOUR - 1, len(data.h1_values) - 1)
        return None if index < 0 else _dec(data.h1_values[index, 3])

    def best_bid(self, market: str) -> Decimal | None:
        price = self.last_price(market)
        return None if price is None else price * (1 - self.data.markets[market].spread / 2)

    def state(self, market: str, indicator: Any, kind: str) -> bool:
        daily = kind == "favorable"
        i = self._count(market, "1d" if daily else "4h")
        if i == 0:
            return False
        data = self.data.markets[market]
        if isinstance(indicator, Supertrend) and kind in ("buy_state", "sell_state"):
            # Une seule série Supertrend par réglage, partagée avec le trailing.
            trend = self._supertrend_series(market, indicator)
            return bool(trend[i - 1, 0] == (1 if kind == "buy_state" else -1))
        frame = data.d1 if daily else data.h4
        values = self._series(
            (market, indicator, kind),
            lambda: getattr(indicator, kind)(frame).to_numpy(dtype=bool),
        )
        return bool(values[i - 1])

    def _supertrend_series(self, market: str, params: Supertrend) -> Any:
        h4 = self.data.markets[market].h4
        return self._series(
            (market, params),
            lambda: core.supertrend(h4, params.atr_period, params.multiplier)[
                ["direction", "line"]
            ].to_numpy("float64"),
        )

    def _last(self, market: str, key: tuple[Any, ...], compute: Any) -> Decimal | None:
        i = self._count(market, "4h")
        if i == 0:
            return None
        return _dec(self._series((market, *key), compute)[i - 1])

    def atr_4h(self, market: str) -> Decimal | None:
        h4 = self.data.markets[market].h4
        return self._last(market, ("atr",), lambda: core.atr(h4, ATR_PERIOD).to_numpy())

    def highest_high_4h(self, market: str, n: int) -> Decimal | None:
        h4 = self.data.markets[market].h4
        return self._last(market, ("high", n), lambda: core.highest(h4["high"], n).to_numpy())

    def lowest_low_4h(self, market: str, n: int) -> Decimal | None:
        h4 = self.data.markets[market].h4
        return self._last(market, ("low", n), lambda: core.lowest(h4["low"], n).to_numpy())

    def supertrend_4h(self, market: str, params: Supertrend) -> tuple[int, Decimal | None]:
        i = self._count(market, "4h")
        if i == 0:
            return 0, None
        values = self._supertrend_series(market, params)
        return int(values[i - 1, 0]), _dec(values[i - 1, 1])


# --- Broker historique --------------------------------------------------------------------


class HistoricalBroker:
    """`Broker` sur bougies historiques, sans carnet d'ordres (décision 26)."""

    def __init__(self, ctx: BacktestContext, fees: FeeSchedule) -> None:
        self.ctx, self.fees = ctx, fees
        self.accounts: dict[int, Account] = {}
        self.orders: dict[int, Order] = {}
        self.current_bar: dict[str, np.ndarray] = {}
        self._next_id = 1

    def open_account(self, sim_id: int, initial_cash: Decimal) -> Account:
        self.accounts[sim_id] = Account(sim_id, initial_cash, initial_cash)
        return self.accounts[sim_id]

    def cash(self, sim_id: int) -> Decimal:
        return self.accounts[sim_id].available

    def total_cash(self, sim_id: int) -> Decimal:
        return self.accounts[sim_id].cash_total

    def holdings(self, sim_id: int) -> dict[str, Decimal]:
        return dict(self.accounts[sim_id].holdings)

    def available_quantity(self, sim_id: int, market: str) -> Decimal:
        return self.accounts[sim_id].available_quantity(market)

    def open_orders(self, sim_id: int) -> list[Order]:
        return list(self.accounts[sim_id].open_orders.values())

    def min_order_quote(self, market: str) -> Decimal:
        return self.ctx.data.markets[market].rules.min_order_quote

    def _rules(self, market: str) -> MarketRules:
        return self.ctx.data.markets[market].rules

    def _record(self, order: Order) -> Order:
        order = replace(order, id=self._next_id)
        self._next_id += 1
        self.orders[order.id] = order
        return order

    def _quantity_for_budget(
        self, rules: MarketRules, price: Decimal, budget: Decimal, liquidity: Liquidity
    ) -> Decimal:
        quantity = rules.round_quantity(budget / (price * (1 + self.fees.rate(liquidity))))
        while (
            quantity > 0 and quantity * price + self.fees.fee(quantity * price, liquidity) > budget
        ):
            quantity -= rules.quantity_step
        return quantity

    def _fill(
        self,
        sim_id: int,
        market: str,
        side: Side,
        reason: Reason,
        quantity: Decimal,
        price: Decimal,
        now: int,
    ) -> Order:
        notional = quantity * price
        order = self._record(
            Order(
                id=0,
                sim_id=sim_id,
                market=market,
                side=side,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                reason=reason,
                quantity=quantity,
                price=price,
                notional=notional,
                fee=self.fees.fee(notional, Liquidity.TAKER),
                liquidity=Liquidity.TAKER,
                reserved=None,
                book_bid=None,
                book_ask=None,
                created_at=now,
                filled_at=now,
            )
        )
        self.accounts[sim_id].apply_fill(order)
        return order

    async def buy_market(
        self, sim_id: int, market: str, quote_amount: Decimal, reason: Reason, now: int
    ) -> Order | Rejection:
        account, rules = self.accounts[sim_id], self._rules(market)
        last = self.ctx.last_price(market)
        if last is None or quote_amount > account.available:
            return Rejection(sim_id, market, Side.BUY, RejectReason.INSUFFICIENT_FUNDS, "")
        price = last * (1 + self.ctx.data.markets[market].spread / 2)
        quantity = self._quantity_for_budget(rules, price, quote_amount, Liquidity.TAKER)
        problem = rules.check_minimum(quantity, price)
        if problem:
            return Rejection(sim_id, market, Side.BUY, *problem)
        return self._fill(sim_id, market, Side.BUY, reason, quantity, price, now)

    async def sell_market(
        self,
        sim_id: int,
        market: str,
        quantity: Decimal,
        reason: Reason,
        now: int,
        trigger_price: Decimal | None = None,
    ) -> Order | Rejection:
        account, rules = self.accounts[sim_id], self._rules(market)
        quantity = rules.round_quantity(quantity)
        if quantity <= 0 or quantity > account.available_quantity(market):
            return Rejection(sim_id, market, Side.SELL, RejectReason.INSUFFICIENT_QUANTITY, "")
        spread = self.ctx.data.markets[market].spread
        bar = self.current_bar.get(market)
        if reason in STOP_REASONS and trigger_price is not None:
            level = trigger_price
            if bar is not None:
                level = min(level, Decimal(repr(float(bar[0]))))  # ouverture sous le stop
            price = level * (1 - spread)
        else:
            last = self.ctx.last_price(market)
            if last is None:
                return Rejection(sim_id, market, Side.SELL, RejectReason.EMPTY_BOOK, "")
            price = last * (1 - spread / 2)
        problem = rules.check_minimum(quantity, price)
        if problem:
            return Rejection(sim_id, market, Side.SELL, *problem)
        return self._fill(sim_id, market, Side.SELL, reason, quantity, price, now)

    async def place_limit_buy(
        self,
        sim_id: int,
        market: str,
        price: Decimal,
        quote_amount: Decimal,
        expires_at: int,
        reason: Reason,
        now: int,
    ) -> Order | Rejection:
        account, rules = self.accounts[sim_id], self._rules(market)
        limit = rules.round_price(price, Side.BUY)
        quantity = self._quantity_for_budget(rules, limit, quote_amount, Liquidity.MAKER)
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return Rejection(sim_id, market, Side.BUY, *problem)
        cost = quantity * limit + self.fees.fee(quantity * limit, Liquidity.MAKER)
        if cost > account.available:
            return Rejection(sim_id, market, Side.BUY, RejectReason.INSUFFICIENT_FUNDS, "")
        order = self._record(
            Order(
                id=0,
                sim_id=sim_id,
                market=market,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.OPEN,
                reason=reason,
                quantity=quantity,
                price=limit,
                notional=None,
                fee=None,
                liquidity=None,
                reserved=cost,
                book_bid=None,
                book_ask=None,
                created_at=now,
                expires_at=expires_at,
            )
        )
        account.open_orders[order.id] = order
        return order

    async def place_limit_sell(
        self,
        sim_id: int,
        market: str,
        price: Decimal,
        quantity: Decimal,
        reason: Reason,
        now: int,
        expires_at: int | None = None,
    ) -> Order | Rejection:
        account, rules = self.accounts[sim_id], self._rules(market)
        limit = rules.round_price(price, Side.SELL)
        quantity = rules.round_quantity(quantity)
        if quantity <= 0 or quantity > account.available_quantity(market):
            return Rejection(sim_id, market, Side.SELL, RejectReason.INSUFFICIENT_QUANTITY, "")
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return Rejection(sim_id, market, Side.SELL, *problem)
        order = self._record(
            Order(
                id=0,
                sim_id=sim_id,
                market=market,
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                status=OrderStatus.OPEN,
                reason=reason,
                quantity=quantity,
                price=limit,
                notional=None,
                fee=None,
                liquidity=None,
                reserved=None,
                book_bid=None,
                book_ask=None,
                created_at=now,
                expires_at=expires_at,
            )
        )
        account.open_orders[order.id] = order
        return order

    def on_bar(self, market: str, ts: int, bar: np.ndarray) -> list[Order]:
        """Ordres limite traversés par la bougie 1 h démarrant à `ts`."""
        high, low = Decimal(repr(float(bar[1]))), Decimal(repr(float(bar[2])))
        filled = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                if order.market != market or order.price is None or ts < order.created_at:
                    continue
                if order.expires_at is not None and ts >= order.expires_at:
                    continue
                crossed = low < order.price if order.side is Side.BUY else high > order.price
                if crossed:
                    notional = order.quantity * order.price
                    done = replace(
                        order,
                        status=OrderStatus.FILLED,
                        notional=notional,
                        fee=self.fees.fee(notional, Liquidity.MAKER),
                        liquidity=Liquidity.MAKER,
                        reserved=None,
                        filled_at=ts,
                    )
                    del account.open_orders[order.id]
                    account.apply_fill(done)
                    self.orders[done.id] = done
                    filled.append(done)
        return filled

    def expire_orders(self, now: int) -> list[Order]:
        expired = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                if order.expires_at is not None and order.expires_at <= now:
                    expired.append(self._close(account, order, OrderStatus.EXPIRED))
        return expired

    async def cancel_order(self, order_id: int, now: int) -> Order:
        for account in self.accounts.values():
            if order_id in account.open_orders:
                return self._close(account, account.open_orders[order_id], OrderStatus.CANCELLED)
        raise KeyError(order_id)

    def _close(self, account: Account, order: Order, status: OrderStatus) -> Order:
        closed = replace(order, status=status, reserved=None)
        del account.open_orders[order.id]
        self.orders[order.id] = closed
        return closed


# --- Exécution d'un backtest --------------------------------------------------------------


@dataclass(frozen=True)
class BacktestResult:
    final_value: Decimal
    total_return: float
    max_drawdown: float
    trades: int
    wins: int
    fees_paid: Decimal
    orders: tuple[Order, ...] = ()

    @property
    def score(self) -> float:
        """Rendement / drawdown max (décision 23) ; 0 sous 10 trades clôturés."""
        ratio = calmar_ratio(self.total_return * 100, self.max_drawdown * 100, self.trades)
        return 0.0 if ratio is None else ratio


async def _run(
    config: StrategyConfig,
    data: Dataset,
    start: int,
    end: int,
    fees: FeeSchedule,
    cache: dict[tuple[Any, ...], Any] | None,
    keep_orders: bool,
) -> BacktestResult:
    ctx = BacktestContext(data, cache)
    broker = HistoricalBroker(ctx, fees)
    account = broker.open_account(1, INITIAL_CAPITAL)
    sim = Simulation(1, config, broker, fees, list(data.markets), INITIAL_CAPITAL)
    hourly = config.exits.recalc is StopRecalc.HOURLY
    peak, max_drawdown = INITIAL_CAPITAL, Decimal(0)
    for ts in range(start, end, HOUR):
        ctx.set_now(ts)
        active = set(sim.positions) | {o.market for o in account.open_orders.values()}
        highs: dict[str, Decimal] = {}
        for market in sorted(active):
            bar = data.markets[market].bar(ts)
            if bar is None:
                continue
            broker.current_bar[market] = bar
            if market in sim.positions:
                await sim.on_price(market, Decimal(repr(float(bar[2]))), ts)
                highs[market] = Decimal(repr(float(bar[1])))
            if any(o.market == market for o in account.open_orders.values()):
                for order in broker.on_bar(market, ts, bar):
                    await sim.on_order_filled(order, ctx, ts)
        now = ts + HOUR
        ctx.set_now(now)
        broker.current_bar.clear()
        for order in broker.expire_orders(now):
            sim.on_order_closed(order)
        if now % BAR_4H == 0:
            await sim.on_4h_close(now, ctx)
            value = sim.portfolio_value(ctx)
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, 1 - value / peak)
        elif hourly and highs:
            await sim.on_1h_close(now, ctx, highs)
    final = sim.portfolio_value(ctx)
    return BacktestResult(
        final_value=final,
        total_return=float(final / INITIAL_CAPITAL - 1),
        max_drawdown=float(max_drawdown),
        trades=len(sim.trade_pnls),
        wins=sum(1 for pnl in sim.trade_pnls if pnl > 0),
        fees_paid=account.fees_paid,
        orders=tuple(broker.orders.values()) if keep_orders else (),
    )


def run_backtest(
    config: StrategyConfig,
    data: Dataset,
    start: int,
    end: int,
    fees: FeeSchedule | None = None,
    cache: dict[tuple[Any, ...], Any] | None = None,
    keep_orders: bool = False,
) -> BacktestResult:
    """Backtest de `config` sur [start, end) (décisions aux clôtures 4 h de l'intervalle)."""
    return asyncio.run(_run(config, data, start, end, fees or FeeSchedule(), cache, keep_orders))
