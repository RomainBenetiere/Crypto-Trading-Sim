"""Historique synthétique (marche aléatoire horaire) pour les tests du backtest."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from mcs.backtest import Dataset, MarketData
from mcs.broker.rules import MarketRules
from mcs.market_data import INTERVAL_MS

HOUR = INTERVAL_MS["1h"]
START = 1_600_000_000_000 // INTERVAL_MS["1d"] * INTERVAL_MS["1d"]


def hourly(seed: int, hours: int, start_price: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(0.0002, 0.01, hours)))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.004, hours)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(1, 10, hours),
        },
        index=pd.Index(START + HOUR * np.arange(hours), name="ts", dtype="int64"),
    )


def resample(h1: pd.DataFrame, interval: str) -> pd.DataFrame:
    step = INTERVAL_MS[interval]
    groups = h1.groupby(h1.index // step * step)
    frame = pd.DataFrame(
        {
            "open": groups["open"].first(),
            "high": groups["high"].max(),
            "low": groups["low"].min(),
            "close": groups["close"].last(),
            "volume": groups["volume"].sum(),
        }
    )
    frame.index.name = "ts"
    frame["filled"] = False
    return frame


def market_data(market: str, h1: pd.DataFrame, end: int | None = None) -> MarketData:
    """Données d'une paire, tronquées aux bougies terminées avant `end` le cas échéant."""
    if end is not None:
        h1 = h1[h1.index + HOUR <= end]
    h4, d1 = resample(h1, "4h"), resample(h1, "1d")
    if end is not None:
        h4 = h4[h4.index + INTERVAL_MS["4h"] <= end]
        d1 = d1[d1.index + INTERVAL_MS["1d"] <= end]
    rules = MarketRules(market, "trading", Decimal("0.01"), 6, Decimal("0.0001"), Decimal("5"))
    return MarketData(market, rules, Decimal("0.001"), h1.assign(filled=False), h4, d1)


def dataset(
    days: int = 320, markets: tuple[str, ...] = ("AAA-EUR", "BBB-EUR"), end: int | None = None
) -> Dataset:
    return Dataset(
        {m: market_data(m, hourly(seed, days * 24), end) for seed, m in enumerate(markets, start=1)}
    )
