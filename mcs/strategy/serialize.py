"""Conversion `StrategyConfig` ↔ JSON (colonne `simulations.config_json`).

Les `Decimal` sont écrits en texte, les énumérations par leur valeur, les indicateurs sous la
forme {"type": "EmaCross", ...paramètres}.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

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
from mcs.strategy.config import (
    EntryOrder,
    ExecutionConfig,
    ExitConfig,
    PortfolioConfig,
    SignalConfig,
    Sizing,
    StopConfig,
    StopRecalc,
    StopType,
    StrategyConfig,
    TakeProfitConfig,
    TakeProfitType,
    TrailingActivation,
    TrailingConfig,
    TrailingType,
    TrendAction,
    TrendFilterConfig,
)

INDICATORS: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        EmaCross,
        Macd,
        Supertrend,
        DonchianBreakout,
        AdxConfirmation,
        VolumeConfirmation,
        RsiConfirmation,
        TrendFilter,
    )
}


def indicator_to_dict(indicator: Any) -> dict[str, Any]:
    return {"type": type(indicator).__name__, **_plain(indicator)}


def indicator_from_dict(data: dict[str, Any]) -> Any:
    params = {k: v for k, v in data.items() if k != "type"}
    if data["type"] == "TrendFilter":
        params["kind"] = TrendFilterKind(params["kind"])
    return INDICATORS[data["type"]](**params)


def _plain(obj: Any) -> dict[str, Any]:
    return {f.name: _value(getattr(obj, f.name)) for f in fields(obj)}


def _value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if type(value).__name__ in INDICATORS:
        return indicator_to_dict(value)
    if is_dataclass(value):
        return _plain(value)
    return value


def config_to_dict(config: StrategyConfig) -> dict[str, Any]:
    signals = config.signals
    data = _plain(config)
    data["signals"] = {
        "main": indicator_to_dict(signals.main),
        "main_weight": signals.main_weight,
        "confirmations": [[indicator_to_dict(c), w] for c, w in signals.confirmations],
        "threshold": signals.threshold,
    }
    return data


def config_to_json(config: StrategyConfig) -> str:
    return json.dumps(config_to_dict(config), sort_keys=True)


def _decimals(data: dict[str, Any], *names: str) -> dict[str, Any]:
    return {k: Decimal(v) if k in names else v for k, v in data.items()}


def config_from_dict(data: dict[str, Any]) -> StrategyConfig:
    s = data["signals"]
    trend = data["trend"]
    stop = _decimals(data["stop"], "pct", "atr_multiple")
    trailing = _decimals(data["trailing"], "atr_multiple", "pct", "activation_gain")
    tp = _decimals(data["take_profit"], "r_multiple", "pct", "partial_fraction")
    portfolio = _decimals(
        data["portfolio"],
        "max_per_crypto",
        "max_exposure",
        "max_entry",
        "risk_per_trade",
        "circuit_breaker_drawdown",
    )
    return StrategyConfig(
        signals=SignalConfig(
            main=indicator_from_dict(s["main"]),
            main_weight=s["main_weight"],
            confirmations=tuple((indicator_from_dict(c), w) for c, w in s["confirmations"]),
            threshold=s["threshold"],
        ),
        trend=TrendFilterConfig(
            enabled=trend["enabled"],
            filter=indicator_from_dict(trend["filter"]) if trend["filter"] else None,
            action=TrendAction(trend["action"]),
        ),
        stop=StopConfig(**{**stop, "type": StopType(stop["type"])}),
        trailing=TrailingConfig(
            **{
                **trailing,
                "type": TrailingType(trailing["type"]),
                "activation": TrailingActivation(trailing["activation"]),
            }
        ),
        take_profit=TakeProfitConfig(**{**tp, "type": TakeProfitType(tp["type"])}),
        exits=ExitConfig(
            reverse_signal=data["exits"]["reverse_signal"],
            recalc=StopRecalc(data["exits"]["recalc"]),
            max_holding_bars=data["exits"]["max_holding_bars"],
        ),
        execution=ExecutionConfig(
            entry_order=EntryOrder(data["execution"]["entry_order"]),
            limit_expiry_hours=data["execution"]["limit_expiry_hours"],
        ),
        portfolio=PortfolioConfig(**{**portfolio, "sizing": Sizing(portfolio["sizing"])}),
    )


def config_from_json(raw: str) -> StrategyConfig:
    return config_from_dict(json.loads(raw))
