from decimal import Decimal

import httpx
import pytest
import respx

from mcs.bitvavo.rest import BASE_URL, BitvavoError, BitvavoRest, RateLimiter, parse_market
from tests.conftest import FakeBitvavo

H = 3_600_000


async def test_get_candles_sorted_ascending_with_decimals(
    fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1h", 0, 3)
    candles = await rest.get_candles("BTC-EUR", "1h")
    assert [c.ts for c in candles] == [0, H, 2 * H]
    assert candles[0].close == Decimal("10.5")


async def test_backward_pagination_covers_full_history(
    fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1h", 0, 3000)
    pages = [p async for p in rest.iter_candles_backward("BTC-EUR", "1h", end=3000 * H)]
    assert [len(p) for p in pages] == [1440, 1440, 120]
    all_ts = sorted(c.ts for p in pages for c in p)
    assert all_ts == [i * H for i in range(3000)]
    # Chaque page repart de la plus ancienne bougie de la précédente (end exclu).
    assert [call["end"] for call in fake.candle_calls] == [3000 * H, 1560 * H, 120 * H]


async def test_backward_pagination_stops_at_start(
    fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1h", 0, 3000)
    pages = [
        p async for p in rest.iter_candles_backward("BTC-EUR", "1h", end=3000 * H, start=1000 * H)
    ]
    assert min(c.ts for p in pages for c in p) == 1000 * H
    assert sum(len(p) for p in pages) == 2000


async def test_backward_pagination_crosses_gaps_when_bounded(
    fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    # Pas de bougie sans transaction : un trou de 2 900 h (plus de deux fenêtres) au milieu.
    rows = [[i * H, "1", "1", "1", "1", "1"] for i in [*range(100), *range(3000, 3100)]]
    fake.series[("BTC-EUR", "1h")] = rows
    pages = [p async for p in rest.iter_candles_backward("BTC-EUR", "1h", end=3100 * H, start=0)]
    assert sum(len(p) for p in pages) == 200


async def test_unbounded_pagination_stops_at_first_empty_window(
    fake_api: tuple[FakeBitvavo, respx.MockRouter], rest: BitvavoRest
) -> None:
    fake, _ = fake_api
    fake.add_series("BTC-EUR", "1d", 5000 * 86_400_000, 10)
    pages = [p async for p in rest.iter_candles_backward("BTC-EUR", "1d", end=5010 * 86_400_000)]
    assert sum(len(p) for p in pages) == 10
    assert len(fake.candle_calls) == 2


async def test_retries_on_server_error_then_succeeds(rest: BitvavoRest) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/markets").mock(
            side_effect=[
                httpx.Response(503, text="indisponible"),
                httpx.ConnectError("coupure"),
                httpx.Response(200, json=[]),
            ]
        )
        assert await rest.get_markets() == []
        assert router.calls.call_count == 3


async def test_client_error_is_raised(rest: BitvavoRest) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/XXX-EUR/candles").mock(
            return_value=httpx.Response(400, json={"errorCode": 205, "error": "market invalide"})
        )
        with pytest.raises(BitvavoError) as info:
            await rest.get_candles("XXX-EUR", "1h")
        assert info.value.code == 205


async def test_rate_limit_error_waits_for_reset() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = RateLimiter(clock=lambda: 100.0, sleep=record)
    client = BitvavoRest(httpx.AsyncClient(base_url=BASE_URL), limiter=limiter, sleep=record)
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/markets").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"errorCode": 110, "error": "limit"},
                    headers={
                        "bitvavo-ratelimit-remaining": "0",
                        "bitvavo-ratelimit-resetat": "130000",
                    },
                ),
                httpx.Response(200, json=[]),
            ]
        )
        await client.get_markets()
    assert sleeps == [30.5]


async def test_limiter_pauses_before_exhausting_budget() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = RateLimiter(safety_margin=100, clock=lambda: 10.0, sleep=record)
    limiter.update(
        httpx.Headers({"bitvavo-ratelimit-remaining": "102", "bitvavo-ratelimit-resetat": "15000"})
    )
    await limiter.acquire(1)
    assert sleeps == [] and limiter.remaining == 101
    await limiter.acquire(5)
    assert sleeps == [5.5] and limiter.remaining is None


def test_parse_market_reads_precisions() -> None:
    info = parse_market(
        {
            "market": "PEPE-EUR",
            "status": "trading",
            "base": "PEPE",
            "quote": "EUR",
            "pricePrecision": None,
            "minOrderInBaseAsset": "1303394",
            "minOrderInQuoteAsset": "5.00",
            "maxOrderInBaseAsset": "260678880220136",
            "maxOrderInQuoteAsset": "1000000000.00",
            "quantityDecimals": 0,
            "notionalDecimals": 2,
            "tickSize": "0.000000000100",
            "feeCategory": "A",
        }
    )
    assert info.tick_size == Decimal("1E-10")
    assert info.quantity_decimals == 0
    assert info.min_order_quote == Decimal("5.00")
