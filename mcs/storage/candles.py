"""Accès à la table `candles`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from decimal import Decimal

from mcs.market_data import Candle
from mcs.storage.db import transaction


def upsert_candles(conn: sqlite3.Connection, candles: Iterable[Candle]) -> int:
    """Insère ou remplace des bougies (idempotent). Retourne le nombre de lignes écrites."""
    rows = [(c.market, c.interval, c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles]
    if not rows:
        return 0
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO candles (market, interval, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (market, interval, ts) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume
            """,
            rows,
        )
    return len(rows)


def ts_bounds(conn: sqlite3.Connection, market: str, interval: str) -> tuple[int, int] | None:
    """(plus ancien, plus récent) horodatage stocké, ou None si aucune bougie."""
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE market = ? AND interval = ?",
        (market, interval),
    ).fetchone()
    return None if row[0] is None else (row[0], row[1])


def count_candles(conn: sqlite3.Connection, market: str, interval: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM candles WHERE market = ? AND interval = ?", (market, interval)
        ).fetchone()[0]
    )


def load_candles(
    conn: sqlite3.Connection,
    market: str,
    interval: str,
    start: int | None = None,
    end: int | None = None,
) -> list[Candle]:
    """Bougies triées par date croissante, `start` inclus, `end` exclu."""
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume FROM candles
        WHERE market = ? AND interval = ? AND ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (market, interval, start if start is not None else 0, end if end is not None else 2**62),
    ).fetchall()
    return [
        Candle(
            market=market,
            interval=interval,
            ts=row["ts"],
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
        )
        for row in rows
    ]
