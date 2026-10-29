"""Mode production, palier 1 : choix figé, historique, mise à l'échelle des montants."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from decimal import Decimal as D

import httpx
import pytest

from mcs import production
from mcs.bitvavo.account import BitvavoAccount, sign
from mcs.bitvavo.rest import BASE_URL
from mcs.broker.base import Side
from mcs.engine import poll_balance
from mcs.live.state import Command
from mcs.metrics import compute_metrics, write_metrics
from mcs.storage.orders import load_orders
from mcs.strategy.serialize import config_from_json
from tests.test_engine import command, data, run_bars, setup  # noqa: F401 (fixtures)

SHADOW = production.PRODUCTION_SIM_ID


def add_sim(conn: sqlite3.Connection, sim_id: int, config: str = '{"a": 1}') -> None:
    conn.execute(
        "INSERT INTO simulations (id, pair_id, mode, family, config_json, draw_json, seed,"
        " created_at) VALUES (?, ?, 'optimized', 'macd', ?, '{\"sizing\": \"fixed\"}', 1, 0)",
        (sim_id, sim_id, config),
    )


def test_scaled_quote_rounds_down_and_lifts_to_minimum() -> None:
    scale = production.scale_factor(D(300))
    assert scale == D("0.3")
    assert production.scaled_quote(D(40), scale, D(5)) == D("12.00")
    assert production.scaled_quote(D("33.37"), scale, D(5)) == D("10.01")
    assert production.scaled_quote(D(10), scale, D(5)) == D(5)  # 3 € relevé au minimum


def test_choose_freezes_config_and_keeps_history(conn: sqlite3.Connection) -> None:
    add_sim(conn, 1)
    add_sim(conn, 2, '{"b": 2}')
    write_metrics(conn, [compute_metrics(1, D(1100), [D(1)] * 12, D(2), 0, None)], 5)
    assert production.active_choice(conn) is None

    first = production.choose(conn, 1, D(300), "calmar", 10)
    assert first.scale == D("0.3") and first.pnl_pct == D(10) and first.closed_trades == 12
    second = production.choose(conn, 2, D(500), "pnl", 20)
    assert second.pnl_pct is None and second.config_json == '{"b": 2}'

    assert production.active_choice(conn) == second
    (old,) = [c for c in production.history(conn) if c.id == first.id]
    assert old.replaced_at == 20
    assert [c.sim_id for c in production.history(conn)] == [2, 1]

    conn.execute("DELETE FROM simulations")  # nouveau plan : le choix reste figé
    active = production.active_choice(conn)
    assert active is not None and active.config_json == '{"b": 2}'


def test_choose_rejects_bad_input(conn: sqlite3.Connection) -> None:
    add_sim(conn, 1)
    with pytest.raises(ValueError):
        production.choose(conn, 9, D(300), "pnl", 1)
    with pytest.raises(ValueError):
        production.choose(conn, 1, D(0), "pnl", 1)
    with pytest.raises(ValueError):
        production.choose(conn, 1, D(300), "sharpe", 1)
    assert production.history(conn) == []


# --- Mode ombre -----------------------------------------------------------------------------


async def test_shadow_follows_choice_state_and_capital(setup) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, make_engine, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = make_engine()
    await command(engine, Command.START)
    await run_bars(engine, clock, 6)
    busiest = conn.execute(
        "SELECT sim_id FROM orders GROUP BY sim_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]

    with pytest.raises(ValueError):  # pas de scénario choisi
        production.set_mode(conn, production.Mode.SHADOW, clock())
    production.choose(conn, busiest, D(300), "pnl", clock())
    production.set_mode(conn, production.Mode.SHADOW, clock())
    await engine.process_commands()
    assert engine.shadow is not None and engine.shadow.sim is not None
    assert engine.shadow.broker.total_cash(SHADOW) == D(300)

    await run_bars(engine, clock, 8)
    shadow_orders = load_orders(conn, table="production_orders")
    assert shadow_orders, "aucun ordre ombre en 8 clôtures 4 h"
    assert all(o.sim_id == SHADOW for o in shadow_orders)
    assert all(o.sim_id != SHADOW for o in load_orders(conn))  # tables séparées
    buys = [o for o in shadow_orders if o.side is Side.BUY and o.notional is not None]
    assert all(o.notional >= D(5) for o in buys)  # minimum Bitvavo respecté
    values = conn.execute("SELECT COUNT(*) FROM production_values").fetchone()[0]
    assert values == 8 * 4

    # Redémarrage : portefeuille ombre reconstruit à l'identique.
    snapshot = (
        engine.shadow.broker.total_cash(SHADOW),
        engine.shadow.broker.holdings(SHADOW),
        dict(engine.shadow.sim.positions),
    )
    restarted = make_engine()
    await restarted.process_commands()
    assert restarted.shadow is not None and restarted.shadow.sim is not None
    assert (
        restarted.shadow.broker.total_cash(SHADOW),
        restarted.shadow.broker.holdings(SHADOW),
        dict(restarted.shadow.sim.positions),
    ) == snapshot

    # Hold : l'ombre suit, ordres en attente annulés, positions conservées.
    await command(restarted, Command.STOP)
    assert restarted.shadow.sim is None
    assert not [o for o in load_orders(conn, table="production_orders") if o.status.value == "open"]

    # Nouveau choix à 500 € : liquidités ajustées de +200 €, positions gardées.
    cash = restarted.shadow.broker.total_cash(SHADOW)
    other = 1 if busiest != 1 else 2
    production.choose(conn, other, D(500), "pnl", clock())
    await command(restarted, Command.RESUME)
    shadow = restarted.shadow.sim
    assert shadow is not None and shadow.config == load_config(conn, other)
    assert restarted.shadow.broker.total_cash(SHADOW) == cash + 200
    assert set(shadow.positions) == set(snapshot[2])
    assert production.read_state(conn).capital_applied == D(500)

    # Arrêt de l'ombre depuis le dashboard ; remise à zéro des simulations sans effet sur elle.
    production.set_mode(conn, production.Mode.OFF, clock())
    await restarted.process_commands()
    assert restarted.shadow.sim is None
    await command(restarted, Command.STOP)
    await command(restarted, Command.RESET)
    assert load_orders(conn, table="production_orders")


def load_config(conn: sqlite3.Connection, sim_id: int):  # type: ignore[no-untyped-def]
    raw = conn.execute("SELECT config_json FROM simulations WHERE id = ?", (sim_id,)).fetchone()
    return config_from_json(raw[0])


async def test_poll_balance_records_amounts_or_error(conn: sqlite3.Connection) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json=[
                    {"symbol": "EUR", "available": "312.5", "inOrder": "0"},
                    {"symbol": "BTC", "available": "0", "inOrder": "0"},
                    {"symbol": "ETH", "available": "0.01", "inOrder": "0.002"},
                ],
            )
        return httpx.Response(403, json={"errorCode": 305, "error": "No active API key found."})

    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    account = BitvavoAccount("cle", "secret", client=client, clock=lambda: 1_700_000_000_000)
    await poll_balance(conn, account, 10)
    request = calls[0]
    assert request.method == "GET" and request.url.path == "/v2/balance"
    assert request.headers["bitvavo-access-key"] == "cle"
    assert request.headers["bitvavo-access-signature"] == sign(
        "secret", 1_700_000_000_000, "GET", "/balance"
    )
    state = production.read_state(conn)
    assert state.balances == {"EUR": (D("312.5"), D(0)), "ETH": (D("0.01"), D("0.002"))}
    assert state.balance_at == 10 and state.balance_error is None

    await poll_balance(conn, account, 20)
    state = production.read_state(conn)
    assert state.balance_at == 10 and state.balances is not None  # dernier solde gardé
    assert state.balance_error is not None and "305" in state.balance_error
    assert "secret" not in state.balance_error and "cle" not in state.balance_error
    await account.aclose()


def test_sign_matches_reference_vector() -> None:
    # Vecteur calculé indépendamment : HMAC-SHA256("secret", "1GET/v2/balance").
    expected = hmac.new(b"secret", b"1GET/v2/balance", hashlib.sha256).hexdigest()
    assert sign("secret", 1, "GET", "/balance") == expected
