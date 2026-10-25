"""Métriques, buy & hold, valorisation, synthèse et tornade."""

import random
import sqlite3
from decimal import Decimal

import pytest

from mcs.metrics import (
    HoldingLine,
    SimRecord,
    buy_and_hold_detail,
    buy_and_hold_series,
    buy_and_hold_value,
    cash_balance,
    compute_metrics,
    load_metrics,
    positions_detail,
    synthesis,
    tornado,
    write_metrics,
)
from mcs.sampling import draw_plan
from tests.conftest import make_candle

D = Decimal
TAKER = D("0.0025")
HOUR = 3_600_000


def test_metrics_drawdown_and_win_rate() -> None:
    m = None
    for value in (D(1000), D(1100), D(990), D(1050)):
        m = compute_metrics(1, value, [D(5), D(-2), D(3)], D("4.2"), 2, D(20), m)
    assert m is not None
    assert m.pnl_eur == D(50) and m.pnl_pct == D(5)
    assert m.peak_value == D(1100)
    assert m.max_drawdown_pct == D(10)  # 1 100 → 990
    assert m.closed_trades == 3 and m.win_rate == D(2) / 3
    assert m.bh_gap_eur == D(30) and m.total_fees == D("4.2") and m.open_positions == 2
    empty = compute_metrics(2, D(1000), [], D(0), 0, None)
    assert empty.win_rate is None and empty.bh_gap_eur is None


def test_metrics_round_trip(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO simulations (id, pair_id, mode, family, config_json, seed, created_at)"
        " VALUES (1, 1, 'typical', 'macd', '{}', 1, 0)"
    )
    metrics = compute_metrics(1, D("1012.5"), [D(1)], D("0.5"), 1, D(-3))
    write_metrics(conn, [metrics], 1)
    write_metrics(conn, [metrics], 2)
    assert load_metrics(conn) == {1: metrics}


LINES = [
    HoldingLine("AAA-EUR", D("0.5"), D(500), D("1.24"), D(100), D("4.95")),
    HoldingLine("BBB-EUR", D("0.5"), D(500), D("1.24"), D(10), D("49.5")),
]


def test_buy_and_hold_value_and_detail() -> None:
    assert LINES[0].leftover == D(500) - D(495) - D("1.24")
    value = buy_and_hold_value(LINES, {"AAA-EUR": D(110), "BBB-EUR": D(9)}, TAKER)
    expected = (D("4.95") * 110 + D("49.5") * 9) * (1 - TAKER) + 2 * (D(5) - D("1.24"))
    assert value == expected
    assert buy_and_hold_value([], {}, TAKER) is None
    detail = buy_and_hold_detail(LINES, {"AAA-EUR": D(110), "BBB-EUR": D(9)}, TAKER)
    assert [d.market for d in detail] == ["AAA-EUR", "BBB-EUR"]  # trié par P&L
    assert detail[0].change_pct == D(10) and detail[1].change_pct == D(-10)
    assert sum(d.pnl_eur for d in detail) == expected - 1000


def test_buy_and_hold_series_from_hourly_closes(conn: sqlite3.Connection) -> None:
    from mcs.storage.candles import upsert_candles

    upsert_candles(
        conn, [make_candle("AAA-EUR", "1h", 0, "100"), make_candle("AAA-EUR", "1h", HOUR, "120")]
    )
    upsert_candles(conn, [make_candle("BBB-EUR", "1h", 0, "10")])
    series = buy_and_hold_series(conn, LINES, TAKER, 0, 2 * HOUR)
    assert [ts for ts, _ in series] == [HOUR, 2 * HOUR]
    assert series[1][1] > series[0][1]  # AAA passe de 100 à 120, BBB reste au dernier cours


