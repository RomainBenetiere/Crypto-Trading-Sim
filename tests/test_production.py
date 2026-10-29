"""Mode production, palier 1 : choix figé, historique, mise à l'échelle des montants."""

from __future__ import annotations

import sqlite3
from decimal import Decimal as D

import pytest

from mcs import production
from mcs.metrics import compute_metrics, write_metrics


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
