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


async def test_paper_buy_and_sell_on_real_book(conn: object) -> None:
    from decimal import Decimal

    from mcs.broker.base import Order, Reason
    from mcs.broker.book import CachedBookProvider
    from mcs.broker.fees import FeeSchedule
    from mcs.broker.paper import PaperBroker
    from mcs.broker.rules import MarketRules

    async with BitvavoRest() as rest:
        (info,) = await rest.get_markets(["PEPE-EUR"])
        rules = MarketRules(
            info.market,
            info.status,
            info.tick_size,
            info.quantity_decimals,
            info.min_order_base,
            info.min_order_quote,
        )
        conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO simulations (id, pair_id, mode, family, config_json, seed, created_at)"
            " VALUES (1, 1, 'typical', 'ema', '{}', 1, 0)"
        )
        broker = PaperBroker(
            conn,
            FeeSchedule(),
            CachedBookProvider(rest),  # type: ignore[arg-type]
            rules={"PEPE-EUR": rules},
            spread=lambda _: None,
        )
        broker.open_account(1, Decimal("1000"))
        buy = await broker.buy_market(1, "PEPE-EUR", Decimal("50"), Reason.SIGNAL, now=1)
        assert isinstance(buy, Order)
        sell = await broker.sell_market(1, "PEPE-EUR", buy.quantity, Reason.STOP, now=2)
        assert isinstance(sell, Order)
        assert broker.holdings(1) == {}
        # Aller-retour immédiat : on perd frais + spread + pénalité, mais pas plus de 2 %.
        assert Decimal("980") < broker.cash(1) < Decimal("1000")
