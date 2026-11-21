"""Série RA (décision 49) : 150 simulations, copies des jumelles optimisées du plan, dont les
réglages d'indicateurs sont réoptimisés chaque vendredi à 21 h 05 (heure de Paris), sans
générations ni archives : mêmes numéros, portefeuille et P&L continus.

Indicatif RA-150 : version auto-recalibrée de S-150. Optimisation comme les recalibrages
(12 derniers mois coupés 70/30, moyenne apprentissage / validation, panier figé). Les
nouveaux réglages s'appliquent dès la fin de l'optimisation : positions conservées, stops
jamais abaissés, décisions à la clôture 4 h suivante. Une RA choisie en production y suit
ses réoptimisations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from mcs.recalibration import copy_plan
from mcs.storage.db import transaction

ALL = "kind = 'adaptive'"
PARIS = ZoneInfo("Europe/Paris")
SLOT_WEEKDAY = 4  # vendredi
SLOT_TIME = time(21, 5)  # après la clôture 4 h de 21 h (heure de Paris)


class AdaptiveError(RuntimeError):
    """Demande refusée (message en français)."""


def label(source_sim_id: int) -> str:
    """« RA-150 » : version auto-recalibrée de S-150."""
    return f"RA-{source_sim_id:03d}"


def last_slot(now: int) -> int:
    """Dernier vendredi 21 h 05 (heure de Paris) passé ou en cours, en ms."""
    local = datetime.fromtimestamp(now / 1000, PARIS)
    day = local.date() - timedelta(days=(local.weekday() - SLOT_WEEKDAY) % 7)
    slot = datetime.combine(day, SLOT_TIME, PARIS)
    if slot > local:
        slot = datetime.combine(day - timedelta(days=7), SLOT_TIME, PARIS)
    return int(slot.timestamp() * 1000)


def next_slot(now: int) -> int:
    local = datetime.fromtimestamp(last_slot(now) / 1000, PARIS)
    return int(
        datetime.combine(local.date() + timedelta(days=7), SLOT_TIME, PARIS).timestamp() * 1000
    )


def outdated_clause() -> str:
    """Simulations de la série en attente d'optimisation (création ou réoptimisation en
    cours : `begin_run` remet `optimized_at` à NULL)."""
    return f"{ALL} AND optimized_at IS NULL"


@dataclass(frozen=True)
class AdaptiveState:
    count: int
    started_at: int | None
    run_at: int | None  # début de la dernière réoptimisation hebdomadaire
    applied_at: int | None  # réoptimisation dont les réglages sont appliqués par le moteur
    outdated: int  # simulations pas encore optimisées pour la création ou `run_at`
    message: str | None

    @property
    def exists(self) -> bool:
        return self.count > 0

    @property
    def pending_start(self) -> bool:
        """Créée, en attente de sa première optimisation puis de son démarrage."""
        return self.exists and self.started_at is None

    @property
    def running(self) -> bool:
        """Réoptimisation hebdomadaire en cours (une réoptimisation interrompue en échec est
        marquée appliquée : on attend alors le vendredi suivant)."""
        return (
            self.started_at is not None
            and self.run_at is not None
            and self.outdated > 0
            and self.applied_at != self.run_at
        )

    @property
    def to_apply(self) -> bool:
        return (
            self.started_at is not None
            and self.run_at is not None
            and self.outdated == 0
            and (self.applied_at is None or self.applied_at < self.run_at)
        )

    def due(self, now: int) -> bool:
        """Réoptimisation du vendredi à lancer (rattrapée après un arrêt du moteur)."""
        if self.started_at is None or self.running or self.to_apply:
            return False
        return max(self.run_at or 0, self.started_at) < last_slot(now)


def read_state(conn: sqlite3.Connection) -> AdaptiveState:
    row = conn.execute(
        "SELECT adaptive_run_at, adaptive_applied_at, adaptive_message FROM engine_state"
    ).fetchone()
    count, started = conn.execute(
        f"SELECT COUNT(*), MIN(started_at) FROM simulations WHERE {ALL}"
    ).fetchone()
    outdated = conn.execute(
        f"SELECT COUNT(*) FROM simulations WHERE {outdated_clause()}"
    ).fetchone()[0]
    return AdaptiveState(
        count,
        started,
        row["adaptive_run_at"],
        row["adaptive_applied_at"],
        outdated,
        row["adaptive_message"],
    )


def create_series(conn: sqlite3.Connection, now: int) -> list[int]:
    status = conn.execute("SELECT status FROM engine_state").fetchone()[0]
    if status == "NOT_STARTED":
        raise AdaptiveError("lancez d'abord les simulations.")
    if read_state(conn).exists:
        raise AdaptiveError("la série RA existe déjà.")
    ids = copy_plan(conn, now, "adaptive", None)
    conn.execute(
        "UPDATE engine_state SET adaptive_run_at = NULL, adaptive_applied_at = NULL, "
        "adaptive_message = ?, updated_at = ?",
        ("Série RA créée : première optimisation en cours.", now),
    )
    return ids


def label_of(conn: sqlite3.Connection, sim_id: int) -> str:
    row = conn.execute("SELECT source_sim_id FROM simulations WHERE id = ?", (sim_id,)).fetchone()
    return label(row[0]) if row and row[0] is not None else f"S-{sim_id:03d}"


def ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute(f"SELECT id FROM simulations WHERE {ALL} ORDER BY id")]


def start_series(conn: sqlite3.Connection, now: int, bh_value: Decimal | None) -> list[int]:
    started = ids(conn)
    with transaction(conn):
        conn.execute(
            f"UPDATE simulations SET started_at = ?, bh_start_value = ? WHERE {ALL}",
            (now, bh_value),
        )
    return started


def begin_run(conn: sqlite3.Connection, now: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE engine_state SET adaptive_run_at = ?, updated_at = ?", (now, now))
        conn.execute(f"UPDATE simulations SET optimized_at = NULL WHERE {ALL}")


def mark_applied(conn: sqlite3.Connection, run_at: int, message: str, now: int) -> None:
    conn.execute(
        "UPDATE engine_state SET adaptive_applied_at = ?, adaptive_message = ?, updated_at = ?",
        (run_at, message, now),
    )


def set_message(conn: sqlite3.Connection, message: str, now: int) -> None:
    conn.execute("UPDATE engine_state SET adaptive_message = ?, updated_at = ?", (message, now))


def delete_all(conn: sqlite3.Connection) -> None:
    """Remise à zéro des simulations, ou échec de la première optimisation."""
    conn.execute(f"DELETE FROM simulations WHERE {ALL}")
    conn.execute(
        "UPDATE engine_state SET adaptive_run_at = NULL, adaptive_applied_at = NULL, "
        "adaptive_message = NULL"
    )


def main(argv: list[str] | None = None) -> None:
    """python -m mcs.adaptive create | status"""
    import argparse

    from mcs.config import load_settings
    from mcs.storage.db import connect, now_ms

    parser = argparse.ArgumentParser(prog="python -m mcs.adaptive")
    parser.add_argument("command", choices=("create", "status"))
    args = parser.parse_args(argv)
    settings = load_settings()
    conn = connect(settings.db_path, settings.journal_mode)
    try:
        if args.command == "create":
            created = create_series(conn, now_ms())
            print(f"Série RA créée ({len(created)} simulations) : optimisation par le moteur.")
            return
        state = read_state(conn)
        print(f"Simulations : {state.count}, démarrée : {state.started_at is not None}")
        print(f"Réoptimisation en cours : {state.running} ({state.outdated} restantes)")
        print(f"Dernier événement : {state.message or '—'}")
    except AdaptiveError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        conn.close()


if __name__ == "__main__":
    main()
