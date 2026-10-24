import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from mcs.bitvavo.ws import BitvavoWebSocket
from mcs.market_data import Candle


async def test_ticker_events_are_merged() -> None:
    ws = BitvavoWebSocket(["BTC-EUR"], clock=lambda: 1.0)
    await ws.handle_message('{"event":"ticker","market":"BTC-EUR","bestBid":"71616"}')
    await ws.handle_message('{"event":"ticker","market":"BTC-EUR","bestAsk":"71617"}')
    state = ws.tickers["BTC-EUR"]
    assert (state.best_bid, state.best_ask, state.updated_ms) == (
        Decimal("71616"),
        Decimal("71617"),
        1000,
    )


async def test_candle_event_dispatched() -> None:
    received: list[Candle] = []

    async def on_candle(candle: Candle) -> None:
        received.append(candle)

    ws = BitvavoWebSocket(["BTC-EUR"], on_candle=on_candle)
    await ws.handle_message(
        json.dumps(
            {
                "event": "candle",
                "market": "BTC-EUR",
                "interval": "1m",
                "candle": [[1792862520000, "71608", "71608", "71594", "71594", "0.08896443"]],
            }
        )
    )
    assert received[0].ts == 1792862520000 and received[0].low == Decimal("71594")


async def test_garbage_is_ignored() -> None:
    ws = BitvavoWebSocket(["BTC-EUR"])
    await ws.handle_message("pas du json")
    await ws.handle_message("[1, 2]")


class _FakeConnection:
    def __init__(self, messages: list[str], fail: bool) -> None:
        self.messages, self.fail, self.sent = messages, fail, []

    async def __aenter__(self) -> "_FakeConnection":
        if self.fail:
            raise OSError("réseau coupé")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "_FakeConnection":
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


async def test_reconnects_with_backoff_and_reports_downtime() -> None:
    attempts = iter(
        [
            _FakeConnection(['{"event":"ticker","market":"BTC-EUR","lastPrice":"1"}'], fail=False),
            _FakeConnection([], fail=True),
            _FakeConnection(['{"event":"ticker","market":"BTC-EUR","lastPrice":"2"}'], fail=False),
        ]
    )
    sleeps: list[float] = []
    downtimes: list[float] = []

    def connect(*_: Any, **__: Any) -> _FakeConnection:
        try:
            return next(attempts)
        except StopIteration:
            raise asyncio.CancelledError from None

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def on_reconnect(downtime: float) -> None:
        downtimes.append(downtime)

    ws = BitvavoWebSocket(
        ["BTC-EUR"], connect=connect, sleep=sleep, on_reconnect=on_reconnect, clock=lambda: 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await ws.run()
    assert sleeps == [1.0, 2.0, 4.0]
    assert ws.connections == 2 and len(downtimes) == 1
    assert ws.tickers["BTC-EUR"].last_price == Decimal("2")
