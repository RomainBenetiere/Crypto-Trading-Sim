"""`PaperBroker` : exécution simulée sur données de marché réelles, sans aucun ordre réel.

Règles (SPEC §5 et §13) :
- ordre au marché : parcours du carnet réel (prix moyen pondéré), frais taker ; si le carnet
  ne suffit pas, le reste est exécuté au dernier niveau et une alerte est journalisée ;
- stop ou trailing déclenché : prix de vente dégradé en plus d'un spread médian 24 h ;
- ordre limite d'achat : liquidités réservées dès le placement ; rempli en entier au prix
  limite (frais maker) dès qu'une bougie 1 min postérieure au placement a un plus bas
  strictement inférieur au prix (le niveau doit être traversé, pas seulement touché) ;
  expiré à `expires_at` ;
- ordre limite de vente (take profit) : quantité bloquée ; rempli au prix limite (frais maker)
  quand une bougie 1 min postérieure a un plus haut strictement supérieur ;
- prix au pas du marché, quantités arrondies vers le bas, minimums Bitvavo respectés.

Le broker ne gère que l'argent et les quantités ; les positions (lots, stops) sont gérées
par la stratégie et le risque.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

from mcs.bitvavo.rest import OrderBook
from mcs.broker.base import (
    STOP_REASONS,
    Liquidity,
    Order,
    OrderStatus,
    OrderType,
    Reason,
    Rejection,
    RejectReason,
    Side,
)
from mcs.broker.book import BookProvider, Level, max_quantity_for_budget, walk_quantity
from mcs.broker.fees import FeeSchedule
from mcs.broker.rules import MarketRules, load_rules
from mcs.market_data import MINUTE_MS, Candle
from mcs.storage.orders import insert_order, load_orders, update_order

log = logging.getLogger(__name__)

SpreadLookup = Callable[[str], Decimal | None]


@dataclass
class Account:
    """Compte simulé d'une simulation."""

    sim_id: int
    initial_cash: Decimal
    cash_total: Decimal  # réservations comprises
    holdings: dict[str, Decimal] = field(default_factory=dict)
    open_orders: dict[int, Order] = field(default_factory=dict)
    fees_paid: Decimal = Decimal(0)

    @property
    def reserved(self) -> Decimal:
        return sum((o.reserved or Decimal(0) for o in self.open_orders.values()), Decimal(0))

    @property
    def available(self) -> Decimal:
        return self.cash_total - self.reserved

    def reserved_quantity(self, market: str) -> Decimal:
        """Quantité bloquée par les ordres limite de vente ouverts (take profit)."""
        return sum(
            (
                o.quantity
                for o in self.open_orders.values()
                if o.side is Side.SELL and o.market == market
            ),
            Decimal(0),
        )

    def available_quantity(self, market: str) -> Decimal:
        return self.holdings.get(market, Decimal(0)) - self.reserved_quantity(market)

    def apply_fill(self, order: Order) -> None:
        notional, fee = order.notional or Decimal(0), order.fee or Decimal(0)
        held = self.holdings.get(order.market, Decimal(0))
        if order.side is Side.BUY:
            self.cash_total -= notional + fee
            held += order.quantity
        else:
            self.cash_total += notional - fee
            held -= order.quantity
        if held:
            self.holdings[order.market] = held
        else:
            self.holdings.pop(order.market, None)
        self.fees_paid += fee


