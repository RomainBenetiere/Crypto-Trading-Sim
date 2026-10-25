"""Plan Monte-Carlo : reproductibilité, équilibre, hypercube latin, contraintes, jumelles,
sérialisation et écriture en base."""

import json
import sqlite3
from collections import Counter

import numpy as np
import pytest

from mcs.sampling import (
    CATEGORICALS,
    NUMERICS,
    TYPICAL_MAIN,
    PlanError,
    balanced,
    build_config,
    draw_plan,
    generate_plan,
    load_simulations,
    scale,
)
from mcs.strategy.serialize import config_from_json, config_to_json

SEED = 20261025


@pytest.fixture(scope="module")
def draws() -> list[dict]:
    return draw_plan(SEED)


def test_same_seed_same_plan(draws: list[dict]) -> None:
    assert draw_plan(SEED) == draws
    assert draw_plan(SEED + 1) != draws


def test_balanced_helper() -> None:
    rng = np.random.default_rng(0)
    counts = Counter(balanced(("a", "b", "c", "d"), 150, rng))
    assert sorted(counts.values()) == [37, 37, 38, 38]


def test_categoricals_balanced_within_their_subgroup(draws: list[dict]) -> None:
    for cat in CATEGORICALS:
        values = [d[cat.name] for d in draws if cat.when.test(d)]
        counts = Counter(values)
        assert set(counts) == set(cat.options), cat.name
        assert max(counts.values()) - min(counts.values()) <= 1, cat.name
        assert all(d[cat.name] is None for d in draws if not cat.when.test(d)), cat.name


def test_family_counts(draws: list[dict]) -> None:
    assert sorted(Counter(d["family"] for d in draws).values()) == [37, 37, 38, 38]


def test_numerics_in_range_and_latin_hypercube_strata(draws: list[dict]) -> None:
    for var in NUMERICS:
        eligible = [d for d in draws if var.when.test(d)]
        values = [d[var.name] for d in eligible]
        assert all(var.low <= v <= var.high for v in values), var.name
        assert all(d[var.name] is None for d in draws if not var.when.test(d)), var.name
        if not var.integer and len(values) > 1:
            # Hypercube latin : une valeur par strate de largeur (haut − bas) / n. L'arrondi
            # peut faire basculer un point situé au bord dans la strate voisine.
            n = len(values)
            width = (var.high - var.low) / n
            strata = Counter(min(int((v - var.low) / width), n - 1) for v in values)
            assert max(strata.values()) <= 2, var.name
            assert len(strata) >= 0.9 * n, var.name


def test_integer_scaling_covers_bounds() -> None:
    var = next(v for v in NUMERICS if v.name == "max_positions")
    assert scale(var, 0.0) == 2 and scale(var, 0.9999) == 10
    assert {scale(var, u) for u in np.linspace(0, 0.9999, 90)} == set(range(2, 11))


def test_weights_and_constraints(draws: list[dict]) -> None:
    for draw in draws:
        config = build_config(draw)
        signals = config.signals
        total = signals.main_weight + sum(w for _, w in signals.confirmations)
        assert abs(total - 1) < 1e-9
        if not signals.confirmations:
            assert signals.main_weight == 1.0
        else:
            assert 0.5 <= signals.main_weight <= 0.8
            assert len({w for _, w in signals.confirmations}) == 1  # parts égales
        assert config.trend.enabled == (draw["trend_enabled"] == "yes")


def test_typical_values_used_for_indicators(draws: list[dict]) -> None:
    config = build_config(draws[0])
    assert config.signals.main == TYPICAL_MAIN[draws[0]["family"]]


def test_config_json_round_trip(draws: list[dict]) -> None:
    for draw in draws[:40]:
        config = build_config(draw)
        assert config_from_json(config_to_json(config)) == config


def test_generate_writes_twins(conn: sqlite3.Connection) -> None:
    seed = generate_plan(conn, seed=SEED, pairs=10)
    assert seed == SEED
    sims = load_simulations(conn)
    assert [s.id for s in sims] == list(range(1, 21))
    for typical, optimized in zip(sims[::2], sims[1::2], strict=True):
        assert (typical.mode, optimized.mode) == ("typical", "optimized")
        assert typical.pair_id == optimized.pair_id
        assert typical.draw == optimized.draw
        assert typical.config == optimized.config  # identiques avant optimisation
        assert typical.seed != optimized.seed
    state = conn.execute("SELECT master_seed, optimization_status FROM engine_state").fetchone()
    assert tuple(state) == (SEED, "TODO")
    rows = conn.execute(
        "SELECT role, params_json, typical_params_json FROM indicator_params WHERE sim_id = 1"
    ).fetchall()
    roles = Counter(r["role"] for r in rows)
    expected_confirmations = sum(
        sims[0].draw[f"conf_{n}"] == "yes" for n in ("adx", "volume", "rsi")
    )
    assert roles["main"] == 1 and roles["confirmation"] == expected_confirmations
    assert all(json.loads(r["params_json"]) == json.loads(r["typical_params_json"]) for r in rows)


def test_generate_refuses_existing_plan_and_started_engine(conn: sqlite3.Connection) -> None:
    generate_plan(conn, seed=1, pairs=3)
    with pytest.raises(PlanError):
        generate_plan(conn, seed=2, pairs=3)
    generate_plan(conn, seed=2, pairs=3, force=True)
    assert conn.execute("SELECT master_seed FROM engine_state").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0] == 6
    conn.execute("UPDATE engine_state SET status = 'RUNNING'")
    with pytest.raises(PlanError):
        generate_plan(conn, seed=3, pairs=3, force=True)


def test_reproducible_in_database(conn: sqlite3.Connection, tmp_path: object) -> None:
    from pathlib import Path

    from mcs.config import JournalMode
    from mcs.storage.db import connect

    other = connect(Path(str(tmp_path)) / "other.sqlite", JournalMode.WAL)
    generate_plan(conn, seed=SEED, pairs=20)
    generate_plan(other, seed=SEED, pairs=20)
    query = "SELECT id, config_json, draw_json, seed FROM simulations ORDER BY id"
    assert [tuple(r) for r in conn.execute(query)] == [tuple(r) for r in other.execute(query)]
    other.close()
