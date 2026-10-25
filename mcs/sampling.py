"""Génération du plan Monte-Carlo : 150 configurations de base, chacune en deux jumelles.

    python -m mcs.sampling generate [--seed N] [--force]

- Variables par catégories : options réparties à parts quasi égales (écart ≤ 1), mélangées
  indépendamment ; une variable conditionnelle (ex. type de filtre) est équilibrée dans le
  sous-groupe où elle s'applique.
- Variables numériques : hypercube latin (`scipy.stats.qmc.LatinHypercube`) par groupe de
  variables partageant la même condition, sur le sous-groupe concerné.
- Valeurs des indicateurs et du filtre de fond : non tirées (décision 20). Jumelle `typical`
  = valeurs typiques ; jumelle `optimized` = résultat de l'optimiseur (étape 6), valeurs
  typiques en attendant.
- Poids : signal principal tiré entre 0,5 et 0,8 (1 sans confirmation), le reste réparti à
  parts égales entre les confirmations présentes (décision 21).
- Graine maîtresse enregistrée : même graine = même plan.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
from scipy.stats import qmc

from mcs.config import load_settings
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
from mcs.logging_setup import configure_logging
from mcs.storage.db import connect, now_ms
from mcs.strategy.config import (
    ConfirmationConfig,
    EntryOrder,
    ExecutionConfig,
    ExitConfig,
    MainSignalConfig,
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
from mcs.strategy.serialize import config_from_json, config_to_json, indicator_to_dict

log = logging.getLogger("mcs.sampling")

PAIRS = 150
Draw = dict[str, Any]

# --- Valeurs typiques des indicateurs (SPEC §4) ---------------------------------------------

TYPICAL_MAIN: dict[str, MainSignalConfig] = {
    "ema_cross": EmaCross(21, 55),
    "macd": Macd(12, 26, 9),
    "supertrend": Supertrend(10, 3.0),
    "donchian": DonchianBreakout(20),
}
TYPICAL_CONFIRMATIONS: dict[str, ConfirmationConfig] = {
    "adx": AdxConfirmation(14, 20.0),
    "volume": VolumeConfirmation(20, 1.5),
    "rsi": RsiConfirmation(14, 70.0),
}
TYPICAL_TREND: dict[str, TrendFilter] = {
    "price_above_sma": TrendFilter(TrendFilterKind.PRICE_ABOVE_SMA, 0.02, sma_window=200),
    "ema_cross": TrendFilter(TrendFilterKind.EMA_CROSS, 0.02, ema_fast=50, ema_slow=200),
    "roc": TrendFilter(TrendFilterKind.ROC, 0.02, roc_window=60),
}


# --- Catalogue des variables tirées ---------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    key: str
    test: Callable[[Draw], bool]


ALWAYS = Condition("always", lambda d: True)
TREND_ON = Condition("trend_on", lambda d: d["trend_enabled"] == "yes")
TRAILING_ON = Condition("trailing_on", lambda d: d["trailing_type"] != "none")
HAS_CONFIRMATION = Condition(
    "has_confirmation", lambda d: "yes" in (d["conf_adx"], d["conf_volume"], d["conf_rsi"])
)
STOP_PCT = Condition("stop_pct", lambda d: d["stop_type"] == "fixed_pct")
STOP_ATR = Condition("stop_atr", lambda d: d["stop_type"] == "atr")
STOP_LOWEST = Condition("stop_lowest", lambda d: d["stop_type"] == "lowest_low")
TRAIL_ATR = Condition("trail_atr", lambda d: d["trailing_type"] in ("atr", "chandelier"))
TRAIL_PCT = Condition("trail_pct", lambda d: d["trailing_type"] == "pct")
ACTIVATION_GAIN = Condition(
    "activation_gain",
    lambda d: d["trailing_type"] != "none" and d["trailing_activation"] == "gain_pct",
)
TP_R = Condition("tp_r", lambda d: d["take_profit_type"] in ("r_multiple", "partial"))
TP_PCT = Condition("tp_pct", lambda d: d["take_profit_type"] == "fixed_pct")
HOLD_LIMITED = Condition("hold_limited", lambda d: d["max_holding"] == "limited")
SIZING_RISK = Condition("sizing_risk", lambda d: d["sizing"] == "risk")
ENTRY_LIMIT = Condition("entry_limit", lambda d: d["entry_order"] == "limit")


@dataclass(frozen=True)
class Categorical:
    name: str
    options: tuple[str, ...]
    when: Condition = ALWAYS


@dataclass(frozen=True)
class Numeric:
    name: str
    low: float
    high: float
    integer: bool = False
    decimals: int = 4
    when: Condition = ALWAYS


YES_NO = ("yes", "no")
CATEGORICALS: tuple[Categorical, ...] = (
    Categorical("family", tuple(TYPICAL_MAIN)),
    Categorical("conf_adx", YES_NO),
    Categorical("conf_volume", YES_NO),
    Categorical("conf_rsi", YES_NO),
    Categorical("trend_enabled", YES_NO),
    Categorical("trend_type", tuple(TYPICAL_TREND), TREND_ON),
    Categorical("trend_action", tuple(a.value for a in TrendAction), TREND_ON),
    Categorical("stop_type", tuple(s.value for s in StopType)),
    Categorical("trailing_type", tuple(t.value for t in TrailingType)),
    Categorical("trailing_activation", tuple(a.value for a in TrailingActivation), TRAILING_ON),
    Categorical("take_profit_type", tuple(t.value for t in TakeProfitType)),
    Categorical("reverse_signal", YES_NO),
    Categorical("stop_recalc", tuple(r.value for r in StopRecalc)),
    Categorical("max_holding", ("none", "limited")),
    Categorical("entry_order", tuple(e.value for e in EntryOrder)),
    Categorical("sizing", tuple(s.value for s in Sizing)),
    Categorical("max_entries", ("1", "2", "3")),
)
NUMERICS: tuple[Numeric, ...] = (
    Numeric("max_per_crypto", 0.20, 1.00, decimals=4),
    Numeric("max_exposure", 0.50, 1.00, decimals=4),
    Numeric("max_entry", 0.10, 0.50, decimals=4),
    Numeric("max_positions", 2, 10, integer=True),
    Numeric("pause_after_stop_bars", 0, 6, integer=True),
    Numeric("circuit_breaker_drawdown", 0.10, 0.30, decimals=4),
    Numeric("score_threshold", 0.50, 0.85, decimals=4),
    Numeric("main_weight", 0.50, 0.80, decimals=4, when=HAS_CONFIRMATION),
    Numeric("stop_pct", 0.02, 0.10, when=STOP_PCT),
    Numeric("stop_atr_multiple", 1.5, 4.0, decimals=3, when=STOP_ATR),
    Numeric("stop_lowest_n", 5, 20, integer=True, when=STOP_LOWEST),
    Numeric("trailing_atr_multiple", 2.0, 4.0, decimals=3, when=TRAIL_ATR),
    Numeric("trailing_pct", 0.03, 0.15, when=TRAIL_PCT),
    Numeric("trailing_activation_gain", 0.01, 0.05, when=ACTIVATION_GAIN),
    Numeric("tp_r_multiple", 1.5, 4.0, decimals=3, when=TP_R),
    Numeric("tp_pct", 0.05, 0.30, when=TP_PCT),
    Numeric("max_holding_bars", 30, 180, integer=True, when=HOLD_LIMITED),
    Numeric("risk_per_trade", 0.005, 0.02, decimals=5, when=SIZING_RISK),
    Numeric("limit_expiry_hours", 1, 4, integer=True, when=ENTRY_LIMIT),
)


# --- Tirage ---------------------------------------------------------------------------------


def balanced(options: Sequence[str], size: int, rng: np.random.Generator) -> list[str]:
    """`size` valeurs où chaque option apparaît ⌊size / n⌋ ou ⌈size / n⌉ fois, mélangées."""
    full, rest = divmod(size, len(options))
    values = list(options) * full
    values += [options[i] for i in rng.choice(len(options), size=rest, replace=False)]
    rng.shuffle(values)
    return values


def scale(var: Numeric, u: float) -> float | int:
    if var.integer:
        low, high = int(var.low), int(var.high)
        return min(low + math.floor(u * (high - low + 1)), high)
    return round(var.low + u * (var.high - var.low), var.decimals)


def draw_plan(seed: int, pairs: int = PAIRS) -> list[Draw]:
    """Tire les `pairs` configurations de base (dictionnaires variable → valeur)."""
    rng = np.random.default_rng(seed)
    draws: list[Draw] = [{} for _ in range(pairs)]
    for cat in CATEGORICALS:
        eligible = [i for i, d in enumerate(draws) if cat.when.test(d)]
        for i, value in zip(eligible, balanced(cat.options, len(eligible), rng), strict=True):
            draws[i][cat.name] = value
        for i in set(range(pairs)) - set(eligible):
            draws[i][cat.name] = None
    groups: dict[str, list[Numeric]] = {}
    for var in NUMERICS:
        groups.setdefault(var.when.key, []).append(var)
    for group in groups.values():
        condition = group[0].when
        eligible = [i for i, d in enumerate(draws) if condition.test(d)]
        samples = np.empty((0, len(group)))
        if eligible:
            sampler = qmc.LatinHypercube(
                d=len(group), rng=np.random.default_rng(rng.integers(2**63))
            )
            samples = sampler.random(len(eligible))
        for var_index, var in enumerate(group):
            for row, i in enumerate(eligible):
                draws[i][var.name] = scale(var, float(samples[row, var_index]))
            for i in set(range(pairs)) - set(eligible):
                draws[i][var.name] = None
    return draws


# --- Construction des configurations ------------------------------------------------------


@dataclass(frozen=True)
class IndicatorValues:
    """Valeurs des indicateurs d'une jumelle (seule différence entre les deux)."""

    main: MainSignalConfig
    confirmations: dict[str, ConfirmationConfig] = field(default_factory=dict)
    trend: TrendFilter | None = None


