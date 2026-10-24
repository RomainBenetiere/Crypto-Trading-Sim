"""Indicateurs : valeurs de référence (TA-Lib et exemples calculés à la main), démarrage,
absence de lecture de valeurs futures."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import talib

from mcs.indicators import core
from mcs.indicators.frame import candles_to_frame, fill_gaps
from mcs.market_data import Candle

FIXTURES = Path(__file__).parent / "fixtures"
H4 = 4 * 3_600_000


def load_fixture(name: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES / f"{name}.csv", index_col="ts", dtype="float64")
    frame.index = frame.index.astype("int64")
    return frame


@pytest.fixture(params=["BTC-EUR_4h", "PEPE-EUR_4h"])
def real(request: pytest.FixtureRequest) -> pd.DataFrame:
    return load_fixture(request.param)


def assert_matches(
    ours: pd.Series, reference: np.ndarray, skip: int = 0, atol: float = 0.0
) -> None:
    """Mêmes positions NaN (sauf `skip`) et mêmes valeurs à 1e-9 près en relatif."""
    ours_values = ours.to_numpy(dtype="float64")
    if skip == 0:
        np.testing.assert_array_equal(np.isnan(ours_values), np.isnan(reference))
    mask = ~np.isnan(reference)
    mask[:skip] = False
    np.testing.assert_allclose(ours_values[mask], reference[mask], rtol=1e-9, atol=atol)


# --- Référence TA-Lib (données réelles BTC-EUR et PEPE-EUR, 600 bougies 4 h) ---------------


def test_sma_ema_match_talib(real: pd.DataFrame) -> None:
    close = real["close"].to_numpy()
    for n in (8, 20, 55, 200):
        assert_matches(core.sma(real["close"], n), talib.SMA(close, n))
        assert_matches(core.ema(real["close"], n), talib.EMA(close, n))


def test_rsi_atr_roc_match_talib(real: pd.DataFrame) -> None:
    high, low, close = (real[c].to_numpy() for c in ("high", "low", "close"))
    for n in (7, 14, 21):
        assert_matches(core.rsi(real["close"], n), talib.RSI(close, n))
        assert_matches(core.atr(real, n), talib.ATR(high, low, close, n))
    for n in (30, 60, 90):
        assert_matches(core.roc(real["close"], n), talib.ROC(close, n))


def test_adx_and_directional_indicators_match_talib(real: pd.DataFrame) -> None:
    high, low, close = (real[c].to_numpy() for c in ("high", "low", "close"))
    for n in (10, 14, 20):
        values = core.adx(real, n)
        assert_matches(values["plus_di"], talib.PLUS_DI(high, low, close, n))
        assert_matches(values["minus_di"], talib.MINUS_DI(high, low, close, n))
        assert_matches(values["adx"], talib.ADX(high, low, close, n))


def test_macd_matches_talib_after_warmup(real: pd.DataFrame) -> None:
    # TA-Lib amorce l'EMA rapide autrement : les valeurs convergent après le démarrage
    # (écart résiduel ~1e-10 du prix, visible en relatif près des passages par zéro).
    atol = 1e-8 * float(real["close"].median())
    for fast, slow, signal in ((12, 26, 9), (8, 35, 7), (15, 20, 12)):
        ours = core.macd(real["close"], fast, slow, signal)
        line, signal_line, _ = talib.MACD(real["close"].to_numpy(), fast, slow, signal)
        assert_matches(ours["macd"], line, skip=200, atol=atol)
        assert_matches(ours["signal"], signal_line, skip=200, atol=atol)


# --- Exemples calculés à la main ----------------------------------------------------------


def frame_from(rows: list[tuple[float, float, float]], volume: float = 1.0) -> pd.DataFrame:
    high, low, close = zip(*rows, strict=True)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.Index([i * H4 for i in range(len(rows))], name="ts"),
    )


def test_supertrend_hand_example() -> None:
    # ATR(1) = true range ; multiplicateur 1. Détail du calcul :
    # 1 : milieu 10, TR 2 → bandes 12 / 8 ; clôture 10 ≤ 12 → baissier, ligne 12
    # 2 : milieu 11,5, TR 3 → 14,5 / 8,5 ; haute gardée à 12 ; clôture 12,5 > 12 → haussier
    # 3 : milieu 13, TR 2 → 15 / 11 ; basse remonte à 11 ; haussier, ligne 11
    # 4 : milieu 11,5, TR 3,5 → 15 / 8 ; basse gardée à 11 ; clôture 10,5 < 11 → baissier
    # 5 : milieu 10, TR 2 → 12 / 8 ; haute redescend à 12 ; baissier, ligne 12
    frame = frame_from(
        [(10, 8, 9), (11, 9, 10), (13, 10, 12.5), (14, 12, 13.5), (13, 10, 10.5), (11, 9, 9.5)]
    )
    result = core.supertrend(frame, 1, 1.0)
    assert result["direction"].tolist() == [0, -1, 1, 1, -1, -1]
    np.testing.assert_array_equal(result["line"].to_numpy(), [np.nan, 12, 8.5, 11, 15, 12])


def test_supertrend_lower_band_never_falls_in_uptrend(real: pd.DataFrame) -> None:
    result = core.supertrend(real, 10, 3.0)
    up = result["direction"].to_numpy() == 1
    line = result["line"].to_numpy()
    rising_pairs = up[1:] & up[:-1]
    assert (np.diff(line)[rising_pairs] >= 0).all()


def test_donchian_excludes_current_candle() -> None:
    frame = frame_from([(1, 0, 1), (2, 1, 2), (3, 2, 3), (2, 1, 2), (5, 0.5, 5)])
    channel = core.donchian(frame, 2)
    np.testing.assert_array_equal(channel["upper"].to_numpy(), [np.nan, np.nan, 2, 3, 3])
    np.testing.assert_array_equal(channel["lower"].to_numpy(), [np.nan, np.nan, 0, 1, 1])


def test_relative_volume_uses_previous_candles() -> None:
    volume = pd.Series([1.0, 1.0, 1.0, 3.0, 0.0, 0.0, 0.0, 0.0, 2.0])
    result = core.relative_volume(volume, 3).to_numpy()
    assert np.isnan(result[:3]).all()
    assert result[3] == 3.0 and result[4] == 0.0
    assert np.isnan(result[8])  # moyenne précédente nulle : pas de volume relatif


def test_highest_lowest_include_current() -> None:
    series = pd.Series([3.0, 1.0, 2.0, 5.0])
    assert core.highest(series, 2).tolist()[1:] == [3.0, 2.0, 5.0]
    assert core.lowest(series, 2).tolist()[1:] == [1.0, 1.0, 2.0]


def test_rsi_extremes() -> None:
    rising = pd.Series(np.arange(1.0, 30.0))
    flat = pd.Series(np.ones(30))
    assert core.rsi(rising, 14).dropna().eq(100.0).all()
    assert core.rsi(flat, 14).dropna().eq(50.0).all()


def test_invalid_periods_rejected() -> None:
    with pytest.raises(ValueError):
        core.sma(pd.Series([1.0]), 0)
    with pytest.raises(ValueError):
        core.macd(pd.Series([1.0]), 26, 12, 9)


# --- Pas de lecture de valeurs futures ----------------------------------------------------

INDICATORS: dict[str, Callable[[pd.DataFrame], pd.Series | pd.DataFrame]] = {
    "sma": lambda f: core.sma(f["close"], 20),
    "ema": lambda f: core.ema(f["close"], 21),
    "rsi": lambda f: core.rsi(f["close"], 14),
    "atr": lambda f: core.atr(f, 14),
    "roc": lambda f: core.roc(f["close"], 60),
    "macd": lambda f: core.macd(f["close"], 12, 26, 9),
    "adx": lambda f: core.adx(f, 14),
    "supertrend": lambda f: core.supertrend(f, 10, 3.0),
    "donchian": lambda f: core.donchian(f, 20),
    "relative_volume": lambda f: core.relative_volume(f["volume"], 20),
    "highest": lambda f: core.highest(f["high"], 20),
}


@pytest.mark.parametrize("name", INDICATORS)
def test_values_do_not_depend_on_future_candles(name: str) -> None:
    frame = load_fixture("BTC-EUR_4h")
    full = INDICATORS[name](frame)
    for cut in (30, 117, 350, 599):
        truncated = INDICATORS[name](frame.iloc[:cut])
        pd.testing.assert_frame_equal(
            pd.DataFrame(full).iloc[:cut], pd.DataFrame(truncated), check_exact=True
        )


# --- DataFrame et bougies manquantes ------------------------------------------------------


def _candle(ts: int, close: str, volume: str = "1") -> Candle:
    price = Decimal(close)
    return Candle("PEPE-EUR", "4h", ts, price, price, price, price, Decimal(volume))


def test_candles_to_frame_sorted_float() -> None:
    frame = candles_to_frame([_candle(H4, "0.0000059512"), _candle(0, "0.0000059")])
    assert frame.index.tolist() == [0, H4]
    assert frame["close"].dtype == np.float64
    assert frame["close"].iloc[1] == pytest.approx(5.9512e-6)


def test_fill_gaps_inserts_flat_zero_volume_candles() -> None:
    frame = candles_to_frame([_candle(0, "10", "5"), _candle(3 * H4, "12", "7")])
    filled = fill_gaps(frame, "4h")
    assert filled.index.tolist() == [0, H4, 2 * H4, 3 * H4]
    assert filled["filled"].tolist() == [False, True, True, False]
    gap = filled.loc[H4]
    assert (gap["open"], gap["high"], gap["low"], gap["close"], gap["volume"]) == (
        10.0,
        10.0,
        10.0,
        10.0,
        0.0,
    )


def test_fill_gaps_rejects_misaligned_timestamps() -> None:
    frame = candles_to_frame([_candle(0, "1"), _candle(H4 + 60_000, "1")])
    with pytest.raises(ValueError):
        fill_gaps(frame, "4h")
