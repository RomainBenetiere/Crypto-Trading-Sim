"""Recalibrages (décision 48) : chaque optimisation demandée en cours de live crée une
génération de nouvelles simulations, copies des jumelles optimisées du plan dont seuls les
réglages d'indicateurs sont réoptimisés sur les 12 derniers mois (fenêtre glissante).

Indicatif R2-150 : recalibrage de S-150 (jumelle d'origine) lors de la
2ᵉ optimisation (la 1ʳᵉ est celle du plan). Les recalibrages tournent à 1 000 € depuis leur
démarrage, hors tornade, synthèse, top 5 et production ; au plus `MAX_ACTIVE` générations
tournent en même temps, la plus ancienne est archivée au démarrage d'une nouvelle.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from mcs.sampling import build_config, indicator_rows, sim_ids, typical_indicators
from mcs.storage.db import transaction
from mcs.strategy.serialize import config_to_json

MAX_ACTIVE = 3
PENDING = "kind = 'recalibrated' AND started_at IS NULL AND archived_at IS NULL"
RUNNING = "kind = 'recalibrated' AND started_at IS NOT NULL AND archived_at IS NULL"


class RecalibrationError(RuntimeError):
    """Demande refusée (message en français)."""


def label(generation: int, source_sim_id: int) -> str:
    """« R2-150 » : recalibrage de S-150 lors de la 2ᵉ optimisation."""
    return f"R{generation}-{source_sim_id:03d}"


@dataclass(frozen=True)
class RecalibrationState:
    message: str | None
    pending: int | None  # génération en attente de son optimisation
    active: tuple[int, ...]  # générations qui tournent, de la plus ancienne à la plus récente


def read_state(conn: sqlite3.Connection) -> RecalibrationState:
    message = conn.execute("SELECT recalibration_message FROM engine_state").fetchone()[0]
    pending = conn.execute(f"SELECT MAX(generation) FROM simulations WHERE {PENDING}").fetchone()[0]
    active = tuple(
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT generation FROM simulations WHERE {RUNNING} ORDER BY generation"
        )
    )
    return RecalibrationState(message, pending, active)


def set_message(conn: sqlite3.Connection, message: str, now: int) -> None:
    conn.execute(
        "UPDATE engine_state SET recalibration_message = ?, updated_at = ?", (message, now)
    )


def create_generation(conn: sqlite3.Connection, now: int) -> int:
    """Copie les jumelles optimisées du plan (configuration de base identique, valeurs typiques
    des indicateurs en attendant l'optimisation) en une génération en attente."""
    if read_state(conn).pending is not None:
        raise RecalibrationError("une génération de recalibrage est déjà en préparation.")
    generation = (
        int(
            conn.execute(
                "SELECT COALESCE(MAX(generation), 1) FROM simulations WHERE kind = 'recalibrated'"
            ).fetchone()[0]
        )
        + 1
    )
    copy_plan(conn, now, "recalibrated", generation)
    return generation


def copy_plan(conn: sqlite3.Connection, now: int, kind: str, generation: int | None) -> list[int]:
    """Copie les jumelles optimisées du plan (configuration de base identique, valeurs typiques
    des indicateurs en attendant l'optimisation), en attente de démarrage (`started_at` NULL).
    Retourne les identifiants créés."""
    sources = conn.execute(
        "SELECT id, family, draw_json FROM simulations "
        "WHERE kind = 'plan' AND mode = 'optimized' ORDER BY id"
    ).fetchall()
    if not sources:
        raise RecalibrationError("aucune jumelle optimisée dans le plan.")
    pair = int(conn.execute("SELECT MAX(pair_id) FROM simulations").fetchone()[0])
    rows, params = [], []
    for source in sources:
        draw = json.loads(source["draw_json"])
        config = build_config(draw)
        pair += 1
        sim_id = sim_ids(pair)[1]
        rows.append(
            (
                sim_id,
                pair,
                source["family"],
                config_to_json(config),
                source["draw_json"],
                secrets.randbits(31),  # nouveaux essais de l'hypercube à chaque génération
                now,
                generation,
                source["id"],
            )
        )
        params.extend(indicator_rows(sim_id, config, typical_indicators(draw)))
    with transaction(conn):
        conn.executemany(
            """INSERT INTO simulations (id, pair_id, mode, family, config_json, draw_json, seed,
                   created_at, kind, generation, source_sim_id)
               VALUES (?, ?, 'optimized', ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*row[:7], kind, *row[7:]) for row in rows],
        )
        conn.executemany(
            """INSERT INTO indicator_params (sim_id, role, indicator, params_json,
                   typical_params_json, weight, score_threshold) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
    return [row[0] for row in rows]


def pending_ready(conn: sqlite3.Connection) -> bool:
    """Toutes les simulations de la génération en attente sont optimisées."""
    return not conn.execute(
        f"SELECT COUNT(*) FROM simulations WHERE {PENDING} AND optimized_at IS NULL"
    ).fetchone()[0]


def discard_pending(conn: sqlite3.Connection) -> None:
    conn.execute(f"DELETE FROM simulations WHERE {PENDING}")


def start_generation(
    conn: sqlite3.Connection, now: int, bh_value: Decimal | None
) -> tuple[list[int], list[int]]:
    """Démarre la génération en attente et archive les plus anciennes au-delà de `MAX_ACTIVE`.
    Retourne (simulations archivées, simulations démarrées)."""
    started = [r[0] for r in conn.execute(f"SELECT id FROM simulations WHERE {PENDING}")]
    active = read_state(conn).active
    old = active[: max(len(active) - (MAX_ACTIVE - 1), 0)]
    archived = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM simulations WHERE {RUNNING} AND generation IN "
            f"({','.join(map(str, old)) or 'NULL'})"
        )
    ]
    with transaction(conn):
        conn.execute(
            f"UPDATE simulations SET archived_at = ? WHERE id IN ({','.join(map(str, archived))})",
            (now,),
        )
        conn.execute(
            f"UPDATE simulations SET started_at = ?, bh_start_value = ? WHERE {PENDING}",
            (now, bh_value),
        )
    return archived, started


def delete_all(conn: sqlite3.Connection) -> None:
    """Remise à zéro des simulations : tous les recalibrages disparaissent."""
    conn.execute("DELETE FROM simulations WHERE kind = 'recalibrated'")
    conn.execute("UPDATE engine_state SET recalibration_message = NULL")
