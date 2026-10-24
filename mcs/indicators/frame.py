"""Conversion des bougies en DataFrame pour le calcul vectorisé des indicateurs.

Les indicateurs travaillent en `float64` (toléré par CLAUDE.md pour le calcul vectorisé) ;
les montants et niveaux d'ordre repassent en `Decimal` dans la stratégie et le broker.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from mcs.market_data import INTERVAL_MS, Candle

OHLCV = ["open", "high", "low", "close", "volume"]


def candles_to_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    """DataFrame indexé par `ts` (début de bougie, ms UTC), colonnes OHLCV en float64."""
    frame = pd.DataFrame(
        {
            "open": [float(c.open) for c in candles],
            "high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles],
            "volume": [float(c.volume) for c in candles],
        },
        index=pd.Index([c.ts for c in candles], name="ts", dtype="int64"),
    )
    return frame.sort_index()


def fill_gaps(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Insère les bougies absentes (aucune transaction, panne) : O = H = L = C = clôture
    précédente, volume 0. Ajoute la colonne booléenne `filled`.

    Ainsi une fenêtre de N bougies couvre toujours N intervalles réels.
    """
    if frame.empty:
        return frame.assign(filled=pd.Series(dtype=bool))
    step = INTERVAL_MS[interval]
    first, last = int(frame.index[0]), int(frame.index[-1])
    if (last - first) % step or any(int(ts) % step for ts in (first, last)):
        raise ValueError(f"Horodatages non alignés sur l'intervalle {interval}.")
    full = pd.Index(np.arange(first, last + step, step, dtype="int64"), name="ts")
    out = frame.reindex(full)
    filled = out["close"].isna()
    out["close"] = out["close"].ffill()
    for column in ("open", "high", "low"):
        out[column] = out[column].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    out["filled"] = filled
    return out[[*OHLCV, "filled"]]
