"""Tests contre la vraie API publique Bitvavo (désactivés par défaut : MCS_LIVE_TESTS=1)."""

import os
import time

import pytest

from mcs.bitvavo.rest import BitvavoRest
from mcs.market_data import INTERVAL_MS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("MCS_LIVE_TESTS") != "1", reason="MCS_LIVE_TESTS != 1"),
]


async def test_markets_and_candles_contract() -> None:
    async with BitvavoRest() as rest:
        (pepe,) = await rest.get_markets(["PEPE-EUR"])
        assert pepe.tick_size is not None and pepe.min_order_quote is not None
        candles = await rest.get_candles("BTC-EUR", "4h", limit=3)
        assert len(candles) == 3
        assert all(c.ts % INTERVAL_MS["4h"] == 0 for c in candles)
        assert candles[-1].ts + INTERVAL_MS["4h"] > time.time() * 1000  # la dernière est en cours
        book = await rest.get_book("BTC-EUR", depth=1)
        assert book.bids[0][0] < book.asks[0][0]
