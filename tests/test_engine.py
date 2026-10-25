"""Moteur live : machine à états, commandes, Hold, remise à zéro, redémarrage, événements."""

import sqlite3
from decimal import Decimal

import pytest

from mcs.bitvavo.rest import OrderBook
from mcs.broker.base import OrderStatus, Reason, Side
from mcs.broker.fees import FeeSchedule
from mcs.engine import Engine
from mcs.live.state import Command, Status, read_state, request_command
from mcs.market_data import Candle
from mcs.sampling import generate_plan
from mcs.storage.markets import MarketInfo, update_market_stats, upsert_market_info
from mcs.storage.orders import load_orders
from tests.backtest_data import HOUR, dataset

D = Decimal
BAR = 4 * HOUR
FEES = FeeSchedule()
MARKETS = ("AAA-EUR", "BBB-EUR")


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class Books:
    def __init__(self, data) -> None:  # type: ignore[no-untyped-def]
        self.data = data

    async def get_book(self, market: str) -> OrderBook:
        price = D(repr(float(self.data.markets[market].h1["close"].iloc[-1])))
        bid, ask = (
            (price * D("0.9995")).quantize(D("0.01")),
            (price * D("1.0005")).quantize(D("0.01")),
        )
        return OrderBook(market, [(bid, D(10_000))], [(ask, D(10_000))], 0)


@pytest.fixture(scope="module")
def data():  # type: ignore[no-untyped-def]
    return dataset(days=400, markets=MARKETS)


@pytest.fixture
def setup(conn: sqlite3.Connection, data):  # type: ignore[no-untyped-def]
    infos = [
        MarketInfo(m, m[:3], "EUR", "trading", D("0.01"), 6, 2, D("0.0001"), D(5), None, None, "A")
        for m in MARKETS
    ]
    upsert_market_info(conn, infos, MARKETS)
    for m in MARKETS:
        update_market_stats(conn, m, first_daily_ts=0, history_days=400, volume_24h_eur=None)
    generate_plan(conn, seed=3, pairs=4)
    clock = Clock(data.end + 3_000)

    def make_engine() -> Engine:
        engine = Engine(conn, FEES, Books(data), clock=clock, data_loader=lambda c, b: data,
                        optimizer_launcher=launched.append)  # fmt: skip
        engine.startup()
        return engine

    launched: list[int] = []
    return conn, clock, make_engine, launched


async def command(engine: Engine, name: Command) -> str:
    request_command(engine.conn, name, engine.clock())
    assert await engine.process_commands()
    row = engine.conn.execute("SELECT result FROM commands ORDER BY id DESC LIMIT 1").fetchone()
    return str(row[0])


async def run_bars(engine: Engine, clock: Clock, bars: int) -> None:
    for _ in range(bars * 4):
        clock.now += HOUR
        await engine.on_tick(clock.now)


async def test_state_machine(setup) -> None:  # type: ignore[no-untyped-def]
    conn, _, make_engine, launched = setup
    engine = make_engine()
    assert "refusée" in await command(engine, Command.START)  # optimisation pas terminée
    assert "refusée" in await command(engine, Command.STOP)
    assert "lancée" in await command(engine, Command.OPTIMIZE)
    assert launched == [4]
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    assert "lancé" in await command(engine, Command.START)
    state = read_state(conn)
    assert state.status is Status.RUNNING and state.basket == MARKETS and state.started_at
    assert "refusée" in await command(engine, Command.OPTIMIZE)
    assert "refusée" in await command(engine, Command.RESET)  # seulement depuis Hold
    bh = conn.execute(
        "SELECT market, quantity, buy_fee, allocated_eur FROM buy_and_hold"
    ).fetchall()
    assert [r["market"] for r in bh] == list(MARKETS)
    assert all(D(r["quantity"]) > 0 and D(r["buy_fee"]) > 0 for r in bh)
    assert sum(D(r["allocated_eur"]) for r in bh) == D(1000)
    assert "Hold" in await command(engine, Command.STOP)
    assert "reprise" in await command(engine, Command.RESUME)
    await command(engine, Command.STOP)
    assert "remis à zéro" in await command(engine, Command.RESET)
    state = read_state(conn)
    assert state.status is Status.NOT_STARTED and state.basket == ()
    assert state.optimization_status == "DONE"  # configurations conservées
    for table in ("orders", "positions", "portfolio_values", "buy_and_hold", "run_periods"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0] == 8