def test_positions_detail_and_cash(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO simulations (id, pair_id, mode, family, config_json, seed, created_at)"
        " VALUES (1, 1, 'typical', 'macd', '{}', 1, 0)"
    )
    conn.execute(
        """INSERT INTO orders (sim_id, market, order_type, side, status, price, quantity,
               notional, fee, reason, created_at) VALUES
           (1, 'AAA-EUR', 'market', 'buy', 'filled', '100', '2', '200', '0.5', 'signal', 1),
           (1, 'AAA-EUR', 'market', 'sell', 'filled', '110', '1', '110', '0.275', 'take_profit', 2),
           (1, 'AAA-EUR', 'limit', 'buy', 'open', '90', '1', NULL, NULL, 'signal', 3)"""
    )
    conn.execute(
        """INSERT INTO positions (sim_id, market, lots, quantity, avg_entry_price,
               invested_cost, buy_fees, stop, trailing, take_profit, opened_at, updated_at)
           VALUES (1, 'AAA-EUR', 1, '1', '100', '100.25', '0.25', '95', NULL, NULL, 1, 2)"""
    )
    assert cash_balance(conn, 1) == D(1000) - D("200.5") + D("109.725")
    (detail,) = positions_detail(conn, 1, {"AAA-EUR": D(120)}, TAKER)
    assert detail.value == D(120) and detail.sell_fees_estimate == D("0.3")
    assert detail.unrealized_pnl == D(120) - D("0.3") - D("100.25")
    assert detail.stop == D(95) and detail.trailing is None
    (unpriced,) = positions_detail(conn, 1, {}, TAKER)
    assert unpriced.value is None and unpriced.unrealized_pnl is None


# --- Synthèse et tornade --------------------------------------------------------------------


def make_records(seed: int = 11) -> list[SimRecord]:
    """Plan réel de 150 paires, P&L aux effets connus : stop ATR +60 €, montant max par entrée
    +200 € par unité, jumelle optimisée +15 €, bruit de 10 €."""
    rng = random.Random(seed)
    records = []
    for k, draw in enumerate(draw_plan(seed), start=1):
        base = (60 if draw["stop_type"] == "atr" else 0) + 200 * draw["max_entry"]
        for sim_id, mode, bonus in ((2 * k - 1, "typical", 0), (2 * k, "optimized", 15)):
            pnl = base + bonus + rng.gauss(0, 10)
            records.append(SimRecord(sim_id, k, mode, draw["family"], draw, pnl, 12))
    return records


def test_tornado_finds_known_effects() -> None:
    records = make_records()
    rows = {r.variable: r for r in tornado(records)}
    stop = rows["Stop initial"]
    assert stop.high_label == "Multiple ATR" and stop.significant
    assert stop.high_pnl - stop.low_pnl == pytest.approx(60, abs=12)
    entry = rows["Montant max par entrée"]
    assert entry.significant and entry.low_label.startswith("Tiers bas (≤ ")
    assert entry.low_n == entry.high_n == 100  # 300 simulations : tiers de 100
    calibration = rows["Calibrage"]
    assert calibration.high_label == "Optimisé" and calibration.significant
    # Variables sans effet : peu de barres « significatives ». Le test pire / meilleure option
    # à 2 erreurs-types dépasse le taux nominal de 5 % (choix des extrêmes parmi k options,
    # déséquilibres d'échantillonnage avec la variable à fort effet) : tolérance de 15 %.
    effects = {"Stop initial", "Montant max par entrée", "Calibrage"}
    stop_params = {"Stop multiple d'ATR", "Stop % fixe", "Stop plus bas des N bougies"}
    neutral = [r for name, r in rows.items() if name not in effects | stop_params]
    assert sum(r.significant for r in neutral) <= 0.15 * len(neutral)
    ordered = tornado(records)
    assert [r.amplitude for r in ordered] == sorted((r.amplitude for r in ordered), reverse=True)


def test_tornado_conditional_variables_use_their_subgroup() -> None:
    records = make_records()
    rows = {r.variable: r for r in tornado(records)}
    atr = rows["Stop multiple d'ATR"]
    subgroup = sum(1 for r in records if r.draw["stop_type"] == "atr")
    assert atr.low_n == atr.high_n == subgroup // 3
    filter_type = rows["Type de filtre de fond"]
    with_filter = sum(1 for r in records if r.draw["trend_enabled"] == "yes")
    assert filter_type.low_n + filter_type.high_n <= with_filter


def test_synthesis() -> None:
    records = [
        SimRecord(1, 1, "typical", "macd", {}, -10.0, 4),
        SimRecord(2, 1, "optimized", "macd", {}, 30.0, 8),
        SimRecord(3, 2, "typical", "donchian", {}, 5.0, 12),
        SimRecord(4, 2, "optimized", "donchian", {}, 1.0, 10),
    ]
    s = synthesis(records, bh_pnl=2.0)
    assert s is not None
    assert (s.count, s.pnl_median, s.best_pnl, s.best_sim) == (4, 3.0, 30.0, 2)
    assert (s.beat_bh, s.beat_bh_share) == (2, 0.5)
    assert s.optimized_gap_mean == pytest.approx((40 - 4) / 2)
    assert s.trades_mean == 8.5 and s.preliminary
    assert synthesis([], None) is None
