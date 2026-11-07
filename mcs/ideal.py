"""Simulation « idéale » déduite de la tornade : pour chaque variable, la modalité (ou le tiers de
valeurs) au meilleur P&L ou au meilleur ratio de Calmar moyen.

Même découpage que la tornade du dashboard (`mcs.metrics.tornado`) : options pour les variables
catégorielles, tiers bas / milieu / haut pour les numériques, sur les seules simulations où la
variable existe (ex. type de trailing seulement si un trailing est tiré). Les variables sont
choisies dans l'ordre du catalogue, si bien qu'une variable conditionnelle n'est retenue que si
sa condition est remplie par les choix déjà faits.

La commande est en lecture seule. Le moteur s'en sert aussi pour créer des générations de
simulations idéales (décision 46) : jumelles typique / optimisée pour chaque objectif, qui
tournent à côté des 300 depuis leur démarrage. Les effets sont marginaux (une variable à la
fois, sans interaction) : c'est une hypothèse à valider, pas un optimum garanti.

python -m mcs.ideal                   # P&L et ratio de Calmar
python -m mcs.ideal --json ideal.json # tirages retenus, au format `draw_json`
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sqlite3
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from mcs.config import load_settings
from mcs.metrics import (
    METRICS,
    MIN_TRADES_FOR_TORNADO,
    OPTION_LABELS,
    VARIABLE_LABELS,
    SimRecord,
    load_records,
    records_for,
)
from mcs.sampling import (
    CATEGORICALS,
    NUMERICS,
    PENDING_IDEAL,
    Categorical,
    Draw,
    Numeric,
    build_config,
    indicator_rows,
    sim_ids,
    typical_indicators,
)
from mcs.storage.db import connect, transaction
from mcs.strategy.serialize import config_to_json


@dataclass(frozen=True)
class Group:
    label: str
    mean: float
    se: float
    n: int


@dataclass(frozen=True)
class Choice:
    variable: str
    value: object  # valeur retenue (None : variable sans objet)
    best: Group | None
    worst: Group | None

    @property
    def significant(self) -> bool:
        """Écart meilleur / pire groupe supérieur à deux erreurs-types (comme la tornade)."""
        if self.best is None or self.worst is None:
            return False
        return abs(self.best.mean - self.worst.mean) > 2 * math.hypot(self.best.se, self.worst.se)

    @property
    def amplitude(self) -> float:
        if self.best is None or self.worst is None:
            return 0.0
        return self.best.mean - self.worst.mean


@dataclass(frozen=True)
class Ideal:
    metric: str
    draw: Draw
    choices: list[Choice]
    calibration: Choice | None
    neighbours: list[tuple[SimRecord, int]]  # (simulation, nombre de différences)


def _group(label: str, values: Sequence[float]) -> Group:
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else math.inf
    return Group(label, statistics.fmean(values), se, len(values))


def _categorical(cat: Categorical, records: Sequence[SimRecord]) -> Choice:
    groups: dict[str, list[float]] = {}
    for r in records:
        value = r.draw.get(cat.name)
        if value is not None:
            groups.setdefault(str(value), []).append(r.pnl)
    ranked = sorted((_group(k, v) for k, v in groups.items() if len(v) >= 2), key=lambda g: g.mean)
    if not ranked:
        return Choice(cat.name, None, None, None)
    return Choice(cat.name, ranked[-1].label, ranked[-1], ranked[0])


def _numeric(var: Numeric, records: Sequence[SimRecord]) -> Choice:
    points = sorted(
        ((float(str(v)), r.pnl) for r in records if (v := r.draw.get(var.name)) is not None),
        key=lambda p: p[0],
    )
    third = len(points) // 3
    if third < 2:
        return Choice(var.name, None, None, None)
    tiers = [points[:third], points[third:-third], points[-third:]]
    groups = [
        (
            _group(f"{name} ({_num(t[0][0])} à {_num(t[-1][0])})", [p for _, p in t]),
            statistics.median(x for x, _ in t),
        )
        for name, t in zip(("Tiers bas", "Tiers milieu", "Tiers haut"), tiers, strict=True)
    ]
    best = max(groups, key=lambda g: g[0].mean)
    worst = min(groups, key=lambda g: g[0].mean)
    value: float | int = round(best[1]) if var.integer else round(best[1], var.decimals)
    return Choice(var.name, value, best[0], worst[0])


def _num(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _calibration(records: Sequence[SimRecord]) -> Choice | None:
    by_mode: dict[str, list[float]] = {}
    for r in records:
        by_mode.setdefault(r.mode, []).append(r.pnl)
    groups = sorted((_group(m, v) for m, v in by_mode.items() if len(v) >= 2), key=lambda g: g.mean)
    if len(groups) < 2:
        return None
    return Choice("calibration", groups[-1].label, groups[-1], groups[0])


def _fallback(var: Categorical | Numeric, records: Sequence[SimRecord]) -> object:
    """Variable applicable mais trop peu tirée pour comparer : option la plus fréquente, ou
    médiane des valeurs observées (milieu de la plage à défaut)."""
    seen = [v for r in records if (v := r.draw.get(var.name)) is not None]
    if isinstance(var, Categorical):
        return Counter(map(str, seen)).most_common(1)[0][0] if seen else var.options[0]
    middle = statistics.median(float(str(v)) for v in seen) if seen else (var.low + var.high) / 2
    return round(middle) if var.integer else round(middle, var.decimals)


def ideal(records: Sequence[SimRecord], metric: str, neighbours: int = 5) -> Ideal:
    chosen = records_for(records, metric)
    draw: Draw = {}
    choices: list[Choice] = []
    for cat in CATEGORICALS:
        if not cat.when.test(draw):
            draw[cat.name] = None
            continue
        choice = _categorical(cat, chosen)
        draw[cat.name] = choice.value if choice.value is not None else _fallback(cat, chosen)
        choices.append(choice)
    for var in NUMERICS:
        if not var.when.test(draw):
            draw[var.name] = None
            continue
        choice = _numeric(var, chosen)
        draw[var.name] = choice.value if choice.value is not None else _fallback(var, chosen)
        choices.append(choice)
    categorical = [c.name for c in CATEGORICALS]
    distance = [
        (r, sum(1 for name in categorical if r.draw.get(name) != draw[name])) for r in chosen
    ]
    closest = sorted(distance, key=lambda d: (d[1], -d[0].pnl))[:neighbours]
    return Ideal(metric, draw, choices, _calibration(chosen), closest)


# --- Générations de simulations idéales (décision 46) ------------------------------------


class IdealError(RuntimeError):
    """Demande refusée (message en français, affiché dans le dashboard)."""


@dataclass(frozen=True)
class IdealState:
    requested_at: int | None
    message: str | None
    pending: int | None  # génération en attente de son optimisation
    current: int | None  # génération qui tourne


def read_ideal_state(conn: sqlite3.Connection) -> IdealState:
    row = conn.execute("SELECT ideal_requested_at, ideal_message FROM engine_state").fetchone()
    pending = conn.execute(
        f"SELECT MAX(generation) FROM simulations WHERE {PENDING_IDEAL}"
    ).fetchone()[0]
    current = conn.execute(
        "SELECT MAX(generation) FROM simulations WHERE kind = 'ideal' "
        "AND started_at IS NOT NULL AND archived_at IS NULL"
    ).fetchone()[0]
    return IdealState(row["ideal_requested_at"], row["ideal_message"], pending, current)


def request_generation(conn: sqlite3.Connection, now: int) -> None:
    """Écrit par le dashboard ; le moteur crée la génération dans les 2 s."""
    status = conn.execute("SELECT status FROM engine_state").fetchone()[0]
    if status == "NOT_STARTED":
        raise IdealError("Lancez d'abord les simulations.")
    state = read_ideal_state(conn)
    if state.requested_at is not None or state.pending is not None:
        raise IdealError("Une génération est déjà en préparation.")
    conn.execute("UPDATE engine_state SET ideal_requested_at = ?, updated_at = ?", (now, now))


def finish_request(conn: sqlite3.Connection, message: str, now: int) -> None:
    conn.execute(
        """UPDATE engine_state SET ideal_requested_at = NULL, ideal_message = ?, updated_at = ?""",
        (message, now),
    )


def create_generation(conn: sqlite3.Connection, now: int) -> int:
    """Calcule les simulations idéales P&L et Calmar sur les métriques du plan et les enregistre
    en jumelles typique / optimisée, en attente (`started_at` NULL) de l'optimisation."""
    records = load_records(conn)
    if not records or statistics.fmean(r.trades for r in records) < MIN_TRADES_FOR_TORNADO:
        raise IdealError(
            f"il faut en moyenne {MIN_TRADES_FOR_TORNADO} trades clôturés par simulation."
        )
    if sum(1 for r in records if r.calmar is not None) < 2 * 3:
        raise IdealError("trop peu de simulations ont un ratio de Calmar.")
    generation = (
        int(conn.execute("SELECT COALESCE(MAX(generation), 0) FROM simulations").fetchone()[0]) + 1
    )
    pair = conn.execute("SELECT MAX(pair_id) FROM simulations").fetchone()[0]
    rows, params = [], []
    for objective in METRICS:
        draw = ideal(records, objective, neighbours=0).draw
        config = build_config(draw)
        typical = typical_indicators(draw)
        pair += 1
        for sim_id, mode in zip(sim_ids(pair), ("typical", "optimized"), strict=True):
            rows.append(
                (
                    sim_id,
                    pair,
                    mode,
                    draw["family"],
                    config_to_json(config),
                    json.dumps(draw, sort_keys=True),
                    secrets.randbits(31),
                    now,
                    objective,
                    generation,
                )
            )
            params.extend(indicator_rows(sim_id, config, typical))
    with transaction(conn):
        conn.executemany(
            """INSERT INTO simulations (id, pair_id, mode, family, config_json, draw_json, seed,
                   created_at, kind, objective, generation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ideal', ?, ?)""",
            rows,
        )
        conn.executemany(
            """INSERT INTO indicator_params (sim_id, role, indicator, params_json,
                   typical_params_json, weight, score_threshold) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
    return generation


def pending_ready(conn: sqlite3.Connection) -> bool:
    """Toutes les jumelles optimisées de la génération en attente sont optimisées."""
    return not conn.execute(
        f"SELECT COUNT(*) FROM simulations WHERE {PENDING_IDEAL} "
        "AND mode = 'optimized' AND optimized_at IS NULL"
    ).fetchone()[0]


def discard_pending(conn: sqlite3.Connection) -> None:
    conn.execute(f"DELETE FROM simulations WHERE {PENDING_IDEAL}")


def start_generation(
    conn: sqlite3.Connection, now: int, bh_value: Decimal | None
) -> tuple[list[int], list[int]]:
    """Archive la génération en cours et démarre celle en attente.
    Retourne (simulations archivées, simulations démarrées)."""
    archived = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM simulations WHERE kind = 'ideal' "
            "AND started_at IS NOT NULL AND archived_at IS NULL"
        )
    ]
    started = [r[0] for r in conn.execute(f"SELECT id FROM simulations WHERE {PENDING_IDEAL}")]
    with transaction(conn):
        conn.execute(
            "UPDATE simulations SET archived_at = ? WHERE kind = 'ideal' "
            "AND started_at IS NOT NULL AND archived_at IS NULL",
            (now,),
        )
        conn.execute(
            f"UPDATE simulations SET started_at = ?, bh_start_value = ? WHERE {PENDING_IDEAL}",
            (now, bh_value),
        )
    return archived, started


def delete_all(conn: sqlite3.Connection) -> None:
    """Remise à zéro des simulations : les idéales disparaissent avec les données dont elles
    sont tirées (ordres, positions et valeurs supprimés en cascade)."""
    conn.execute("DELETE FROM simulations WHERE kind = 'ideal'")
    conn.execute("UPDATE engine_state SET ideal_requested_at = NULL, ideal_message = NULL")


# --- Rapport --------------------------------------------------------------------------------


def _value(choice: Choice) -> str:
    if choice.value is None:
        return "—"
    if isinstance(choice.value, str):
        return OPTION_LABELS.get(choice.value, choice.value)
    return _num(float(str(choice.value)))


MODE_LABELS = {"typical": "Typique", "optimized": "Optimisé"}


def _label(key: str) -> str:
    return MODE_LABELS.get(key) or OPTION_LABELS.get(key, key)


def report(result: Ideal) -> str:
    unit = "€" if result.metric == "pnl" else ""
    lines = [f"## Simulation idéale — {METRICS[result.metric]}", ""]
    lines.append("| Variable | Valeur retenue | Moyenne | Pire groupe | Écart | Significatif |")
    lines.append("|---|---|---|---|---|---|")
    ordered = sorted(result.choices, key=lambda c: c.amplitude, reverse=True)
    for c in ordered:
        if c.best is None or c.worst is None:
            continue
        lines.append(
            f"| {VARIABLE_LABELS[c.variable]} | {_value(c)} (n={c.best.n}) "
            f"| {c.best.mean:+.2f} {unit} | {_label(c.worst.label)} : {c.worst.mean:+.2f} {unit} "
            f"| {c.amplitude:.2f} | {'oui' if c.significant else 'non'} |"
        )
    if result.calibration is not None:
        cal = result.calibration
        assert cal.best is not None and cal.worst is not None
        lines += [
            "",
            f"Calibrage des indicateurs : {_label(cal.best.label)} ({cal.best.mean:+.2f} {unit}) "
            f"contre {_label(cal.worst.label)} ({cal.worst.mean:+.2f} {unit}), "
            f"{'significatif' if cal.significant else 'non significatif'}.",
        ]
    lines += ["", "Simulations existantes les plus proches (variables catégorielles) :", ""]
    for r, diff in result.neighbours:
        lines.append(
            f"- S-{r.sim_id:03d} ({_label(r.mode)}) : {diff} différence(s), "
            f"{METRICS[result.metric]} {r.pnl:+.2f} {unit}, {r.trades} trades"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.ideal",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, help="base à analyser (défaut : DB_PATH du .env)")
    parser.add_argument("--json", type=Path, help="écrit les tirages retenus dans ce fichier")
    args = parser.parse_args(argv)
    settings = load_settings()
    conn = connect(args.db or settings.db_path, settings.journal_mode)
    try:
        records = load_records(conn)
    finally:
        conn.close()
    if not records:
        raise SystemExit("Aucune métrique en base : les simulations n'ont pas encore tourné.")
    results = [ideal(records, metric) for metric in METRICS]
    print("\n\n".join(report(r) for r in results))
    if args.json:
        args.json.write_text(
            json.dumps({r.metric: r.draw for r in results}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
