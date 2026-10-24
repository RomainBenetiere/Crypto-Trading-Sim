"""Accès aux tables `markets` et `latest_prices`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from mcs.storage.db import now_ms, transaction

MIN_HISTORY_DAYS = 200


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Caractéristiques d'un marché lues via `GET /markets`."""

    market: str
    base: str
    quote: str
    status: str
    tick_size: Decimal | None
    quantity_decimals: int | None
    notional_decimals: int | None
    min_order_base: Decimal | None
    min_order_quote: Decimal | None
    max_order_base: Decimal | None
    max_order_quote: Decimal | None
    fee_category: str | None


@dataclass(frozen=True, slots=True)
class LatestPrice:
    market: str
    price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    ts: int


def upsert_market_info(
    conn: sqlite3.Connection, infos: Iterable[MarketInfo], basket: Iterable[str]
) -> None:
    """Met à jour les caractéristiques des marchés sans toucher à la sélection de l'utilisateur."""
    in_basket = set(basket)
    stamp = now_ms()
    with transaction(conn):
        conn.execute("UPDATE markets SET in_basket = 0")
        conn.executemany(
            """
            INSERT INTO markets (
                market, base, quote, status, in_basket, tick_size, quantity_decimals,
                notional_decimals, min_order_base, min_order_quote, max_order_base,
                max_order_quote, fee_category, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (market) DO UPDATE SET
                base = excluded.base, quote = excluded.quote, status = excluded.status,
                in_basket = excluded.in_basket, tick_size = excluded.tick_size,
                quantity_decimals = excluded.quantity_decimals,
                notional_decimals = excluded.notional_decimals,
                min_order_base = excluded.min_order_base,
                min_order_quote = excluded.min_order_quote,
                max_order_base = excluded.max_order_base,
                max_order_quote = excluded.max_order_quote,
                fee_category = excluded.fee_category, updated_at = excluded.updated_at
            """,
            [
                (
                    i.market,
                    i.base,
                    i.quote,
                    i.status,
                    int(i.market in in_basket),
                    i.tick_size,
                    i.quantity_decimals,
                    i.notional_decimals,
                    i.min_order_base,
                    i.min_order_quote,
                    i.max_order_base,
                    i.max_order_quote,
                    i.fee_category,
                    stamp,
                )
                for i in infos
            ],
        )


def update_market_stats(
    conn: sqlite3.Connection,
    market: str,
    *,
    first_daily_ts: int | None,
    history_days: int | None,
    volume_24h_eur: Decimal | None,
) -> None:
    """Met à jour l'historique et le volume. À la première mesure de l'historique, la paire est
    cochée par défaut si elle a au moins 200 jours (la sélection de l'utilisateur prime ensuite)."""
    with transaction(conn):
        row = conn.execute(
            "SELECT history_days FROM markets WHERE market = ?", (market,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Marché inconnu : {market}")
        first_measure = row["history_days"] is None and history_days is not None
        conn.execute(
            """
            UPDATE markets SET first_daily_ts = ?, history_days = ?, volume_24h_eur = ?,
                selected = CASE WHEN ? THEN ? ELSE selected END, updated_at = ?
            WHERE market = ?
            """,
            (
                first_daily_ts,
                history_days,
                volume_24h_eur,
                first_measure,
                int((history_days or 0) >= MIN_HISTORY_DAYS),
                now_ms(),
                market,
            ),
        )


def basket_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM markets WHERE in_basket = 1 ORDER BY market").fetchall()


def selected_markets(conn: sqlite3.Connection) -> list[str]:
    return [
        row["market"]
        for row in conn.execute(
            "SELECT market FROM markets WHERE in_basket = 1 AND selected = 1 ORDER BY market"
        )
    ]


def upsert_latest_prices(conn: sqlite3.Connection, prices: Iterable[LatestPrice]) -> None:
    rows = [(p.market, p.price, p.best_bid, p.best_ask, p.ts) for p in prices]
    if not rows:
        return
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO latest_prices (market, price, best_bid, best_ask, ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (market) DO UPDATE SET price = excluded.price,
                best_bid = excluded.best_bid, best_ask = excluded.best_ask, ts = excluded.ts
            """,
            rows,
        )
