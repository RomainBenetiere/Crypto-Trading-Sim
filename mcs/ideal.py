"""Simulation « idéale » déduite de la tornade : pour chaque variable, la modalité (ou le tiers de
valeurs) au meilleur P&L ou au meilleur ratio de Calmar moyen.

Même découpage que la tornade du dashboard (`mcs.metrics.tornado`) : options pour les variables
catégorielles, tiers bas / milieu / haut pour les numériques, sur les seules simulations où la
variable existe (ex. type de trailing seulement si un trailing est tiré). Les variables sont
choisies dans l'ordre du catalogue, si bien qu'une variable conditionnelle n'est retenue que si
sa condition est remplie par les choix déjà faits.

Lecture seule de la base. Les effets sont marginaux (une variable à la fois, sans interaction) :
c'est une hypothèse à valider, pas un optimum garanti.

python -m mcs.ideal                   # P&L et ratio de Calmar
python -m mcs.ideal --json ideal.json # tirages retenus, au format `draw_json`
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcs.config import load_settings
from mcs.metrics import (
    METRICS,
    OPTION_LABELS,
    VARIABLE_LABELS,
    SimRecord,
    load_records,
    records_for,
)
from mcs.sampling import CATEGORICALS, NUMERICS, Categorical, Draw, Numeric
from mcs.storage.db import connect


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


def ideal(records: Sequence[SimRecord], metric: str, neighbours: int = 5) -> Ideal:
    chosen = records_for(records, metric)
    draw: Draw = {}
    choices: list[Choice] = []
    for cat in CATEGORICALS:
        if not cat.when.test(draw):
            draw[cat.name] = None
            continue
        choice = _categorical(cat, chosen)
        draw[cat.name] = choice.value
        choices.append(choice)
    for var in NUMERICS:
        if not var.when.test(draw):
            draw[var.name] = None
            continue
        choice = _numeric(var, chosen)
        draw[var.name] = choice.value
        choices.append(choice)
    categorical = [c.name for c in CATEGORICALS]
    distance = [
        (r, sum(1 for name in categorical if r.draw.get(name) != draw[name])) for r in chosen
    ]
    closest = sorted(distance, key=lambda d: (d[1], -d[0].pnl))[:neighbours]
    return Ideal(metric, draw, choices, _calibration(chosen), closest)


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
