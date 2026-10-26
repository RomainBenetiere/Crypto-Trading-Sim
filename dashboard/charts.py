"""Calculs de l'onglet « Graphiques » : bougies, indicateurs et états de signal par paire.

Les indicateurs sont calculés sur tout l'historique en base avec le code du moteur
(`mcs.indicators`), puis seule la fenêtre demandée est affichée : les valeurs visibles sont
exactement celles que les simulations utilisent. Le filtre de fond journalier est aligné sur
l'intervalle affiché sans fuite (bougie journalière visible seulement une fois clôturée).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from mcs.indicators import core
from mcs.indicators.align import as_of, as_of_flag
from mcs.indicators.frame import fill_gaps
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
)
from mcs.market_data import INTERVAL_MS
from mcs.strategy.config import StrategyConfig
from mcs.strategy.serialize import config_from_json

DAY = INTERVAL_MS["1d"]
INTERVALS = {"1 h": "1h", "4 h": "4h", "1 j": "1d"}
DEFAULT_DAYS = {"1h": 14, "4h": 60, "1d": 365}
WINDOWS = [7, 14, 30, 60, 90, 180, 365, 730]


TYPICAL_EMA = EmaCross(21, 55)
TYPICAL_MACD = Macd(12, 26, 9)
TYPICAL_SUPERTREND = Supertrend(10, 3.0)
TYPICAL_DONCHIAN = DonchianBreakout(20)
TYPICAL_ADX = AdxConfirmation(14, 20.0)
TYPICAL_VOLUME = VolumeConfirmation(20, 1.5)
TYPICAL_RSI = RsiConfirmation(14, 70.0)
TYPICAL_TREND = TrendFilter(TrendFilterKind.PRICE_ABOVE_SMA, 0.02, sma_window=200)


@dataclass(frozen=True)
class ChartIndicators:
    """Réglages affichés : valeurs typiques, ou celles d'une simulation."""

    ema: EmaCross = TYPICAL_EMA
    macd: Macd = TYPICAL_MACD
    supertrend: Supertrend = TYPICAL_SUPERTREND
    donchian: DonchianBreakout = TYPICAL_DONCHIAN
    adx: AdxConfirmation = TYPICAL_ADX
    volume: VolumeConfirmation = TYPICAL_VOLUME
    rsi: RsiConfirmation = TYPICAL_RSI
    trend: TrendFilter = TYPICAL_TREND
    main: Any = TYPICAL_EMA
    trend_enabled: bool = True
    label: str = "réglages typiques (signal EMA 21 / 55, filtre prix > SMA 200)"


def indicators_for(config: StrategyConfig, label: str) -> ChartIndicators:
    """Indicateurs d'une simulation ; les familles qu'elle n'utilise pas gardent le typique."""
    values: dict[str, Any] = {}
    main = config.signals.main
    main_kinds: dict[str, type] = {
        "ema": EmaCross,
        "macd": Macd,
        "supertrend": Supertrend,
        "donchian": DonchianBreakout,
    }
    confirmation_kinds: dict[str, type] = {
        "adx": AdxConfirmation,
        "volume": VolumeConfirmation,
        "rsi": RsiConfirmation,
    }
    values.update({n: main for n, k in main_kinds.items() if isinstance(main, k)})
    for confirmation, _ in config.signals.confirmations:
        values.update(
            {n: confirmation for n, k in confirmation_kinds.items() if isinstance(confirmation, k)}
        )
    if config.trend.enabled and config.trend.filter is not None:
        values["trend"] = config.trend.filter
    return ChartIndicators(**values, main=main, trend_enabled=config.trend.enabled, label=label)


def simulation_config(conn: sqlite3.Connection, sim_id: int) -> StrategyConfig | None:
    row = conn.execute("SELECT config_json FROM simulations WHERE id = ?", (sim_id,)).fetchone()
    return None if row is None else config_from_json(row[0])


def load_history(
    conn: sqlite3.Connection, market: str, interval: str, start: int | None = None
) -> pd.DataFrame:
    """Historique clôturé de la paire depuis `start` (tout par défaut), trous comblés comme
    dans le moteur. Lecture directe en pandas : même conversion texte → float que le moteur."""
    frame = pd.read_sql_query(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE market = ? AND interval = ? AND ts >= ? ORDER BY ts",
        conn,
        params=(market, interval, start or 0),
        index_col="ts",
    )
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "filled"])
    frame = frame.astype("float64")
    frame.index = frame.index.astype("int64")
    return fill_gaps(frame, interval)


# Le moteur ne calcule pas d'indicateurs en 1 h : avance de calcul limitée pour cet intervalle.
H1_WARMUP_BARS = 1000


def history_start(interval: str, last_ts: int, days: int) -> int | None:
    """Début de l'historique à charger : tout en 4 h et 1 j (valeurs exactes du moteur)."""
    if interval != "1h":
        return None
    return last_ts - days * DAY - H1_WARMUP_BARS * INTERVAL_MS["1h"]