def confirmations_of(draw: Draw) -> list[str]:
    return [name for name in TYPICAL_CONFIRMATIONS if draw[f"conf_{name}"] == "yes"]


def typical_indicators(draw: Draw) -> IndicatorValues:
    return IndicatorValues(
        main=TYPICAL_MAIN[draw["family"]],
        confirmations={name: TYPICAL_CONFIRMATIONS[name] for name in confirmations_of(draw)},
        trend=TYPICAL_TREND[draw["trend_type"]] if draw["trend_enabled"] == "yes" else None,
    )


def _dec(value: float | None, default: str) -> Decimal:
    return Decimal(repr(value)) if value is not None else Decimal(default)


def build_config(draw: Draw, indicators: IndicatorValues | None = None) -> StrategyConfig:
    values = indicators or typical_indicators(draw)
    names = confirmations_of(draw)
    main_weight = draw["main_weight"] if names else 1.0
    share = (1.0 - main_weight) / len(names) if names else 0.0
    trend_on = draw["trend_enabled"] == "yes"
    trailing_type = TrailingType(draw["trailing_type"])
    return StrategyConfig(
        signals=SignalConfig(
            main=values.main,
            main_weight=main_weight,
            confirmations=tuple((values.confirmations[n], share) for n in names),
            threshold=draw["score_threshold"],
        ),
        trend=TrendFilterConfig(
            enabled=trend_on,
            filter=values.trend if trend_on else None,
            action=TrendAction(draw["trend_action"]) if trend_on else TrendAction.BLOCK,
        ),
        stop=StopConfig(
            type=StopType(draw["stop_type"]),
            pct=_dec(draw["stop_pct"], "0.05"),
            atr_multiple=_dec(draw["stop_atr_multiple"], "2"),
            lowest_n=draw["stop_lowest_n"] or 10,
        ),
        trailing=TrailingConfig(
            type=trailing_type,
            atr_multiple=_dec(draw["trailing_atr_multiple"], "3"),
            pct=_dec(draw["trailing_pct"], "0.08"),
            activation=TrailingActivation(draw["trailing_activation"] or "immediate"),
            activation_gain=_dec(draw["trailing_activation_gain"], "0.02"),
        ),
        take_profit=TakeProfitConfig(
            type=TakeProfitType(draw["take_profit_type"]),
            r_multiple=_dec(draw["tp_r_multiple"], "2"),
            pct=_dec(draw["tp_pct"], "0.15"),
        ),
        exits=ExitConfig(
            reverse_signal=draw["reverse_signal"] == "yes",
            recalc=StopRecalc(draw["stop_recalc"]),
            max_holding_bars=draw["max_holding_bars"],
        ),
        execution=ExecutionConfig(
            entry_order=EntryOrder(draw["entry_order"]),
            limit_expiry_hours=draw["limit_expiry_hours"] or 1,
        ),
        portfolio=PortfolioConfig(
            max_per_crypto=_dec(draw["max_per_crypto"], "0.4"),
            max_exposure=_dec(draw["max_exposure"], "1"),
            max_entry=_dec(draw["max_entry"], "0.25"),
            sizing=Sizing(draw["sizing"]),
            risk_per_trade=_dec(draw["risk_per_trade"], "0.01"),
            max_entries=int(draw["max_entries"]),
            max_positions=draw["max_positions"],
            pause_after_stop_bars=draw["pause_after_stop_bars"],
            circuit_breaker_drawdown=_dec(draw["circuit_breaker_drawdown"], "0.2"),
        ),
    )


