"""Client REST Bitvavo, données publiques uniquement.

Points vérifiés dans la documentation officielle (OpenAPI `exchange-rest-api.yaml`, oct. 2026) :
- limite de 1 000 points de poids par minute et par IP, en-têtes `bitvavo-ratelimit-*` ;
- `GET /{market}/candles` : poids 1, au plus 1 440 bougies, de la plus récente à la plus ancienne,
  `start` inclus et `end` exclu, alignés sur l'intervalle (UTC) ; pas de bougie sans transaction ;
- `GET /markets` : `tickSize`, `quantityDecimals`, `notionalDecimals`, minimums d'ordre
  (`pricePrecision` est désormais nul) ;
- `GET /{market}/book` : poids 1 ; `GET /ticker/24h?market=` : poids 1.

Ce module ne contient volontairement aucun endpoint d'ordre ni d'authentification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from mcs.market_data import INTERVAL_MS, Candle
from mcs.storage.markets import MarketInfo

log = logging.getLogger(__name__)

BASE_URL = "https://api.bitvavo.com/v2"
MAX_CANDLES = 1440
SAFETY_MARGIN = 100
MAX_ATTEMPTS = 6

Sleep = Callable[[float], Awaitable[None]]


class BitvavoError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str) -> None:
        super().__init__(f"Bitvavo HTTP {status} (code {code}) : {message}")
        self.status, self.code = status, code


@dataclass(frozen=True, slots=True)
class Ticker24h:
    market: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    volume_quote: Decimal | None
    ts: int


@dataclass(frozen=True, slots=True)
class OrderBook:
    market: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    ts_ns: int


class RateLimiter:
    """Suit le budget de poids annoncé par Bitvavo et attend la remise à zéro s'il s'épuise."""

    def __init__(
        self,
        safety_margin: int = SAFETY_MARGIN,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.safety_margin = safety_margin
        self.remaining: int | None = None
        self.reset_at_ms: int | None = None
        self._clock, self._sleep = clock, sleep
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            if self.remaining is None or self.reset_at_ms is None:
                return
            if self.remaining - weight < self.safety_margin:
                wait = self.reset_at_ms / 1000 - self._clock()
                if wait > 0:
                    log.info("Limite de requêtes presque atteinte : pause de %.1f s.", wait + 0.5)
                    await self._sleep(wait + 0.5)
                self.remaining = None
                return
            self.remaining -= weight

    def update(self, headers: httpx.Headers) -> None:
        remaining = headers.get("bitvavo-ratelimit-remaining")
        reset_at = headers.get("bitvavo-ratelimit-resetat")
        if remaining is not None and reset_at is not None:
            self.remaining, self.reset_at_ms = int(remaining), int(reset_at)

    async def wait_until_reset(self, default_s: float = 60.0) -> None:
        wait = default_s
        if self.reset_at_ms is not None:
            wait = max(self.reset_at_ms / 1000 - self._clock(), 1.0)
        await self._sleep(wait + 0.5)
        self.remaining = None


def _dec(value: Any) -> Decimal | None:
    return None if value is None or value == "" else Decimal(str(value))


def parse_candles(market: str, interval: str, payload: list[list[Any]]) -> list[Candle]:
    """Convertit la réponse Bitvavo en bougies triées par date croissante."""
    candles = [
        Candle(
            market=market,
            interval=interval,
            ts=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
        )
        for row in payload
    ]
    candles.sort(key=lambda c: c.ts)
    return candles


def parse_market(data: dict[str, Any]) -> MarketInfo:
    def integer(key: str) -> int | None:
        value = data.get(key)
        return None if value is None else int(value)

    return MarketInfo(
        market=data["market"],
        base=data["base"],
        quote=data["quote"],
        status=data["status"],
        tick_size=_dec(data.get("tickSize")),
        quantity_decimals=integer("quantityDecimals"),
        notional_decimals=integer("notionalDecimals"),
        min_order_base=_dec(data.get("minOrderInBaseAsset")),
        min_order_quote=_dec(data.get("minOrderInQuoteAsset")),
        max_order_base=_dec(data.get("maxOrderInBaseAsset")),
        max_order_quote=_dec(data.get("maxOrderInQuoteAsset")),
        fee_category=data.get("feeCategory"),
    )


class BitvavoRest:
    """Client asynchrone des endpoints publics de données de marché."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BitvavoRest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None, weight: int = 1) -> Any:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self.limiter.acquire(weight)
            try:
                response = await self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise
                delay = min(2**attempt, 60)
                log.warning("Erreur réseau sur %s (%s), nouvel essai dans %d s.", path, exc, delay)
                await self._sleep(delay)
                continue
            self.limiter.update(response.headers)
            if response.status_code == 200:
                return response.json()
            code, message = _error_details(response)
            # 429 / codes 105 (banni) et 110 (limite) : attendre la remise à zéro du budget.
            if response.status_code == 429 or code in (105, 110):
                log.warning("Limite Bitvavo atteinte sur %s (%s) : attente.", path, message)
                await self.limiter.wait_until_reset()
                continue
            if response.status_code >= 500 and attempt < MAX_ATTEMPTS:
                delay = min(2**attempt, 60)
                log.warning(
                    "Erreur %d sur %s, nouvel essai dans %d s.", response.status_code, path, delay
                )
                await self._sleep(delay)
                continue
            raise BitvavoError(response.status_code, code, message)
        raise BitvavoError(0, None, f"échec après {MAX_ATTEMPTS} tentatives sur {path}")

    async def get_markets(self, markets: Iterable[str] | None = None) -> list[MarketInfo]:
        payload = await self._get("/markets")
        infos = [parse_market(item) for item in payload]
        if markets is None:
            return infos
        wanted = set(markets)
        return [i for i in infos if i.market in wanted]

    async def get_candles(
        self,
        market: str,
        interval: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = MAX_CANDLES,
    ) -> list[Candle]:
        params: dict[str, Any] = {"interval": interval, "limit": limit}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        payload = await self._get(f"/{market}/candles", params)
        return parse_candles(market, interval, payload)

    async def iter_candles_backward(
        self, market: str, interval: str, *, end: int, start: int | None = None
    ) -> AsyncIterator[list[Candle]]:
        """Pages de bougies dans [start, end), de la plus récente à la plus ancienne.

        Chaque page est triée par date croissante. Bitvavo ne renvoie pas de bougie pour une
        période sans transaction : une page incomplète ne signifie donc pas la fin de
        l'historique. On parcourt des fenêtres explicites de 1 440 intervalles (qui tiennent
        toujours en une requête). Sans `start`, on s'arrête à la première fenêtre vide.
        """
        window = INTERVAL_MS[interval] * MAX_CANDLES
        floor = start if start is not None else 0
        cursor = end
        while cursor > floor:
            window_start = max(cursor - window, floor)
            page = await self.get_candles(market, interval, start=window_start, end=cursor)
            cursor = window_start
            if page:
                yield page
            elif start is None:
                return

    async def get_ticker_24h(self, market: str) -> Ticker24h:
        data = await self._get("/ticker/24h", {"market": market})
        return Ticker24h(
            market=data["market"],
            last=_dec(data.get("last")),
            bid=_dec(data.get("bid")),
            ask=_dec(data.get("ask")),
            volume_quote=_dec(data.get("volumeQuote")),
            ts=int(data.get("timestamp") or 0),
        )

    async def get_book(self, market: str, depth: int = 25) -> OrderBook:
        data = await self._get(f"/{market}/book", {"depth": depth})
        return OrderBook(
            market=data["market"],
            bids=[(Decimal(p), Decimal(q)) for p, q in data["bids"]],
            asks=[(Decimal(p), Decimal(q)) for p, q in data["asks"]],
            ts_ns=int(data.get("timestamp") or 0),
        )


def _error_details(response: httpx.Response) -> tuple[int | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200]
    if isinstance(body, dict):
        code = body.get("errorCode")
        return (int(code) if code is not None else None), str(body.get("error", body))
    return None, str(body)[:200]
