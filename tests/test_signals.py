"""États d'achat / de vente, démarrage, filtre de fond avec hystérésis, alignement 1 j → 4 h."""

import time

import numpy as np
import pandas as pd
import pytest

from mcs.indicators import core
from mcs.indicators.align import as_of, as_of_flag
from mcs.indicators.signals import (
    AdxConfirmation,
    DonchianBreakout,
    EmaCross,
    Macd,
    RsiConfirmation,
    Supertrend,
    TrendFilter,
    TrendFilterKind,
    VolumeConfirmation,
    hysteresis_state,
)
from tests.test_indicators import H4, frame_from, load_fixture

DAY = 24 * 3_600_000

ALL_SIGNALS = [
    EmaCross(21, 55),
    Macd(12, 26, 9),
    Supertrend(10, 3.0),
    DonchianBreakout(20),
    AdxConfirmation(14, 20.0),
    VolumeConfirmation(20, 1.5),
    RsiConfirmation(14, 70.0),
]


@pytest.mark.parametrize("signal", ALL_SIGNALS, ids=lambda s: type(s).__name__)
def test_no_signal_before_warmup(signal: object) -> None:
    frame = load_fixture("BTC-EUR_4h")
    state = signal.buy_state(frame)  # type: ignore[attr-defined]
    warmup = signal.warmup  # type: ignore[attr-defined]
    assert not state.iloc[: warmup - 1].any()
    assert state.dtype == bool and len(state) == len(frame)


# Première valeur calculable = indice `warmup − 1` pour chaque indicateur sous-jacent.
WARMUP_CASES = [
    (EmaCross(21, 55), lambda f: core.ema(f["close"], 55)),
    (Macd(12, 26, 9), lambda f: core.macd(f["close"], 12, 26, 9)["signal"]),
    (Supertrend(10, 3.0), lambda f: core.supertrend(f, 10, 3.0)["line"]),
    (DonchianBreakout(20), lambda f: core.donchian(f, 20)["upper"]),
    (AdxConfirmation(14, 20.0), lambda f: core.adx(f, 14)["adx"]),
    (VolumeConfirmation(20, 1.5), lambda f: core.relative_volume(f["volume"], 20)),
    (RsiConfirmation(14, 70.0), lambda f: core.rsi(f["close"], 14)),
    (TrendFilter(TrendFilterKind.PRICE_ABOVE_SMA, 0.02, sma_window=200), None),
    (TrendFilter(TrendFilterKind.EMA_CROSS, 0.02, ema_fast=50, ema_slow=200), None),
    (TrendFilter(TrendFilterKind.ROC, 0.02, roc_window=60), None),
]


@pytest.mark.parametrize("signal, value", WARMUP_CASES, ids=lambda x: type(x).__name__)
def test_warmup_is_first_computable_value(signal: object, value: object) -> None:
    frame = load_fixture("BTC-EUR_4h")
    series = signal.gap(frame) if value is None else value(frame)  # type: ignore[attr-defined, operator]
    first_valid = int(np.flatnonzero(series.notna().to_numpy())[0])
    assert first_valid == signal.warmup - 1  # type: ignore[attr-defined]


def test_ema_cross_is_a_state() -> None:
    frame = frame_from([(p, p, p) for p in [10.0] * 5 + [11, 12, 13, 14, 15, 16, 17]])
    buy = EmaCross(2, 4).buy_state(frame)
    # Tant que la hausse dure, l'EMA rapide reste au-dessus : l'état reste vrai.
    assert buy.iloc[5:].all() and not buy.iloc[:5].any()
    assert not EmaCross(2, 4).sell_state(frame).any()


def test_donchian_breakout_true_only_on_breakout_candles() -> None:
    closes = [10, 11, 10, 12, 12, 11, 13]
    frame = frame_from([(c, c - 0.5, c) for c in closes])
    buy = DonchianBreakout(2).buy_state(frame)
    assert buy.tolist() == [False, False, False, True, False, False, True]


def test_buy_and_sell_states_never_overlap() -> None:
    frame = load_fixture("PEPE-EUR_4h")
    for signal in ALL_SIGNALS[:4]:
        both = signal.buy_state(frame) & signal.sell_state(frame)  # type: ignore[attr-defined]
        assert not both.any()


