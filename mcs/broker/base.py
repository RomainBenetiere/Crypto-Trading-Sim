"""Interface `Broker` commune : la stratégie ne sait pas si elle parle au `PaperBroker`
ou à un futur `BitvavoBroker`. Montants, prix et quantités en `Decimal`."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class Reason(StrEnum):
    """Motif d'un ordre (colonne `orders.reason`)."""

    SIGNAL = "signal"
    STOP = "stop"
    TRAILING = "trailing"
    TAKE_PROFIT = "take_profit"
    REVERSE_SIGNAL = "reverse_signal"
    MAX_DURATION = "max_duration"
    TREND_FILTER = "trend_filter"
    CIRCUIT_BREAKER = "circuit_breaker"


# Stops déclenchés : pénalité d'exécution supplémentaire (SPEC §5 et §13).
STOP_REASONS = frozenset({Reason.STOP, Reason.TRAILING})


class RejectReason(StrEnum):
    UNKNOWN_MARKET = "unknown_market"
    MARKET_NOT_TRADING = "market_not_trading"
    BELOW_MINIMUM = "below_minimum"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INSUFFICIENT_QUANTITY = "insufficient_quantity"
    INVALID_PRICE = "invalid_price"
    EMPTY_BOOK = "empty_book"


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    sim_id: int
    market: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    reason: Reason
    quantity: Decimal
    price: Decimal | None  # limite, ou prix moyen d'exécution une fois rempli
    notional: Decimal | None  # quantité × prix exécuté, frais exclus
    fee: Decimal | None
    liquidity: Liquidity | None
    reserved: Decimal | None  # liquidités bloquées par un ordre limite d'achat ouvert
    book_bid: Decimal | None
    book_ask: Decimal | None
    created_at: int
    expires_at: int | None = None
    filled_at: int | None = None


@dataclass(frozen=True, slots=True)
class Rejection:
    sim_id: int
    market: str
    side: Side
    reason: RejectReason
    detail: str


class Broker(Protocol):
    async def buy_market(
        self, sim_id: int, market: str, quote_amount: Decimal, reason: Reason, now: int
    ) -> Order | Rejection:
        """Achète pour au plus `quote_amount` € frais compris."""

    async def sell_market(
        self, sim_id: int, market: str, quantity: Decimal, reason: Reason, now: int
    ) -> Order | Rejection: ...

    async def place_limit_buy(
        self,
        sim_id: int,
        market: str,
        price: Decimal,
        quote_amount: Decimal,
        expires_at: int,
        reason: Reason,
        now: int,
    ) -> Order | Rejection:
        """Ordre limite d'achat pour au plus `quote_amount` € frais compris."""

    async def cancel_order(self, order_id: int, now: int) -> Order: ...

    def cash(self, sim_id: int) -> Decimal:
        """Liquidités disponibles (hors réservations des ordres ouverts)."""

    def holdings(self, sim_id: int) -> dict[str, Decimal]: ...

    def open_orders(self, sim_id: int) -> list[Order]: ...