@dataclass
class ChartData:
    frame: pd.DataFrame  # fenêtre affichée (OHLCV)
    overlays: dict[str, pd.Series]
    panels: dict[str, pd.DataFrame]
    main_buy: pd.Series | None  # état « signal principal en achat » (4 h)
    trend_ok: pd.Series | None  # filtre de fond favorable, aligné sans fuite
    filter_line: pd.Series | None  # SMA / EMA du filtre de fond, en escalier


def chart_data(
    h: pd.DataFrame, d1: pd.DataFrame, interval: str, start: int, settings: ChartIndicators
) -> ChartData:
    """Indicateurs sur tout l'historique `h`, restitués sur la fenêtre [start, fin]."""
    view = h.index >= start
    frame = h[view]

    def cut(series: pd.Series) -> pd.Series:
        return series[view]

    ema, st, dc = settings.ema, settings.supertrend, settings.donchian
    trend_values = core.supertrend(h, st.atr_period, st.multiplier)
    channel = core.donchian(h, dc.period)
    overlays = {
        f"EMA {ema.fast}": cut(core.ema(h["close"], ema.fast)),
        f"EMA {ema.slow}": cut(core.ema(h["close"], ema.slow)),
        f"Supertrend {st.atr_period} / {st.multiplier:g}": cut(trend_values["line"]),
        f"Donchian {dc.period} haut": cut(channel["upper"]),
        f"Donchian {dc.period} bas": cut(channel["lower"]),
    }
    supertrend_up = cut(trend_values["direction"] == 1)
    macd = core.macd(h["close"], settings.macd.fast, settings.macd.slow, settings.macd.signal)
    adx = core.adx(h, settings.adx.period)
    panels = {
        "Volume": pd.DataFrame(
            {
                "volume": frame["volume"],
                "moyenne": cut(core.sma(h["volume"], settings.volume.window)),
            }
        ),
        f"RSI {settings.rsi.period}": pd.DataFrame(
            {"rsi": cut(core.rsi(h["close"], settings.rsi.period))}
        ),
        f"MACD {settings.macd.fast} / {settings.macd.slow} / {settings.macd.signal}": macd[view],
        f"ADX {settings.adx.period}": adx[view],
    }
    panels["_supertrend_up"] = pd.DataFrame({"up": supertrend_up})
    main_buy = cut(settings.main.buy_state(h)) if interval == "4h" else None
    trend_ok = filter_line = None
    if settings.trend_enabled and len(d1):
        favorable = settings.trend.favorable(d1)
        trend_ok = as_of_flag(favorable, "1d", frame.index, interval)
        line = _filter_line(d1, settings.trend)
        if line is not None:
            values = _as_of_values(line, frame.index, interval)
            filter_line = pd.Series(values, index=frame.index)
    return ChartData(frame, overlays, panels, main_buy, trend_ok, filter_line)


def _filter_line(d1: pd.DataFrame, flt: TrendFilter) -> pd.Series | None:
    if flt.kind is TrendFilterKind.PRICE_ABOVE_SMA:
        return core.sma(d1["close"], flt.sma_window)
    if flt.kind is TrendFilterKind.EMA_CROSS:
        return core.ema(d1["close"], flt.ema_slow)
    return None  # ROC : pas de ligne de prix


def _as_of_values(daily: pd.Series, index: pd.Index, interval: str) -> np.ndarray:
    values: np.ndarray = as_of(daily, "1d", index, interval).to_numpy()
    return values


# --- Mosaïque -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tile:
    market: str
    closes: pd.Series
    last: float | None
    change_pct: float | None
    trend_ok: bool | None


def tile(conn: sqlite3.Connection, market: str, days: int, now: int) -> Tile:
    recent_frame = load_history(conn, market, "4h", start=now - days * DAY)
    recent = recent_frame["close"] if len(recent_frame) else pd.Series(dtype=float)
    d1 = load_history(conn, market, "1d")
    last = float(recent.iloc[-1]) if len(recent) else None
    change = (last / float(recent.iloc[0]) - 1) * 100 if last and len(recent) > 1 else None
    trend = ChartIndicators().trend
    ok = bool(trend.favorable(d1).iloc[-1]) if len(d1) >= trend.warmup else None
    return Tile(market, recent, last, change, ok)


# --- Ordres et positions d'une simulation ------------------------------------------------


def simulation_orders(
    conn: sqlite3.Connection, sim_id: int, market: str, start: int
) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT side, reason, price, quantity, filled_at FROM orders
           WHERE sim_id = ? AND market = ? AND status = 'filled' AND filled_at >= ?
           ORDER BY filled_at""",
        conn,
        params=(sim_id, market, start),
    )


def simulation_position(
    conn: sqlite3.Connection, sim_id: int, market: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT stop, trailing, take_profit, avg_entry_price, state_json FROM positions "
        "WHERE sim_id = ? AND market = ?",
        (sim_id, market),
    ).fetchone()
    if row is None:
        return None
    state = json.loads(row["state_json"])
    return {
        "stop": row["stop"],
        "trailing": row["trailing"] if state.get("trailing_active") else None,
        "take_profit": row["take_profit"],
        "entry": row["avg_entry_price"],
    }
