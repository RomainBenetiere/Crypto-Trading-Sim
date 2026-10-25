"""État persistant du moteur : `engine_state`, `run_periods`, `sim_state`, `commands`."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from mcs.storage.db import transaction
from mcs.strategy.simulation import Simulation


class Status(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    HOLD = "HOLD"


class Command(StrEnum):
    OPTIMIZE = "optimize"
    START = "start"
    STOP = "stop"
    RESUME = "resume"
    RESET = "reset"


@dataclass(frozen=True)
class EngineState:
    status: Status
    optimization_status: str
    started_at: int | None
    heartbeat_at: int | None
    basket: tuple[str, ...]


def read_state(conn: sqlite3.Connection) -> EngineState:
    row = conn.execute(
        "SELECT status, optimization_status, started_at, heartbeat_at, basket_json "
        "FROM engine_state"
    ).fetchone()
    return EngineState(
        status=Status(row["status"]),
        optimization_status=row["optimization_status"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        basket=tuple(json.loads(row["basket_json"])) if row["basket_json"] else (),
    )


def heartbeat(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("UPDATE engine_state SET heartbeat_at = ? WHERE id = 1", (now,))


# --- Périodes ------------------------------------------------------------------------------


def close_open_period(conn: sqlite3.Connection, ts: int) -> None:
    conn.execute("UPDATE run_periods SET ended_at = ? WHERE ended_at IS NULL", (ts,))


def open_period(conn: sqlite3.Connection, kind: str, ts: int) -> None:
    close_open_period(conn, ts)
    conn.execute("INSERT INTO run_periods (kind, started_at) VALUES (?, ?)", (kind, ts))


def record_outage(conn: sqlite3.Connection, since: int, now: int, resume_kind: str) -> None:
    """Coupure non prévue (décision 29) : [dernier signe de vie, maintenant) en interruption."""
    with transaction(conn):
        close_open_period(conn, since)
        conn.execute(
            "INSERT INTO run_periods (kind, started_at, ended_at) VALUES ('outage', ?, ?)",
            (since, now),
        )
        conn.execute("INSERT INTO run_periods (kind, started_at) VALUES (?, ?)", (resume_kind, now))


def period_totals(conn: sqlite3.Connection, now: int) -> dict[str, int]:
    """Durées cumulées (ms) par type de période."""
    totals = {"active": 0, "hold": 0, "outage": 0}
    for row in conn.execute("SELECT kind, started_at, ended_at FROM run_periods"):
        totals[row["kind"]] += (row["ended_at"] or now) - row["started_at"]
    return totals


def set_status(conn: sqlite3.Connection, status: Status, now: int, **fields: object) -> None:
    totals = period_totals(conn, now)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        "UPDATE engine_state SET status = ?, active_ms = ?, hold_ms = ?, updated_at = ?"
        + (f", {assignments}" if assignments else ""),
        (status.value, totals["active"], totals["hold"] + totals["outage"], now, *fields.values()),
    )


# --- État des simulations -----------------------------------------------------------------


class SimStateStore:
    """Pause après stop, circuit breaker, plus haut de référence et P&L des trades."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, sims: list[Simulation], now: int) -> None:
        rows = [
            (
                sim.sim_id,
                json.dumps(
                    {
                        "pause_until": sim.pause_until,
                        "breaker_until": sim.breaker_until,
                        "peak_value": str(sim.peak_value),
                        "trade_pnls": [str(p) for p in sim.trade_pnls],
                    }
                ),
                now,
            )
            for sim in sims
        ]
        with transaction(self.conn):
            self.conn.executemany(
                """INSERT INTO sim_state (sim_id, state_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (sim_id) DO UPDATE SET state_json = excluded.state_json,
                       updated_at = excluded.updated_at""",
                rows,
            )

    def restore(self, sim: Simulation) -> None:
        row = self.conn.execute(
            "SELECT state_json FROM sim_state WHERE sim_id = ?", (sim.sim_id,)
        ).fetchone()
        if row is None:
            return
        state = json.loads(row[0])
        sim.pause_until = {k: int(v) for k, v in state["pause_until"].items()}
        sim.breaker_until = state["breaker_until"]
        sim.peak_value = Decimal(state["peak_value"])
        sim.trade_pnls = [Decimal(p) for p in state["trade_pnls"]]


# --- Commandes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingCommand:
    id: int
    command: str


def next_command(conn: sqlite3.Connection) -> PendingCommand | None:
    row = conn.execute(
        "SELECT id, command FROM commands WHERE processed_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return None if row is None else PendingCommand(row["id"], row["command"])


def complete_command(conn: sqlite3.Connection, command_id: int, now: int, result: str) -> None:
    conn.execute(
        "UPDATE commands SET processed_at = ?, result = ? WHERE id = ?", (now, result, command_id)
    )


def request_command(conn: sqlite3.Connection, command: Command, now: int) -> int:
    """Utilisé par le dashboard (et les tests) : dépose une commande pour le moteur."""
    cursor = conn.execute(
        "INSERT INTO commands (command, requested_at) VALUES (?, ?)", (command.value, now)
    )
    return int(cursor.lastrowid or 0)
