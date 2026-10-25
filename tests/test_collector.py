import sqlite3
from decimal import Decimal

import respx

from mcs.bitvavo.rest import BitvavoRest
from mcs.collector import DAY_MS, backfill, check_consistency, history_start, sync_candles
from mcs.market_data import Candle, floor_ts
from mcs.storage.candles import count_candles, ts_bounds, upsert_candles
from mcs.storage.db import now_ms
from tests.conftest import FakeBitvavo, make_candle

H = 3_600_000
M = 60_000


async def test_sync_fetches_history_then_only_new_closed_candles(
    conn: sqlite3.Connection, fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1h", 0, 3001)  # la 3001e bougie (ts 3000 h) est en cours
    now = 3000 * H + 10 * M

    first = await sync_candles(conn, rest, "BTC-EUR", "1h", now)
    assert (first.recent, first.older) == (0, 3000)
    assert ts_bounds(conn, "BTC-EUR", "1h") == (0, 2999 * H)  # bougie en cours exclue

    # Une heure plus tard : seule la nouvelle bougie clôturée (plus la dernière, rafraîchie).
    fake.add_series("BTC-EUR", "1h", 0, 3002)
    fake.candle_calls.clear()
    second = await sync_candles(conn, rest, "BTC-EUR", "1h", now + H)
    assert second.recent == 2
    assert count_candles(conn, "BTC-EUR", "1h") == 3001
    # Une seule requête : la plus ancienne bougie est déjà au début de l'historique (ts 0).
    assert len(fake.candle_calls) == 1


async def test_sync_bounds_intraday_history_by_first_daily_candle(
    conn: sqlite3.Connection, fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    # Cotation au jour 100 ; deux longues périodes sans transaction (fenêtres vides).
    fake.add_series("NIL-EUR", "1d", 100 * DAY_MS, 200)
    hours = [100 * DAY_MS + i * H for i in range(10)] + [
        100 * DAY_MS + i * H for i in range(3000, 3010)
    ]
    fake.series[("NIL-EUR", "1h")] = [[ts, "1", "1", "1", "1", "1"] for ts in hours]
    now = 300 * DAY_MS
    await sync_candles(conn, rest, "NIL-EUR", "1d", now)
    result = await sync_candles(conn, rest, "NIL-EUR", "1h", now)
    assert result.older == 20
    assert min(c["start"] for c in fake.candle_calls if c["interval"] == "1h") == 100 * DAY_MS


async def test_sync_fills_gap_after_long_outage(
    conn: sqlite3.Connection, fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1m", 0, 5000)
    upsert_candles(conn, [make_candle("BTC-EUR", "1m", 0), make_candle("BTC-EUR", "1m", M)])
    await sync_candles(conn, rest, "BTC-EUR", "1m", 5000 * M, start=0)
    assert count_candles(conn, "BTC-EUR", "1m") == 5000


async def test_minute_history_is_limited(
    conn: sqlite3.Connection, fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    now = 10 * DAY_MS
    fake.add_series("BTC-EUR", "1m", 0, 10 * 1440)
    start = history_start("1m", now, minute_days=2)
    assert start == 8 * DAY_MS
    await sync_candles(conn, rest, "BTC-EUR", "1m", now, start)
    assert count_candles(conn, "BTC-EUR", "1m") == 2 * 1440
    assert history_start("1d", now, 2) is None


async def test_backfill_runs_each_market_and_interval(
    conn: sqlite3.Connection, fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    today = floor_ts(now_ms(), "1d")
    fake.add_series("BTC-EUR", "1d", today - 5 * DAY_MS, 5)
    fake.add_series("PEPE-EUR", "1d", today - 3 * DAY_MS, 3)
    results = await backfill(conn, rest, ["BTC-EUR", "PEPE-EUR"], ["1d"])
    assert [(r.market, r.older) for r in results] == [("BTC-EUR", 5), ("PEPE-EUR", 3)]


def test_consistency_check_detects_mismatch(conn: sqlite3.Connection) -> None:
    def minute(i: int, price: str) -> Candle:
        p = Decimal(price)
        return Candle("BTC-EUR", "1m", i * M, p, p, p, p, Decimal(1))

    upsert_candles(conn, [minute(0, "10"), minute(30, "12"), minute(59, "11")])
    official = Candle(
        "BTC-EUR", "1h", 0, Decimal(10), Decimal(12), Decimal(10), Decimal(11), Decimal(3)
    )
    upsert_candles(conn, [official])
    assert check_consistency(conn, "BTC-EUR", "1h", 0) == []

    upsert_candles(
        conn,
        [
            Candle(
                "BTC-EUR", "1h", 0, Decimal(10), Decimal(13), Decimal(10), Decimal(11), Decimal(3)
            )
        ],
    )
    assert check_consistency(conn, "BTC-EUR", "1h", 0) == ["high : officiel 13 / 1 min 12"]


def test_consistency_tolerates_volume_rounding_and_needs_full_minutes(
    conn: sqlite3.Connection,
) -> None:
    p = Decimal(10)
    upsert_candles(conn, [Candle("ETH-EUR", "1m", 30 * M, p, p, p, p, Decimal("110510752.5918"))])
    official = Candle("ETH-EUR", "1h", 0, p, p, p, p, Decimal("110510752.59180005"))
    upsert_candles(conn, [official])
    # L'historique 1 min commence au milieu de l'heure : pas de comparaison possible.
    assert check_consistency(conn, "ETH-EUR", "1h", 0) == []
    upsert_candles(conn, [Candle("ETH-EUR", "1m", 0, p, p, p, p, Decimal(0))])
    assert check_consistency(conn, "ETH-EUR", "1h", 0) == []  # écart d'arrondi toléré
    upsert_candles(conn, [Candle("ETH-EUR", "1h", 0, p, p, p, p, Decimal(100))])
    assert check_consistency(conn, "ETH-EUR", "1h", 0)[0].startswith("volume")