def test_confirmations() -> None:
    frame = load_fixture("BTC-EUR_4h")
    adx = core.adx(frame, 14)
    expected = (adx["adx"] > 25) & (adx["plus_di"] > adx["minus_di"])
    assert AdxConfirmation(14, 25.0).buy_state(frame).equals(expected.fillna(False))
    rsi = core.rsi(frame["close"], 14)
    assert RsiConfirmation(14, 60.0).buy_state(frame).equals((rsi < 60).fillna(False))


def test_parameter_constraints() -> None:
    with pytest.raises(ValueError):
        EmaCross(55, 21)
    with pytest.raises(ValueError):
        Macd(26, 12, 9)
    with pytest.raises(ValueError):
        TrendFilter(TrendFilterKind.EMA_CROSS, 0.02, ema_fast=200, ema_slow=50)


# --- Filtre de fond -----------------------------------------------------------------------


def test_hysteresis_band() -> None:
    gap = pd.Series([np.nan, 0.01, 0.03, 0.01, -0.01, -0.03, -0.01, 0.025, np.nan, 0.01])
    state = hysteresis_state(gap, 0.02)
    assert state.tolist() == [False, False, True, True, True, False, False, True, False, False]


def test_zero_hysteresis_follows_sign() -> None:
    gap = pd.Series([0.1, 0.0, -0.1, 0.0, 0.2])
    assert hysteresis_state(gap, 0.0).tolist() == [True, True, False, False, True]


def test_trend_filter_price_above_sma() -> None:
    closes = [100.0] * 3 + [101.0, 104.0, 103.0, 99.0, 96.0]
    frame = frame_from([(c, c, c) for c in closes])
    flt = TrendFilter(TrendFilterKind.PRICE_ABOVE_SMA, 0.02, sma_window=3)
    # écarts : 0, 0,0066, 0,0296, 0,0067, −0,0278, −0,0390
    assert flt.favorable(frame).tolist() == [False] * 4 + [True, True, False, False]


def test_trend_filter_roc_uses_percent() -> None:
    closes = [100.0, 100.0, 103.0, 101.0, 97.0]
    frame = frame_from([(c, c, c) for c in closes])
    flt = TrendFilter(TrendFilterKind.ROC, 0.02, roc_window=2)
    # ROC : nan, nan, +3 %, +1 %, −3 %
    assert flt.favorable(frame).tolist() == [False, False, True, True, False]


# --- Alignement 1 j → 4 h sans fuite --------------------------------------------------------


def test_daily_value_visible_only_after_daily_close() -> None:
    daily = pd.Series([1.0, 2.0], index=pd.Index([0, DAY]))
    four_h = pd.Index([i * H4 for i in range(12)])
    aligned = as_of(daily, "1d", four_h, "4h")
    # Bougie 4 h de 20 h le jour 0 : clôture à minuit, en même temps que la journalière 0.
    assert np.isnan(aligned.iloc[:5]).all()
    assert aligned.iloc[5] == 1.0
    assert (aligned.iloc[6:11] == 1.0).all()
    assert aligned.iloc[11] == 2.0


def test_alignment_never_uses_unclosed_daily_candle() -> None:
    daily_frame = load_fixture("BTC-EUR_1d")
    four_h = load_fixture("BTC-EUR_4h").index
    flt = TrendFilter(TrendFilterKind.PRICE_ABOVE_SMA, 0.02, sma_window=50)
    favorable = flt.favorable(daily_frame)
    aligned = as_of_flag(favorable, "1d", four_h, "4h")
    for ts in four_h[::7]:
        decided_at = ts + H4
        closed = favorable[favorable.index + DAY <= decided_at]
        expected = bool(closed.iloc[-1]) if len(closed) else False
        assert aligned.loc[ts] == expected


def test_alignment_with_empty_source() -> None:
    aligned = as_of_flag(pd.Series([], dtype=bool), "1d", pd.Index([0, H4]), "4h")
    assert aligned.tolist() == [False, False]


# --- Performance (préparation de l'optimiseur) ---------------------------------------------


def test_full_indicator_set_is_fast_enough() -> None:
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 17_000)))
    frame = frame_from([(c * 1.01, c * 0.99, c) for c in closes])
    started = time.perf_counter()
    for signal in ALL_SIGNALS:
        signal.buy_state(frame)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{elapsed:.2f} s pour 17 000 bougies"
