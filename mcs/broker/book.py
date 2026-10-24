"""Exécution simulée contre le carnet d'ordres réel : prix moyen pondéré en parcourant
les niveaux (estimation du spread et du slippage propre à chaque paire)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from mcs.bitvavo.rest import BitvavoRest, OrderBook

log = logging.getLogger(__name__)

Level = tuple[Decimal, Decimal]  # (prix, quantité), du meilleur au moins bon
BOOK_DEPTH = 1000
BOOK_TTL_S = 2.0


@dataclass(frozen=True, slots=True)
class Execution:
    quantity: Decimal
    notional: Decimal
    levels_used: int
    short: bool  # carnet insuffisant : le reste exécuté au dernier niveau

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.quantity


def walk_quantity(levels: Sequence[Level], quantity: Decimal) -> Execution:
    """Coût d'exécution de `quantity` en consommant les niveaux dans l'ordre."""
    if not levels:
        raise ValueError("Carnet vide.")
    remaining, notional, used = quantity, Decimal(0), 0
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        used += 1
    short = remaining > 0
    if short:
        notional += remaining * levels[-1][0]
    return Execution(quantity, notional, used, short)


def max_quantity_for_budget(levels: Sequence[Level], budget: Decimal, fee_rate: Decimal) -> Decimal:
    """Plus grande quantité (non arrondie) dont le coût, frais compris, tient dans `budget`."""
    if not levels:
        raise ValueError("Carnet vide.")
    available = budget / (1 + fee_rate)  # montant hors frais
    quantity = Decimal(0)
    for price, size in levels:
        cost = price * size
        if cost >= available:
            return quantity + available / price
        quantity += size
        available -= cost
    return quantity + available / levels[-1][0]


class BookProvider(Protocol):
    async def get_book(self, market: str) -> OrderBook: ...


class CachedBookProvider:
    """Carnets REST mis en cache quelques secondes : quand de nombreuses simulations
    décident sur la même clôture, une seule requête par paire."""

    def __init__(
        self,
        rest: BitvavoRest,
        ttl_s: float = BOOK_TTL_S,
        depth: int = BOOK_DEPTH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rest, self._ttl, self._depth, self._clock = rest, ttl_s, depth, clock
        self._cache: dict[str, tuple[float, OrderBook]] = {}

    async def get_book(self, market: str) -> OrderBook:
        cached = self._cache.get(market)
        if cached and self._clock() - cached[0] < self._ttl:
            return cached[1]
        book = await self._rest.get_book(market, self._depth)
        self._cache[market] = (self._clock(), book)
        return book
