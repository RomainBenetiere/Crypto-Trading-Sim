"""Lecture de la base pour le dashboard (et les deux seules écritures : commandes, panier)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from dashboard import fmt
from mcs.live.state import Command, period_totals, request_command
from mcs.metrics import OPTION_LABELS
from mcs.storage.db import now_ms, transaction

ROLE_LABELS = {
    "main": "Signal principal",
    "confirmation": "Confirmation",
    "trend_filter": "Filtre de fond 1 j (bloquant)",
}
MODE_LABELS = {"typical": "Typique", "optimized": "Optimisé"}
STATUS_LABELS = {"NOT_STARTED": "Non démarré", "RUNNING": "En cours", "HOLD": "Hold"}
OPTIMIZATION_LABELS = {"TODO": "À faire", "RUNNING": "En cours", "DONE": "Terminée"}
FAMILY_LABELS = {k: OPTION_LABELS[k] for k in ("ema_cross", "macd", "supertrend", "donchian")}


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# --- Pilotage -------------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineInfo:
    status: str
    optimization_status: str
    optimized_at: int | None
    started_at: int | None
    heartbeat_at: int | None
    active_ms: int
    paused_ms: int
    simulations: int
    optimized: int
    pending_command: str | None
    last_command: str | None
    last_result: str | None


def engine_info(conn: sqlite3.Connection, now: int | None = None) -> EngineInfo:
    now = now or now_ms()
    row = conn.execute("SELECT * FROM engine_state").fetchone()
    totals = period_totals(conn, now)
    counts = conn.execute(
        "SELECT COUNT(*), SUM(mode = 'optimized' AND optimized_at IS NOT NULL) FROM simulations"
    ).fetchone()
    pending = conn.execute(
        "SELECT command FROM commands WHERE processed_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    last = conn.execute(
        "SELECT command, result FROM commands WHERE processed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return EngineInfo(
        status=row["status"],
        optimization_status=row["optimization_status"],
        optimized_at=row["optimized_at"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        active_ms=totals["active"],
        paused_ms=totals["hold"] + totals["outage"],
        simulations=counts[0] or 0,
        optimized=counts[1] or 0,
        pending_command=pending[0] if pending else None,
        last_command=last[0] if last else None,
        last_result=last[1] if last else None,
    )


def allowed_commands(info: EngineInfo) -> list[Command]:
    """Boutons proposés selon la machine à états (SPEC §8)."""
    if info.pending_command is not None:
        return []
    if info.status == "NOT_STARTED":
        commands = []
        if info.simulations and info.optimization_status != "RUNNING":
            commands.append(Command.OPTIMIZE)
        if info.optimization_status == "DONE":
            commands.append(Command.START)
        return commands
    if info.status == "RUNNING":
        return [Command.STOP]
    return [Command.RESUME, Command.RESET]


def send_command(conn: sqlite3.Connection, command: Command) -> int:
    return request_command(conn, command, now_ms())


def periods(conn: sqlite3.Connection) -> list[tuple[str, int, int | None]]:
    return [
        (r["kind"], r["started_at"], r["ended_at"])
        for r in conn.execute("SELECT kind, started_at, ended_at FROM run_periods ORDER BY id")
    ]


# --- Simulations ----------------------------------------------------------------------------


def _option(draw: dict[str, Any], name: str) -> str:
    value = draw.get(name)
    return "—" if value is None else OPTION_LABELS.get(str(value), str(value))


def simulations_table(conn: sqlite3.Connection) -> pd.DataFrame:
    """Une ligne par simulation : configuration lisible et métriques (si le live a démarré)."""
    rows = []
    for r in conn.execute(
        """SELECT s.id, s.pair_id, s.mode, s.family, s.draw_json, m.pnl_eur, m.pnl_pct,
                  m.max_drawdown_pct, m.closed_trades, m.win_rate, m.total_fees, m.bh_gap_eur,
                  m.open_positions
           FROM simulations s LEFT JOIN metrics m ON m.sim_id = s.id ORDER BY s.id"""
    ):
        draw = json.loads(r["draw_json"])
        num = _numbers(r)

        rows.append(
            {
                "sim_id": r["id"],
                "Simulation": fmt.sim_label(r["id"]),
                "Paire": f"P-{r['pair_id']:03d}",
                "Calibrage": MODE_LABELS[r["mode"]],
                "Famille": FAMILY_LABELS.get(r["family"], r["family"]),
                "Filtre 1 j": _option(draw, "trend_enabled"),
                "Type de filtre": _option(draw, "trend_type"),
                "Stop": _option(draw, "stop_type"),
                "Trailing": _option(draw, "trailing_type"),
                "Take profit": _option(draw, "take_profit_type"),
                "Ordres": _option(draw, "entry_order"),
                "Dimensionnement": _option(draw, "sizing"),
                "% max par crypto": _pct(draw.get("max_per_crypto")),
                "Exposition max": _pct(draw.get("max_exposure")),
                "Max par entrée": _pct(draw.get("max_entry")),
                "Positions max": draw.get("max_positions"),
                "pnl_eur": num("pnl_eur"),
                "pnl_pct": num("pnl_pct"),
                "dd_pct": num("max_drawdown_pct"),
                "trades": r["closed_trades"],
                "win_rate": num("win_rate"),
                "fees": num("total_fees"),
                "bh_gap": num("bh_gap_eur"),
                "open_positions": r["open_positions"],
            }
        )
    return pd.DataFrame(rows)


def _numbers(row: sqlite3.Row) -> Any:
    def num(key: str) -> float | None:
        return None if row[key] is None else float(row[key])

    return num


def _pct(fraction: Any) -> str:
    return "—" if fraction is None else fmt.pct(float(fraction) * 100, 0)


def value_series(conn: sqlite3.Connection, sim_ids: list[int]) -> pd.DataFrame:
    if not sim_ids:
        return pd.DataFrame(columns=["sim_id", "ts", "value"])
    marks = ",".join("?" * len(sim_ids))
    frame = pd.read_sql_query(
        f"SELECT sim_id, ts, total_value FROM portfolio_values WHERE sim_id IN ({marks}) ORDER BY ts",
        conn,
        params=sim_ids,
    )
    frame["value"] = frame.pop("total_value").astype(float)
    return frame


# --- Indicateurs ----------------------------------------------------------------------------


def describe_indicator(params: dict[str, Any]) -> str:
    """Résumé lisible des valeurs d'un indicateur (« EMA 21 / 55 »)."""
    kind = params.get("type")
    n = fmt.number
    if kind == "EmaCross":
        return f"EMA {params['fast']} / {params['slow']}"
    if kind == "Macd":
        return f"MACD {params['fast']} / {params['slow']} / {params['signal']}"
    if kind == "Supertrend":
        return f"ATR {params['atr_period']} · multiplicateur {n(params['multiplier'], 2).rstrip('0').rstrip(',')}"
    if kind == "DonchianBreakout":
        return f"Période {params['period']}"
    if kind == "AdxConfirmation":
        return f"ADX {params['period']} · seuil {n(params['threshold'], 1)}"
    if kind == "VolumeConfirmation":
        return f"Fenêtre {params['window']} · × {n(params['multiple'], 2)}"
    if kind == "RsiConfirmation":
        return f"RSI {params['period']} · < {n(params['max_level'], 1)}"
    if kind == "TrendFilter":
        margin = f"marge {n(float(params['hysteresis']) * 100, 2)} %"
        if params["kind"] == "price_above_sma":
            return f"Prix > SMA {params['sma_window']} · {margin}"
        if params["kind"] == "ema_cross":
            return f"EMA {params['ema_fast']} / {params['ema_slow']} · {margin}"
        return f"ROC {params['roc_window']} j · {margin}"
    return json.dumps(params)


