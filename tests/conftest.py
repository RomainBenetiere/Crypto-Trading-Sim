from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcs.bitvavo.rest import BASE_URL, MAX_CANDLES, BitvavoRest, RateLimiter
from mcs.config import JournalMode
from mcs.market_data import INTERVAL_MS, Candle
from mcs.storage.db import connect


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite", JournalMode.WAL)
    yield connection
    connection.close()


def make_candle(
    market: str, interval: str, ts: int, price: str = "100", volume: str = "1"
) -> Candle:
    p = Decimal(price)
    return Candle(market, interval, ts, p, p + 1, p - 1, p, Decimal(volume))


async def no_sleep(_: float) -> None:
    return None


class FakeBitvavo:
    """Simule `GET /{market}/candles` (ordre décroissant, start inclus, end exclu, limite 1440)
    et `GET /ticker/24h` à partir de séries en mémoire."""

    def __init__(self) -> None:
        self.series: dict[tuple[str, str], list[list[Any]]] = {}
        self.candle_calls: list[dict[str, Any]] = []

    def add_series(self, market: str, interval: str, start: int, count: int) -> None:
        step = INTERVAL_MS[interval]
        self.series[(market, interval)] = [
            [start + i * step, "10", "11", "9", "10.5", "2"] for i in range(count)
        ]

    def candles(self, request: httpx.Request, market: str) -> httpx.Response:
        params = request.url.params
        interval = params["interval"]
        start = int(params.get("start", 0))
        end = int(params.get("end", 2**62))
        limit = int(params.get("limit", MAX_CANDLES))
        self.candle_calls.append(
            {"market": market, "interval": interval, "start": start, "end": end}
        )
        rows = [r for r in self.series.get((market, interval), []) if start <= r[0] < end]
        rows.sort(key=lambda r: r[0], reverse=True)
        headers = {
            "bitvavo-ratelimit-remaining": "900",
            "bitvavo-ratelimit-resetat": "0",
            "bitvavo-ratelimit-limit": "1000",
        }
        return httpx.Response(200, json=rows[:limit], headers=headers)


@pytest.fixture
def fake_api() -> Iterator[tuple[FakeBitvavo, respx.MockRouter]]:
    fake = FakeBitvavo()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get(path__regex=r"/(?P<market>[A-Z0-9]+-EUR)/candles").mock(side_effect=fake.candles)
        router.get("/ticker/24h").mock(
            return_value=httpx.Response(
                200,
                json={
                    "market": "BTC-EUR",
                    "last": "71617",
                    "bid": "71616",
                    "ask": "71617",
                    "volumeQuote": "38468658.67",
                    "timestamp": 1,
                },
            )
        )
        yield fake, router


@pytest.fixture
async def rest() -> BitvavoRest:
    return BitvavoRest(
        httpx.AsyncClient(base_url=BASE_URL), limiter=RateLimiter(sleep=no_sleep), sleep=no_sleep
    )
