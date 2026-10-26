"""Onglet « Graphiques » : mêmes indicateurs que le moteur, fenêtre, alignement, rendu."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dashboard import charts
from mcs.config import JournalMode
from mcs.indicators import core
from mcs.indicators.signals import EmaCross
from mcs.market_data import INTERVAL_MS, Candle
from mcs.sampling import build_config, draw_plan
from mcs.storage.candles import upsert_candles
from mcs.storage.db import connect
from tests.backtest_data import dataset
from tests.test_dashboard import MARKETS, app_db, prepare, run_app  # noqa: F401

D = Decimal
BAR = INTERVAL_MS["4h"]
DAY = INTERVAL_MS["1d"]


@pytest.fixture(scope="module")
def data():  # type: ignore[no-untyped-def]
    return dataset(days=320, markets=MARKETS)


def store(conn: sqlite3.Connection, data) -> None:  # type: ignore[no-untyped-def]
    for market, md in data.markets.items():
        for interval, frame in (("1h", md.h1), ("4h", md.h4), ("1d", md.d1)):
            upsert_candles(
                conn,
                [
                    Candle(market, interval, int(ts), *(D(repr(float(v))) for v in row))
                    for ts, row in zip(
                        frame.index,
                        frame[["open", "high", "low", "close", "volume"]].to_numpy(),
                        strict=True,
                    )
                ],
            )


def test_indicators_match_engine_on_window(data) -> None:  # type: ignore[no-untyped-def]
    md = data.markets["AAA-EUR"]
    start = int(md.h4.index[-1]) - 60 * DAY
    result = charts.chart_data(md.h4, md.d1, "4h", start, charts.ChartIndicators())
    assert int(result.frame.index[0]) >= start and result.frame.index[-1] == md.h4.index[-1]
    assert len(result.frame) == 60 * 6 + 1
    full = core.ema(md.h4["close"], 55)
    pd.testing.assert_series_equal(result.overlays["EMA 55"], full[md.h4.index >= start])
    macd = core.macd(md.h4["close"], 12, 26, 9)[md.h4.index >= start]
    pd.testing.assert_frame_equal(result.panels["MACD 12 / 26 / 9"], macd)
    expected_buy = EmaCross(21, 55).buy_state(md.h4)[md.h4.index >= start]
    pd.testing.assert_series_equal(result.main_buy, expected_buy)


def test_trend_filter_aligned_without_lookahead(data) -> None:  # type: ignore[no-untyped-def]
    md = data.markets["AAA-EUR"]
    start = int(md.h4.index[-1]) - 30 * DAY
    settings = charts.ChartIndicators()
    result = charts.chart_data(md.h4, md.d1, "4h", start, settings)
    favorable = settings.trend.favorable(md.d1)
    for ts in result.frame.index[::5]:
        closed = favorable[favorable.index + DAY <= ts + BAR]  # journalières clôturées
        assert result.trend_ok.loc[ts] == (bool(closed.iloc[-1]) if len(closed) else False)
    assert np.isfinite(result.filter_line.to_numpy()).all()


def test_no_signal_band_outside_4h(data) -> None:  # type: ignore[no-untyped-def]
    md = data.markets["AAA-EUR"]
    result = charts.chart_data(md.d1, md.d1, "1d", int(md.d1.index[-30]), charts.ChartIndicators())
    assert result.main_buy is None and len(result.frame) == 30


def test_indicators_for_simulation() -> None:
    draw = next(d for d in draw_plan(3) if d["family"] == "macd" and d["conf_rsi"] == "yes")
    config = build_config(draw)
    settings = charts.indicators_for(config, "test")
    assert settings.macd == config.signals.main and settings.main == config.signals.main
    assert settings.ema == charts.TYPICAL_EMA  # famille non utilisée : typique
    assert settings.trend_enabled == config.trend.enabled


def test_tile(conn: sqlite3.Connection, data) -> None:  # type: ignore[no-untyped-def]
    store(conn, data)
    now = int(data.markets["AAA-EUR"].h4.index[-1]) + BAR
    tile = charts.tile(conn, "AAA-EUR", 14, now)
    assert tile.last == pytest.approx(float(data.markets["AAA-EUR"].h4["close"].iloc[-1]))
    assert tile.change_pct is not None and tile.trend_ok in (True, False)
    assert len(tile.closes) == 14 * 6
    empty = charts.tile(conn, "XXX-EUR", 14, now)
    assert empty.last is None and empty.trend_ok is None


async def test_app_charts_tab(app_db: Path, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn = connect(app_db, JournalMode.WAL)
    prepare(conn)
    store(conn, data)
    conn.close()
    at = run_app()
    assert "Graphiques" in [t.label for t in at.tabs]
    assert any(s.label == "Paire" for s in at.selectbox)
    at.selectbox(key="chart_sim").set_value(2).run()
    assert not at.exception
    at.selectbox(key="chart_interval").set_value("1 j").run()
    assert not at.exception


def test_fast_loading_matches_engine_loading(conn: sqlite3.Connection, data) -> None:  # type: ignore[no-untyped-def]
    from mcs.indicators.frame import candles_to_frame, fill_gaps
    from mcs.storage.candles import load_candles

    store(conn, data)
    fast = charts.load_history(conn, "AAA-EUR", "4h")
    engine = fill_gaps(candles_to_frame(load_candles(conn, "AAA-EUR", "4h")), "4h")
    pd.testing.assert_frame_equal(fast, engine)
    window = charts.load_history(conn, "AAA-EUR", "4h", start=int(fast.index[-10]))
    assert len(window) == 10
    assert charts.history_start("4h", 0, 60) is None and charts.history_start("1h", 10**12, 7)
