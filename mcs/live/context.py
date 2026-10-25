"""Contexte de marché du moteur live.

Même calcul des indicateurs que le backtest (séries sur tout l'historique en base, lues à la
dernière bougie clôturée) ; les cours viennent du ticker WebSocket quand il est frais, sinon
de la dernière bougie 1 h. Les bougies clôturées sont ajoutées au fil de l'eau.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal

import pandas as pd

from mcs.backtest import BacktestContext, Dataset, MarketData
from mcs.bitvavo.ws import TickerState
from mcs.indicators.frame import candles_to_frame, fill_gaps
from mcs.storage.candles import load_candles
from mcs.strategy.config import CHANDELIER_PERIOD, StopType, StrategyConfig, TrailingType

TICKER_STALE_MS = 120_000


class LiveContext(BacktestContext):
    def __init__(
        self,
        data: Dataset,
        tickers: Mapping[str, TickerState] | None = None,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        super().__init__(data)
        self.tickers = tickers if tickers is not None else {}
        self.clock = clock

    def _ticker(self, market: str) -> TickerState | None:
        state = self.tickers.get(market)
        if state is None or self.clock() - state.updated_ms >= TICKER_STALE_MS:
            return None
        return state

    def last_price(self, market: str) -> Decimal | None:
        state = self._ticker(market)
        if state is not None and state.last_price is not None:
            return state.last_price
        return super().last_price(market)

    def best_bid(self, market: str) -> Decimal | None:
        state = self._ticker(market)
        if state is not None and state.best_bid is not None:
            return state.best_bid
        return super().best_bid(market)

    def append_closed(self, conn: sqlite3.Connection, market: str) -> int:
        """Ajoute les bougies 1 h, 4 h et 1 j clôturées arrivées en base depuis le dernier
        appel ; invalide les séries en cache de la paire. Retourne le nombre ajouté."""
        data = self.data.markets[market]
        frames = {"1h": data.h1, "4h": data.h4, "1d": data.d1}
        added = 0
        for interval, frame in frames.items():
            after = int(frame.index[-1]) + 1 if len(frame) else 0
            new = load_candles(conn, market, interval, start=after)
            if new:
                combined = pd.concat([frame.drop(columns="filled"), candles_to_frame(new)])
                frames[interval] = fill_gaps(combined, interval)
                added += len(new)
        if added:
            self.data.markets[market] = MarketData(
                market, data.rules, data.spread, frames["1h"], frames["4h"], frames["1d"]
            )
            for key in [k for k in self.cache if k[0] == market]:
                del self.cache[key]
            self._counts.clear()
        return added

    def warm(self, configs: Iterable[StrategyConfig]) -> None:
        """Précalcule les séries utilisées par les simulations (appelé dans un thread avant
        les décisions de la clôture 4 h, pour ne pas bloquer la surveillance des stops)."""
        configs = list(configs)
        for market in self.data.markets:
            self.atr_4h(market)
            for config in configs:
                signals = config.signals
                self.state(market, signals.main, "buy_state")
                if config.exits.reverse_signal:
                    self.state(market, signals.main, "sell_state")
                for confirmation, _ in signals.confirmations:
                    self.state(market, confirmation, "buy_state")
                if config.trend.enabled:
                    self.state(market, config.trend.filter, "favorable")
                if config.stop.type is StopType.LOWEST_LOW:
                    self.lowest_low_4h(market, config.stop.lowest_n)
                if config.trailing.type is TrailingType.CHANDELIER:
                    self.highest_high_4h(market, CHANDELIER_PERIOD)
                if config.trailing.type is TrailingType.SUPERTREND:
                    self.supertrend_4h(market, config.supertrend_for_trailing)
