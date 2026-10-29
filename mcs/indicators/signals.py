"""États d'achat et de vente des indicateurs (décisions de l'utilisateur, SPEC §13).

- Un indicateur est en « signal d'achat » tant que sa condition est vraie (état, pas
  événement). Exception : la cassure Donchian n'est vraie que sur les bougies qui clôturent
  au-dessus du plus haut des N bougies précédentes.
- `sell_state` (signaux principaux) sert à la sortie sur signal inverse.
- `warmup` = nombre de bougies nécessaires à la première valeur ; une paire qui ne les a pas
  est exclue des signaux.
- Filtre de fond : bande d'hystérésis symétrique autour de 0 (voir `TrendFilter`).

Toutes les séries sont booléennes, False tant que l'indicateur n'est pas calculable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np
import pandas as pd

from mcs.indicators import core


class MainSignal(Protocol):
    @property
    def warmup(self) -> int: ...
    def buy_state(self, frame: pd.DataFrame) -> pd.Series: ...
    def sell_state(self, frame: pd.DataFrame) -> pd.Series: ...


class Confirmation(Protocol):
    @property
    def warmup(self) -> int: ...
    def buy_state(self, frame: pd.DataFrame) -> pd.Series: ...


def _bool(condition: pd.Series) -> pd.Series:
    return condition.fillna(False).astype(bool)


# --- Signaux principaux (4 h) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmaCross:
    fast: int
    slow: int

    def __post_init__(self) -> None:
        if not 1 <= self.fast < self.slow:
            raise ValueError("EMA : il faut 1 ≤ rapide < lente.")

    @property
    def warmup(self) -> int:
        return self.slow

    def _lines(self, frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        return core.ema(frame["close"], self.fast), core.ema(frame["close"], self.slow)

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        fast, slow = self._lines(frame)
        return _bool(fast > slow)

    def sell_state(self, frame: pd.DataFrame) -> pd.Series:
        fast, slow = self._lines(frame)
        return _bool(fast < slow)


@dataclass(frozen=True, slots=True)
class Macd:
    fast: int
    slow: int
    signal: int

    def __post_init__(self) -> None:
        if not 1 <= self.fast < self.slow or self.signal < 1:
            raise ValueError("MACD : il faut 1 ≤ rapide < lent et signal ≥ 1.")

    @property
    def warmup(self) -> int:
        return self.slow + self.signal - 1

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        values = core.macd(frame["close"], self.fast, self.slow, self.signal)
        return _bool(values["macd"] > values["signal"])

    def sell_state(self, frame: pd.DataFrame) -> pd.Series:
        values = core.macd(frame["close"], self.fast, self.slow, self.signal)
        return _bool(values["macd"] < values["signal"])


@dataclass(frozen=True, slots=True)
class Supertrend:
    atr_period: int
    multiplier: float

    @property
    def warmup(self) -> int:
        return self.atr_period + 1

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        return core.supertrend(frame, self.atr_period, self.multiplier)["direction"] == 1

    def sell_state(self, frame: pd.DataFrame) -> pd.Series:
        return core.supertrend(frame, self.atr_period, self.multiplier)["direction"] == -1


@dataclass(frozen=True, slots=True)
class DonchianBreakout:
    period: int

    @property
    def warmup(self) -> int:
        return self.period + 1

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        channel = core.donchian(frame, self.period)
        return _bool(frame["close"] > channel["upper"])

    def sell_state(self, frame: pd.DataFrame) -> pd.Series:
        channel = core.donchian(frame, self.period)
        return _bool(frame["close"] < channel["lower"])


# --- Confirmations (4 h) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdxConfirmation:
    period: int
    threshold: float

    @property
    def warmup(self) -> int:
        return 2 * self.period

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        values = core.adx(frame, self.period)
        return _bool((values["adx"] > self.threshold) & (values["plus_di"] > values["minus_di"]))


@dataclass(frozen=True, slots=True)
class VolumeConfirmation:
    window: int
    multiple: float

    @property
    def warmup(self) -> int:
        return self.window + 1

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        return _bool(core.relative_volume(frame["volume"], self.window) >= self.multiple)


@dataclass(frozen=True, slots=True)
class RsiConfirmation:
    period: int
    max_level: float

    @property
    def warmup(self) -> int:
        return self.period + 1

    def buy_state(self, frame: pd.DataFrame) -> pd.Series:
        return _bool(core.rsi(frame["close"], self.period) < self.max_level)


# --- Filtre de fond (1 j) -------------------------------------------------------------------


class TrendFilterKind(StrEnum):
    PRICE_ABOVE_SMA = "price_above_sma"
    EMA_CROSS = "ema_cross"
    ROC = "roc"


@dataclass(frozen=True, slots=True)
class TrendFilter:
    """Filtre de fond bloquant, avec hystérésis symétrique de largeur `hysteresis` (0,02 = 2 %).

    Écart mesuré : prix / SMA − 1, EMA rapide / EMA lente − 1, ou ROC / 100. Le filtre devient
    favorable quand l'écart dépasse +hystérésis, défavorable sous −hystérésis, et garde son
    état entre les deux. Il est défavorable tant que l'indicateur n'est pas calculable.
    """

    kind: TrendFilterKind
    hysteresis: float
    sma_window: int = 200
    ema_fast: int = 50
    ema_slow: int = 200
    roc_window: int = 60

    def __post_init__(self) -> None:
        if self.hysteresis < 0:
            raise ValueError("Hystérésis négative.")
        if self.kind is TrendFilterKind.EMA_CROSS and not 1 <= self.ema_fast < self.ema_slow:
            raise ValueError("Filtre EMA : il faut 1 ≤ rapide < lente.")

    @property
    def warmup(self) -> int:
        if self.kind is TrendFilterKind.PRICE_ABOVE_SMA:
            return self.sma_window
        if self.kind is TrendFilterKind.EMA_CROSS:
            return self.ema_slow
        return self.roc_window + 1

    def gap(self, frame: pd.DataFrame) -> pd.Series:
        close = frame["close"]
        if self.kind is TrendFilterKind.PRICE_ABOVE_SMA:
            return close / core.sma(close, self.sma_window) - 1.0
        if self.kind is TrendFilterKind.EMA_CROSS:
            return core.ema(close, self.ema_fast) / core.ema(close, self.ema_slow) - 1.0
        return core.roc(close, self.roc_window) / 100.0

    def favorable(self, frame: pd.DataFrame) -> pd.Series:
        return hysteresis_state(self.gap(frame), self.hysteresis)


def hysteresis_state(gap: pd.Series, band: float) -> pd.Series:
    """True au-dessus de +band, False sous −band, état précédent entre les deux (False au
    départ et tant que `gap` est NaN)."""
    out = []
    state = False
    for value in gap.to_numpy(dtype="float64").tolist():
        if value != value:  # NaN
            state = False
        elif value > band:
            state = True
        elif value < -band:
            state = False
        out.append(state)
    return pd.Series(np.array(out, dtype=bool), index=gap.index)
