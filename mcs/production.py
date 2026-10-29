"""Mode production (SPEC §15) : choix du scénario (palier 1) et mode ombre (palier 2), sans
aucun ordre réel.

Le scénario choisi parmi les meilleures simulations est figé dans `production_choices`
(copie de sa configuration) ; un nouveau choix remplace le précédent, l'historique est gardé.
Les montants réels sont ceux de la simulation mis à l'échelle du capital engagé, relevés au
minimum d'ordre du marché (5 € sur Bitvavo) quand l'échelle les fait passer en dessous.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from mcs.metrics import INITIAL_CAPITAL, METRICS
from mcs.storage.db import transaction

DEFAULT_CAPITAL = Decimal(300)
# Identifiant du portefeuille de production dans les tables `production_*` (colonne sim_id).
PRODUCTION_SIM_ID = 0
# Minimum d'ordre courant de Bitvavo, pour l'affichage ; les ordres utiliseront celui de chaque
# marché (`markets.min_order_quote`).
DISPLAY_MIN_ORDER = Decimal(5)
CENT = Decimal("0.01")


class Mode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"


MODE_LABELS = {Mode.OFF: "Arrêté", Mode.SHADOW: "Ombre (aucun ordre envoyé)"}


@dataclass(frozen=True, slots=True)
class ProductionState:
    mode: Mode
    started_at: int | None
    capital_applied: Decimal | None
    balances: dict[str, tuple[Decimal, Decimal]] | None  # symbole → (disponible, en ordre)
    balance_at: int | None
    balance_error: str | None


def read_state(conn: sqlite3.Connection) -> ProductionState:
    row = conn.execute("SELECT * FROM production_state WHERE id = 1").fetchone()
    balances = None
    if row["balance_json"] is not None:
        balances = {
            symbol: (Decimal(available), Decimal(in_order))
            for symbol, (available, in_order) in json.loads(row["balance_json"]).items()
        }
    return ProductionState(
        mode=Mode(row["mode"]),
        started_at=row["started_at"],
        capital_applied=_dec(row["capital_applied"]),
        balances=balances,
        balance_at=row["balance_at"],
        balance_error=row["balance_error"],
    )


def set_mode(conn: sqlite3.Connection, mode: Mode, now: int) -> None:
    """Écrit par le dashboard ; le moteur applique le changement dans les 2 s."""
    if mode is Mode.SHADOW and active_choice(conn) is None:
        raise ValueError("Choisissez d'abord un scénario.")
    conn.execute(
        """UPDATE production_state SET mode = ?, updated_at = ?,
               started_at = CASE WHEN ? = 'off' THEN NULL ELSE COALESCE(started_at, ?) END
           WHERE id = 1""",
        (mode, now, mode, now),
    )


def set_capital_applied(conn: sqlite3.Connection, capital: Decimal, now: int) -> None:
    conn.execute(
        "UPDATE production_state SET capital_applied = ?, updated_at = ? WHERE id = 1",
        (capital, now),
    )


def record_balances(
    conn: sqlite3.Connection,
    balances: dict[str, tuple[Decimal, Decimal]] | None,
    error: str | None,
    now: int,
) -> None:
    """Dernier solde réel lu (montants seulement, jamais la clé) ou erreur de lecture."""
    if balances is None:
        conn.execute(
            "UPDATE production_state SET balance_error = ?, updated_at = ? WHERE id = 1",
            (error, now),
        )
        return
    payload = json.dumps({s: [str(a), str(o)] for s, (a, o) in sorted(balances.items())})
    conn.execute(
        """UPDATE production_state SET balance_json = ?, balance_at = ?, balance_error = NULL,
               updated_at = ? WHERE id = 1""",
        (payload, now, now),
    )


@dataclass(frozen=True, slots=True)
class Choice:
    id: int
    sim_id: int
    mode: str
    family: str
    config_json: str
    draw_json: str
    metric: str
    pnl_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    closed_trades: int | None
    capital: Decimal
    scale: Decimal
    chosen_at: int
    replaced_at: int | None


def scale_factor(capital: Decimal) -> Decimal:
    """Rapport capital engagé ÷ capital d'une simulation (300 € → 0,3)."""
    return capital / INITIAL_CAPITAL


def scaled_quote(sim_amount: Decimal, scale: Decimal, min_quote: Decimal) -> Decimal:
    """Montant réel d'un ordre de la simulation : mis à l'échelle, arrondi au centime
    inférieur, relevé au minimum d'ordre du marché s'il passe en dessous."""
    scaled = (sim_amount * scale).quantize(CENT, rounding=ROUND_DOWN)
    return max(scaled, min_quote)


def choose(
    conn: sqlite3.Connection, sim_id: int, capital: Decimal, metric: str, now: int
) -> Choice:
    """Fige la configuration de `sim_id` comme scénario de production. Les positions réelles
    éventuelles restent ouvertes et seront gérées avec les paramètres du nouveau scénario."""
    if metric not in METRICS:
        raise ValueError(f"Classement inconnu : {metric}")
    if capital <= 0:
        raise ValueError("Le capital engagé doit être positif.")
    with transaction(conn):
        sim = conn.execute(
            """SELECT s.id, s.mode, s.family, s.config_json, s.draw_json, m.pnl_pct,
                      m.max_drawdown_pct, m.closed_trades
               FROM simulations s LEFT JOIN metrics m ON m.sim_id = s.id WHERE s.id = ?""",
            (sim_id,),
        ).fetchone()
        if sim is None:
            raise ValueError(f"Simulation inconnue : {sim_id}")
        conn.execute(
            "UPDATE production_choices SET replaced_at = ? WHERE replaced_at IS NULL", (now,)
        )
        cursor = conn.execute(
            """INSERT INTO production_choices (sim_id, mode, family, config_json, draw_json,
                   metric, pnl_pct, max_drawdown_pct, closed_trades, capital, scale, chosen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sim["id"],
                sim["mode"],
                sim["family"],
                sim["config_json"],
                sim["draw_json"],
                metric,
                sim["pnl_pct"],
                sim["max_drawdown_pct"],
                sim["closed_trades"],
                capital,
                scale_factor(capital),
                now,
            ),
        )
    choice = _load(conn, "WHERE id = ?", (cursor.lastrowid,))
    assert choice
    return choice[0]


def active_choice(conn: sqlite3.Connection) -> Choice | None:
    choices = _load(conn, "WHERE replaced_at IS NULL ORDER BY id DESC LIMIT 1", ())
    return choices[0] if choices else None


def history(conn: sqlite3.Connection) -> list[Choice]:
    """Choix successifs, le plus récent d'abord."""
    return _load(conn, "ORDER BY id DESC", ())


def _load(conn: sqlite3.Connection, clause: str, args: tuple[object, ...]) -> list[Choice]:
    return [
        Choice(
            id=r["id"],
            sim_id=r["sim_id"],
            mode=r["mode"],
            family=r["family"],
            config_json=r["config_json"],
            draw_json=r["draw_json"],
            metric=r["metric"],
            pnl_pct=_dec(r["pnl_pct"]),
            max_drawdown_pct=_dec(r["max_drawdown_pct"]),
            closed_trades=r["closed_trades"],
            capital=Decimal(r["capital"]),
            scale=Decimal(r["scale"]),
            chosen_at=r["chosen_at"],
            replaced_at=r["replaced_at"],
        )
        for r in conn.execute(f"SELECT * FROM production_choices {clause}", args)
    ]


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
