"""Métriques (SPEC §6), synthèse, tornade et valorisations pour le dashboard.

- Métriques par simulation, écrites chaque heure par le moteur dans `metrics` : P&L (valeur
  − 1 000 €, positions nettes des frais de vente estimés au taux taker), drawdown max depuis
  un plus haut de la valeur horaire, trades clôturés, taux de réussite frais compris, frais
  totaux, écart au buy & hold.
- Tornade (§11), calculée à la demande à partir de `metrics` (donc au rythme horaire) :
  catégories → pire / meilleure option ; nombres → tiers bas / tiers haut ; barre pâle si
  l'écart est inférieur à 2 erreurs-types ; variables conditionnelles sur leur sous-groupe ;
  calibrage mesuré par l'écart apparié entre jumelles.

Argent en `Decimal` ; les statistiques de la tornade et de la synthèse sont en float
(affichage).
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from mcs.sampling import CATEGORICALS, NUMERICS
from mcs.storage.db import transaction
from mcs.storage.orders import ORDER_TABLES
from mcs.storage.positions import POSITION_TABLES

INITIAL_CAPITAL = Decimal(1000)
MIN_TRADES_FOR_TORNADO = 10
# Ratio de Calmar : P&L % ÷ drawdown max %, même définition que le critère de l'optimiseur
# (décisions 23 et 42) ; non défini sous 10 trades clôturés.
CALMAR_DRAWDOWN_FLOOR_PCT = 1.0
CALMAR_MIN_TRADES = 10
METRICS = {"pnl": "P&L", "calmar": "Ratio de Calmar"}
HOUR = 3_600_000


# --- Métriques par simulation ---------------------------------------------------------------


def calmar_ratio(pnl_pct: float, max_drawdown_pct: float, closed_trades: int) -> float | None:
    """P&L % ÷ drawdown max % (drawdown plancher 1 %), None sous 10 trades clôturés."""
    if closed_trades < CALMAR_MIN_TRADES:
        return None
    return pnl_pct / max(max_drawdown_pct, CALMAR_DRAWDOWN_FLOOR_PCT)


@dataclass(frozen=True)
class SimMetrics:
    sim_id: int
    pnl_eur: Decimal
    pnl_pct: Decimal
    max_drawdown_pct: Decimal
    closed_trades: int
    win_rate: Decimal | None
    total_fees: Decimal
    bh_gap_eur: Decimal | None
    peak_value: Decimal
    open_positions: int


def compute_metrics(
    sim_id: int,
    value: Decimal,
    trade_pnls: Sequence[Decimal],
    total_fees: Decimal,
    open_positions: int,
    bh_pnl: Decimal | None,
    previous: SimMetrics | None = None,
) -> SimMetrics:
    """Met à jour les métriques avec la nouvelle valeur horaire `value`."""
    peak = max(previous.peak_value if previous else INITIAL_CAPITAL, value)
    drawdown = (1 - value / peak) * 100 if peak > 0 else Decimal(0)
    max_drawdown = max(previous.max_drawdown_pct if previous else Decimal(0), drawdown)
    pnl = value - INITIAL_CAPITAL
    wins = sum(1 for p in trade_pnls if p > 0)
    return SimMetrics(
        sim_id=sim_id,
        pnl_eur=pnl,
        pnl_pct=pnl / INITIAL_CAPITAL * 100,
        max_drawdown_pct=max_drawdown,
        closed_trades=len(trade_pnls),
        win_rate=Decimal(wins) / len(trade_pnls) if trade_pnls else None,
        total_fees=total_fees,
        bh_gap_eur=pnl - bh_pnl if bh_pnl is not None else None,
        peak_value=peak,
        open_positions=open_positions,
    )


def write_metrics(conn: sqlite3.Connection, metrics: Iterable[SimMetrics], now: int) -> None:
    rows = [
        (
            m.sim_id,
            m.pnl_eur,
            m.pnl_pct,
            m.max_drawdown_pct,
            m.closed_trades,
            m.win_rate,
            m.total_fees,
            m.bh_gap_eur,
            m.peak_value,
            m.open_positions,
            now,
        )
        for m in metrics
    ]
    with transaction(conn):
        conn.executemany(
            """INSERT INTO metrics (sim_id, pnl_eur, pnl_pct, max_drawdown_pct, closed_trades,
                   win_rate, total_fees, bh_gap_eur, peak_value, open_positions, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (sim_id) DO UPDATE SET pnl_eur = excluded.pnl_eur,
                   pnl_pct = excluded.pnl_pct, max_drawdown_pct = excluded.max_drawdown_pct,
                   closed_trades = excluded.closed_trades, win_rate = excluded.win_rate,
                   total_fees = excluded.total_fees, bh_gap_eur = excluded.bh_gap_eur,
                   peak_value = excluded.peak_value, open_positions = excluded.open_positions,
                   updated_at = excluded.updated_at""",
            rows,
        )


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def load_metrics(conn: sqlite3.Connection) -> dict[int, SimMetrics]:
    return {
        row["sim_id"]: SimMetrics(
            sim_id=row["sim_id"],
            pnl_eur=Decimal(row["pnl_eur"]),
            pnl_pct=Decimal(row["pnl_pct"]),
            max_drawdown_pct=Decimal(row["max_drawdown_pct"]),
            closed_trades=row["closed_trades"],
            win_rate=_dec(row["win_rate"]),
            total_fees=Decimal(row["total_fees"]),
            bh_gap_eur=_dec(row["bh_gap_eur"]),
            peak_value=Decimal(row["peak_value"] or INITIAL_CAPITAL),
            open_positions=row["open_positions"],
        )
        for row in conn.execute("SELECT * FROM metrics")
    }


# --- Buy & hold -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingLine:
    market: str
    weight: Decimal
    allocated: Decimal
    buy_fee: Decimal
    start_price: Decimal
    quantity: Decimal

    @property
    def leftover(self) -> Decimal:
        """Reliquat non investi de l'allocation (arrondis de quantité)."""
        return self.allocated - self.quantity * self.start_price - self.buy_fee


def load_buy_and_hold(conn: sqlite3.Connection) -> list[HoldingLine]:
    return [
        HoldingLine(
            row["market"],
            Decimal(row["weight"]),
            Decimal(row["allocated_eur"]),
            Decimal(row["buy_fee"]),
            Decimal(row["start_price"]),
            Decimal(row["quantity"]),
        )
        for row in conn.execute("SELECT * FROM buy_and_hold ORDER BY market")
    ]


def buy_and_hold_value(
    lines: Sequence[HoldingLine], prices: Mapping[str, Decimal], taker: Decimal
) -> Decimal | None:
    """Valeur nette des frais de vente estimés (même convention que les simulations)."""
    if not lines:
        return None
    total = Decimal(0)
    for line in lines:
        price = prices.get(line.market, line.start_price)
        total += line.quantity * price * (1 - taker) + line.leftover
    return total


@dataclass(frozen=True)
class HoldingDetail:
    market: str
    weight: Decimal
    start_price: Decimal
    price: Decimal
    change_pct: Decimal
    quantity: Decimal
    net_value: Decimal
    pnl_eur: Decimal


def buy_and_hold_detail(
    lines: Sequence[HoldingLine], prices: Mapping[str, Decimal], taker: Decimal
) -> list[HoldingDetail]:
    details = []
    for line in lines:
        price = prices.get(line.market, line.start_price)
        net = line.quantity * price * (1 - taker)
        details.append(
            HoldingDetail(
                line.market,
                line.weight,
                line.start_price,
                price,
                (price / line.start_price - 1) * 100,
                line.quantity,
                net,
                net + line.leftover - line.allocated,
            )
        )
    return sorted(details, key=lambda d: d.pnl_eur, reverse=True)


def buy_and_hold_series(
    conn: sqlite3.Connection, lines: Sequence[HoldingLine], taker: Decimal, start: int, end: int
) -> list[tuple[int, Decimal]]:
    """Valeur horaire du buy & hold, reconstituée à partir des clôtures 1 h."""
    closes: dict[str, dict[int, Decimal]] = {}
    for line in lines:
        rows = conn.execute(
            "SELECT ts, close FROM candles WHERE market = ? AND interval = '1h' "
            "AND ts >= ? AND ts < ? ORDER BY ts",
            (line.market, start - HOUR, end),
        )
        closes[line.market] = {r["ts"] + HOUR: Decimal(r["close"]) for r in rows}
    series = []
    last: dict[str, Decimal] = {line.market: line.start_price for line in lines}
    for ts in range(start - start % HOUR + HOUR, end + 1, HOUR):
        for line in lines:
            if ts in closes[line.market]:
                last[line.market] = closes[line.market][ts]
        value = buy_and_hold_value(lines, last, taker)
        assert value is not None
        series.append((ts, value))
    return series


# --- Valorisation d'une simulation (dashboard, à la minute) ------------------------------


@dataclass(frozen=True)
class PositionDetail:
    market: str
    lots: int
    quantity: Decimal
    avg_entry_price: Decimal
    price: Decimal | None
    invested_cost: Decimal
    value: Decimal | None
    buy_fees: Decimal
    sell_fees_estimate: Decimal | None
    unrealized_pnl: Decimal | None
    stop: Decimal | None
    trailing: Decimal | None
    take_profit: Decimal | None


def latest_prices(conn: sqlite3.Connection) -> dict[str, Decimal]:
    return {r["market"]: Decimal(r["price"]) for r in conn.execute("SELECT * FROM latest_prices")}


def positions_detail(
    conn: sqlite3.Connection,
    sim_id: int,
    prices: Mapping[str, Decimal],
    taker: Decimal,
    table: str = "positions",
) -> list[PositionDetail]:
    """Positions d'une simulation (ou du portefeuille ombre, `production_positions`)."""
    if table not in POSITION_TABLES:
        raise ValueError(f"Table de positions inconnue : {table}")
    details = []
    for row in conn.execute(f"SELECT * FROM {table} WHERE sim_id = ? ORDER BY market", (sim_id,)):
        quantity, cost = Decimal(row["quantity"]), Decimal(row["invested_cost"])
        price = prices.get(row["market"])
        value = quantity * price if price is not None else None
        sell_fees = value * taker if value is not None else None
        details.append(
            PositionDetail(
                market=row["market"],
                lots=row["lots"],
                quantity=quantity,
                avg_entry_price=Decimal(row["avg_entry_price"]),
                price=price,
                invested_cost=cost,
                value=value,
                buy_fees=Decimal(row["buy_fees"]),
                sell_fees_estimate=sell_fees,
                unrealized_pnl=value - sell_fees - cost
                if value is not None and sell_fees is not None
                else None,
                stop=_dec(row["stop"]),
                trailing=_dec(row["trailing"]),
                take_profit=_dec(row["take_profit"]),
            )
        )
    return details


def cash_balance(
    conn: sqlite3.Connection,
    sim_id: int,
    initial: Decimal = INITIAL_CAPITAL,
    table: str = "orders",
) -> Decimal:
    """Liquidités totales reconstituées à partir des ordres remplis."""
    if table not in ORDER_TABLES:
        raise ValueError(f"Table d'ordres inconnue : {table}")
    cash = initial
    for row in conn.execute(
        f"SELECT side, notional, fee FROM {table} WHERE sim_id = ? AND status = 'filled'",
        (sim_id,),
    ):
        notional, fee = Decimal(row["notional"]), Decimal(row["fee"])
        cash += notional - fee if row["side"] == "sell" else -(notional + fee)
    return cash


# --- Synthèse et tornade --------------------------------------------------------------------


@dataclass(frozen=True)
class SimRecord:
    """Une simulation pour l'analyse : tirage, calibrage et métriques."""

    sim_id: int
    pair_id: int
    mode: str
    family: str
    draw: dict[str, object]
    pnl: float
    trades: int
    calmar: float | None = None


def load_records(conn: sqlite3.Connection) -> list[SimRecord]:
    rows = conn.execute(
        """SELECT s.id, s.pair_id, s.mode, s.family, s.draw_json, m.pnl_eur, m.pnl_pct,
                  m.max_drawdown_pct, m.closed_trades
           FROM simulations s JOIN metrics m ON m.sim_id = s.id ORDER BY s.id"""
    )
    return [
        SimRecord(
            r["id"],
            r["pair_id"],
            r["mode"],
            r["family"],
            json.loads(r["draw_json"]),
            float(r["pnl_eur"]),
            r["closed_trades"],
            calmar_ratio(float(r["pnl_pct"]), float(r["max_drawdown_pct"]), r["closed_trades"]),
        )
        for r in rows
    ]


def records_for(records: Sequence[SimRecord], metric: str) -> list[SimRecord]:
    """Les mêmes simulations, la valeur analysée (`pnl`) remplacée par la métrique choisie.
    Pour le ratio de Calmar, les simulations sans ratio (moins de 10 trades) sont écartées."""
    if metric == "pnl":
        return list(records)
    if metric != "calmar":
        raise ValueError(f"Métrique inconnue : {metric}")
    return [replace(r, pnl=r.calmar) for r in records if r.calmar is not None]


@dataclass(frozen=True)
class Synthesis:
    count: int
    pnl_median: float
    pnl_mean: float
    best_pnl: float
    best_sim: int
    bh_pnl: float | None
    beat_bh: int
    beat_bh_share: float
    optimized_gap_mean: float | None
    trades_mean: float

    @property
    def preliminary(self) -> bool:
        """Moins de 10 trades clôturés en moyenne : tornade pas encore exploitable."""
        return self.trades_mean < MIN_TRADES_FOR_TORNADO


def paired_gaps(records: Sequence[SimRecord]) -> list[float]:
    """P&L optimisé − typique pour chaque paire de jumelles."""
    by_pair: dict[int, dict[str, float]] = {}
    for r in records:
        by_pair.setdefault(r.pair_id, {})[r.mode] = r.pnl
    return [p["optimized"] - p["typical"] for p in by_pair.values() if len(p) == 2]


def synthesis(records: Sequence[SimRecord], bh_pnl: float | None) -> Synthesis | None:
    if not records:
        return None
    pnls = [r.pnl for r in records]
    best = max(records, key=lambda r: r.pnl)
    beat = sum(1 for p in pnls if bh_pnl is not None and p > bh_pnl)
    gaps = paired_gaps(records)
    return Synthesis(
        count=len(records),
        pnl_median=statistics.median(pnls),
        pnl_mean=statistics.fmean(pnls),
        best_pnl=best.pnl,
        best_sim=best.sim_id,
        bh_pnl=bh_pnl,
        beat_bh=beat,
        beat_bh_share=beat / len(records),
        optimized_gap_mean=statistics.fmean(gaps) if gaps else None,
        trades_mean=statistics.fmean(r.trades for r in records),
    )


@dataclass(frozen=True)
class TornadoRow:
    variable: str
    low_label: str
    low_pnl: float
    low_n: int
    high_label: str
    high_pnl: float
    high_n: int
    significant: bool

    @property
    def amplitude(self) -> float:
        return abs(self.high_pnl - self.low_pnl)


VARIABLE_LABELS: dict[str, str] = {
    "family": "Famille d'indicateur",
    "conf_adx": "Confirmation ADX",
    "conf_volume": "Confirmation volume relatif",
    "conf_rsi": "Confirmation RSI",
    "trend_enabled": "Filtre de fond 1 j",
    "trend_type": "Type de filtre de fond",
    "trend_action": "Action au retournement",
    "stop_type": "Stop initial",
    "trailing_type": "Trailing stop",
    "trailing_activation": "Activation du trailing",
    "take_profit_type": "Take profit",
    "reverse_signal": "Sortie sur signal inverse",
    "stop_recalc": "Recalcul du stop",
    "max_holding": "Durée maximale de détention",
    "entry_order": "Type d'ordre d'entrée",
    "sizing": "Dimensionnement",
    "max_entries": "Renforcement (entrées max)",
    "max_per_crypto": "% max sur une crypto",
    "max_exposure": "Exposition totale max",
    "max_entry": "Montant max par entrée",
    "max_positions": "Positions simultanées max",
    "pause_after_stop_bars": "Pause après un stop",
    "circuit_breaker_drawdown": "Circuit breaker",
    "score_threshold": "Seuil du score",
    "main_weight": "Poids du signal principal",
    "stop_pct": "Stop % fixe",
    "stop_atr_multiple": "Stop multiple d'ATR",
    "stop_lowest_n": "Stop plus bas des N bougies",
    "trailing_atr_multiple": "Trailing multiple d'ATR",
    "trailing_pct": "Trailing %",
    "trailing_activation_gain": "Gain d'activation du trailing",
    "tp_r_multiple": "Take profit (ratio R)",
    "tp_pct": "Take profit %",
    "max_holding_bars": "Durée max (bougies 4 h)",
    "risk_per_trade": "Risque par trade",
    "limit_expiry_hours": "Expiration de l'ordre limite",
}
OPTION_LABELS: dict[str, str] = {
    "yes": "Oui",
    "no": "Non",
    "ema_cross": "Croisement EMA",
    "macd": "MACD",
    "supertrend": "Supertrend",
    "donchian": "Cassure Donchian",
    "price_above_sma": "Prix > SMA",
    "roc": "Momentum ROC",
    "block": "Bloquer les achats",
    "block_and_exit": "Bloquer et sortir",
    "fixed_pct": "% fixe",
    "atr": "Multiple ATR",
    "lowest_low": "Plus bas des N bougies",
    "none": "Aucun",
    "pct": "%",
    "chandelier": "Chandelier",
    "immediate": "Immédiate",
    "gain_pct": "Après un gain",
    "one_r": "Après 1R",
    "r_multiple": "Ratio R",
    "partial": "Partiel",
    "close_4h": "Clôture 4 h",
    "hourly": "Chaque heure",
    "limited": "Limitée",
    "limit": "Limite (maker)",
    "market": "Marché (taker)",
    "risk": "Basé sur le risque",
    "1": "Non",
    "2": "2 entrées",
    "3": "3 entrées",
}


def _stats(values: Sequence[float]) -> tuple[float, float]:
    """(moyenne, erreur-type)."""
    mean = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else math.inf
    return mean, se


def _row(
    variable: str, low: tuple[str, Sequence[float]], high: tuple[str, Sequence[float]]
) -> TornadoRow:
    (low_label, low_values), (high_label, high_values) = low, high
    low_mean, low_se = _stats(low_values)
    high_mean, high_se = _stats(high_values)
    return TornadoRow(
        variable,
        low_label,
        low_mean,
        len(low_values),
        high_label,
        high_mean,
        len(high_values),
        abs(high_mean - low_mean) > 2 * math.sqrt(low_se**2 + high_se**2),
    )


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def tornado(records: Sequence[SimRecord]) -> list[TornadoRow]:
    """Lignes de la tornade triées par amplitude décroissante."""
    rows: list[TornadoRow] = []
    gaps = paired_gaps(records)
    if len(gaps) >= 2:
        typical = [r.pnl for r in records if r.mode == "typical"]
        optimized = [r.pnl for r in records if r.mode == "optimized"]
        mean_gap, se_gap = _stats(gaps)
        ordered = sorted(
            [("Typique", typical), ("Optimisé", optimized)], key=lambda g: statistics.fmean(g[1])
        )
        row = _row("Calibrage", ordered[0], ordered[1])
        rows.append(
            TornadoRow(
                row.variable,
                row.low_label,
                row.low_pnl,
                row.low_n,
                row.high_label,
                row.high_pnl,
                row.high_n,
                abs(mean_gap) > 2 * se_gap,
            )
        )
    for cat in CATEGORICALS:
        groups: dict[str, list[float]] = {}
        for r in records:
            value = r.draw.get(cat.name)
            if value is not None:
                groups.setdefault(str(value), []).append(r.pnl)
        groups = {k: v for k, v in groups.items() if len(v) >= 2}
        if len(groups) < 2:
            continue
        ranked = sorted(groups.items(), key=lambda g: statistics.fmean(g[1]))
        (low_key, low), (high_key, high) = ranked[0], ranked[-1]
        rows.append(
            _row(
                VARIABLE_LABELS[cat.name],
                (OPTION_LABELS.get(low_key, low_key), low),
                (OPTION_LABELS.get(high_key, high_key), high),
            )
        )
    for var in NUMERICS:
        # Tri sur la valeur seule (ordre des simulations en cas d'égalité) : trier aussi sur
        # le P&L exagérerait l'effet des variables entières à valeurs répétées.
        points: list[tuple[float, float]] = sorted(
            ((float(str(v)), r.pnl) for r in records if (v := r.draw.get(var.name)) is not None),
            key=lambda point: point[0],
        )
        third = len(points) // 3
        if third < 2:
            continue
        bottom, top = points[:third], points[-third:]
        rows.append(
            _row(
                VARIABLE_LABELS[var.name],
                (f"Tiers bas (≤ {_number(bottom[-1][0])})", [p for _, p in bottom]),
                (f"Tiers haut (≥ {_number(top[0][0])})", [p for _, p in top]),
            )
        )
    return sorted(rows, key=lambda r: r.amplitude, reverse=True)
