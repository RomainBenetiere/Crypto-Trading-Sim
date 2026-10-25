"""Optimisation 70/30 des simulations optimisées (SPEC §3, décisions 23 à 26).

    python -m mcs.optimizer run [--workers N] [--only SIM_ID ...] [--trials 40] [--force]

Pour chaque simulation `optimized` : 40 jeux de valeurs d'indicateurs (valeurs typiques + 39
tirés par hypercube latin dans les plages du §4) sont évalués par backtest sur les 70 % les
plus anciens de l'historique ; le meilleur selon rendement / drawdown est retenu puis évalué
sur les 30 % suivants (score de validation, enregistré sans écarter la simulation). Seules
les valeurs des indicateurs et du filtre de fond changent.
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import qmc

from mcs.backtest import Dataset, load_dataset, run_backtest
from mcs.broker.fees import FeeSchedule
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
from mcs.sampling import (
    IndicatorValues,
    StoredSimulation,
    build_config,
    confirmations_of,
    indicator_rows,
    load_simulations,
    typical_indicators,
)
from mcs.storage.db import connect, now_ms
from mcs.strategy.config import StrategyConfig
from mcs.strategy.serialize import config_to_json

log = logging.getLogger("mcs.optimizer")

TRIALS = 40


class OptimizationError(RuntimeError):
    """Optimisation refusée (message en français)."""


@dataclass(frozen=True)
class Param:
    name: str
    low: float
    high: float
    integer: bool = True
    decimals: int = 2

    def value(self, u: float) -> float | int:
        if self.integer:
            low, high = int(self.low), int(self.high)
            return min(low + math.floor(u * (high - low + 1)), high)
        return round(self.low + u * (self.high - self.low), self.decimals)


# Plages du §4.
MAIN_SPACE: dict[str, tuple[Param, ...]] = {
    "ema_cross": (Param("fast", 8, 30), Param("slow", 30, 100)),
    "macd": (Param("fast", 8, 15), Param("slow", 20, 35), Param("signal", 7, 12)),
    "supertrend": (Param("atr_period", 7, 14), Param("multiplier", 2, 4, integer=False)),
    "donchian": (Param("period", 20, 55),),
}
CONFIRMATION_SPACE: dict[str, tuple[Param, ...]] = {
    "adx": (Param("period", 10, 20), Param("threshold", 15, 30, integer=False, decimals=1)),
    "volume": (Param("window", 10, 30), Param("multiple", 1.2, 2.0, integer=False)),
    "rsi": (Param("period", 7, 21), Param("max_level", 60, 80, integer=False, decimals=1)),
}
TREND_SPACE: dict[str, tuple[Param, ...]] = {
    "price_above_sma": (Param("sma_window", 50, 200),),
    "ema_cross": (Param("ema_fast", 20, 50), Param("ema_slow", 50, 200)),
    "roc": (Param("roc_window", 30, 90),),
}
HYSTERESIS = Param("hysteresis", 0.0, 0.03, integer=False, decimals=4)


def search_space(draw: dict[str, Any]) -> list[tuple[str, Param]]:
    """Paramètres optimisables de la simulation : (groupe, paramètre)."""
    space = [("main", p) for p in MAIN_SPACE[draw["family"]]]
    for name in confirmations_of(draw):
        space += [(name, p) for p in CONFIRMATION_SPACE[name]]
    if draw["trend_enabled"] == "yes":
        space += [("trend", p) for p in (*TREND_SPACE[draw["trend_type"]], HYSTERESIS)]
    return space


def indicators_from(draw: dict[str, Any], values: dict[tuple[str, str], Any]) -> IndicatorValues:
    """Construit les indicateurs en respectant les contraintes (lente > rapide)."""

    def group(name: str) -> dict[str, Any]:
        return {param: v for (g, param), v in values.items() if g == name}

    main = group("main")
    family = draw["family"]
    if family == "ema_cross":
        main_signal: Any = EmaCross(main["fast"], max(main["slow"], main["fast"] + 1))
    elif family == "macd":
        main_signal = Macd(main["fast"], main["slow"], main["signal"])
    elif family == "supertrend":
        main_signal = Supertrend(main["atr_period"], float(main["multiplier"]))
    else:
        main_signal = DonchianBreakout(main["period"])
    builders: dict[str, Any] = {
        "adx": lambda p: AdxConfirmation(p["period"], float(p["threshold"])),
        "volume": lambda p: VolumeConfirmation(p["window"], float(p["multiple"])),
        "rsi": lambda p: RsiConfirmation(p["period"], float(p["max_level"])),
    }
    confirmations = {n: builders[n](group(n)) for n in confirmations_of(draw)}
    trend = None
    if draw["trend_enabled"] == "yes":
        p = group("trend")
        hysteresis = float(p["hysteresis"])
        kind = draw["trend_type"]
        if kind == "price_above_sma":
            trend = TrendFilter(
                TrendFilterKind.PRICE_ABOVE_SMA, hysteresis, sma_window=p["sma_window"]
            )
        elif kind == "ema_cross":
            slow = max(p["ema_slow"], p["ema_fast"] + 1)
            trend = TrendFilter(
                TrendFilterKind.EMA_CROSS, hysteresis, ema_fast=p["ema_fast"], ema_slow=slow
            )
        else:
            trend = TrendFilter(TrendFilterKind.ROC, hysteresis, roc_window=p["roc_window"])
    return IndicatorValues(main_signal, confirmations, trend)


def typical_values(draw: dict[str, Any]) -> dict[tuple[str, str], Any]:
    typical = typical_indicators(draw)
    values: dict[tuple[str, str], Any] = {}
    for group, param in search_space(draw):
        if group == "main":
            source: Any = typical.main
        elif group == "trend":
            source = typical.trend
        else:
            source = typical.confirmations[group]
        values[(group, param.name)] = getattr(source, param.name)
    return values


def candidate_values(
    draw: dict[str, Any], seed: int, trials: int
) -> list[dict[tuple[str, str], Any]]:
    """Valeurs typiques puis `trials − 1` jeux tirés par hypercube latin (reproductible)."""
    space = search_space(draw)
    sampler = qmc.LatinHypercube(d=len(space), rng=np.random.default_rng(seed))
    candidates = [typical_values(draw)]
    for row in sampler.random(trials - 1):
        candidates.append(
            {(g, p.name): p.value(float(u)) for (g, p), u in zip(space, row, strict=True)}
        )
    return candidates


@dataclass(frozen=True)
class OptimizationResult:
    sim_id: int
    config: StrategyConfig
    train_score: float
    validation_score: float
    trials: int
    seconds: float


def optimize(
    sim: StoredSimulation,
    data: Dataset,
    fees: FeeSchedule,
    trials: int = TRIALS,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> OptimizationResult:
    started = time.perf_counter()
    start, cut, end = data.split()
    cache = cache if cache is not None else {}
    best: tuple[float, StrategyConfig] | None = None
    for values in candidate_values(sim.draw, sim.seed, trials):
        config = build_config(sim.draw, indicators_from(sim.draw, values))
        score = run_backtest(config, data, start, cut, fees, cache).score
        if best is None or score > best[0]:
            best = (score, config)
    assert best is not None
    validation = run_backtest(best[1], data, cut, end, fees, cache).score
    return OptimizationResult(
        sim.id, best[1], best[0], validation, trials, time.perf_counter() - started
    )


# --- Exécution parallèle ------------------------------------------------------------------

_WORKER: dict[str, Any] = {}


def _init_worker(data: Dataset, fees: FeeSchedule) -> None:
    logging.getLogger().setLevel(logging.WARNING)
    _WORKER.update(data=data, fees=fees, cache={})


def _worker_optimize(sim: StoredSimulation, trials: int) -> OptimizationResult:
    return optimize(sim, _WORKER["data"], _WORKER["fees"], trials, _WORKER["cache"])


def save_result(
    conn: sqlite3.Connection, result: OptimizationResult, sim: StoredSimulation
) -> None:
    rows = indicator_rows(result.sim_id, result.config, typical_indicators(sim.draw))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE simulations SET config_json = ?, train_score = ?, validation_score = ?,
                   optimized_at = ? WHERE id = ?""",
            (
                config_to_json(result.config),
                repr(result.train_score),
                repr(result.validation_score),
                now_ms(),
                result.sim_id,
            ),
        )
        conn.execute("DELETE FROM indicator_params WHERE sim_id = ?", (result.sim_id,))
        conn.executemany(
            """INSERT INTO indicator_params (sim_id, role, indicator, params_json,
                   typical_params_json, weight, score_threshold) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def pending_simulations(
    conn: sqlite3.Connection, only: Sequence[int] | None = None
) -> list[StoredSimulation]:
    done = {r[0] for r in conn.execute("SELECT id FROM simulations WHERE optimized_at IS NOT NULL")}
    sims = [s for s in load_simulations(conn) if s.mode == "optimized"]
    if only:
        return [s for s in sims if s.id in set(only)]
    return [s for s in sims if s.id not in done]


def _set_status(conn: sqlite3.Connection, status: str, finished: bool = False) -> None:
    stamp = now_ms()
    if finished:
        conn.execute(
            "UPDATE engine_state SET optimization_status = ?, optimized_at = ?, updated_at = ?",
            (status, stamp, stamp),
        )
    else:
        conn.execute(
            "UPDATE engine_state SET optimization_status = ?, updated_at = ?", (status, stamp)
        )


def run(
    conn: sqlite3.Connection,
    fees: FeeSchedule,
    workers: int = 1,
    trials: int = TRIALS,
    only: Sequence[int] | None = None,
    force: bool = False,
    data: Dataset | None = None,
) -> list[OptimizationResult]:
    state = conn.execute("SELECT status, optimization_status FROM engine_state").fetchone()
    if state["status"] != "NOT_STARTED":
        raise OptimizationError("Optimisation impossible : le moteur doit être « Non démarré ».")
    if state["optimization_status"] == "RUNNING" and not force:
        raise OptimizationError(
            "Une optimisation est déjà en cours (--force si elle a été interrompue brutalement)."
        )
    sims = pending_simulations(conn, only)
    if not sims:
        raise OptimizationError("Aucune simulation optimisée à traiter (plan absent ou déjà fait).")
    data = data or load_dataset(conn)
    _set_status(conn, "RUNNING")
    by_id = {s.id: s for s in sims}
    results: list[OptimizationResult] = []
    try:
        if workers <= 1:
            cache: dict[tuple[Any, ...], Any] = {}
            for sim in sims:
                result = optimize(sim, data, fees, trials, cache)
                save_result(conn, result, sim)
                results.append(result)
                _log_progress(result, len(results), len(sims))
        else:
            with ProcessPoolExecutor(
                workers, initializer=_init_worker, initargs=(data, fees)
            ) as pool:
                futures = [pool.submit(_worker_optimize, sim, trials) for sim in sims]
                for future in as_completed(futures):
                    result = future.result()
                    save_result(conn, result, by_id[result.sim_id])
                    results.append(result)
                    _log_progress(result, len(results), len(sims))
    finally:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM simulations WHERE mode = 'optimized' AND optimized_at IS NULL"
        ).fetchone()[0]
        if remaining == 0:
            _set_status(conn, "DONE", finished=True)
        else:
            _set_status(conn, "TODO")
    return results


def _log_progress(result: OptimizationResult, done: int, total: int) -> None:
    log.info(
        "Sim %d optimisée (%d/%d) : apprentissage %.3f, validation %.3f, %.0f s.",
        result.sim_id,
        done,
        total,
        result.train_score,
        result.validation_score,
        result.seconds,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.optimizer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--only", type=int, nargs="*", help="identifiants de simulations")
    parser.add_argument("--force", action="store_true", help="ignore un statut RUNNING orphelin")
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_dir, "optimizer", settings.secrets)
    conn = connect(settings.db_path, settings.journal_mode)
    fees = FeeSchedule(settings.fee_maker, settings.fee_taker)
    try:
        results = run(conn, fees, args.workers, args.trials, args.only, args.force)
    except OptimizationError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        conn.close()
    print(f"{len(results)} simulations optimisées.")


if __name__ == "__main__":
    main()