# --- Stockage -------------------------------------------------------------------------------


class PlanError(RuntimeError):
    """Génération refusée (message en français)."""


@dataclass(frozen=True)
class StoredSimulation:
    id: int
    pair_id: int
    mode: str
    family: str
    seed: int
    config: StrategyConfig
    draw: Draw


def sim_ids(pair_id: int) -> tuple[int, int]:
    """(typique, optimisée) : 2k − 1 et 2k."""
    return 2 * pair_id - 1, 2 * pair_id


def generate_plan(
    conn: sqlite3.Connection, seed: int | None = None, force: bool = False, pairs: int = PAIRS
) -> int:
    """Tire et enregistre le plan. Retourne la graine maîtresse utilisée."""
    state = conn.execute("SELECT status, optimization_status FROM engine_state").fetchone()
    if state["status"] != "NOT_STARTED" or state["optimization_status"] == "RUNNING":
        raise PlanError("Génération impossible : le moteur doit être « Non démarré ».")
    existing = conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
    if existing and not force:
        raise PlanError(
            f"Un plan de {existing} simulations existe déjà (relancer avec --force pour le "
            "remplacer)."
        )
    master = seed if seed is not None else secrets.randbits(63)
    draws = draw_plan(master, pairs)
    sim_seeds = np.random.SeedSequence(master).generate_state(2 * pairs, dtype=np.uint32)
    stamp = now_ms()
    rows, params = [], []
    for k, draw in enumerate(draws, start=1):
        config = build_config(draw)
        typical = typical_indicators(draw)
        for sim_id, mode in zip(sim_ids(k), ("typical", "optimized"), strict=True):
            rows.append(
                (
                    sim_id,
                    k,
                    mode,
                    draw["family"],
                    config_to_json(config),
                    json.dumps(draw, sort_keys=True),
                    int(sim_seeds[sim_id - 1]),
                    stamp,
                )
            )
            params.extend(_indicator_rows(sim_id, config, typical))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM simulations")
        conn.executemany(
            """INSERT INTO simulations (id, pair_id, mode, family, config_json, draw_json, seed,
                   created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.executemany(
            """INSERT INTO indicator_params (sim_id, role, indicator, params_json,
                   typical_params_json, weight, score_threshold) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
        conn.execute(
            """UPDATE engine_state SET master_seed = ?, optimization_status = 'TODO',
                   optimized_at = NULL, updated_at = ?""",
            (master, stamp),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.info(
        "Plan généré : %d configurations, %d simulations, graine %d.", pairs, len(rows), master
    )
    return master


def _indicator_rows(
    sim_id: int, config: StrategyConfig, typical: IndicatorValues
) -> list[tuple[Any, ...]]:
    signals = config.signals
    rows: list[tuple[Any, ...]] = [
        (
            sim_id,
            "main",
            type(signals.main).__name__,
            json.dumps(indicator_to_dict(signals.main)),
            json.dumps(indicator_to_dict(typical.main)),
            str(signals.main_weight),
            str(signals.threshold),
        ),
    ]
    typical_confs = list(typical.confirmations.values())
    for (conf, weight), typ in zip(signals.confirmations, typical_confs, strict=True):
        rows.append(
            (
                sim_id,
                "confirmation",
                type(conf).__name__,
                json.dumps(indicator_to_dict(conf)),
                json.dumps(indicator_to_dict(typ)),
                str(weight),
                str(signals.threshold),
            )
        )
    if config.trend.enabled and config.trend.filter is not None and typical.trend is not None:
        rows.append(
            (
                sim_id,
                "trend_filter",
                config.trend.filter.kind.value,
                json.dumps(indicator_to_dict(config.trend.filter)),
                json.dumps(indicator_to_dict(typical.trend)),
                None,
                None,
            )
        )
    return rows


def load_simulations(conn: sqlite3.Connection) -> list[StoredSimulation]:
    return [
        StoredSimulation(
            id=row["id"],
            pair_id=row["pair_id"],
            mode=row["mode"],
            family=row["family"],
            seed=row["seed"],
            config=config_from_json(row["config_json"]),
            draw=json.loads(row["draw_json"]),
        )
        for row in conn.execute("SELECT * FROM simulations ORDER BY id")
    ]


# --- Commande -------------------------------------------------------------------------------


def summary(draws: Sequence[Draw]) -> str:
    families = Counter(d["family"] for d in draws)
    return ", ".join(f"{name} {count}" for name, count in sorted(families.items()))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.sampling",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("generate",))
    parser.add_argument("--seed", type=int, help="graine maîtresse (aléatoire par défaut)")
    parser.add_argument("--force", action="store_true", help="remplace un plan existant")
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_dir, "sampling", settings.secrets)
    conn = connect(settings.db_path, settings.journal_mode)
    try:
        master = generate_plan(conn, args.seed, args.force)
    except PlanError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        conn.close()
    print(f"Plan généré (graine {master}) : {summary(draw_plan(master))}.")


if __name__ == "__main__":
    main()