INDICATOR_LABELS = {
    "EmaCross": "Croisement EMA",
    "Macd": "MACD",
    "Supertrend": "Supertrend",
    "DonchianBreakout": "Cassure Donchian",
    "AdxConfirmation": "ADX",
    "VolumeConfirmation": "Volume relatif",
    "RsiConfirmation": "RSI (surachat)",
    "price_above_sma": "Prix > SMA",
    "ema_cross": "EMA rapide > lente",
    "roc": "Momentum ROC",
}


def indicator_params_table(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = []
    for r in conn.execute(
        """SELECT p.sim_id, s.mode, s.family, p.role, p.indicator, p.params_json,
                  p.typical_params_json, p.weight, p.score_threshold
           FROM indicator_params p JOIN simulations s ON s.id = p.sim_id
           ORDER BY p.sim_id, CASE p.role WHEN 'main' THEN 0 WHEN 'confirmation' THEN 1 ELSE 2 END"""
    ):
        used, typical = json.loads(r["params_json"]), json.loads(r["typical_params_json"])
        rows.append(
            {
                "Simulation": fmt.sim_label(r["sim_id"]),
                "Calibrage": MODE_LABELS[r["mode"]],
                "Famille": FAMILY_LABELS.get(r["family"], r["family"]),
                "Rôle": ROLE_LABELS[r["role"]],
                "Indicateur": INDICATOR_LABELS.get(r["indicator"], r["indicator"]),
                "Valeurs utilisées": ("• " if used != typical else "") + describe_indicator(used),
                "Valeurs typiques": describe_indicator(typical),
                "Poids": None if r["weight"] is None else float(r["weight"]),
                "Seuil du score": None
                if r["score_threshold"] is None
                else float(r["score_threshold"]),
                "differs": used != typical,
            }
        )
    return pd.DataFrame(rows)


# --- Marchés --------------------------------------------------------------------------------


def markets_table(conn: sqlite3.Connection, taker: Decimal) -> pd.DataFrame:
    rows = []
    for r in conn.execute("SELECT * FROM markets WHERE in_basket = 1 ORDER BY market"):
        spread = None if r["spread_estimate_pct"] is None else float(r["spread_estimate_pct"])
        rows.append(
            {
                "Prendre en compte": bool(r["selected"]),
                "Actif": r["base"],
                "Paire": r["market"],
                "Historique 1 j": r["history_days"],
                "200 jours": "Atteint" if (r["history_days"] or 0) >= 200 else "Insuffisant",
                "Spread estimé": spread,
                "Coût aller-retour": None if spread is None else 2 * float(taker) * 100 + spread,
                "Volume 24 h": None if r["volume_24h_eur"] is None else float(r["volume_24h_eur"]),
                "Volatilité (ATR 4 h)": None if r["atr_4h_pct"] is None else float(r["atr_4h_pct"]),
            }
        )
    return pd.DataFrame(rows)


def save_selection(conn: sqlite3.Connection, selected: list[str]) -> None:
    """Valide la sélection du panier (appliquée au prochain lancement)."""
    with transaction(conn):
        conn.execute("UPDATE markets SET selected = 0 WHERE in_basket = 1")
        conn.executemany(
            "UPDATE markets SET selected = 1 WHERE market = ?", [(m,) for m in selected]
        )
