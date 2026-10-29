"""Flux WebSocket Bitvavo (public) : ticker et bougies 1 min, avec reconnexion automatique.

Formats observés (oct. 2026) sur `wss://ws.bitvavo.com/v2/` :
- `{"event": "ticker", "market": ..., "bestBid": ..., "bestAsk": ..., "lastPrice": ...}` :
  seuls les champs modifiés sont envoyés, l'état est donc fusionné par marché ;
- `{"event": "candle", "market": ..., "interval": "1m", "candle": [[ts, o, h, l, c, v]]}` :
  bougie en cours, renvoyée à chaque transaction (elle n'est clôturée qu'au changement de minute).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets

from mcs.bitvavo.rest import parse_candles
from mcs.market_data import Candle

log = logging.getLogger(__name__)

WS_URL = "wss://ws.bitvavo.com/v2/"
MAX_BACKOFF_S = 60.0
STABLE_CONNECTION_S = 60.0

CandleHandler = Callable[[Candle], Awaitable[None]]
ReconnectHandler = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class TickerState:
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    last_price: Decimal | None = None
    updated_ms: int = 0


class BitvavoWebSocket:
    def __init__(
        self,
        markets: Sequence[str],
        *,
        on_candle: CandleHandler | None = None,
        on_reconnect: ReconnectHandler | None = None,
        connect: Callable[..., Any] = websockets.connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.markets = list(markets)
        self.tickers: dict[str, TickerState] = {m: TickerState() for m in self.markets}
        self._on_candle, self._on_reconnect = on_candle, on_reconnect
        self._connect, self._sleep, self._clock = connect, sleep, clock
        self.connections = 0

    def subscribe_message(self) -> str:
        return json.dumps(
            {
                "action": "subscribe",
                "channels": [
                    {"name": "ticker", "markets": self.markets},
                    {"name": "candles", "interval": ["1m"], "markets": self.markets},
                ],
            }
        )

    async def run(self) -> None:
        """Boucle infinie (à annuler pour arrêter) : connexion, abonnement, reconnexion."""
        backoff = 1.0
        disconnected_at: float | None = None
        while True:
            connected_at = self._clock()
            try:
                async with self._connect(
                    WS_URL, open_timeout=30, ping_interval=20, ping_timeout=60
                ) as ws:
                    await ws.send(self.subscribe_message())
                    self.connections += 1
                    if disconnected_at is not None:
                        downtime = self._clock() - disconnected_at
                        log.info("WebSocket reconnecté après %.1f s de coupure.", downtime)
                        if self._on_reconnect is not None:
                            await self._on_reconnect(downtime)
                    else:
                        log.info("WebSocket connecté (%d marchés).", len(self.markets))
                    async for raw in ws:
                        await self.handle_message(raw)
                    reason = "fermeture par le serveur"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # toute coupure doit mener à une reconnexion
                reason = f"{type(exc).__name__}: {exc}"
            disconnected_at = self._clock()
            if disconnected_at - connected_at >= STABLE_CONNECTION_S:
                backoff = 1.0
            log.warning("WebSocket coupé (%s), reconnexion dans %.0f s.", reason, backoff)
            await self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def handle_message(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            log.warning("Message WebSocket illisible : %r", raw[:200])
            return
        if not isinstance(message, dict):
            return
        event = message.get("event")
        if event == "ticker":
            self._update_ticker(message)
        elif event == "candle":
            if self._on_candle is None:
                return
            for candle in parse_candles(
                message["market"], message["interval"], message.get("candle", [])
            ):
                await self._on_candle(candle)
        elif event == "subscribed":
            log.debug("Abonnements : %s", message.get("subscriptions"))
        elif "error" in message:
            log.error("Erreur WebSocket Bitvavo : %s", message)

    def _update_ticker(self, message: dict[str, Any]) -> None:
        state = self.tickers.setdefault(message["market"], TickerState())
        for key, attr in (
            ("bestBid", "best_bid"),
            ("bestAsk", "best_ask"),
            ("lastPrice", "last_price"),
        ):
            value = message.get(key)
            if value not in (None, ""):
                setattr(state, attr, Decimal(str(value)))
        state.updated_ms = int(self._clock() * 1000)
