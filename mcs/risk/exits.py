"""Niveaux de sortie : stop initial, trailing, take profit.

Les valeurs d'indicateurs (ATR 4 h, plus hauts / plus bas, ligne Supertrend) sont lues dans le
contexte de marché, qui ne contient que des bougies clôturées ; les niveaux sont en `Decimal`.
"""

from __future__ import annotations

import math
from decimal import Decimal

from mcs.risk.position import Position
from mcs.strategy.config import (
    CHANDELIER_PERIOD,
    StopType,
    StrategyConfig,
    TakeProfitType,
    TrailingActivation,
    TrailingType,
)
from mcs.strategy.context import MarketContext


def to_decimal(value: float | None) -> Decimal | None:
    return None if value is None or math.isnan(value) else Decimal(repr(float(value)))


def initial_stop(
    config: StrategyConfig, entry_price: Decimal, ctx: MarketContext, market: str
) -> Decimal | None:
    """Stop initial pour une entrée à `entry_price` (None si incalculable ou ≥ entrée)."""
    stop = config.stop
    level: Decimal | None
    if stop.type is StopType.FIXED_PCT:
        level = entry_price * (1 - stop.pct)
    elif stop.type is StopType.ATR:
        atr = ctx.atr_4h(market)
        level = None if atr is None else entry_price - stop.atr_multiple * atr
    else:
        level = ctx.lowest_low_4h(market, stop.lowest_n)
    if level is None or level <= 0 or level >= entry_price:
        return None
    return level


def trailing_level(
    config: StrategyConfig, position: Position, ctx: MarketContext, market: str
) -> Decimal | None:
    """Niveau de trailing courant (avant la règle « ne fait que monter »)."""
    trailing = config.trailing
    if trailing.type is TrailingType.NONE:
        return None
    if trailing.type is TrailingType.PCT:
        return position.highest_high * (1 - trailing.pct)
    if trailing.type is TrailingType.ATR:
        atr = ctx.atr_4h(market)
        return None if atr is None else position.highest_high - trailing.atr_multiple * atr
    if trailing.type is TrailingType.CHANDELIER:
        atr = ctx.atr_4h(market)
        highest = ctx.highest_high_4h(market, CHANDELIER_PERIOD)
        if atr is None or highest is None:
            return None
        return highest - trailing.atr_multiple * atr
    direction, line = ctx.supertrend_4h(market, config.supertrend_for_trailing)
    return line if direction == 1 else None  # pas de support en tendance baissière


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
