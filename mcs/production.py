"""Mode production (SPEC §15) : choix du scénario (palier 1), mode ombre (palier 2) et état
du mode réel (palier 3 ; les ordres sont envoyés par `mcs/broker/live.py`).

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
    reset_requested_at: int | None = None


SHADOW_TABLES = (
    "production_orders",
    "production_positions",
    "production_sim_state",
    "production_values",
)


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
        reset_requested_at=row["shadow_reset_requested_at"],
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


def request_shadow_reset(conn: sqlite3.Connection, now: int) -> None:
    """Écrit par le dashboard : l'ombre doit être arrêtée ; le moteur efface dans les 2 s."""
    if read_state(conn).mode is not Mode.OFF:
        raise ValueError("Arrêtez d'abord l'ombre.")
    conn.execute(
        "UPDATE production_state SET shadow_reset_requested_at = ?, updated_at = ? WHERE id = 1",
        (now, now),
    )


def reset_shadow(conn: sqlite3.Connection, now: int) -> None:
    """Appliqué par le moteur, ombre en pause : portefeuille ombre effacé (ordres, positions,
    état, valeurs). Il repartira du capital engagé au prochain démarrage."""
    with transaction(conn):
        for table in SHADOW_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            """UPDATE production_state SET shadow_reset_requested_at = NULL,
                   capital_applied = NULL, updated_at = ? WHERE id = 1""",
            (now,),
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


class LiveStatus(StrEnum):
    OFF = "off"
    ON = "on"
    WINDING_DOWN = "winding_down"  # plus d'achat ; positions gérées jusqu'à leur clôture


LIVE_LABELS = {
    LiveStatus.OFF: "Arrêté",
    LiveStatus.ON: "Ordres réels actifs",
    LiveStatus.WINDING_DOWN: "Liquidation progressive (plus d'achat)",
}
# Perte maximale du jour (décision 45) : au-delà, achats bloqués jusqu'au lendemain.
DAILY_LOSS_LIMIT = Decimal("0.10")
LIVE_CONFIRMATION = "ORDRES RÉELS"


@dataclass(frozen=True, slots=True)
class LiveState:
    status: LiveStatus
    started_at: int | None
    armed: bool  # double verrou du `.env` levé (écrit par le moteur)
    max_capital: Decimal | None
    capital_applied: Decimal | None
    day: str | None
    day_start_value: Decimal | None
    buys_blocked_day: str | None
    emergency_requested_at: int | None
    last_event: str | None
    updated_at: int


def read_live(conn: sqlite3.Connection) -> LiveState:
    row = conn.execute("SELECT * FROM live_state WHERE id = 1").fetchone()
    return LiveState(
        status=LiveStatus(row["status"]),
        started_at=row["started_at"],
        armed=bool(row["armed"]),
        max_capital=_dec(row["max_capital"]),
        capital_applied=_dec(row["capital_applied"]),
        day=row["day"],
        day_start_value=_dec(row["day_start_value"]),
        buys_blocked_day=row["buys_blocked_day"],
        emergency_requested_at=row["emergency_requested_at"],
        last_event=row["last_event"],
        updated_at=row["updated_at"],
    )


def set_live_armed(
    conn: sqlite3.Connection, armed: bool, max_capital: Decimal | None, now: int
) -> None:
    """Écrit par le moteur au démarrage, d'après le `.env` (le dashboard n'y a pas accès)."""
    conn.execute(
        "UPDATE live_state SET armed = ?, max_capital = ?, updated_at = ? WHERE id = 1",
        (int(armed), max_capital, now),
    )


