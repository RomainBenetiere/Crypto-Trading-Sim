"""Optimiseur : espace de recherche, contraintes, reproductibilité, écriture, reprise."""

import json
import sqlite3
from dataclasses import replace

import pytest

from mcs.broker.fees import FeeSchedule
from mcs.optimizer import (
    CONFIRMATION_SPACE,
    MAIN_SPACE,
    TREND_SPACE,
    OptimizationError,
    candidate_values,
    indicators_from,
    run,
    search_space,
    typical_values,
)
from mcs.sampling import (
    build_config,
    draw_plan,
    generate_plan,
    load_simulations,
    typical_indicators,
)
from tests.backtest_data import dataset

SEED = 7
FEES = FeeSchedule()


@pytest.fixture(scope="module")
def draws() -> list[dict]:
    return draw_plan(SEED)


def test_search_space_matches_draw(draws: list[dict]) -> None:
    for draw in draws:
        groups = {g for g, _ in search_space(draw)}
        expected = {"main", *(n for n in ("adx", "volume", "rsi") if draw[f"conf_{n}"] == "yes")}
        if draw["trend_enabled"] == "yes":
            expected.add("trend")
        assert groups == expected


def test_candidates_reproducible_in_range_and_typical_first(draws: list[dict]) -> None:
    for draw in draws[:30]:
        candidates = candidate_values(draw, 123, 40)
        assert candidates == candidate_values(draw, 123, 40)
        assert candidates[0] == typical_values(draw)
        assert len(candidates) == 40
        ranges = {
            **{("main", p.name): p for p in MAIN_SPACE[draw["family"]]},
            **{(n, p.name): p for n, ps in CONFIRMATION_SPACE.items() for p in ps},
        }
        if draw["trend_enabled"] == "yes":
            ranges |= {("trend", p.name): p for p in TREND_SPACE[draw["trend_type"]]}
        for values in candidates[1:]:
            for key, value in values.items():
                if key in ranges:
                    assert ranges[key].low <= value <= ranges[key].high, key


def test_typical_values_rebuild_typical_indicators(draws: list[dict]) -> None:
    for draw in draws[:40]:
        assert indicators_from(draw, typical_values(draw)) == typical_indicators(draw)


def test_constraints_slow_above_fast(draws: list[dict]) -> None:
    draw = next(d for d in draws if d["family"] == "ema_cross")
    values = typical_values(draw) | {("main", "fast"): 30, ("main", "slow"): 30}
    main = indicators_from(draw, values).main
    assert main.slow > main.fast  # type: ignore[union-attr]


def test_only_indicator_values_change(draws: list[dict]) -> None:
    for draw in draws[:30]:
        typical = build_config(draw)
        for values in candidate_values(draw, 1, 5):
            optimized = build_config(draw, indicators_from(draw, values))
            neutral = replace(
                optimized,
                signals=replace(
                    optimized.signals,
                    main=typical.signals.main,
                    confirmations=typical.signals.confirmations,
                ),
                trend=replace(optimized.trend, filter=typical.trend.filter),
            )
            assert neutral == typical
            assert [w for _, w in optimized.signals.confirmations] == [
                w for _, w in typical.signals.confirmations
            ]


def test_run_optimizes_saves_and_resumes(conn: sqlite3.Connection) -> None:
    data = dataset(days=300)
    generate_plan(conn, seed=SEED, pairs=2)
    results = run(conn, FEES, workers=1, trials=3, data=data)
    assert sorted(r.sim_id for r in results) == [2, 4]
    sims = {s.id: s for s in load_simulations(conn)}
    for result in results:
        assert sims[result.sim_id].config == result.config
        row = conn.execute(
            "SELECT train_score, validation_score, optimized_at FROM simulations WHERE id = ?",
            (result.sim_id,),
        ).fetchone()
        assert float(row["train_score"]) == result.train_score and row["optimized_at"]
        typical_twin = sims[result.sim_id - 1]
        assert typical_twin.config == build_config(typical_twin.draw)  # jumelle typique intacte
        params = conn.execute(
            "SELECT params_json FROM indicator_params WHERE sim_id = ? AND role = 'main'",
            (result.sim_id,),
        ).fetchone()
        assert json.loads(params[0])["type"] == type(result.config.signals.main).__name__
    state = conn.execute("SELECT optimization_status, optimized_at FROM engine_state").fetchone()
    assert state["optimization_status"] == "DONE" and state["optimized_at"]

    with pytest.raises(OptimizationError):  # tout est déjà fait
        run(conn, FEES, trials=3, data=data)
    conn.execute("UPDATE simulations SET optimized_at = NULL WHERE id = 4")
    conn.execute("UPDATE engine_state SET optimization_status = 'TODO'")
    resumed = run(conn, FEES, trials=3, data=data)
    assert [r.sim_id for r in resumed] == [4]  # reprise : seule la simulation restante
    recalibrated = run(conn, FEES, trials=3, data=data, recalibrate=True)
    assert sorted(r.sim_id for r in recalibrated) == [2, 4]  # « Optimiser » à nouveau


def test_run_is_reproducible(conn: sqlite3.Connection, tmp_path: object) -> None:
    from pathlib import Path

    from mcs.config import JournalMode
    from mcs.storage.db import connect

    data = dataset(days=300)
    other = connect(Path(str(tmp_path)) / "other.sqlite", JournalMode.WAL)
    for c in (conn, other):
        generate_plan(c, seed=SEED, pairs=1)
    first = run(conn, FEES, trials=3, data=data)
    second = run(other, FEES, trials=3, data=data)
    assert first[0].config == second[0].config
    assert first[0].validation_score == second[0].validation_score
    other.close()


def test_run_refused_when_running_or_started(conn: sqlite3.Connection) -> None:
    generate_plan(conn, seed=SEED, pairs=1)
    conn.execute("UPDATE engine_state SET optimization_status = 'RUNNING'")
    with pytest.raises(OptimizationError):
        run(conn, FEES, trials=2, data=dataset(days=250))
    conn.execute("UPDATE engine_state SET optimization_status = 'TODO', status = 'RUNNING'")
    with pytest.raises(OptimizationError):
        run(conn, FEES, trials=2, data=dataset(days=250))
