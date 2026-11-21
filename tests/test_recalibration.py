"""Recalibrages (décision 48) : génération, optimisation, démarrage, archivage, échec, remise
à zéro, critère de sélection apprentissage / validation."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import mcs.optimizer as optimizer
import mcs.recalibration as recalibration
from mcs.backtest import Dataset
from mcs.live.state import Command
from mcs.metrics import load_records
from mcs.optimizer import MEAN, TRAIN, optimize, run_recalibration
from mcs.sampling import StoredSimulation, build_config, draw_plan
from mcs.storage.db import JournalMode, connect
from mcs.strategy.serialize import config_from_json
from tests.test_engine import FEES, command, data, run_bars, setup  # noqa: F401 (fixtures)
from tests.test_ideal import FakeProcess


async def optimize_pending(conn: sqlite3.Connection, dataset: Dataset) -> list[int]:
    """L'optimiseur tourne dans son propre processus en production : ici, sa propre connexion
    dans un thread."""
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def work() -> list[int]:
        own = connect(Path(path), JournalMode.WAL)
        try:
            return sorted(r.sim_id for r in run_recalibration(own, FEES, trials=2, data=dataset))
        finally:
            own.close()

    return await asyncio.to_thread(work)


def generation_ids(conn: sqlite3.Connection, generation: int) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM simulations WHERE kind = 'recalibrated' AND generation = ? ORDER BY id",
            (generation,),
        )
    ]


async def test_recalibration_lifecycle(setup, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    launched: list[FakeProcess] = []

    def launcher(workers: int) -> FakeProcess:
        launched.append(FakeProcess())
        return launched[-1]

    engine.recalibration_launcher = launcher
    await command(engine, Command.START)
    await run_bars(engine, clock, 2)

    result = await command(engine, Command.OPTIMIZE)
    assert "génération R2 créée (4 simulations)" in result and len(launched) == 1
    rows = conn.execute(
        "SELECT * FROM simulations WHERE kind = 'recalibrated' ORDER BY id"
    ).fetchall()
    assert [r["source_sim_id"] for r in rows] == [2, 4, 6, 8]
    assert all(r["mode"] == "optimized" and r["started_at"] is None for r in rows)
    assert [recalibration.label(r["generation"], r["source_sim_id"]) for r in rows] == [
        "R2-002",
        "R2-004",
        "R2-006",
        "R2-008",
    ]
    for row in rows:  # même configuration de base que le plan (valeurs typiques en attendant)
        plan = conn.execute(
            "SELECT draw_json, config_json FROM simulations WHERE id = ?",
            (row["source_sim_id"] - 1,),
        ).fetchone()
        assert row["draw_json"] == plan["draw_json"]
        assert config_from_json(row["config_json"]) == config_from_json(plan["config_json"])
    first = [r["id"] for r in rows]
    assert not set(first) & set(engine.sims)
    assert "déjà en préparation" in await command(engine, Command.OPTIMIZE)

    assert await optimize_pending(conn, data) == first
    await engine.process_commands()
    state = recalibration.read_state(conn)
    assert state.pending is None and state.active == (2,)
    assert "R2 démarrée (4 simulations)" in (state.message or "")
    assert set(first) <= set(engine.sims) and engine.ideal_bh[first[0]] is not None
    scores = conn.execute(
        "SELECT train_score, validation_score FROM simulations WHERE id = ?", (first[0],)
    ).fetchone()
    assert scores["train_score"] is not None and scores["validation_score"] is not None
    await run_bars(engine, clock, 2)
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE sim_id = ?", (first[0],)).fetchone()[0]
    assert len(load_records(conn)) == 8  # tornade : plan seulement

    # Générations R3 et R4 en Hold : 3 actives au plus, R2 archivée au démarrage de R5.
    await command(engine, Command.STOP)
    for generation in (3, 4, 5):
        assert f"R{generation}" in await command(engine, Command.OPTIMIZE)
        await optimize_pending(conn, data)
        await engine.process_commands()
    state = recalibration.read_state(conn)
    assert state.active == (3, 4, 5)
    archived = {r[0] for r in conn.execute("SELECT id FROM simulations WHERE archived_at")}
    assert archived == set(first) and not archived & set(engine.sims)
    assert "4 simulations plus anciennes archivées" in (state.message or "")

    restarted = make_engine()
    assert set(restarted.sims) == set(range(1, 9)) | {
        i for g in (3, 4, 5) for i in generation_ids(conn, g)
    }

    # Échec de l'optimisation : la génération en préparation est abandonnée.
    restarted.recalibration_launcher = lambda workers: FakeProcess(code=1)
    await command(restarted, Command.OPTIMIZE)  # création + lancement
    await restarted.process_commands()  # processus terminé sans résultat
    state = recalibration.read_state(conn)
    assert state.pending is None and "R6 abandonnée" in (state.message or "")
    assert not generation_ids(conn, 6)

    await command(restarted, Command.RESET)
    assert not conn.execute(
        "SELECT COUNT(*) FROM simulations WHERE kind = 'recalibrated'"
    ).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0] == 8


def test_selection_on_train_or_mean(data, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Apprentissage croissant avec l'EMA rapide, validation décroissante : le plan retient
    la plus rapide la plus longue, le recalibrage le meilleur compromis."""
    draw = next(d for d in draw_plan(seed=5) if d["family"] == "ema_cross")
    sim = StoredSimulation(2, 1, "optimized", "ema_cross", 11, build_config(draw), draw)
    _, cut, _ = data.split()

    class Result:
        def __init__(self, score: float) -> None:
            self.score = score

    def fake(config: Any, dataset: Any, start: int, end: int, fees: Any, cache: Any) -> Result:
        fast = config.signals.main.fast
        return Result(fast / 10 if start < cut else -((fast - 12) ** 2) / 10)

    monkeypatch.setattr(optimizer, "run_backtest", fake)
    train = optimize(sim, data, FEES, trials=40, select=TRAIN)
    mean = optimize(sim, data, FEES, trials=40, select=MEAN)
    assert train.config.signals.main.fast > mean.config.signals.main.fast  # type: ignore[union-attr]
    assert (mean.train_score + mean.validation_score) / 2 > (
        train.train_score + train.validation_score
    ) / 2
    assert json.dumps(mean.validation_score)  # score enregistré tel que sélectionné
