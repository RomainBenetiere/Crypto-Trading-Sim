"""Rapport de santé du moteur (rodage, étape 10).

    python -m mcs.health [--hours 72]

Vérifie, sur les dernières heures, la mécanique plutôt que les résultats : données
collectées, signes de vie et interruptions, cycles 4 h, valeurs et métriques écrites,
cohérence des comptes, réseau et journaux. Chaque contrôle donne OK, À surveiller ou
Anomalie ; le code de sortie vaut 1 s'il y a au moins une anomalie.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from mcs.config import load_settings
from mcs.live.state import read_state
from mcs.market_data import INTERVAL_MS, floor_ts
from mcs.metrics import cash_balance
from mcs.storage.db import connect, now_ms

HOUR = INTERVAL_MS["1h"]
BAR_4H = INTERVAL_MS["4h"]
HEARTBEAT_LIMIT_MS = 3 * 60_000
PRICE_LIMIT_MS = 3 * 60_000
METRICS_LIMIT_MS = 70 * 60_000
CYCLE_LIMIT_S = 60.0
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+)\s+([\w.]+) : (.*)$")


class Level(StrEnum):
    OK = "OK"
    WARN = "À surveiller"
    FAIL = "Anomalie"


@dataclass(frozen=True)
class Check:
    section: str
    name: str
    level: Level
    detail: str


# --- Journaux -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogLine:
    ts: int
    level: str
    logger: str
    message: str


def read_logs(
    log_dir: Path, since: int, names: Iterable[str] = ("engine", "collector")
) -> list[LogLine]:
    """Lignes des journaux (rotations comprises) postérieures à `since` (ms UTC). Les horodatages
    des journaux sont en heure locale de la machine qui les a écrits."""
    lines = []
    for name in names:
        for path in sorted(log_dir.glob(f"{name}.log*")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw in content.splitlines():
                match = LOG_LINE.match(raw)
                if not match:
                    continue
                stamp = int(time.mktime(time.strptime(match[1], "%Y-%m-%d %H:%M:%S")) * 1000)
                if stamp >= since:
                    lines.append(LogLine(stamp, match[2], match[3], match[4]))
    return sorted(lines, key=lambda line: line.ts)


# --- Contrôles ------------------------------------------------------------------------------


def _active_hours(conn: sqlite3.Connection, since: int, now: int) -> list[int]:
    """Bornes horaires couvertes par une période active ou de Hold (hors interruptions)."""
    hours = set()
    for row in conn.execute(
        "SELECT kind, started_at, ended_at FROM run_periods WHERE kind != 'outage'"
    ):
        start = max(row["started_at"], since)
        end = min(row["ended_at"] or now, now)
        ts = floor_ts(start, "1h") + HOUR
        while ts <= end:
            hours.add(ts)
            ts += HOUR
    return sorted(hours)


def check_engine(
    conn: sqlite3.Connection, now: int, since: int, logs: Sequence[LogLine]
) -> list[Check]:
    checks = []
    state = read_state(conn)
    age = None if state.heartbeat_at is None else now - state.heartbeat_at
    if age is not None and age <= HEARTBEAT_LIMIT_MS:
        checks.append(Check("Moteur", "Signe de vie", Level.OK, f"il y a {age // 1000} s"))
    else:
        level = Level.FAIL if state.status.value != "NOT_STARTED" or age is not None else Level.WARN
        detail = "jamais" if age is None else f"il y a {age // 60_000} min"
        checks.append(Check("Moteur", "Signe de vie", level, f"dernier signe de vie : {detail}"))
    outages = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(ended_at - started_at), 0) FROM run_periods "
        "WHERE kind = 'outage' AND ended_at >= ?",
        (since,),
    ).fetchone()
    if outages[0]:
        checks.append(
            Check(
                "Moteur",
                "Interruptions",
                Level.WARN,
                f"{outages[0]} interruption(s), {outages[1] // 60_000} min au total",
            )
        )
    else:
        checks.append(Check("Moteur", "Interruptions", Level.OK, "aucune"))
    if state.status.value == "RUNNING":
        cycles = [
            float(m[1])
            for line in logs
            if (m := re.search(r"Clôture 4 h traitée en ([\d.]+) s", line.message))
        ]
        active = [h for h in _active_hours(conn, since, now) if h % BAR_4H == 0]
        expected = len(active)
        slowest = max(cycles, default=0.0)
        level = Level.OK
        if len(cycles) < expected:
            level = Level.WARN
        if slowest > CYCLE_LIMIT_S:
            level = Level.FAIL
        checks.append(
            Check(
                "Moteur",
                "Cycles 4 h",
                level,
                f"{len(cycles)} traités / {expected} attendus, le plus long {slowest:.1f} s",
            )
        )
    return checks


def check_data(
    conn: sqlite3.Connection, now: int, since: int, markets: Sequence[str]
) -> list[Check]:
    checks = []
    expected_4h = floor_ts(now, "4h") - BAR_4H
    stale = [
        m
        for m in markets
        if (
            conn.execute(
                "SELECT MAX(ts) FROM candles WHERE market = ? AND interval = '4h'", (m,)
            ).fetchone()[0]
            or 0
        )
        < expected_4h - BAR_4H
    ]
    checks.append(
        Check(
            "Données",
            "Dernière bougie 4 h",
            Level.FAIL if stale else Level.OK,
            f"en retard : {', '.join(stale)}" if stale else f"{len(markets)} paires à jour",
        )
    )
    # Heures sans aucune bougie 1 min sur toutes les paires : coupure de collecte (une paire
    # peu liquide peut, elle, rester une heure sans transaction).
    start, end = floor_ts(max(since, now - 7 * 24 * HOUR), "1h"), floor_ts(now, "1h") - HOUR
    hours_with_data = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT ts - ts % ? FROM candles WHERE interval = '1m' AND ts >= ? AND ts < ?",
            (HOUR, start, end),
        )
    }
    missing = [h for h in range(start, end, HOUR) if h not in hours_with_data]
    level = Level.OK if not missing else Level.FAIL if len(missing) > 2 else Level.WARN
    checks.append(
        Check(
            "Données",
            "Collecte 1 min",
            level,
            f"{len(missing)} heure(s) sans aucune bougie 1 min"
            if missing
            else "aucune heure manquante",
        )
    )
    prices = conn.execute("SELECT MIN(ts), COUNT(*) FROM latest_prices").fetchone()
    if prices[1]:
        age = now - prices[0]
        level = Level.OK if age <= PRICE_LIMIT_MS else Level.WARN
        checks.append(
            Check("Données", "Derniers cours", level, f"plus ancien : il y a {age // 1000} s")
        )
    else:
        checks.append(Check("Données", "Derniers cours", Level.WARN, "aucun cours enregistré"))
    return checks


def check_simulations(conn: sqlite3.Connection, now: int, since: int) -> list[Check]:
    state = read_state(conn)
    if state.status.value == "NOT_STARTED":
        return [Check("Simulations", "État", Level.OK, "non démarrées")]
    checks = []
    sims = [r[0] for r in conn.execute("SELECT id FROM simulations")]
    # Valeurs écrites quelques secondes après chaque heure pleine : marge de 5 min.
    hours = [h for h in _active_hours(conn, since, now) if h <= now - 5 * 60_000]
    written = dict(
        conn.execute(
            "SELECT ts, COUNT(*) FROM portfolio_values WHERE ts >= ? GROUP BY ts", (since,)
        ).fetchall()
    )
    incomplete = [h for h in hours if written.get(h, 0) < len(sims)]
    checks.append(
        Check(
            "Simulations",
            "Valeurs horaires",
            Level.OK if not incomplete else Level.FAIL,
            f"{len(hours) - len(incomplete)} / {len(hours)} heures complètes",
        )
    )
    fresh = conn.execute("SELECT MIN(updated_at), COUNT(*) FROM metrics").fetchone()
    if fresh[1] < len(sims) or now - (fresh[0] or 0) > METRICS_LIMIT_MS:
        checks.append(
            Check(
                "Simulations",
                "Métriques",
                Level.FAIL,
                f"{fresh[1]} / {len(sims)} simulations, mises à jour il y a "
                f"{(now - (fresh[0] or 0)) // 60_000} min",
            )
        )
    else:
        checks.append(
            Check("Simulations", "Métriques", Level.OK, f"{len(sims)} simulations à jour")
        )
    checks.append(check_accounts(conn, sims))
    return checks


def check_accounts(conn: sqlite3.Connection, sims: Sequence[int]) -> Check:
    """Liquidités ≥ 0 et quantités détenues (ordres) = positions enregistrées."""
    problems = []
    holdings: dict[tuple[int, str], Decimal] = {}
    for row in conn.execute(
        "SELECT sim_id, market, side, quantity FROM orders WHERE status = 'filled'"
    ):
        sign = 1 if row["side"] == "buy" else -1
        key = (row["sim_id"], row["market"])
        holdings[key] = holdings.get(key, Decimal(0)) + sign * Decimal(row["quantity"])
    positions = {
        (r["sim_id"], r["market"]): Decimal(r["quantity"])
        for r in conn.execute("SELECT sim_id, market, quantity FROM positions")
    }
    for key, quantity in holdings.items():
        if quantity != positions.get(key, Decimal(0)):
            problems.append(
                f"S-{key[0]:03d} {key[1]} : ordres {quantity} ≠ position {positions.get(key, 0)}"
            )
    for key in set(positions) - set(holdings):
        problems.append(f"S-{key[0]:03d} {key[1]} : position sans ordre")
    negative = [s for s in sims if cash_balance(conn, s) < 0]
    problems += [f"S-{s:03d} : liquidités négatives" for s in negative]
    if problems:
        return Check("Simulations", "Comptes", Level.FAIL, "; ".join(problems[:5]))
    return Check("Simulations", "Comptes", Level.OK, "liquidités et positions cohérentes")


def check_logs(logs: Sequence[LogLine]) -> list[Check]:
    checks = []
    disconnects = sum("WebSocket coupé" in line.message for line in logs)
    reconnects = sum("WebSocket reconnecté" in line.message for line in logs)
    level = Level.OK if disconnects <= 3 else Level.WARN
    checks.append(
        Check(
            "Réseau", "WebSocket", level, f"{disconnects} coupure(s), {reconnects} reconnexion(s)"
        )
    )
    limits = sum("Limite Bitvavo atteinte" in line.message for line in logs)
    checks.append(
        Check(
            "Réseau",
            "Limites de requêtes",
            Level.OK if not limits else Level.WARN,
            f"{limits} dépassement(s)",
        )
    )
    rejected = Counter(m[1] for line in logs if (m := re.search(r"refusé \((\w+)", line.message)))
    detail = ", ".join(f"{k} {v}" for k, v in rejected.most_common()) or "aucun"
    checks.append(Check("Ordres", "Ordres refusés", Level.OK, detail))
    gaps = sum("écart officiel / 1 min" in line.message for line in logs)
    checks.append(
        Check(
            "Données",
            "Bougies officielles / 1 min",
            Level.OK if gaps < 20 else Level.WARN,
            f"{gaps} écart(s) relevé(s)",
        )
    )
    errors = Counter(line.logger for line in logs if line.level in ("ERROR", "CRITICAL"))
    warnings = Counter(line.logger for line in logs if line.level == "WARNING")
    level = Level.FAIL if errors else Level.WARN if sum(warnings.values()) > 20 else Level.OK
    by_logger = ", ".join(f"{k} {v}" for k, v in errors.items()) or "—"
    detail = (
        f"{sum(errors.values())} erreur(s) ({by_logger}), {sum(warnings.values())} avertissement(s)"
    )
    checks.append(Check("Journaux", "Erreurs", level, detail))
    return checks


def run_checks(
    conn: sqlite3.Connection, log_dir: Path, hours: int = 72, now: int | None = None
) -> list[Check]:
    now = now or now_ms()
    since = now - hours * HOUR
    state = read_state(conn)
    markets = list(state.basket) or [
        r[0] for r in conn.execute("SELECT market FROM markets WHERE selected = 1 ORDER BY market")
    ]
    logs = read_logs(log_dir, since)
    return [
        *check_engine(conn, now, since, logs),
        *check_data(conn, now, since, markets),
        *check_simulations(conn, now, since),
        *check_logs(logs),
    ]


def report(checks: Sequence[Check], hours: int) -> str:
    icons = {Level.OK: "✅", Level.WARN: "⚠️ ", Level.FAIL: "❌"}
    lines = [
        f"Rapport de santé — {datetime.now().strftime('%d/%m/%Y %H:%M')} — dernières {hours} h",
        "",
    ]
    order = ["Moteur", "Données", "Simulations", "Réseau", "Ordres", "Journaux"]
    section = None
    for check in sorted(checks, key=lambda c: order.index(c.section)):
        if check.section != section:
            section = check.section
            lines.append(section)
        lines.append(f"  {icons[check.level]} {check.name} : {check.detail}")
    worst = max((c.level for c in checks), key=[Level.OK, Level.WARN, Level.FAIL].index)
    lines += ["", f"Verdict : {worst.value}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.health",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--hours", type=int, default=72, help="fenêtre analysée (heures)")
    args = parser.parse_args(argv)
    settings = load_settings()
    conn = connect(settings.db_path, settings.journal_mode)
    try:
        checks = run_checks(conn, settings.log_dir, args.hours)
    finally:
        conn.close()
    print(report(checks, args.hours))
    raise SystemExit(1 if any(c.level is Level.FAIL for c in checks) else 0)


if __name__ == "__main__":
    main()
