"""Position ouverte d'une simulation sur une paire."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Position:
    market: str
    lots: int
    quantity: Decimal
    avg_entry_price: Decimal
    invested_cost: Decimal  # montant payé (frais d'achat compris) pour la quantité détenue
    buy_fees: Decimal
    risk_per_unit: Decimal  # R : prix d'entrée − stop initial de la première entrée
    initial_stop: Decimal
    stop: Decimal  # niveau effectif, ne fait que monter
    last_entry_price: Decimal
    highest_high: Decimal  # plus haut depuis l'entrée (bougies clôturées)
    opened_at: int
    bars_held: int = 0  # bougies 4 h clôturées depuis l'ouverture
    trailing: Decimal | None = None  # dernier niveau de trailing calculé
    trailing_active: bool = False
    take_profit: Decimal | None = None
    tp_order_id: int | None = None
    partial_done: bool = False

    def raise_stop(self, level: Decimal) -> bool:
        """Remonte le stop si `level` est plus haut. Le stop ne redescend jamais."""
        if level > self.stop:
            self.stop = level
            return True
        return False

    @property
    def stop_from_trailing(self) -> bool:
        return self.trailing_active and self.trailing is not None and self.trailing >= self.stop

    def add_entry(self, quantity: Decimal, notional: Decimal, fee: Decimal, price: Decimal) -> None:
        """Renforcement : nouveau lot, prix moyen pondéré."""
        total = self.quantity + quantity
        self.avg_entry_price = (self.avg_entry_price * self.quantity + notional) / total
        self.quantity = total
        self.invested_cost += notional + fee
        self.buy_fees += fee
        self.lots += 1
        self.last_entry_price = price

    def reduce(self, quantity: Decimal) -> None:
        """Vente partielle : coût investi et frais d'achat réduits au prorata."""
        ratio = quantity / self.quantity
        self.invested_cost -= self.invested_cost * ratio
        self.buy_fees -= self.buy_fees * ratio
        self.quantity -= quantity
