"""Accès à la table `positions` (une ligne par simulation et par paire détenue), ou
`production_positions` pour le mode production."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from mcs.risk.position import Position
from mcs.storage.db import transaction

POSITION_TABLES = frozenset({"positions", "production_positions"})


class SqlitePositionStore:
    def __init__(self, conn: sqlite3.Connection, table: str = "positions") -> None:
        if table not in POSITION_TABLES:
            raise ValueError(f"Table de positions inconnue : {table}")
        self.conn, self.table = conn, table

    def save(self, sim_id: int, position: Position, now: int) -> None:
        state = {
            "risk_per_unit": str(position.risk_per_unit),
            "initial_stop": str(position.initial_stop),
            "last_entry_price": str(position.last_entry_price),
            "highest_high": str(position.highest_high),
            "bars_held": position.bars_held,
            "trailing_active": position.trailing_active,
            "tp_order_id": position.tp_order_id,
            "partial_done": position.partial_done,
            "realized_pnl": str(position.realized_pnl),
        }
        with transaction(self.conn):
            self.conn.execute(
                f"""
                INSERT INTO {self.table} (sim_id, market, lots, quantity, avg_entry_price,
                    invested_cost, buy_fees, stop, trailing, take_profit, state_json,
                    opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sim_id, market) DO UPDATE SET
                    lots = excluded.lots, quantity = excluded.quantity,
                    avg_entry_price = excluded.avg_entry_price,
                    invested_cost = excluded.invested_cost, buy_fees = excluded.buy_fees,
                    stop = excluded.stop, trailing = excluded.trailing,
                    take_profit = excluded.take_profit, state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    sim_id, position.market, position.lots, position.quantity,
                    position.avg_entry_price, position.invested_cost, position.buy_fees,
                    position.stop, position.trailing, position.take_profit, json.dumps(state),
                    position.opened_at, now,
                ),
            )  # fmt: skip

    def delete(self, sim_id: int, market: str) -> None:
        self.conn.execute(
            f"DELETE FROM {self.table} WHERE sim_id = ? AND market = ?", (sim_id, market)
        )

    def load(self, sim_id: int) -> dict[str, Position]:
        positions = {}
        for row in self.conn.execute(f"SELECT * FROM {self.table} WHERE sim_id = ?", (sim_id,)):
            state = json.loads(row["state_json"])
            positions[row["market"]] = Position(
                market=row["market"],
                lots=row["lots"],
                quantity=Decimal(row["quantity"]),
                avg_entry_price=Decimal(row["avg_entry_price"]),
                invested_cost=Decimal(row["invested_cost"]),
                buy_fees=Decimal(row["buy_fees"]),
                risk_per_unit=Decimal(state["risk_per_unit"]),
                initial_stop=Decimal(state["initial_stop"]),
                stop=Decimal(row["stop"]),
                last_entry_price=Decimal(state["last_entry_price"]),
                highest_high=Decimal(state["highest_high"]),
                opened_at=row["opened_at"],
                bars_held=state["bars_held"],
                trailing=Decimal(row["trailing"]) if row["trailing"] is not None else None,
                trailing_active=state["trailing_active"],
                take_profit=Decimal(row["take_profit"]) if row["take_profit"] else None,
                tp_order_id=state["tp_order_id"],
                partial_done=state["partial_done"],
                realized_pnl=Decimal(state.get("realized_pnl", "0")),
            )
        return positions