def start_live(conn: sqlite3.Connection, confirmation: str, now: int) -> None:
    """Demande du dashboard : démarre les ordres réels (double verrou et plafond vérifiés)."""
    live = read_live(conn)
    choice = active_choice(conn)
    if confirmation.strip() != LIVE_CONFIRMATION:
        raise ValueError(f"Saisissez « {LIVE_CONFIRMATION} » pour confirmer.")
    if not live.armed or live.max_capital is None:
        raise ValueError(
            "Mode réel verrouillé : PRODUCTION_LIVE=1, PRODUCTION_MAX_CAPITAL et une clé "
            "« Trader » sont requis dans .env, moteur recréé."
        )
    if choice is None:
        raise ValueError("Choisissez d'abord un scénario.")
    engine = conn.execute("SELECT status FROM engine_state WHERE id = 1").fetchone()
    if engine is None or engine["status"] == "NOT_STARTED":
        raise ValueError("Lancez d'abord les simulations : le mode réel suit le moteur.")
    if choice.capital > live.max_capital:
        raise ValueError(
            f"Capital engagé ({choice.capital} €) supérieur au plafond PRODUCTION_MAX_CAPITAL "
            f"({live.max_capital} €)."
        )
    if live.emergency_requested_at is not None:
        raise ValueError("Arrêt d'urgence en cours.")
    with transaction(conn):
        conn.execute(
            """UPDATE live_state SET status = 'on', started_at = COALESCE(started_at, ?),
                   last_event = ?, updated_at = ? WHERE id = 1""",
            (now, "Démarrage demandé depuis le dashboard.", now),
        )
        # L'ombre tourne en parallèle, pour comparer exécution réelle et simulée.
        conn.execute(
            """UPDATE production_state SET mode = 'shadow', started_at = COALESCE(started_at, ?),
                   updated_at = ? WHERE id = 1""",
            (now, now),
        )


def wind_down_live(conn: sqlite3.Connection, now: int) -> None:
    """Arrêt normal : plus d'achat, positions gérées jusqu'à leur clôture."""
    conn.execute(
        """UPDATE live_state SET status = 'winding_down', last_event = ?, updated_at = ?
           WHERE id = 1 AND status = 'on'""",
        ("Arrêt progressif demandé : plus d'achat.", now),
    )


def request_emergency(conn: sqlite3.Connection, now: int) -> None:
    """Arrêt d'urgence : le moteur annule les ordres, vend les positions du moteur au marché
    et coupe le mode réel."""
    conn.execute(
        """UPDATE live_state SET emergency_requested_at = ?, last_event = ?, updated_at = ?
           WHERE id = 1 AND status != 'off'""",
        (now, "Arrêt d'urgence demandé.", now),
    )


def set_live_status(conn: sqlite3.Connection, status: LiveStatus, event: str, now: int) -> None:
    """Écrit par le moteur (fin de liquidation, arrêt d'urgence exécuté)."""
    conn.execute(
        """UPDATE live_state SET status = ?, last_event = ?, updated_at = ?,
               started_at = CASE WHEN ? = 'off' THEN NULL ELSE started_at END,
               emergency_requested_at = CASE WHEN ? = 'off' THEN NULL
                                             ELSE emergency_requested_at END
           WHERE id = 1""",
        (status, event, now, status, status),
    )


def record_live_event(conn: sqlite3.Connection, event: str, now: int) -> None:
    conn.execute("UPDATE live_state SET last_event = ?, updated_at = ? WHERE id = 1", (event, now))


def set_live_capital_applied(conn: sqlite3.Connection, capital: Decimal, now: int) -> None:
    conn.execute(
        "UPDATE live_state SET capital_applied = ?, updated_at = ? WHERE id = 1", (capital, now)
    )


def update_live_day(conn: sqlite3.Connection, day: str, value: Decimal, now: int) -> LiveState:
    """Suivi de la perte du jour : valeur de départ à chaque nouveau jour (Europe/Paris) ;
    achats bloqués jusqu'au lendemain si la valeur perd `DAILY_LOSS_LIMIT` ou plus."""
    live = read_live(conn)
    if live.day != day or live.day_start_value is None:
        conn.execute(
            "UPDATE live_state SET day = ?, day_start_value = ?, updated_at = ? WHERE id = 1",
            (day, value, now),
        )
    elif live.buys_blocked_day != day and value <= live.day_start_value * (1 - DAILY_LOSS_LIMIT):
        conn.execute(
            """UPDATE live_state SET buys_blocked_day = ?, last_event = ?, updated_at = ?
               WHERE id = 1""",
            (
                day,
                f"Perte du jour ≥ {DAILY_LOSS_LIMIT * 100:.0f} % ({live.day_start_value:.2f} € → "
                f"{value:.2f} €) : achats bloqués jusqu'à demain.",
                now,
            ),
        )
    return read_live(conn)


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
