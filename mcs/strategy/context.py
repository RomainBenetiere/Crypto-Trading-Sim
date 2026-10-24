"""Vue des données de marché fournie aux simulations à l'instant de décision.

Le moteur live et le backtest fournissent chacun leur implémentation ; seules des bougies
clôturées y figurent. Les états des indicateurs sont mis en cache : les simulations qui
partagent les mêmes paramètres ne les recalculent pas.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

import pandas as pd


class MarketContext(Protocol):
    def frame_4h(self, market: str) -> pd.DataFrame:
        """Bougies 4 h clôturées (trous comblés), indexées par `ts`."""

    def frame_1d(self, market: str) -> pd.DataFrame:
        """Bougies 1 j clôturées."""

    def daily_history_days(self, market: str) -> int: ...

    def best_bid(self, market: str) -> Decimal | None: ...

    def last_price(self, market: str) -> Decimal | None: ...

    def state(self, market: str, indicator: Any, kind: str) -> bool:
        """Dernière valeur de `indicator.<kind>(frame)` (buy_state, sell_state, favorable)."""


class FramesContext:
    """Implémentation à partir de DataFrames et de cours fournis (tests, backtest)."""

    def __init__(
        self,
        frames_4h: Mapping[str, pd.DataFrame],
        frames_1d: Mapping[str, pd.DataFrame],
        bids: Mapping[str, Decimal] | None = None,
        prices: Mapping[str, Decimal] | None = None,
        history_days: Mapping[str, int] | None = None,
    ) -> None:
        self.frames_4h, self.frames_1d = dict(frames_4h), dict(frames_1d)
        self.bids, self.prices = dict(bids or {}), dict(prices or {})
        self.history_days = dict(history_days or {})
        self._cache: dict[tuple[str, Any, str, int], bool] = {}

    def frame_4h(self, market: str) -> pd.DataFrame:
        return self.frames_4h[market]

    def frame_1d(self, market: str) -> pd.DataFrame:
        return self.frames_1d.get(market, pd.DataFrame())

    def daily_history_days(self, market: str) -> int:
        if market in self.history_days:
            return self.history_days[market]
        return len(self.frame_1d(market))

    def best_bid(self, market: str) -> Decimal | None:
        return self.bids.get(market)

    def last_price(self, market: str) -> Decimal | None:
        if market in self.prices:
            return self.prices[market]
        frame = self.frames_4h.get(market)
        if frame is None or frame.empty:
            return None
        return Decimal(repr(float(frame["close"].iloc[-1])))

    def state(self, market: str, indicator: Any, kind: str) -> bool:
        frame = self.frame_1d(market) if kind == "favorable" else self.frame_4h(market)
        if frame.empty:
            return False
        key = (market, indicator, kind, int(frame.index[-1]))
        if key not in self._cache:
            self._cache[key] = bool(getattr(indicator, kind)(frame).iloc[-1])
        return self._cache[key]
