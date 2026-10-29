"""Accès à la table `orders` (simulations) ou `production_orders` (mode production)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

from mcs.broker.base import Liquidity, Order, OrderStatus, OrderType, Reason, Side
from mcs.storage.db import transaction

COLUMNS = (
    "sim_id, market, side, order_type, status, reason, quantity, price, notional, fee, "
    "liquidity, reserved, book_bid, book_ask, created_at, expires_at, filled_at"
)
ORDER_TABLES = frozenset({"orders", "production_orders"})


def _table(table: str) -> str:
    if table not in ORDER_TABLES:
        raise ValueError(f"Table d'ordres inconnue : {table}")
    return table


def insert_order(conn: sqlite3.Connection, order: Order, table: str = "orders") -> Order:
    """Enregistre un ordre (son `id` est ignoré) et le renvoie avec l'identifiant attribué."""
    with transaction(conn):
        cursor = conn.execute(
            f"INSERT INTO {_table(table)} ({COLUMNS}) VALUES ({', '.join('?' * 17)})",
            (
                order.sim_id, order.market, order.side, order.order_type, order.status,
                order.reason, order.quantity, order.price, order.notional, order.fee,
                order.liquidity, order.reserved, order.book_bid, order.book_ask,
                order.created_at, order.expires_at, order.filled_at,
            ),
        )  # fmt: skip
    return replace(order, id=int(cursor.lastrowid or 0))


def update_order(conn: sqlite3.Connection, order: Order, table: str = "orders") -> None:
    with transaction(conn):
        conn.execute(
            f"""UPDATE {_table(table)} SET status = ?, price = ?, notional = ?, fee = ?,
                   liquidity = ?, reserved = ?, filled_at = ? WHERE id = ?""",
            (
                order.status, order.price, order.notional, order.fee, order.liquidity,
                order.reserved, order.filled_at, order.id,
            ),
        )  # fmt: skip


def load_orders(
    conn: sqlite3.Connection, sim_id: int | None = None, table: str = "orders"
) -> list[Order]:
    query = f"SELECT id, {COLUMNS} FROM {_table(table)}"
    params: tuple[int, ...] = ()
    if sim_id is not None:
        query += " WHERE sim_id = ?"
        params = (sim_id,)
    return [_from_row(row) for row in conn.execute(query + " ORDER BY id", params)]


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _from_row(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        sim_id=row["sim_id"],
        market=row["market"],
        side=Side(row["side"]),
        order_type=OrderType(row["order_type"]),
        status=OrderStatus(row["status"]),
        reason=Reason(row["reason"]),
        quantity=Decimal(row["quantity"]),
        price=_dec(row["price"]),
        notional=_dec(row["notional"]),
        fee=_dec(row["fee"]),
        liquidity=Liquidity(row["liquidity"]) if row["liquidity"] else None,
        reserved=_dec(row["reserved"]),
        book_bid=_dec(row["book_bid"]),
        book_ask=_dec(row["book_ask"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        filled_at=row["filled_at"],
    )