async def test_trading_hold_and_restart(setup) -> None:  # type: ignore[no-untyped-def]
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    await run_bars(engine, clock, 6)
    values = conn.execute("SELECT COUNT(DISTINCT ts), COUNT(*) FROM portfolio_values").fetchone()
    assert values[0] == 24 and values[1] == 24 * 8  # chaque heure, chaque simulation
    orders = load_orders(conn)
    assert orders, "aucun ordre en 6 clôtures 4 h"

    # Redémarrage : état identique reconstruit depuis la base.
    before = {
        sid: (s.broker.total_cash(sid), s.broker.holdings(sid), dict(s.positions), s.peak_value)
        for sid, s in engine.sims.items()
    }
    restarted = make_engine()
    after = {
        sid: (s.broker.total_cash(sid), s.broker.holdings(sid), dict(s.positions), s.peak_value)
        for sid, s in restarted.sims.items()
    }
    assert after == before
    assert restarted.status is Status.RUNNING

    # Hold : ordres ouverts annulés, plus aucun ordre, valeurs toujours écrites.
    await command(restarted, Command.STOP)
    assert all(not restarted.broker.open_orders(sid) for sid in restarted.sims)
    count = len(load_orders(conn))
    await run_bars(restarted, clock, 2)
    candle = Candle(MARKETS[0], "1m", clock.now, D(1), D(1), D("0.0001"), D(1), D(1))
    await restarted.on_minute_candle(candle)
    assert len(load_orders(conn)) == count
    assert conn.execute("SELECT COUNT(DISTINCT ts) FROM portfolio_values").fetchone()[0] == 32

    # Reprise : take profit re-posés pour les positions qui en avaient un.
    await command(restarted, Command.RESUME)
    for sim in restarted.sims.values():
        for position in sim.positions.values():
            if position.take_profit is not None:
                assert position.tp_order_id is not None


async def test_minute_candle_triggers_stop_and_routes_fills(setup) -> None:  # type: ignore[no-untyped-def]
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    await run_bars(engine, clock, 12)
    holder = next((s for s in engine.sims.values() if s.positions), None)
    assert holder is not None, "aucune position ouverte"
    market, position = next(iter(holder.positions.items()))
    others = {sid: engine.broker.total_cash(sid) for sid in engine.sims if sid != holder.sim_id
              and market not in engine.sims[sid].positions}  # fmt: skip
    clock.now += 60_000
    low = position.stop - D("0.01")
    candle = Candle(market, "1m", clock.now, low, low, low, low, D(1))
    await engine.on_minute_candle(candle)
    assert market not in holder.positions
    sell = [o for o in load_orders(conn, holder.sim_id) if o.side is Side.SELL][-1]
    assert sell.reason in (Reason.STOP, Reason.TRAILING, Reason.TAKE_PROFIT)
    for sid, cash in others.items():  # les autres simulations ne sont pas touchées
        if not [o for o in engine.broker.open_orders(sid) if o.market == market]:
            assert engine.broker.total_cash(sid) == cash


async def test_outage_recorded_on_restart(setup) -> None:  # type: ignore[no-untyped-def]
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    clock.now += 2 * HOUR  # le moteur s'arrête sans prévenir pendant 2 h
    make_engine()
    kinds = [
        tuple(r) for r in conn.execute("SELECT kind, ended_at IS NULL FROM run_periods ORDER BY id")
    ]
    assert kinds == [("active", 0), ("outage", 0), ("active", 1)]
    outage = conn.execute(
        "SELECT started_at, ended_at FROM run_periods WHERE kind = 'outage'"
    ).fetchone()
    assert outage["ended_at"] - outage["started_at"] == 2 * HOUR


async def test_decisions_only_at_4h_close(setup) -> None:  # type: ignore[no-untyped-def]
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    calls: list[int] = []
    for sim in engine.sims.values():
        original = sim.on_4h_close

        async def spy(now, ctx, original=original):  # type: ignore[no-untyped-def]
            calls.append(now)
            await original(now, ctx)

        sim.on_4h_close = spy  # type: ignore[method-assign]
    start = clock.now
    await run_bars(engine, clock, 2)
    boundaries = sorted(set(calls))
    assert all(ts % BAR < HOUR for ts in boundaries) and len(boundaries) == 2
    assert all(ts > start for ts in boundaries)
    assert len(calls) == 2 * len(engine.sims)
    assert all(
        o.status is not OrderStatus.OPEN or o.created_at in boundaries for o in load_orders(conn)
    )


async def test_metrics_written_every_hour_including_hold(setup) -> None:  # type: ignore[no-untyped-def]
    from mcs.metrics import load_metrics

    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    await run_bars(engine, clock, 3)
    metrics = load_metrics(conn)
    assert sorted(metrics) == sorted(engine.sims)
    for sim_id, m in metrics.items():
        assert m.pnl_eur == engine.sims[sim_id].portfolio_value(engine.ctx) - 1000
        assert m.bh_gap_eur is not None and m.open_positions == len(engine.sims[sim_id].positions)
    await command(engine, Command.STOP)
    before = conn.execute("SELECT MAX(updated_at) FROM metrics").fetchone()[0]
    await run_bars(engine, clock, 1)
    assert conn.execute("SELECT MAX(updated_at) FROM metrics").fetchone()[0] > before
