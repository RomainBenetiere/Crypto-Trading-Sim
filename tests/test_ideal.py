import asyncio
import random
import sqlite3
from pathlib import Path

import pytest

import mcs.ideal as ideals
from mcs.backtest import Dataset
from mcs.ideal import ideal, report
from mcs.live.state import Command
from mcs.metrics import SimRecord, load_records
from mcs.optimizer import run_ideal
from mcs.sampling import CATEGORICALS, NUMERICS, draw_plan
from mcs.storage.db import JournalMode, connect
from mcs.storage.orders import load_orders
from tests.test_engine import FEES, command, data, run_bars, setup  # noqa: F401 (fixtures)


def records() -> list[SimRecord]:
    """P&L dicté par deux variables connues (+ bruit), Calmar par une troisième."""
    rng = random.Random(1)
    result = []
    for i, draw in enumerate(draw_plan(seed=7)):
        for mode in ("typical", "optimized"):
            pnl = 20.0 * (draw["trailing_type"] == "atr") - 100.0 * float(draw["max_exposure"])
            calmar = 3.0 * (draw["family"] == "donchian") + rng.gauss(0, 0.3)
            result.append(
                SimRecord(
                    2 * i + (mode == "optimized"),
                    i,
                    mode,
                    str(draw["family"]),
                    draw,
                    pnl + rng.gauss(0, 2),
                    12,
                    calmar,
                )
            )
    return result


def test_ideal_finds_planted_effects_and_respects_conditions() -> None:
    sims = records()
    best_pnl = ideal(sims, "pnl")
    assert best_pnl.draw["trailing_type"] == "atr"
    assert best_pnl.draw["max_exposure"] < 0.67  # tiers bas de 0,50 à 1,00
    assert best_pnl.draw["trailing_pct"] is None  # sans objet avec un trailing ATR
    assert best_pnl.draw["trailing_atr_multiple"] is not None
    assert set(best_pnl.draw) == {v.name for v in (*CATEGORICALS, *NUMERICS)}
    choices = {c.variable: c for c in best_pnl.choices}
    assert choices["trailing_type"].significant and choices["max_exposure"].significant
    assert ideal(sims, "calmar").draw["family"] == "donchian"
    text = report(best_pnl)
    assert "Trailing stop" in text and "Simulations existantes les plus proches" in text


async def optimize_pending(conn: sqlite3.Connection, dataset: Dataset) -> list[int]:
    """Comme le processus séparé du moteur : sa propre connexion, hors de la boucle."""
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def work() -> list[int]:
        own = connect(Path(path), JournalMode.WAL)
        try:
            return sorted(r.sim_id for r in run_ideal(own, FEES, trials=2, data=dataset))
        finally:
            own.close()

    return await asyncio.to_thread(work)


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.code = -9


async def test_engine_generations_lifecycle(setup, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    launched: list[FakeProcess] = []

    def launcher(workers: int) -> FakeProcess:
        launched.append(FakeProcess())
        return launched[-1]

    engine.ideal_launcher = launcher
    with pytest.raises(ideals.IdealError):  # simulations pas lancées
        ideals.request_generation(conn, clock())
    await command(engine, Command.START)
    await run_bars(engine, clock, 2)

    ideals.request_generation(conn, clock())
    await engine.process_commands()
    state = ideals.read_ideal_state(conn)
    assert state.message and "refusée" in state.message and state.requested_at is None

    conn.execute("UPDATE metrics SET closed_trades = 12")
    ideals.request_generation(conn, clock())
    await engine.process_commands()
    state = ideals.read_ideal_state(conn)
    assert state.pending == 1 and state.current is None and len(launched) == 1
    first = [
        (r["id"], r["mode"], r["objective"])
        for r in conn.execute("SELECT * FROM simulations WHERE kind = 'ideal' ORDER BY id")
    ]
    assert first == [
        (9, "typical", "pnl"),
        (10, "optimized", "pnl"),
        (11, "typical", "calmar"),
        (12, "optimized", "calmar"),
    ]
    assert not set(engine.sims) & {9, 10, 11, 12}
    with pytest.raises(ideals.IdealError):  # déjà en préparation
        ideals.request_generation(conn, clock())
    await engine.process_commands()  # optimisation pas finie : rien ne change
    assert ideals.read_ideal_state(conn).pending == 1

    assert await optimize_pending(conn, data) == [10, 12]
    await engine.process_commands()
    state = ideals.read_ideal_state(conn)
    assert state.current == 1 and state.pending is None and "démarrée" in (state.message or "")
    assert {9, 10, 11, 12} <= set(engine.sims) and engine.ideal_bh[9] is not None
    await run_bars(engine, clock, 2)
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE sim_id = 9").fetchone()[0] == 1
    assert len(load_records(conn)) == 8  # tornade : plan seulement

    conn.execute("UPDATE metrics SET closed_trades = 12")  # recalculées chaque heure
    ideals.request_generation(conn, clock())
    await engine.process_commands()
    await optimize_pending(conn, data)
    await engine.process_commands()
    assert ideals.read_ideal_state(conn).current == 2
    archived = {r[0] for r in conn.execute("SELECT id FROM simulations WHERE archived_at")}
    assert archived == {9, 10, 11, 12} and not archived & set(engine.sims)
    assert not [o for o in load_orders(conn) if o.sim_id in archived and o.status.value == "open"]

    restarted = make_engine()
    assert set(restarted.sims) == set(range(1, 9)) | {13, 14, 15, 16}

    restarted.ideal_launcher = lambda workers: FakeProcess(code=1)
    ideals.request_generation(conn, clock())
    await restarted.process_commands()  # création + lancement
    await restarted.process_commands()  # processus terminé sans résultat
    state = ideals.read_ideal_state(conn)
    assert state.pending is None and "abandonnée" in (state.message or "")

    await command(restarted, Command.STOP)
    await command(restarted, Command.RESET)
    assert not conn.execute("SELECT COUNT(*) FROM simulations WHERE kind = 'ideal'").fetchone()[0]
