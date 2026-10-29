"""Indicateurs techniques : fonctions pures sur des séries pandas (float64).

Conventions (celles de TA-Lib, qui sert de référence dans les tests) :
- EMA amorcée par la moyenne simple des `n` premières valeurs, puis alpha = 2 / (n + 1) ;
- lissage de Wilder (ATR, RSI, ADX) amorcé de même, avec alpha = 1 / n ;
- une valeur n'est produite que lorsque la fenêtre est complète (NaN avant).

Aucune fonction ne lit de valeur future : la valeur à l'indice t ne dépend que de [0, t].
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _check_period(n: int) -> None:
    if n < 1:
        raise ValueError(f"Période invalide : {n}")


def sma(series: pd.Series, n: int) -> pd.Series:
    _check_period(n)
    return series.rolling(n, min_periods=n).mean()


def _seeded_ewm(series: pd.Series, n: int, alpha: float) -> pd.Series:
    """Moyenne exponentielle amorcée par la SMA des `n` premières valeurs valides."""
    _check_period(n)
    values = series.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=series.index)
    start = valid[0]
    if np.isnan(values[start : start + n]).any():
        raise ValueError("Valeurs manquantes au milieu de la série.")
    seed_at = start + n - 1
    out[seed_at] = values[start : seed_at + 1].mean()
    tail = pd.Series(values[seed_at:])
    tail.iloc[0] = out[seed_at]
    out[seed_at:] = tail.ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return pd.Series(out, index=series.index)


def ema(series: pd.Series, n: int) -> pd.Series:
    return _seeded_ewm(series, n, 2.0 / (n + 1))


def wilder(series: pd.Series, n: int) -> pd.Series:
    """Lissage de Wilder (RMA)."""
    return _seeded_ewm(series, n, 1.0 / n)


def _wilder_sum(series: pd.Series, n: int) -> pd.Series:
    """Somme lissée de Wilder utilisée pour +DI / −DI (convention TA-Lib) : amorce = somme des
    `n − 1` premières valeurs, puis S = S − S / n + x ; première sortie à la n-ième valeur."""
    _check_period(n)
    values = series.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=series.index)
    start = int(valid[0])
    total = float(values[start : start + n - 1].sum())
    items, smoothed = values.tolist(), out.tolist()  # listes : boucle rapide, mêmes opérations
    for i in range(start + n - 1, len(items)):
        total = total - total / n + items[i]
        smoothed[i] = total
    return pd.Series(np.array(smoothed), index=series.index)


def true_range(frame: pd.DataFrame) -> pd.Series:
    """True range ; NaN sur la première bougie (pas de clôture précédente), comme TA-Lib."""
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    tr = ranges.max(axis=1)
    tr.iloc[:1] = np.nan
    return tr


def atr(frame: pd.DataFrame, n: int) -> pd.Series:
    return wilder(true_range(frame), n)


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0.0), n)
    loss = wilder((-delta).clip(lower=0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 - 100.0 / (1.0 + gain / loss)
    # Aucune baisse sur la fenêtre : RSI = 100 ; ni hausse ni baisse : 50.
    value = value.where(loss != 0, 100.0).where((gain != 0) | (loss != 0), 50.0)
    return value.where(gain.notna())


def roc(close: pd.Series, n: int) -> pd.Series:
    """Rate of change en % : (close / close[t - n] - 1) × 100."""
    _check_period(n)
    return (close / close.shift(n) - 1.0) * 100.0


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    if fast >= slow:
        raise ValueError("MACD : la période rapide doit être inférieure à la lente.")
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return pd.DataFrame(
        {"macd": line, "signal": signal_line, "hist": line - signal_line}, index=close.index
    )


def adx(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """ADX de Wilder avec +DI et −DI."""
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    plus_dm.iloc[:1] = minus_dm.iloc[:1] = np.nan
    tr = _wilder_sum(true_range(frame), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * _wilder_sum(plus_dm, n) / tr
        minus_di = 100.0 * _wilder_sum(minus_dm, n) / tr
        total = plus_di + minus_di
        dx = (100.0 * (plus_di - minus_di).abs() / total).where(total != 0, 0.0)
    dx = dx.where(plus_di.notna())
    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": wilder(dx, n)}, index=frame.index
    )


def supertrend(frame: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """Supertrend : ligne (support en tendance haussière, résistance sinon) et direction
    (+1 haussière, −1 baissière, 0 avant la fin du démarrage)."""
    # Boucle sur des listes Python (bien plus rapide que l'indexation numpy élément par
    # élément), mêmes opérations flottantes : résultats identiques au bit près.
    mid = ((frame["high"] + frame["low"]) / 2.0).to_numpy().tolist()
    band = (multiplier * atr(frame, period).to_numpy()).tolist()
    close = frame["close"].to_numpy().tolist()
    size = len(close)
    nan = math.nan
    upper, lower, line = [nan] * size, [nan] * size, [nan] * size
    direction = [0] * size
    started = False
    for i in range(size):
        b = band[i]
        if b != b:  # NaN : ATR pas encore calculable
            continue
        basic_upper, basic_lower = mid[i] + b, mid[i] - b
        if not started:
            started = True
            upper[i], lower[i] = basic_upper, basic_lower
            direction[i] = 1 if close[i] > basic_upper else -1
        else:
            prev_close, prev_upper, prev_lower = close[i - 1], upper[i - 1], lower[i - 1]
            upper[i] = (
                basic_upper if basic_upper < prev_upper or prev_close > prev_upper else prev_upper
            )
            lower[i] = (
                basic_lower if basic_lower > prev_lower or prev_close < prev_lower else prev_lower
            )
            if direction[i - 1] == -1:
                direction[i] = 1 if close[i] > upper[i] else -1
            else:
                direction[i] = -1 if close[i] < lower[i] else 1
        line[i] = lower[i] if direction[i] == 1 else upper[i]
    return pd.DataFrame(
        {
            "line": np.array(line),
            "direction": np.array(direction, dtype="int64"),
            "upper": np.array(upper),
            "lower": np.array(lower),
        },
        index=frame.index,
    )


def donchian(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """Canal de Donchian sur les `n` bougies précédentes (bougie courante exclue)."""
    _check_period(n)
    return pd.DataFrame(
        {
            "upper": frame["high"].rolling(n, min_periods=n).max().shift(1),
            "lower": frame["low"].rolling(n, min_periods=n).min().shift(1),
        },
        index=frame.index,
    )


def relative_volume(volume: pd.Series, n: int) -> pd.Series:
    """Volume ÷ moyenne des `n` volumes précédents (NaN si cette moyenne est nulle)."""
    _check_period(n)
    mean = volume.rolling(n, min_periods=n).mean().shift(1)
    return (volume / mean.where(mean > 0)).astype("float64")


def highest(series: pd.Series, n: int) -> pd.Series:
    """Plus haut des `n` dernières valeurs, valeur courante incluse."""
    _check_period(n)
    return series.rolling(n, min_periods=n).max()


def lowest(series: pd.Series, n: int) -> pd.Series:
    """Plus bas des `n` dernières valeurs, valeur courante incluse."""
    _check_period(n)
    return series.rolling(n, min_periods=n).min()
