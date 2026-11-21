"""Série RA (décision 49) : créneau du vendredi, création, démarrage, réoptimisation
hebdomadaire appliquée sans rupture, suivi en production, échec, remise à zéro."""

import asyncio
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import mcs.adaptive as adaptive
from mcs import production
from mcs.backtest import Dataset
from mcs.live.state import Command
from mcs.metrics import load_records
from mcs.optimizer import run_adaptive
from mcs.storage.db import JournalMode, connect
from tests.test_engine import FEES, command, data, run_bars, setup  # noqa: F401 (fixtures)
from tests.test_ideal import FakeProcess


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=adaptive.PARIS).timestamp() * 1000)


def test_friday_slots() -> None:
    assert adaptive.last_slot(ms("2026-11-20 21:05")) == ms("2026-11-20 21:05")  # vendredi
    assert adaptive.last_slot(ms("2026-11-20 21:04")) == ms("2026-11-13 21:05")
    assert adaptive.last_slot(ms("2026-11-24 09:00")) == ms("2026-11-20 21:05")
    assert adaptive.next_slot(ms("2026-11-21 10:00")) == ms("2026-11-27 21:05")
    # Passage à l'heure d'été (29/03/2026) : toujours 21 h 05 à Paris.
    assert adaptive.next_slot(ms("2026-03-21 10:00")) == ms("2026-03-27 21:05")
    assert adaptive.next_slot(ms("2026-03-28 10:00")) == ms("2026-04-03 21:05")
    assert adaptive.label(150) == "RA-150"


async def optimize_series(conn: sqlite3.Connection, dataset: Dataset) -> list[int]:
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def work() -> list[int]:
        own = connect(Path(path), JournalMode.WAL)
        try:
            return sorted(r.sim_id for r in run_adaptive(own, FEES, trials=2, data=dataset))
        finally:
            own.close()

    return await asyncio.to_thread(work)


async def test_adaptive_lifecycle(setup, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    launched: list[FakeProcess] = []

    def launcher(workers: int) -> FakeProcess:
        launched.append(FakeProcess())
        return launched[-1]

    engine.adaptive_launcher = launcher
    with pytest.raises(adaptive.AdaptiveError):  # simulations pas lancées
        adaptive.create_series(conn, clock())
    await command(engine, Command.START)
    await run_bars(engine, clock, 2)

    ids = adaptive.create_series(conn, clock())
    assert len(ids) == 4
    with pytest.raises(adaptive.AdaptiveError):
        adaptive.create_series(conn, clock())
    await engine.process_commands()
    state = adaptive.read_state(conn)
    assert state.pending_start and len(launched) == 1 and not set(ids) & set(engine.sims)

    assert await optimize_series(conn, data) == ids
    await engine.process_commands()
    state = adaptive.read_state(conn)
    assert not state.pending_start and not state.running and "démarrée" in (state.message or "")
    assert set(ids) <= set(engine.sims) and engine.ideal_bh[ids[0]] is not None
    sources = [
        r[0]
        for r in conn.execute(
            "SELECT source_sim_id FROM simulations WHERE kind = 'adaptive' ORDER BY id"
        )
    ]
    assert sources == [2, 4, 6, 8]
    await run_bars(engine, clock, 6)
    assert len(load_records(conn)) == 8  # tornade : plan seulement

    # Une RA en production : elle y suivra sa réoptimisation.
    chosen = ids[0]
    first = production.choose(conn, chosen, Decimal(300), "pnl", clock())
    await engine.process_commands()
    assert not adaptive.read_state(conn).due(clock())

    # Vendredi 21 h 05 : réoptimisation, puis application sans rupture.
    clock.now = adaptive.next_slot(clock.now) + 60_000
    before = {i: (engine.broker.total_cash(i), dict(engine.sims[i].positions)) for i in ids}  # type: ignore[union-attr]
    await engine.process_commands()
    state = adaptive.read_state(conn)
    assert state.running and state.run_at == clock.now and len(launched) == 2
    await engine.process_commands()  # optimisation pas finie : rien ne change
    assert adaptive.read_state(conn).running
    assert await optimize_series(conn, data) == ids
    await engine.process_commands()
    state = adaptive.read_state(conn)
    assert not state.running and not state.to_apply and state.applied_at == state.run_at
    assert "appliquée à 4 simulations, production mise à jour (RA-002)" in (state.message or "")
    after = {i: (engine.broker.total_cash(i), dict(engine.sims[i].positions)) for i in ids}  # type: ignore[union-attr]
    assert {i: (c, set(p)) for i, (c, p) in after.items()} == {
        i: (c, set(p)) for i, (c, p) in before.items()
    }
    stored = conn.execute("SELECT config_json FROM simulations WHERE id = ?", (chosen,)).fetchone()[
        0
    ]
    current = production.active_choice(conn)
    assert current is not None and current.id != first.id and current.sim_id == chosen
    assert current.config_json == stored and current.capital == Decimal(300)
    restarted = make_engine()
    assert set(ids) <= set(restarted.sims)
    assert not adaptive.read_state(conn).due(clock())

    # Vendredi suivant, optimiseur en échec : réglages précédents gardés, vendredi d'après.
    restarted.adaptive_launcher = lambda workers: FakeProcess(code=1)
    clock.now = adaptive.next_slot(clock.now) + 60_000
    await restarted.process_commands()  # lancement
    await restarted.process_commands()  # processus terminé sans résultat
    state = adaptive.read_state(conn)
    assert not state.running and "interrompue appliquée à 0 simulations" in (state.message or "")
    assert not state.due(clock()) and state.due(adaptive.next_slot(clock.now) + 1)

    await command(restarted, Command.STOP)
    await command(restarted, Command.RESET)
    assert not adaptive.read_state(conn).exists
