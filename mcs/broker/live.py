"""`BitvavoBroker` : ordres réels sur le compte Bitvavo (mode production, palier 3, SPEC §15).

Même interface que le `PaperBroker` : la stratégie ne voit pas la différence. Le broker tient le
compte du portefeuille du moteur (capital engagé + exécutions réelles), jamais le solde complet
du compte : les cryptos détenues hors du moteur ne sont ni comptées ni vendues.

Règles :
- chaque ordre est d'abord enregistré (`live_orders`, statut ouvert) avec un `clientOrderId`
  aléatoire, puis envoyé ; si la réponse se perd (coupure réseau), l'ordre est retrouvé par cet
  identifiant au lieu d'être renvoyé (jamais de double ordre) ;
- achats soumis au garde-fou (`guard` : plafond de capital, perte du jour, arrêt) et au solde
  réel en euros ;
- ordre au marché : achat par `amountQuote` (budget hors frais, arrondi au centime inférieur),
  vente par quantité ; montants et frais réels repris de la réponse de Bitvavo ;
- ordre limite : `postOnly` (frais maker garantis, refusé s'il croiserait le carnet), suivi
  par `poll` chaque minute, expiré par annulation chez Bitvavo ;
- annulation d'un ordre partiellement exécuté : la partie exécutée est enregistrée comme un
  ordre rempli de quantité réduite.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

import httpx

from mcs.bitvavo.rest import BitvavoError
from mcs.bitvavo.trading import BitvavoTrading, ExchangeOrder
from mcs.broker.base import (
    Liquidity,
    Order,
    OrderStatus,
    OrderType,
    Reason,
    Rejection,
    RejectReason,
    Side,
)
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import Account
from mcs.broker.rules import MarketRules
from mcs.storage.orders import insert_order, load_orders, update_order

log = logging.getLogger(__name__)

TABLE = "live_orders"
CENT = Decimal("0.01")
# Un ordre au marché est normalement rempli dans la réponse ; sinon, quelques relectures.
MARKET_FILL_CHECKS = 5
MARKET_FILL_WAIT_S = 0.5

# Garde-fou des achats : renvoie le motif de refus, ou None si l'achat est permis.
BuyGuard = Callable[[int, Decimal], str | None]
Sleep = Callable[[float], Awaitable[None]]


class BitvavoBroker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeSchedule,
        trading: BitvavoTrading,
        rules: Mapping[str, MarketRules],
        guard: BuyGuard | None = None,
        sleep: Sleep = asyncio.sleep,
        new_client_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.conn, self.fees, self.trading = conn, fees, trading
        self.rules = dict(rules)
        self.guard = guard
        self._sleep, self._new_client_id = sleep, new_client_id
        self.accounts: dict[int, Account] = {}
        self.client_ids: dict[int, str] = {}

    # --- Comptes ---------------------------------------------------------------------------

    def open_account(self, sim_id: int, initial_cash: Decimal) -> Account:
        """Portefeuille du moteur : capital engagé + exécutions réelles enregistrées."""
        account = Account(sim_id, initial_cash, initial_cash)
        for order in load_orders(self.conn, sim_id, TABLE):
            if order.status is OrderStatus.FILLED:
                account.apply_fill(order)
            elif order.status is OrderStatus.OPEN:
                account.open_orders[order.id] = order
        for row in self.conn.execute(
            f"SELECT id, client_order_id FROM {TABLE} WHERE sim_id = ? AND status = 'open'",
            (sim_id,),
        ):
            if row["client_order_id"]:
                self.client_ids[row["id"]] = row["client_order_id"]
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

    def min_order_quote(self, market: str) -> Decimal:
        rules = self.rules.get(market)
        return rules.min_order_quote if rules is not None else Decimal(0)

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
        # Budget envoyé hors frais : le total débité reste sous `quote_amount`.
        budget = (quote_amount / (1 + self.fees.taker)).quantize(CENT, rounding=ROUND_DOWN)
        if budget < rules.min_order_quote:
            return self._reject(
                sim_id,
                market,
                Side.BUY,
                RejectReason.BELOW_MINIMUM,
                f"{budget} € < minimum {rules.min_order_quote} €",
            )
        refused = await self._check_buy(account, market, quote_amount)
        if refused is not None:
            return refused
        order = self._record_open(Order(
            id=0, sim_id=sim_id, market=market, side=Side.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.OPEN, reason=reason, quantity=Decimal(0), price=None,
            notional=None, fee=None, liquidity=None, reserved=quote_amount, book_bid=None,
            book_ask=None, created_at=now,
        ))  # fmt: skip
        return await self._send_market(account, order, now, amount_quote=budget)

    async def sell_market(
        self,
        sim_id: int,
        market: str,
        quantity: Decimal,
        reason: Reason,
        now: int,
        trigger_price: Decimal | None = None,  # ignoré : Bitvavo exécute au carnet réel
    ) -> Order | Rejection:
        account = self.accounts[sim_id]
        rules = self._rules(sim_id, market, Side.SELL)
        if isinstance(rules, Rejection):
            return rules
        held = account.available_quantity(market)
        if quantity > held:
            # Exécution partielle antérieure (take profit annulé en cours de remplissage) :
            # on vend ce que le portefeuille du moteur détient réellement.
            log.warning("Réel : vente %s ramenée de %s à %s détenus.", market, quantity, held)
        quantity = rules.round_quantity(min(quantity, held))
        if quantity <= 0:
            return self._reject(
                sim_id, market, Side.SELL, RejectReason.INSUFFICIENT_QUANTITY, f"{held} disponibles"
            )
        order = self._record_open(Order(
            id=0, sim_id=sim_id, market=market, side=Side.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.OPEN, reason=reason, quantity=quantity, price=None,
            notional=None, fee=None, liquidity=None, reserved=None, book_bid=None,
            book_ask=None, created_at=now,
        ))  # fmt: skip
        return await self._send_market(account, order, now, amount=quantity)

    async def _send_market(
        self,
        account: Account,
        order: Order,
        now: int,
        amount: Decimal | None = None,
        amount_quote: Decimal | None = None,
    ) -> Order | Rejection:
        client_id = self.client_ids[order.id]
        try:
            result: ExchangeOrder | None = await self.trading.create_order(
                order.market, order.side, "market", client_id,
                amount=amount, amount_quote=amount_quote,
            )  # fmt: skip
        except BitvavoError as exc:
            self._close(account, order, OrderStatus.CANCELLED, now)
            return self._reject(
                account.sim_id, order.market, order.side, RejectReason.EXCHANGE_ERROR, str(exc)
            )
        except httpx.HTTPError as exc:
            log.warning("Réel : réponse perdue pour l'ordre %d (%s), relecture.", order.id, exc)
            result = None
        for _ in range(MARKET_FILL_CHECKS):
            if result is not None and result.done:
                break
            await self._sleep(MARKET_FILL_WAIT_S)
            try:
                result = await self.trading.get_order(order.market, client_id)
            except (httpx.HTTPError, BitvavoError) as exc:
                log.warning("Réel : relecture de l'ordre %d impossible (%s).", order.id, exc)
            if result is None:
                break  # inconnu de Bitvavo : jamais créé
        if result is None:
            self._close(account, order, OrderStatus.CANCELLED, now)
            return self._reject(
                account.sim_id,
                order.market,
                order.side,
                RejectReason.EXCHANGE_ERROR,
                "ordre non créé",
            )
        if not result.done:
            # Toujours en cours : il reste ouvert, `poll` l'enregistrera à son remplissage.
            log.warning("Réel : ordre au marché %d encore %s.", order.id, result.status)
            return self._reject(
                account.sim_id,
                order.market,
                order.side,
                RejectReason.EXCHANGE_ERROR,
                f"ordre {result.status}",
            )
        settled = self._settle(account, order, result, now, OrderStatus.CANCELLED)
        if settled.status is not OrderStatus.FILLED:
            return self._reject(
                account.sim_id,
                order.market,
                order.side,
                RejectReason.EXCHANGE_ERROR,
                f"ordre {result.status}",
            )
        return settled

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
        rate = self.fees.maker
        quantity = rules.round_quantity(quote_amount / (limit * (1 + rate)))
        cost = quantity * limit * (1 + rate)
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return self._reject(sim_id, market, Side.BUY, *problem)
        refused = await self._check_buy(account, market, cost)
        if refused is not None:
            return refused
        order = self._record_open(Order(
            id=0, sim_id=sim_id, market=market, side=Side.BUY, order_type=OrderType.LIMIT,
            status=OrderStatus.OPEN, reason=reason, quantity=quantity, price=limit,
            notional=None, fee=None, liquidity=None, reserved=cost, book_bid=None,
            book_ask=None, created_at=now, expires_at=expires_at,
        ))  # fmt: skip
        return await self._send_limit(account, order, now)

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
        problem = rules.check_minimum(quantity, limit)
        if problem:
            return self._reject(sim_id, market, Side.SELL, *problem)
        order = self._record_open(Order(
            id=0, sim_id=sim_id, market=market, side=Side.SELL, order_type=OrderType.LIMIT,
            status=OrderStatus.OPEN, reason=reason, quantity=quantity, price=limit,
            notional=None, fee=None, liquidity=None, reserved=None, book_bid=None,
            book_ask=None, created_at=now, expires_at=expires_at,
        ))  # fmt: skip
        return await self._send_limit(account, order, now)

    async def _send_limit(self, account: Account, order: Order, now: int) -> Order | Rejection:
        assert order.price is not None
        client_id = self.client_ids[order.id]
        try:
            result: ExchangeOrder | None = await self.trading.create_order(
                order.market, order.side, "limit", client_id,
                amount=order.quantity, price=order.price, post_only=True,
            )  # fmt: skip
        except BitvavoError as exc:
            self._close(account, order, OrderStatus.CANCELLED, now)
            return self._reject(
                account.sim_id, order.market, order.side, RejectReason.EXCHANGE_ERROR, str(exc)
            )
        except httpx.HTTPError as exc:
            log.warning("Réel : réponse perdue pour l'ordre %d (%s), relecture.", order.id, exc)
            try:
                result = await self.trading.get_order(order.market, client_id)
            except (httpx.HTTPError, BitvavoError):
                # Ni confirmé ni infirmé : l'ordre reste ouvert, `poll` tranchera.
                return order
            if result is None:
                self._close(account, order, OrderStatus.CANCELLED, now)
                return self._reject(
                    account.sim_id,
                    order.market,
                    order.side,
                    RejectReason.EXCHANGE_ERROR,
                    "ordre non créé",
                )
        if result is not None and result.done:
            settled = self._settle(account, order, result, now, OrderStatus.CANCELLED)
            if settled.status is OrderStatus.FILLED:
                return settled
            return self._reject(
                account.sim_id,
                order.market,
                order.side,
                RejectReason.INVALID_PRICE,
                "postOnly : la limite croiserait le carnet",
            )
        log.info(
            "Réel : limite %s %s %s à %s posée.",
            order.side,
            order.quantity,
            order.market,
            order.price,
        )
        return order

    # --- Suivi et annulation ---------------------------------------------------------------

    async def poll(self, now: int) -> list[Order]:
        """Relit chez Bitvavo les ordres ouverts ; renvoie ceux qui sont clos (remplis,
        éventuellement partiellement, ou annulés / expirés côté Bitvavo)."""
        closed = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                client_id = self.client_ids.get(order.id)
                if client_id is None:
                    continue
                try:
                    result = await self.trading.get_order(order.market, client_id)
                except (httpx.HTTPError, BitvavoError) as exc:
                    log.warning("Réel : suivi de l'ordre %d impossible (%s).", order.id, exc)
                    continue
                if result is None:
                    closed.append(self._close(account, order, OrderStatus.CANCELLED, now))
                elif result.done:
                    closed.append(self._settle(account, order, result, now, OrderStatus.CANCELLED))
        return closed

    async def expire_orders(self, now: int) -> list[Order]:
        expired = []
        for account in self.accounts.values():
            for order in list(account.open_orders.values()):
                if order.expires_at is not None and order.expires_at <= now:
                    try:
                        expired.append(await self._cancel(account, order, now, OrderStatus.EXPIRED))
                    except (httpx.HTTPError, BitvavoError) as exc:
                        log.warning("Réel : expiration de l'ordre %d reportée (%s).", order.id, exc)
        return expired

    async def cancel_order(self, order_id: int, now: int) -> Order:
        """Annule chez Bitvavo puis relit l'ordre : rempli entre-temps, il est enregistré
        comme tel (statut `filled`, quantité exécutée)."""
        for account in self.accounts.values():
            if order_id in account.open_orders:
                return await self._cancel(
                    account, account.open_orders[order_id], now, OrderStatus.CANCELLED
                )
        raise KeyError(f"Ordre ouvert introuvable : {order_id}")

    async def _cancel(self, account: Account, order: Order, now: int, status: OrderStatus) -> Order:
        client_id = self.client_ids.get(order.id)
        if client_id is None:
            return self._close(account, order, status, now)
        await self.trading.cancel_order(order.market, client_id)
        result = await self.trading.get_order(order.market, client_id)
        if result is None:
            return self._close(account, order, status, now)
        return self._settle(account, order, result, now, status)

    # --- Interne ---------------------------------------------------------------------------

    async def _check_buy(self, account: Account, market: str, cost: Decimal) -> Rejection | None:
        if self.guard is not None:
            blocked = self.guard(account.sim_id, cost)
            if blocked is not None:
                return self._reject(
                    account.sim_id, market, Side.BUY, RejectReason.RISK_LIMIT, blocked
                )
        if cost > account.available:
            return self._reject(
                account.sim_id,
                market,
                Side.BUY,
                RejectReason.INSUFFICIENT_FUNDS,
                f"{cost} € demandés, {account.available} € disponibles",
            )
        try:
            balances = await self.trading.balances()
        except (httpx.HTTPError, BitvavoError) as exc:
            return self._reject(
                account.sim_id,
                market,
                Side.BUY,
                RejectReason.EXCHANGE_ERROR,
                f"solde illisible ({exc})",
            )
        euros = next((b.available for b in balances if b.symbol == "EUR"), Decimal(0))
        if cost > euros:
            return self._reject(
                account.sim_id,
                market,
                Side.BUY,
                RejectReason.INSUFFICIENT_FUNDS,
                f"{cost} € demandés, {euros} € sur le compte",
            )
        return None

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
        log.warning("Réel : ordre %s %s refusé (%s : %s).", side, market, reason, detail)
        return Rejection(sim_id, market, side, reason, detail)

    def _record_open(self, order: Order) -> Order:
        """Enregistre l'ordre avant l'envoi, avec son `clientOrderId`."""
        client_id = self._new_client_id()
        order = insert_order(self.conn, order, TABLE)
        self.conn.execute(
            f"UPDATE {TABLE} SET client_order_id = ? WHERE id = ?", (client_id, order.id)
        )
        self.client_ids[order.id] = client_id
        self.accounts[order.sim_id].open_orders[order.id] = order
        return order

    def _settle(
        self,
        account: Account,
        order: Order,
        result: ExchangeOrder,
        now: int,
        unfilled: OrderStatus,
    ) -> Order:
        """Enregistre l'issue d'un ordre clos chez Bitvavo (exécution réelle ou rien)."""
        self.conn.execute(
            f"UPDATE {TABLE} SET exchange_order_id = ? WHERE id = ?", (result.order_id, order.id)
        )
        if result.filled_amount <= 0 or result.filled_quote <= 0:
            return self._close(account, order, unfilled, now)
        base = order.market.split("-")[0]
        quantity, notional = result.filled_amount, result.filled_quote
        fee = result.fee_paid
        if result.fee_currency == base:
            # Frais prélevés en crypto : moins de quantité reçue, frais convertis en euros.
            price = notional / quantity
            fee_eur = fee * price
            quantity -= fee
            notional -= fee_eur
            fee = fee_eur
        elif result.fee_currency not in (None, "EUR"):
            log.warning(
                "Réel : frais de l'ordre %d en %s, non comptés.", order.id, result.fee_currency
            )
            fee = Decimal(0)
        filled = replace(
            order, status=OrderStatus.FILLED, quantity=quantity, price=notional / quantity,
            notional=notional, fee=fee,
            liquidity=Liquidity.TAKER if order.order_type is OrderType.MARKET else Liquidity.MAKER,
            reserved=None, filled_at=now,
        )  # fmt: skip
        update_order(self.conn, filled, TABLE)
        account.open_orders.pop(order.id, None)
        self.client_ids.pop(order.id, None)
        account.apply_fill(filled)
        log.info(
            "Réel : %s %s %s %s à %s, frais %s € (Bitvavo %s).",
            order.order_type,
            order.side,
            quantity,
            order.market,
            filled.price,
            fee,
            result.order_id,
        )
        return filled

    def _close(self, account: Account, order: Order, status: OrderStatus, now: int) -> Order:
        closed = replace(order, status=status, reserved=None, filled_at=None)
        update_order(self.conn, closed, TABLE)
        account.open_orders.pop(order.id, None)
        self.client_ids.pop(order.id, None)
        log.info("Réel : ordre %d %s (%s).", order.id, status, order.market)
        return closed
