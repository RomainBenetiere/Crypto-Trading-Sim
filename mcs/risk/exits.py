"""Niveaux de sortie : stop initial, trailing, take profit.

Les indicateurs sont calculés en float sur les bougies 4 h clôturées ; les niveaux sont
convertis en `Decimal` avant d'être comparés aux prix.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd

from mcs.indicators import core
from mcs.risk.position import Position
from mcs.strategy.config import (
    ATR_PERIOD,
    CHANDELIER_PERIOD,
    StopType,
    StrategyConfig,
    TakeProfitType,
    TrailingActivation,
    TrailingType,
)


def to_decimal(value: float) -> Decimal | None:
    return None if value is None or math.isnan(value) else Decimal(repr(float(value)))


def last_atr(frame: pd.DataFrame) -> Decimal | None:
    return to_decimal(core.atr(frame, ATR_PERIOD).iloc[-1]) if len(frame) else None


def initial_stop(
    config: StrategyConfig, entry_price: Decimal, frame: pd.DataFrame
) -> Decimal | None:
    """Stop initial pour une entrée à `entry_price` (None si incalculable ou ≥ entrée)."""
    stop = config.stop
    level: Decimal | None
    if stop.type is StopType.FIXED_PCT:
        level = entry_price * (1 - stop.pct)
    elif stop.type is StopType.ATR:
        atr = last_atr(frame)
        level = None if atr is None else entry_price - stop.atr_multiple * atr
    else:
        lows = core.lowest(frame["low"], stop.lowest_n)
        level = to_decimal(lows.iloc[-1]) if len(lows) else None
    if level is None or level <= 0 or level >= entry_price:
        return None
    return level


def trailing_level(
    config: StrategyConfig, position: Position, frame: pd.DataFrame
) -> Decimal | None:
    """Niveau de trailing courant (avant la règle « ne fait que monter »)."""
    trailing = config.trailing
    if trailing.type is TrailingType.NONE:
        return None
    if trailing.type is TrailingType.PCT:
        return position.highest_high * (1 - trailing.pct)
    if trailing.type is TrailingType.ATR:
        atr = last_atr(frame)
        return None if atr is None else position.highest_high - trailing.atr_multiple * atr
    if trailing.type is TrailingType.CHANDELIER:
        atr = last_atr(frame)
        highest = to_decimal(core.highest(frame["high"], CHANDELIER_PERIOD).iloc[-1])
        if atr is None or highest is None:
            return None
        return highest - trailing.atr_multiple * atr
    st = config.supertrend_for_trailing
    line = core.supertrend(frame, st.atr_period, st.multiplier)
    if line["direction"].iloc[-1] != 1:
        return None  # pas de support Supertrend en tendance baissière
    return to_decimal(line["line"].iloc[-1])


def trailing_should_activate(config: StrategyConfig, position: Position) -> bool:
    activation = config.trailing.activation
    if activation is TrailingActivation.IMMEDIATE:
        return True
    if activation is TrailingActivation.GAIN_PCT:
        return position.highest_high >= position.avg_entry_price * (
            1 + config.trailing.activation_gain
        )
    return position.highest_high >= position.avg_entry_price + position.risk_per_unit


def take_profit_level(config: StrategyConfig, position: Position) -> Decimal | None:
    tp = config.take_profit
    if tp.type is TakeProfitType.NONE:
        return None
    if tp.type is TakeProfitType.FIXED_PCT:
        return position.avg_entry_price * (1 + tp.pct)
    if tp.type is TakeProfitType.PARTIAL and position.partial_done:
        return None
    return position.avg_entry_price + tp.r_multiple * position.risk_per_unit


def take_profit_quantity(config: StrategyConfig, position: Position) -> Decimal:
    if config.take_profit.type is TakeProfitType.PARTIAL:
        return position.quantity * config.take_profit.partial_fraction
    return position.quantity
