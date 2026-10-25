"""Vue des données de marché fournie aux simulations à l'instant de décision.

Le moteur live et le backtest fournissent chacun leur implémentation ; seules des bougies
clôturées y figurent. Les indicateurs sont calculés sur tout l'historique disponible (aucune
valeur ne lit le futur, voir tests de l'étape 2) et mis en cache : les simulations qui
partagent les mêmes paramètres ne les recalculent pas.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

import pandas as pd

from mcs.indicators import core
from mcs.indicators.signals import Supertrend
from mcs.strategy.config import ATR_PERIOD


def _dec(value: float) -> Decimal | None:
    return None if math.isnan(value) else Decimal(repr(float(value)))


class MarketContext(Protocol):
    def frame_4h(self, market: str) -> pd.DataFrame:
        """Bougies 4 h clôturées (trous comblés), indexées par `ts`."""

    def bars_4h(self, market: str) -> int:
        """Nombre de bougies 4 h clôturées disponibles."""

    def daily_history_days(self, market: str) -> int: ...

    def best_bid(self, market: str) -> Decimal | None: ...

    def last_price(self, market: str) -> Decimal | None: ...

    def state(self, market: str, indicator: Any, kind: str) -> bool:
        """Dernière valeur de `indicator.<kind>(...)` : buy_state / sell_state sur les
        bougies 4 h, favorable sur les bougies 1 j."""

    def atr_4h(self, market: str) -> Decimal | None:
        """ATR 4 h (période 14) de la dernière bougie clôturée."""

    def highest_high_4h(self, market: str, n: int) -> Decimal | None:
        """Plus haut des `n` dernières bougies 4 h clôturées (n = 1 : la dernière)."""

    def lowest_low_4h(self, market: str, n: int) -> Decimal | None: ...

    def supertrend_4h(self, market: str, params: Supertrend) -> tuple[int, Decimal | None]:
        """(direction, ligne) Supertrend de la dernière bougie clôturée."""


class FramesContext:
    """Implémentation à partir de DataFrames et de cours fournis (tests)."""

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
        self._cache: dict[tuple[Any, ...], Any] = {}

    def frame_4h(self, market: str) -> pd.DataFrame:
        return self.frames_4h[market]

    def frame_1d(self, market: str) -> pd.DataFrame:
        return self.frames_1d.get(market, pd.DataFrame())

    def bars_4h(self, market: str) -> int:
        return len(self.frames_4h.get(market, ()))

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
        return _dec(frame["close"].iloc[-1])

    def _cached(self, key: tuple[Any, ...], frame: pd.DataFrame, compute: Any) -> Any:
        full_key = (*key, int(frame.index[-1]), len(frame))
        if full_key not in self._cache:
            self._cache[full_key] = compute()
        return self._cache[full_key]

    def state(self, market: str, indicator: Any, kind: str) -> bool:
        frame = self.frame_1d(market) if kind == "favorable" else self.frame_4h(market)
        if frame.empty:
            return False
        return bool(
            self._cached(
                (market, indicator, kind), frame, lambda: getattr(indicator, kind)(frame).iloc[-1]
            )
        )

    def atr_4h(self, market: str) -> Decimal | None:
        frame = self.frame_4h(market)
        if frame.empty:
            return None
        atr: Decimal | None = self._cached(
            (market, "atr"), frame, lambda: _dec(core.atr(frame, ATR_PERIOD).iloc[-1])
        )
        return atr

    def highest_high_4h(self, market: str, n: int) -> Decimal | None:
        frame = self.frame_4h(market)
        return None if len(frame) < n else _dec(frame["high"].iloc[-n:].max())

    def lowest_low_4h(self, market: str, n: int) -> Decimal | None:
        frame = self.frame_4h(market)
        return None if len(frame) < n else _dec(frame["low"].iloc[-n:].min())

    def supertrend_4h(self, market: str, params: Supertrend) -> tuple[int, Decimal | None]:
        frame = self.frame_4h(market)
        if frame.empty:
            return 0, None

        def compute() -> tuple[int, Decimal | None]:
            values = core.supertrend(frame, params.atr_period, params.multiplier)
            return int(values["direction"].iloc[-1]), _dec(values["line"].iloc[-1])

        result: tuple[int, Decimal | None] = self._cached((market, params), frame, compute)
        return result
