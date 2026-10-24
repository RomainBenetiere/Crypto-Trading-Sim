"""Frais Bitvavo simulés, prélevés en € à l'achat comme à la vente."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from mcs.broker.base import Liquidity
from mcs.config import DEFAULT_FEE_MAKER, DEFAULT_FEE_TAKER

FEE_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    maker: Decimal = DEFAULT_FEE_MAKER
    taker: Decimal = DEFAULT_FEE_TAKER

    def rate(self, liquidity: Liquidity) -> Decimal:
        return self.maker if liquidity is Liquidity.MAKER else self.taker

    def fee(self, notional: Decimal, liquidity: Liquidity) -> Decimal:
        return (notional * self.rate(liquidity)).quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
