"""Une simulation : décisions d'entrée et de sortie, indépendantes de la source des données.

Le moteur (live ou backtest) appelle :
- `on_4h_close` à la clôture de chaque bougie 4 h : sorties (filtre de fond, signal inverse,
  durée max), mise à jour des trailings, puis entrées ;
- `on_1h_close` à la clôture de chaque bougie 1 h : trailing si recalcul horaire ;
- `on_price` à chaque bougie 1 min (en cours ou clôturée) : franchissement du stop ;
- `on_order_filled` / `on_order_closed` pour les ordres limite remplis, expirés ou annulés.

Règles : SPEC §4 et décisions 11 à 18 (§13).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from mcs.broker.base import Broker, Order, OrderStatus, Reason, Rejection, Side
from mcs.broker.fees import FeeSchedule
from mcs.market_data import INTERVAL_MS
from mcs.risk.exits import (
    initial_stop,
    take_profit_level,
    take_profit_quantity,
    trailing_level,
    trailing_should_activate,
)
from mcs.risk.position import Position
from mcs.risk.sizing import SizingInputs, entry_amount
from mcs.strategy.config import (
    CIRCUIT_BREAKER_PAUSE_BARS,
    MIN_DAILY_HISTORY,
    EntryOrder,
    StopRecalc,
    StrategyConfig,
    TakeProfitType,
    TrailingType,
    TrendAction,
)
from mcs.strategy.context import MarketContext

log = logging.getLogger(__name__)

BAR_4H = INTERVAL_MS["4h"]
HOUR = INTERVAL_MS["1h"]
FALLBACK_STOP = Decimal("0.05")
# Marge d'un achat relevé au minimum : l'arrondi de la quantité ne doit pas le faire refuser.
LIFT_MARGIN = Decimal("1.01")


class PositionStore(Protocol):
    def save(self, sim_id: int, position: Position, now: int) -> None: ...
    def delete(self, sim_id: int, market: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingEntry:
    order_id: int
    is_add: bool


class Simulation:
    def __init__(
        self,
        sim_id: int,
        config: StrategyConfig,
        broker: Broker,
        fees: FeeSchedule,
        markets: list[str],
        initial_capital: Decimal = Decimal(1000),
        store: PositionStore | None = None,
        lift_to_minimum: bool = False,
    ) -> None:
        self.sim_id, self.config, self.broker, self.fees = sim_id, config, broker, fees
        # Mode production : un achat sous le minimum d'ordre est relevé au minimum (décision 43)
        # au lieu d'être abandonné, si les liquidités le permettent.
        self.lift_to_minimum = lift_to_minimum
        self.markets = sorted(markets)
        self.store = store
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingEntry] = {}
        self.pause_until: dict[str, int] = {}
        self.breaker_until: int | None = None
        self.peak_value = initial_capital
        self.trade_pnls: list[Decimal] = []  # P&L net de chaque position clôturée

    # --- Valorisation et circuit breaker ---------------------------------------------------

    def position_value(self, market: str, ctx: MarketContext) -> Decimal:
        """Valeur nette des frais de vente estimés (taux taker)."""
        position = self.positions.get(market)
        price = ctx.last_price(market)
        if position is None or price is None:
            return Decimal(0)
        return position.quantity * price * (1 - self.fees.taker)

    def portfolio_value(self, ctx: MarketContext) -> Decimal:
        return self.broker.total_cash(self.sim_id) + sum(
            (self.position_value(m, ctx) for m in self.positions), Decimal(0)
        )

    def _update_breaker(self, now: int, value: Decimal) -> None:
        if self.breaker_until is not None and now >= self.breaker_until:
            log.info("Sim %d : fin de la pause du circuit breaker.", self.sim_id)
            self.breaker_until = None
            self.peak_value = value
        self.peak_value = max(self.peak_value, value)
        threshold = self.peak_value * (1 - self.config.portfolio.circuit_breaker_drawdown)
        if self.breaker_until is None and value < threshold:
            self.breaker_until = now + CIRCUIT_BREAKER_PAUSE_BARS * BAR_4H
            log.info("Sim %d : circuit breaker (valeur %s, plus haut %s) : pause de 7 jours.",
                        self.sim_id, value, self.peak_value)  # fmt: skip

    # --- Événements ------------------------------------------------------------------------

    async def on_4h_close(self, now: int, ctx: MarketContext) -> None:
        self._update_breaker(now, self.portfolio_value(ctx))
        for market in sorted(self.positions):
            position = self.positions[market]
            position.bars_held += 1
            high = ctx.highest_high_4h(market, 1)
            if high is not None:
                position.highest_high = max(position.highest_high, high)
            reason = self._exit_reason(market, position, ctx)
            if reason is not None:
                await self.exit(market, reason, now, price=ctx.last_price(market))
                continue
            self._update_trailing(position, ctx, market)
            self._save(position, now)
        candidates = []
        for market in self.markets:
            score = self._entry_score(market, now, ctx)
            if score is not None:
                candidates.append((-score, market))
        for _, market in sorted(candidates):
            await self._enter(market, now, ctx)

    async def on_1h_close(
        self, now: int, ctx: MarketContext, hourly_highs: Mapping[str, Decimal]
    ) -> None:
        if self.config.exits.recalc is not StopRecalc.HOURLY:
            return
        for market, position in self.positions.items():
            if market in hourly_highs:
                position.highest_high = max(position.highest_high, hourly_highs[market])
            self._update_trailing(position, ctx, market)
            self._save(position, now)

    async def on_price(self, market: str, low: Decimal, now: int) -> bool:
        """Déclenche le stop si `low` l'atteint. Retourne True si une sortie a eu lieu."""
        position = self.positions.get(market)
        if position is None or low > position.stop:
            return False
        reason = Reason.TRAILING if position.stop_from_trailing else Reason.STOP
        return await self.exit(market, reason, now, trigger_price=position.stop, price=low)

    async def on_order_filled(self, order: Order, ctx: MarketContext, now: int) -> None:
        if order.side is Side.BUY:
            pending = self.pending.get(order.market)
            if pending is None or pending.order_id != order.id:
                return
            del self.pending[order.market]
            self._open_or_add(order, ctx, now)
            await self._sync_take_profit(order.market, now)
            return
        position = self.positions.get(order.market)
        if position is None or position.tp_order_id != order.id:
            return
        position.tp_order_id = None
        assert order.notional is not None and order.fee is not None
        cost = position.invested_cost * order.quantity / position.quantity
        position.realized_pnl += order.notional - order.fee - cost
        position.reduce(order.quantity)
        if self.config.take_profit.type is TakeProfitType.PARTIAL and position.quantity > 0:
            position.partial_done = True
            position.take_profit = None
            position.trailing_active = True
            # Après le take profit partiel, le reste suit le trailing ; sans trailing
            # configuré, le stop remonte au prix d'entrée moyen.
            if self.config.trailing.type is TrailingType.NONE:
                position.raise_stop(position.avg_entry_price)
            else:
                self._update_trailing(position, ctx, order.market)
            log.info("Sim %d : take profit partiel %s, %s restants.", self.sim_id,
                     order.market, position.quantity)  # fmt: skip
            self._save(position, now)
        else:
            self._close_position(order.market, Reason.TAKE_PROFIT, now)

    async def restore_take_profits(self, now: int) -> None:
        """Re-pose les take profit annulés (reprise après un Hold, décision 28)."""
        for market, position in sorted(self.positions.items()):
            if position.tp_order_id is None and position.take_profit is not None:
                await self._sync_take_profit(market, now)

    async def resync_take_profits(self, now: int) -> None:
        """Recalcule tous les take profit (nouveaux paramètres en mode production)."""
        for market in sorted(self.positions):
            await self._sync_take_profit(market, now)

    def on_order_closed(self, order: Order) -> None:
        """Ordre expiré ou annulé."""
        pending = self.pending.get(order.market)
        if pending is not None and pending.order_id == order.id:
            del self.pending[order.market]
        position = self.positions.get(order.market)
        if position is not None and position.tp_order_id == order.id:
            position.tp_order_id = None

    # --- Sorties ---------------------------------------------------------------------------

    def _exit_reason(self, market: str, position: Position, ctx: MarketContext) -> Reason | None:
        trend = self.config.trend
        if (
            trend.enabled
            and trend.action is TrendAction.BLOCK_AND_EXIT
            and not ctx.state(market, trend.filter, "favorable")
        ):
            return Reason.TREND_FILTER
        if self.config.exits.reverse_signal and ctx.state(
            market, self.config.signals.main, "sell_state"
        ):
            return Reason.REVERSE_SIGNAL
        max_bars = self.config.exits.max_holding_bars
        if max_bars is not None and position.bars_held >= max_bars:
            return Reason.MAX_DURATION
        return None

    async def exit(
        self,
        market: str,
        reason: Reason,
        now: int,
        trigger_price: Decimal | None = None,
        price: Decimal | None = None,
    ) -> bool:
        """Sortie complète au marché (le take profit en attente est d'abord annulé).
        `trigger_price` : niveau du stop déclenché (utilisé par le backtest historique) ;
        `price` : cours de référence, pour ne pas tenter de vendre un reliquat valant moins que
        le minimum d'ordre (refus certain, comme sur Bitvavo)."""
        position = self.positions.get(market)
        if position is None:
            return False
        if price is not None and position.quantity * price < self.broker.min_order_quote(market):
            return False
        if position.tp_order_id is not None:
            await self.broker.cancel_order(position.tp_order_id, now)
            position.tp_order_id = None
        result = await self.broker.sell_market(
            self.sim_id, market, position.quantity, reason, now, trigger_price=trigger_price
        )
        if isinstance(result, Rejection):
            log.info("Sim %d : sortie %s (%s) impossible : %s.", self.sim_id, market, reason,
                        result.detail)  # fmt: skip
            return False
        assert result.notional is not None and result.fee is not None
        position.realized_pnl += result.notional - result.fee - position.invested_cost
        self._close_position(market, reason, now)
        return True

    def _close_position(self, market: str, reason: Reason, now: int) -> None:
        self.trade_pnls.append(self.positions.pop(market).realized_pnl)
        if self.store is not None:
            self.store.delete(self.sim_id, market)
        pause = self.config.portfolio.pause_after_stop_bars
        if reason in (Reason.STOP, Reason.TRAILING) and pause > 0:
            self.pause_until[market] = now + pause * BAR_4H
        log.info("Sim %d : position %s clôturée (%s).", self.sim_id, market, reason)

    def _update_trailing(self, position: Position, ctx: MarketContext, market: str) -> None:
        if self.config.trailing.type is TrailingType.NONE:
            return
        level = trailing_level(self.config, position, ctx, market)
        if level is not None:
            position.trailing = level
        if not position.trailing_active and trailing_should_activate(self.config, position):
            position.trailing_active = True
        if position.trailing_active and position.trailing is not None:
            position.raise_stop(position.trailing)

    # --- Entrées ---------------------------------------------------------------------------

    def _entry_score(self, market: str, now: int, ctx: MarketContext) -> float | None:
        """Score d'entrée si toutes les conditions sont réunies, sinon None."""
        config = self.config
        if self.breaker_until is not None or self.pause_until.get(market, 0) > now:
            return None
        if market in self.pending:
            return None
        position = self.positions.get(market)
        if position is None:
            open_count = len(self.positions) + sum(1 for p in self.pending.values() if not p.is_add)
            if open_count >= config.portfolio.max_positions:
                return None
        elif position.lots >= config.portfolio.max_entries:
            return None
        if ctx.daily_history_days(market) < MIN_DAILY_HISTORY:
            return None
        if ctx.bars_4h(market) < config.warmup_4h:
            return None
        if config.trend.enabled and not ctx.state(market, config.trend.filter, "favorable"):
            return None
        signals = config.signals
        if not ctx.state(market, signals.main, "buy_state"):
            return None  # signal principal obligatoire (décision 11)
        score = signals.main_weight + sum(
            weight for conf, weight in signals.confirmations if ctx.state(market, conf, "buy_state")
        )
        if score < signals.threshold - 1e-12:
            return None
        if position is not None:
            # Renforcement seulement après +1R depuis la dernière entrée (décision 14).
            price = ctx.last_price(market)
            if price is None or price < position.last_entry_price + position.risk_per_unit:
                return None
        return score

    async def _enter(self, market: str, now: int, ctx: MarketContext) -> None:
        config = self.config
        is_add = market in self.positions
        if config.execution.entry_order is EntryOrder.LIMIT:
            reference = ctx.best_bid(market)  # décision 12
        else:
            reference = ctx.last_price(market)
        if reference is None:
            return
        stop = initial_stop(config, reference, ctx, market)
        if is_add:
            stop = self.positions[market].stop if stop is None else stop
        if stop is None:
            log.info("Sim %d : %s ignoré, stop initial incalculable.", self.sim_id, market)
            return
        capital = self.portfolio_value(ctx)
        amount = entry_amount(
            config.portfolio,
            SizingInputs(
                capital=capital,
                available_cash=self.broker.cash(self.sim_id),
                exposure=self._exposure(ctx),
                market_exposure=self._exposure(ctx, market),
                stop_distance=(reference - stop) / reference,
            ),
        )
        # Sous le minimum d'ordre (frais compris), l'achat serait refusé : inutile de l'envoyer.
        minimum = self.broker.min_order_quote(market) * (1 + self.fees.taker)
        if amount < minimum:
            lifted = minimum * LIFT_MARGIN
            if not (
                self.lift_to_minimum and amount > 0 and self.broker.cash(self.sim_id) >= lifted
            ):
                return
            amount = lifted
        if config.execution.entry_order is EntryOrder.MARKET:
            result = await self.broker.buy_market(self.sim_id, market, amount, Reason.SIGNAL, now)
            if isinstance(result, Order):
                self._open_or_add(result, ctx, now)
                await self._sync_take_profit(market, now)
            return
        expires_at = now + config.execution.limit_expiry_hours * HOUR
        placed = await self.broker.place_limit_buy(
            self.sim_id, market, reference, amount, expires_at, Reason.SIGNAL, now
        )
        if isinstance(placed, Order) and placed.status is OrderStatus.OPEN:
            self.pending[market] = PendingEntry(placed.id, is_add)

    def _exposure(self, ctx: MarketContext, market: str | None = None) -> Decimal:
        markets = [market] if market is not None else list(self.positions)
        total = sum((self.position_value(m, ctx) for m in markets), Decimal(0))
        reserved = sum(
            (o.reserved or Decimal(0) for o in self.broker.open_orders(self.sim_id)
             if o.side is Side.BUY and (market is None or o.market == market)),
            Decimal(0),
        )  # fmt: skip
        return total + reserved

    def _open_or_add(self, order: Order, ctx: MarketContext, now: int) -> None:
        assert order.price is not None and order.notional is not None and order.fee is not None
        market, price = order.market, order.price
        position = self.positions.get(market)
        if position is not None:
            position.add_entry(order.quantity, order.notional, order.fee, price)
            position.raise_stop(position.avg_entry_price)  # décision 14
            log.info("Sim %d : renforcement %s (lot %d) à %s, stop %s.", self.sim_id, market,
                     position.lots, price, position.stop)  # fmt: skip
        else:
            stop = initial_stop(self.config, price, ctx, market)
            if stop is None:
                atr = ctx.atr_4h(market)
                stop = price - 2 * atr if atr is not None and atr < price else None
                stop = stop if stop is not None else price * (1 - FALLBACK_STOP)
                log.warning("Sim %d : stop initial %s recalculé par défaut (%s).", self.sim_id,
                            market, stop)  # fmt: skip
            position = Position(
                market=market, lots=1, quantity=order.quantity, avg_entry_price=price,
                invested_cost=order.notional + order.fee, buy_fees=order.fee,
                risk_per_unit=price - stop, initial_stop=stop, stop=stop,
                last_entry_price=price, highest_high=price, opened_at=now,
            )  # fmt: skip
            self.positions[market] = position
            self._update_trailing(position, ctx, market)
            log.info("Sim %d : entrée %s à %s, stop %s (R %s).", self.sim_id, market, price,
                     stop, position.risk_per_unit)  # fmt: skip
        self._save(position, now)

    async def _sync_take_profit(self, market: str, now: int) -> None:
        position = self.positions.get(market)
        if position is None:
            return
        level = take_profit_level(self.config, position)
        if position.tp_order_id is not None:
            await self.broker.cancel_order(position.tp_order_id, now)
            position.tp_order_id = None
        position.take_profit = level
        quantity = take_profit_quantity(self.config, position)
        if level is not None and quantity * level >= self.broker.min_order_quote(market):
            placed = await self.broker.place_limit_sell(
                self.sim_id, market, level, quantity, Reason.TAKE_PROFIT, now
            )
            if isinstance(placed, Order):
                position.tp_order_id = placed.id
        self._save(position, now)

    def _save(self, position: Position, now: int) -> None:
        if self.store is not None:
            self.store.save(self.sim_id, position, now)
