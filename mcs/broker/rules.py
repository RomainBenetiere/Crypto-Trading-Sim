"""Règles de chaque marché Bitvavo : pas de prix, décimales de quantité, minimums."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal

from mcs.broker.base import RejectReason, Side


@dataclass(frozen=True, slots=True)
class MarketRules:
    market: str
    status: str
    tick_size: Decimal
    quantity_decimals: int
    min_order_base: Decimal
    min_order_quote: Decimal

    @property
    def quantity_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.quantity_decimals)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Arrondi vers le bas : on n'achète ni ne vend jamais plus que prévu."""
        return quantity.quantize(self.quantity_step, rounding=ROUND_DOWN)

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        """Limite d'achat arrondie vers le bas, de vente vers le haut (jamais plus défavorable
        que demandé), sur le pas de prix du marché."""
        rounding = ROUND_FLOOR if side is Side.BUY else ROUND_CEILING
        ticks = (price / self.tick_size).to_integral_value(rounding=rounding)
        return (ticks * self.tick_size).normalize()

    def check_minimum(self, quantity: Decimal, price: Decimal) -> tuple[RejectReason, str] | None:
        if quantity <= 0 or quantity < self.min_order_base:
            return RejectReason.BELOW_MINIMUM, (
                f"quantité {quantity} < minimum {self.min_order_base} {self.market}"
            )
        if quantity * price < self.min_order_quote:
            return RejectReason.BELOW_MINIMUM, (
                f"montant {quantity * price:.2f} € < minimum {self.min_order_quote} €"
            )
        return None


def load_rules(conn: sqlite3.Connection) -> dict[str, MarketRules]:
    """Règles des marchés connus (table `markets`, mise à jour par le collecteur)."""
    rules = {}
    for row in conn.execute(
        """SELECT market, status, tick_size, quantity_decimals, min_order_base, min_order_quote
           FROM markets WHERE tick_size IS NOT NULL AND quantity_decimals IS NOT NULL"""
    ):
        rules[row["market"]] = MarketRules(
            market=row["market"],
            status=row["status"],
            tick_size=Decimal(row["tick_size"]),
            quantity_decimals=int(row["quantity_decimals"]),
            min_order_base=Decimal(row["min_order_base"] or 0),
            min_order_quote=Decimal(row["min_order_quote"] or 0),
        )
    return rules
