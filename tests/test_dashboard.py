"""Dashboard : requêtes, règles des boutons, rendu des trois onglets (Streamlit AppTest)."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from dashboard import queries
from mcs.broker.fees import FeeSchedule
from mcs.config import JournalMode
from mcs.engine import Engine
from mcs.live.state import Command, request_command
from mcs.sampling import generate_plan
from mcs.storage.db import connect
from mcs.storage.markets import MarketInfo, update_market_stats, upsert_market_info
from tests.backtest_data import HOUR, dataset
from tests.test_engine import Books, Clock

D = Decimal
MARKETS = ("AAA-EUR", "BBB-EUR")
APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


def prepare(conn: sqlite3.Connection, history: tuple[int, int] = (400, 150)) -> None:
    infos = [
        MarketInfo(m, m[:3], "EUR", "trading", D("0.01"), 6, 2, D("0.0001"), D(5), None, None, "A")
        for m in MARKETS
    ]
    upsert_market_info(conn, infos, MARKETS)
    for market, days in zip(MARKETS, history, strict=True):
        update_market_stats(
            conn, market, first_daily_ts=0, history_days=days, volume_24h_eur=D(1000)
        )
    conn.execute("UPDATE markets SET selected = 1")
    generate_plan(conn, seed=5, pairs=4)


async def run_live(conn: sqlite3.Connection, bars: int = 8) -> Engine:
    data = dataset(days=400, markets=MARKETS)
    clock = Clock(data.end + 3_000)
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    engine = Engine(conn, FeeSchedule(), Books(data), clock=clock, data_loader=lambda c, b: data)
    engine.startup()
    request_command(conn, Command.START, clock())
    await engine.process_commands()
    for _ in range(bars * 4):
        clock.now += HOUR
        await engine.on_tick(clock.now)
    return engine


# --- Requêtes -------------------------------------------------------------------------------


def test_allowed_commands_follow_state_machine(conn: sqlite3.Connection) -> None:
    prepare(conn)
    info = queries.engine_info(conn)
    assert queries.allowed_commands(info) == [Command.OPTIMIZE]
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    assert queries.allowed_commands(queries.engine_info(conn)) == [Command.OPTIMIZE, Command.START]
    conn.execute("UPDATE engine_state SET optimization_status = 'RUNNING'")
    assert queries.allowed_commands(queries.engine_info(conn)) == []
    conn.execute("UPDATE engine_state SET status = 'RUNNING', optimization_status = 'DONE'")
    assert queries.allowed_commands(queries.engine_info(conn)) == [Command.STOP]
    conn.execute("UPDATE engine_state SET status = 'HOLD'")
    assert queries.allowed_commands(queries.engine_info(conn)) == [Command.RESUME, Command.RESET]
    queries.send_command(conn, Command.RESUME)
    info = queries.engine_info(conn)
    assert info.pending_command == "resume" and queries.allowed_commands(info) == []


def test_describe_indicators() -> None:
    assert queries.describe_indicator({"type": "EmaCross", "fast": 21, "slow": 55}) == "EMA 21 / 55"
    assert (
        queries.describe_indicator({"type": "Supertrend", "atr_period": 10, "multiplier": 3.0})
        == "ATR 10 · multiplicateur 3"
    )
    trend = {
        "type": "TrendFilter",
        "kind": "price_above_sma",
        "hysteresis": 0.02,
        "sma_window": 200,
        "ema_fast": 50,
        "ema_slow": 200,
        "roc_window": 60,
    }
    assert queries.describe_indicator(trend) == "Prix > SMA 200 · marge 2,00 %"


def test_indicator_table_marks_optimized_values(conn: sqlite3.Connection) -> None:
    prepare(conn)
    conn.execute(
        """UPDATE indicator_params SET params_json = '{"type": "EmaCross", "fast": 9, "slow": 40}'
           WHERE sim_id = 2 AND role = 'main'"""
    )
    table = queries.indicator_params_table(conn)
    marked = table[table["differs"]]
    assert list(marked["Simulation"]) == ["S-002"]
    assert marked.iloc[0]["Valeurs utilisées"] == "• EMA 9 / 40"
    assert set(table["Rôle"]) <= set(queries.ROLE_LABELS.values())


def test_markets_table_and_selection(conn: sqlite3.Connection) -> None:
    prepare(conn)
    conn.execute("UPDATE markets SET spread_estimate_pct = '0.1' WHERE market = 'AAA-EUR'")
    table = queries.markets_table(conn, D("0.0025"))
    aaa = table[table["Paire"] == "AAA-EUR"].iloc[0]
    assert aaa["Coût aller-retour"] == pytest.approx(0.6)  # 2 × 0,25 % + 0,1 %
    assert list(table["200 jours"]) == ["Atteint", "Insuffisant"]
    queries.save_selection(conn, ["BBB-EUR"])
    assert list(queries.markets_table(conn, D("0.0025"))["Prendre en compte"]) == [False, True]


async def test_simulations_table_with_metrics(conn: sqlite3.Connection) -> None:
    prepare(conn)
    await run_live(conn, bars=3)
    table = queries.simulations_table(conn)
    assert len(table) == 8 and table["pnl_eur"].notna().all()
    assert set(table["Calibrage"]) == {"Typique", "Optimisé"}
    assert queries.value_series(conn, [1, 2])["sim_id"].nunique() == 2


# --- Application (AppTest) ------------------------------------------------------------------


@pytest.fixture
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "app.sqlite"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "WAL")
    monkeypatch.setenv("CRYPTO_BASKET", ",".join(MARKETS))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    return path


def run_app():  # type: ignore[no-untyped-def]
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def commands(path: Path) -> list[str]:
    conn = connect(path, JournalMode.WAL)
    rows = [r[0] for r in conn.execute("SELECT command FROM commands ORDER BY id")]
    conn.close()
    return rows


def test_app_not_started_and_optimize_button(app_db: Path) -> None:
    conn = connect(app_db, JournalMode.WAL)
    prepare(conn)
    conn.close()
    at = run_app()
    assert [t.label for t in at.tabs] == [
        "Suivi des simulations",
        "Paramètres des indicateurs",
        "Marchés du panier",
    ]
    assert any("Non démarré" in str(m.value) for m in at.metric)
    assert {b.label for b in at.button} >= {"Optimiser les indicateurs", "Valider la sélection"}
    assert "Lancer les simulations" not in {b.label for b in at.button}
    at.button(key="btn_optimize").click().run()
    assert not at.exception
    assert commands(app_db) == ["optimize"]


async def test_app_running_shows_metrics_and_stop(app_db: Path) -> None:
    conn = connect(app_db, JournalMode.WAL)
    prepare(conn)
    await run_live(conn)
    conn.close()
    at = run_app()
    labels = {m.label for m in at.metric}
    assert {
        "Meilleur P&L",
        "P&L médian",
        "Buy & hold",
        "Battent le buy & hold",
        "Optimisé − typique",
        "Trades clôturés",
    } <= labels
    assert (
        len(at.dataframe) >= 4
    )  # top 5, positions ou buy & hold, toutes les simulations, indicateurs
    at.button(key="btn_stop").click().run()
    assert commands(app_db)[-1] == "stop"


async def test_app_hold_reset_needs_confirmation(app_db: Path) -> None:
    conn = connect(app_db, JournalMode.WAL)
    prepare(conn)
    engine = await run_live(conn, bars=2)
    request_command(conn, Command.STOP, engine.clock())
    await engine.process_commands()
    conn.close()
    at = run_app()
    assert {"Reprendre", "Remettre à zéro"} <= {b.label for b in at.button}
    at.button(key="btn_reset").click().run()
    assert commands(app_db)[-1] == "stop"  # pas encore envoyée : confirmation demandée
    assert any("configurations sont conservées" in w.value for w in at.warning)
    at.button(key="btn_confirm").click().run()
    assert commands(app_db)[-1] == "reset"