def spread_from_table(conn: sqlite3.Connection) -> SpreadLookup:
    """Spread médian 24 h (`markets.spread_estimate_pct`, en %) converti en fraction."""

    def lookup(market: str) -> Decimal | None:
        row = conn.execute(
            "SELECT spread_estimate_pct FROM markets WHERE market = ?", (market,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return Decimal(row[0]) / 100

    return lookup


class PaperBroker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeSchedule,
        books: BookProvider,
        rules: Mapping[str, MarketRules] | None = None,
        spread: SpreadLookup | None = None,
    ) -> None:
        self.conn, self.fees, self.books = conn, fees, books
        self.rules = dict(rules) if rules is not None else load_rules(conn)
        self.spread = spread or spread_from_table(conn)
        self.accounts: dict[int, Account] = {}

    # --- Comptes ---------------------------------------------------------------------------

    def open_account(self, sim_id: int, initial_cash: Decimal) -> Account:
        """Ouvre le compte, ou le reconstitue à partir des ordres enregistrés (reprise)."""
        account = Account(sim_id, initial_cash, initial_cash)
        for order in load_orders(self.conn, sim_id):
            if order.status is OrderStatus.FILLED:
                account.apply_fill(order)
            elif order.status is OrderStatus.OPEN:
                account.open_orders[order.id] = order
        self.accounts[sim_id] = account
        return account

    def account(self, sim_id: int) -> Account:
        return self.accounts[sim_id]

    def cash(self, sim_id: int) -> Decimal:
        return self.accounts[sim_id].available

    def total_cash(self, sim_id: int) -> Decimal:
        return self.accounts[sim_id].cash_total

    def holdings(self, sim_id: int) -> dict[str, Decimal]:
        return dict(self.accounts[sim_id].holdings)

    def available_quantity(self, sim_id: int, market: str) -> Decimal:
        return self.accounts[sim_id].available_quantity(market)

    def open_orders(self, sim_id: int) -> list[Order]:
        return list(self.accounts[sim_id].open_orders.values())

    # --- Ordres au marché ------------------------------------------------------------------

    async def buy_market(
        self, sim_id: int, market: str, quote_amount: Decimal, reason: Reason, now: int
    ) -> Order | Rejection:
        account = self.accounts[sim_id]
        rules = self._rules(sim_id, market, Side.BUY)
        if isinstance(rules, Rejection):
            return rules
        if quote_amount > account.available:
            return self._reject(
                sim_id,
                market,
                Side.BUY,
                RejectReason.INSUFFICIENT_FUNDS,
                f"{quote_amount} € demandés, {account.available} € disponibles",
            )
        book = await self.books.get_book(market)
        if not book.asks:
            return self._reject(sim_id, market, Side.BUY, RejectReason.EMPTY_BOOK, "aucune offre")
        rate = self.fees.rate(Liquidity.TAKER)
        quantity = rules.round_quantity(max_quantity_for_budget(book.asks, quote_amount, rate))
        while quantity > 0:
            execution = walk_quantity(book.asks, quantity)
            fee = self.fees.fee(execution.notional, Liquidity.TAKER)
            if execution.notional + fee <= quote_amount:
                break
            quantity -= rules.quantity_step
        if quantity <= 0:
            return self._reject(
                sim_id,
                market,
                Side.BUY,
                RejectReason.BELOW_MINIMUM,
                f"{quote_amount} € ne permettent aucune quantité",
            )
        problem = rules.check_minimum(quantity, execution.average_price)
        if problem:
            return self._reject(sim_id, market, Side.BUY, *problem)
        if execution.short:
            log.warning(
                "Carnet %s insuffisant pour %s : reste exécuté au dernier niveau.", market, quantity
            )
        return self._record_market_fill(
            account, market, Side.BUY, reason, quantity, execution.notional, fee, book, now
        )

    async def sell_market(
        self,
        sim_id: int,
        market: str,
        quantity: Decimal,
        reason: Reason,
        now: int,
        trigger_price: Decimal | None = None,  # ignoré : le carnet réel fait foi en live
    ) -> Order | Rejection:
        account = self.accounts[sim_id]
        rules = self._rules(sim_id, market, Side.SELL)
        if isinstance(rules, Rejection):
            return rules
        quantity = rules.round_quantity(quantity)
        held = account.available_quantity(market)
        if quantity <= 0 or quantity > held:
            return self._reject(
                sim_id,
                market,
                Side.SELL,
                RejectReason.INSUFFICIENT_QUANTITY,
                f"{quantity} demandés, {held} disponibles",
            )
        book = await self.books.get_book(market)
        if not book.bids:
            return self._reject(
                sim_id, market, Side.SELL, RejectReason.EMPTY_BOOK, "aucune demande"
            )
        execution = walk_quantity(book.bids, quantity)
        notional = execution.notional
        if reason in STOP_REASONS:
            penalty = self.stop_penalty(market, book)
            notional = notional * (1 - penalty)
            log.info(
                "Stop %s : pénalité de %s %% sur le prix de vente.",
                market,
                (penalty * 100).normalize(),
            )
        problem = rules.check_minimum(quantity, notional / quantity)
        if problem:
            return self._reject(sim_id, market, Side.SELL, *problem)
        if execution.short:
            log.warning(
                "Carnet %s insuffisant pour %s : reste exécuté au dernier niveau.", market, quantity
            )
        fee = self.fees.fee(notional, Liquidity.TAKER)
        return self._record_market_fill(
            account, market, Side.SELL, reason, quantity, notional, fee, book, now
        )

    def stop_penalty(self, market: str, book: OrderBook) -> Decimal:
        """Fraction retirée du prix d'un stop déclenché : spread médian 24 h de la paire,
        à défaut le spread instantané du carnet."""
        estimate = self.spread(market)
        if estimate is not None:
            return estimate
        if book.bids and book.asks:
            bid, ask = book.bids[0][0], book.asks[0][0]
            return (ask - bid) / ((ask + bid) / 2)
        return Decimal(0)

    # --- Ordres limite ---------------------------------------------------------------------

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
        account = self.accounts[sim_id]
        rules = self._rules(sim_id, market, Side.BUY)
        if isinstance(rules, Rejection):
            return rules
        limit = rules.round_price(price, Side.BUY)
        if limit <= 0 or expires_at <= now:
            return self._reject(
                sim_id,
                market,
                Side.BUY,
                RejectReason.INVALID_PRICE,
                f"limite {price}, expiration {expires_at}",
            )
        rate = self.fees.rate(Liquidity.MAKER)
        quantity = rules.round_quantity(quote_amount / (limit * (1 + rate)))
        while quantity > 0:
            cost = quantity * limit + self.fees.fee(quantity * limit, Liquidity.MAKER)
            if cost <= quote_amount:
                break
            quantity -= rules.quantity_step
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return self._reject(sim_id, market, Side.BUY, *problem)
        if cost > account.available:
            return self._reject(
                sim_id,
                market,
                Side.BUY,
                RejectReason.INSUFFICIENT_FUNDS,
                f"{cost} € à réserver, {account.available} € disponibles",
            )
        book = await self.books.get_book(market)
        order = insert_order(self.conn, Order(
            id=0, sim_id=sim_id, market=market, side=Side.BUY, order_type=OrderType.LIMIT,
            status=OrderStatus.OPEN, reason=reason, quantity=quantity, price=limit,
            notional=None, fee=None, liquidity=None, reserved=cost,
            book_bid=_best(book.bids), book_ask=_best(book.asks), created_at=now,
            expires_at=expires_at,
        ))  # fmt: skip
        account.open_orders[order.id] = order
        log.info(
            "Sim %d : limite d'achat %s %s à %s (réservé %s €, carnet %s / %s).",
            sim_id,
            quantity,
            market,
            limit,
            cost,
            order.book_bid,
            order.book_ask,
        )
        return order

    async def place_limit_sell(
        self,
        sim_id: int,
        market: str,
        price: Decimal,
        quantity: Decimal,
        reason: Reason,
        now: int,
        expires_at: int | None = None,
    ) -> Order | Rejection:
        """Ordre limite de vente (take profit) : la quantité est bloquée jusqu'au remplissage
        ou à l'annulation."""
        account = self.accounts[sim_id]
        rules = self._rules(sim_id, market, Side.SELL)
        if isinstance(rules, Rejection):
            return rules
        limit = rules.round_price(price, Side.SELL)
        quantity = rules.round_quantity(quantity)
        available = account.available_quantity(market)
        if quantity <= 0 or quantity > available:
            return self._reject(
                sim_id,
                market,
                Side.SELL,
                RejectReason.INSUFFICIENT_QUANTITY,
                f"{quantity} demandés, {available} disponibles",
            )
        if limit <= 0 or (expires_at is not None and expires_at <= now):
            return self._reject(
                sim_id, market, Side.SELL, RejectReason.INVALID_PRICE, f"limite {price}"
            )
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return self._reject(sim_id, market, Side.SELL, *problem)
        order = insert_order(self.conn, Order(
            id=0, sim_id=sim_id, market=market, side=Side.SELL, order_type=OrderType.LIMIT,
            status=OrderStatus.OPEN, reason=reason, quantity=quantity, price=limit,
            notional=None, fee=None, liquidity=None, reserved=None, book_bid=None,
            book_ask=None, created_at=now, expires_at=expires_at,
        ))  # fmt: skip
        account.open_orders[order.id] = order
        log.info("Sim %d : limite de vente %s %s à %s.", sim_id, quantity, market, limit)
        return order

    def on_candle(self, candle: Candle) -> list[Order]:
        """Remplit les ordres limite traversés par une bougie 1 min (achat : plus bas
        strictement sous la limite ; vente : plus haut strictement au-dessus)."""
        if candle.interval != "1m":
            raise ValueError("Les ordres limite sont évalués sur les bougies 1 min.")
        filled = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                if order.market != candle.market or order.price is None:
                    continue
                # Seules les bougies entièrement postérieures au placement comptent.
                if candle.ts < _next_minute(order.created_at):
                    continue
                if order.expires_at is not None and candle.ts >= order.expires_at:
                    continue
                crossed = (
                    candle.low < order.price
                    if order.side is Side.BUY
                    else candle.high > order.price
                )
                if crossed:
                    filled.append(self._fill_limit(account, order, candle))
        return filled

    def expire_orders(self, now: int) -> list[Order]:
        expired = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                if order.expires_at is not None and order.expires_at <= now:
                    expired.append(self._close(account, order, OrderStatus.EXPIRED, now))
        return expired

    async def cancel_order(self, order_id: int, now: int) -> Order:
        for account in self.accounts.values():
            if order_id in account.open_orders:
                return self._close(
                    account, account.open_orders[order_id], OrderStatus.CANCELLED, now
                )
        raise KeyError(f"Ordre ouvert introuvable : {order_id}")

    # --- Interne ---------------------------------------------------------------------------

    def _rules(self, sim_id: int, market: str, side: Side) -> MarketRules | Rejection:
        rules = self.rules.get(market)
        if rules is None:
            return self._reject(sim_id, market, side, RejectReason.UNKNOWN_MARKET, market)
        if rules.status != "trading":
            return self._reject(sim_id, market, side, RejectReason.MARKET_NOT_TRADING, rules.status)
        return rules

    def _reject(
        self, sim_id: int, market: str, side: Side, reason: RejectReason, detail: str
    ) -> Rejection:
        log.info("Sim %d : ordre %s %s refusé (%s : %s).", sim_id, side, market, reason, detail)
        return Rejection(sim_id, market, side, reason, detail)

    def _record_market_fill(
        self,
        account: Account,
        market: str,
        side: Side,
        reason: Reason,
        quantity: Decimal,
        notional: Decimal,
        fee: Decimal,
        book: OrderBook,
        now: int,
    ) -> Order:
        order = insert_order(self.conn, Order(
            id=0, sim_id=account.sim_id, market=market, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, reason=reason,
            quantity=quantity, price=notional / quantity, notional=notional, fee=fee,
            liquidity=Liquidity.TAKER, reserved=None, book_bid=_best(book.bids),
            book_ask=_best(book.asks), created_at=now, filled_at=now,
        ))  # fmt: skip
        account.apply_fill(order)
        log.info(
            "Sim %d : %s marché %s %s (%s) à %s, frais %s €, carnet %s / %s.",
            account.sim_id,
            side,
            quantity,
            market,
            reason,
            order.price,
            fee,
            order.book_bid,
            order.book_ask,
        )
        return order

    def _fill_limit(self, account: Account, order: Order, candle: Candle) -> Order:
        assert order.price is not None
        notional = order.quantity * order.price
        filled = replace(
            order, status=OrderStatus.FILLED, notional=notional,
            fee=self.fees.fee(notional, Liquidity.MAKER), liquidity=Liquidity.MAKER,
            reserved=None, filled_at=max(candle.ts, order.created_at),
        )  # fmt: skip
        update_order(self.conn, filled)
        del account.open_orders[order.id]
        account.apply_fill(filled)
        log.info(
            "Sim %d : limite %s %s %s remplie à %s (bougie 1 min %s-%s), frais %s €.",
            account.sim_id,
            order.side,
            order.quantity,
            order.market,
            order.price,
            candle.low,
            candle.high,
            filled.fee,
        )
        return filled

    def _close(self, account: Account, order: Order, status: OrderStatus, now: int) -> Order:
        closed = replace(order, status=status, reserved=None, filled_at=None)
        update_order(self.conn, closed)
        del account.open_orders[order.id]
        log.info("Sim %d : ordre %d %s (%s).", account.sim_id, order.id, status, order.market)
        return closed


def _best(levels: list[Level]) -> Decimal | None:
    return levels[0][0] if levels else None


def _next_minute(ts: int) -> int:
    return -(-ts // MINUTE_MS) * MINUTE_MS
