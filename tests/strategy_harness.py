"""Banc d'essai des simulations : bougies 4 h construites à la main, carnet dérivé du cours,
PaperBroker réel et stockage SQLite des positions."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pandas as pd

from mcs.bitvavo.rest import OrderBook
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import PaperBroker
from mcs.broker.rules import MarketRules
from mcs.market_data import INTERVAL_MS, Candle
from mcs.storage.positions import SqlitePositionStore
from mcs.strategy.config import StrategyConfig
from mcs.strategy.context import FramesContext
from mcs.strategy.simulation import Simulation

D = Decimal
BAR = INTERVAL_MS["4h"]
DAY = INTERVAL_MS["1d"]
MARKET = "TST-EUR"
RULES = MarketRules(MARKET, "trading", D("0.01"), 8, D("0.00001"), D("5"))
FEES = FeeSchedule(maker=D("0.0015"), taker=D("0.0025"))
HALF_SPREAD = D("0.0005")


class Switch:
    """Indicateur piloté par le test (signal principal, confirmation ou filtre)."""

    warmup = 1

    def __init__(self, buy: bool = False, sell: bool = False, favorable: bool = True) -> None:
        self.buy, self.sell, self.is_favorable = buy, sell, favorable

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        return pd.Series(self.buy, index=frame.index)

    def sell_state(self, frame: pd.DataFrame) -> pd.Series:
        return pd.Series(self.sell, index=frame.index)

    def favorable(self, frame: pd.DataFrame) -> pd.Series:
        return pd.Series(self.is_favorable, index=frame.index)


class PriceBooks:
    """Carnet profond centré sur le dernier cours (± 0,05 %)."""

    def __init__(self) -> None:
        self.price = D(100)

    async def get_book(self, market: str) -> OrderBook:
        bid = (self.price * (1 - HALF_SPREAD)).quantize(D("0.01"))
        ask = (self.price * (1 + HALF_SPREAD)).quantize(D("0.01"))
        return OrderBook(market, [(bid, D(1000))], [(ask, D(1000))], 0)


class Harness:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: StrategyConfig,
        history: int = 30,
        daily_days: int = 250,
    ) -> None:
        conn.execute(
            "INSERT INTO simulations (id, pair_id, mode, family, config_json, seed, created_at)"
            " VALUES (1, 1, 'typical', 'test', '{}', 1, 0)"
        )
        self.conn = conn
        self.books = PriceBooks()
        self.broker = PaperBroker(
            conn, FEES, self.books, rules={MARKET: RULES}, spread=lambda _: D("0.001")
        )
        self.broker.open_account(1, D(1000))
        self.store = SqlitePositionStore(conn)
        self.sim = Simulation(1, config, self.broker, FEES, [MARKET], store=self.store)
        self.rows: list[tuple[float, float, float]] = [(100.0, 101.0, 99.0)] * history
        self.daily_days = daily_days

    @property
    def now(self) -> int:
        """Instant de décision : clôture de la dernière bougie 4 h."""
        return len(self.rows) * BAR

    @property
    def price(self) -> Decimal:
        return D(repr(self.rows[-1][0]))

    def frame(self) -> pd.DataFrame:
        close, high, low = zip(*self.rows, strict=True)
        return pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close, "volume": 1.0},
            index=pd.Index([i * BAR for i in range(len(self.rows))], name="ts"),
        )

    def ctx(self) -> FramesContext:
        daily = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            index=pd.Index([i * DAY for i in range(self.daily_days)], name="ts"),
        )
        price = self.price
        bid = (price * (1 - HALF_SPREAD)).quantize(D("0.01"))
        return FramesContext(
            {MARKET: self.frame()}, {MARKET: daily}, bids={MARKET: bid}, prices={MARKET: price}
        )

    async def bar(self, close: float, high: float | None = None, low: float | None = None) -> None:
        """Ajoute une bougie 4 h : stops vérifiés sur son plus bas, ordres limite sur ses
        extrêmes (une bougie 1 min équivalente), puis clôture 4 h."""
        high = close * 1.01 if high is None else high
        low = close * 0.99 if low is None else low
        start = self.now
        self.books.price = D(repr(close))
        minute = Candle(
            MARKET,
            "1m",
            start + 60_000,
            D(repr(close)),
            D(repr(high)),
            D(repr(low)),
            D(repr(close)),
            D(1),
        )
        self.rows.append((close, high, low))
        ctx = self.ctx()
        await self.sim.on_price(MARKET, minute.low, start + 60_000)
        for order in self.broker.on_candle(minute):
            await self.sim.on_order_filled(order, ctx, start + 60_000)
        for order in self.broker.expire_orders(self.now):
            self.sim.on_order_closed(order)
        await self.sim.on_4h_close(self.now, ctx)
