import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from mcs.config import JournalMode
from mcs.logging_setup import SecretRedactingFilter
from mcs.storage.candles import count_candles, load_candles, ts_bounds, upsert_candles
from mcs.storage.db import SCHEMA_VERSION, connect
from mcs.storage.markets import (
    LatestPrice,
    MarketInfo,
    basket_markets,
    selected_markets,
    update_market_stats,
    upsert_latest_prices,
    upsert_market_info,
)
from tests.conftest import make_candle

EXPECTED_TABLES = {
    "simulations",
    "indicator_params",
    "markets",
    "candles",
    "orders",
    "positions",
    "portfolio_values",
    "latest_prices",
    "metrics",
    "buy_and_hold",
    "engine_state",
    "run_periods",
    "commands",
}


def test_schema_created_with_wal_and_version(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == EXPECTED_TABLES
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    state = conn.execute("SELECT status, optimization_status FROM engine_state").fetchone()
    assert tuple(state) == ("NOT_STARTED", "TODO")


def test_connect_is_idempotent_and_delete_mode(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    connect(path, JournalMode.DELETE).close()
    conn = connect(path, JournalMode.DELETE)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("SELECT COUNT(*) FROM engine_state").fetchone()[0] == 1
    conn.close()


def test_engine_state_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE engine_state SET status = 'PAUSED'")


def test_candle_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    candles = [make_candle("BTC-EUR", "1h", ts * 3_600_000) for ts in range(5)]
    upsert_candles(conn, candles)
    upsert_candles(conn, candles)
    assert count_candles(conn, "BTC-EUR", "1h") == 5
    upsert_candles(conn, [make_candle("BTC-EUR", "1h", 0, price="200")])
    assert load_candles(conn, "BTC-EUR", "1h")[0].close == Decimal("200")
    assert ts_bounds(conn, "BTC-EUR", "1h") == (0, 4 * 3_600_000)
    assert ts_bounds(conn, "ETH-EUR", "1h") is None


def test_decimal_round_trip_keeps_tiny_prices(conn: sqlite3.Connection) -> None:
    tiny = make_candle("PEPE-EUR", "1m", 0, price="0.0000059512", volume="260678880220136")
    upsert_candles(conn, [tiny])
    stored = load_candles(conn, "PEPE-EUR", "1m")[0]
    assert stored == tiny
    assert conn.execute("SELECT typeof(close) FROM candles").fetchone()[0] == "text"
    upsert_candles(conn, [make_candle("PEPE-EUR", "1m", 0, price="1.00E-10")])
    assert conn.execute("SELECT close FROM candles").fetchone()[0] == "0.000000000100"


def test_load_candles_range_is_start_inclusive_end_exclusive(conn: sqlite3.Connection) -> None:
    upsert_candles(conn, [make_candle("BTC-EUR", "1m", i * 60_000) for i in range(10)])
    loaded = load_candles(conn, "BTC-EUR", "1m", 2 * 60_000, 5 * 60_000)
    assert [c.ts for c in loaded] == [120_000, 180_000, 240_000]


def _info(market: str) -> MarketInfo:
    return MarketInfo(
        market,
        market.split("-")[0],
        "EUR",
        "trading",
        Decimal("0.01"),
        8,
        2,
        Decimal("0.0001"),
        Decimal("5"),
        None,
        None,
        "A",
    )


def test_market_selection_defaults_on_200_days_and_is_kept(conn: sqlite3.Connection) -> None:
    upsert_market_info(conn, [_info("BTC-EUR"), _info("NIL-EUR")], ["BTC-EUR", "NIL-EUR"])
    update_market_stats(
        conn, "BTC-EUR", first_daily_ts=0, history_days=2500, volume_24h_eur=Decimal("1")
    )
    update_market_stats(conn, "NIL-EUR", first_daily_ts=0, history_days=150, volume_24h_eur=None)
    assert selected_markets(conn) == ["BTC-EUR"]

    # Choix de l'utilisateur : NIL cochée, BTC décochée ; les mises à jour suivantes le respectent.
    conn.execute("UPDATE markets SET selected = 1 - selected")
    update_market_stats(conn, "BTC-EUR", first_daily_ts=0, history_days=2501, volume_24h_eur=None)
    upsert_market_info(conn, [_info("BTC-EUR"), _info("NIL-EUR")], ["BTC-EUR", "NIL-EUR"])
    assert selected_markets(conn) == ["NIL-EUR"]

    # Une paire retirée du panier n'est plus listée.
    upsert_market_info(conn, [_info("BTC-EUR")], ["BTC-EUR"])
    assert [r["market"] for r in basket_markets(conn)] == ["BTC-EUR"]


def test_latest_prices_overwrite(conn: sqlite3.Connection) -> None:
    upsert_latest_prices(conn, [LatestPrice("BTC-EUR", Decimal("1"), None, None, 1)])
    upsert_latest_prices(
        conn, [LatestPrice("BTC-EUR", Decimal("2"), Decimal("1.9"), Decimal("2.1"), 2)]
    )
    rows = conn.execute("SELECT price, best_bid, ts FROM latest_prices").fetchall()
    assert [tuple(r) for r in rows] == [("2", "1.9", 2)]


def test_secret_redaction() -> None:
    record = logging.LogRecord("x", logging.INFO, "", 0, "clé %s", ("abc123",), None)
    SecretRedactingFilter(["abc123"]).filter(record)
    assert record.getMessage() == "clé ***"


def test_migrations_from_v1(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    conn = connect(path, JournalMode.DELETE)
    conn.executescript(
        """
        DROP INDEX orders_open;
        ALTER TABLE orders DROP COLUMN notional;
        ALTER TABLE orders DROP COLUMN liquidity;
        ALTER TABLE orders DROP COLUMN reserved;
        ALTER TABLE positions DROP COLUMN state_json;
        PRAGMA user_version = 1;
        """
    )
    conn.close()
    conn = connect(path, JournalMode.DELETE)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    assert {"notional", "liquidity", "reserved"} <= columns
    position_columns = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
    assert "state_json" in position_columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
