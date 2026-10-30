"""Mode réel (SPEC §15, palier 3) : le scénario choisi passe de vrais ordres sur Bitvavo.

Même stratégie que l'ombre, sur son propre portefeuille (capital engagé + exécutions réelles,
tables `live_*`), avec un `BitvavoBroker`. Garde-fous (décision 45) :
- double verrou : le runner n'existe que si `.env` contient `PRODUCTION_LIVE=1`, un plafond
  `PRODUCTION_MAX_CAPITAL` et la clé ; le dashboard demande ensuite le démarrage, confirmé ;
- plafond : coût des positions + achats en attente + nouvel achat ≤ plafond ;
- perte du jour (Europe/Paris) ≥ 10 % de la valeur de départ : achats bloqués jusqu'au
  lendemain, positions toujours gérées ;
- Hold des simulations : plus d'achat ni de décision 4 h, achats en attente annulés, mais stops
  et take profit toujours gérés ;
- arrêt progressif : plus d'achat, positions gérées jusqu'à leur clôture, puis arrêt ;
- arrêt d'urgence : ordres annulés, positions du moteur vendues au marché (une tentative ; un
  échec est signalé pour une vente manuelle), mode coupé. Les cryptos détenues hors du moteur
  ne sont jamais touchées.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from mcs import production
from mcs.bitvavo.trading import BitvavoTrading
from mcs.broker.base import Order, OrderStatus, OrderType, Reason, Side
from mcs.broker.fees import FeeSchedule
from mcs.broker.live import BitvavoBroker
from mcs.broker.rules import MarketRules
from mcs.live.state import SimStateStore
from mcs.market_data import Candle
from mcs.production import LiveStatus
from mcs.storage.db import transaction
from mcs.storage.positions import SqlitePositionStore
from mcs.strategy.context import MarketContext
from mcs.strategy.serialize import config_from_json
from mcs.strategy.simulation import PendingEntry, Simulation

log = logging.getLogger(__name__)

SIM_ID = production.PRODUCTION_SIM_ID
PARIS = ZoneInfo("Europe/Paris")


def paris_day(now: int) -> str:
    return datetime.fromtimestamp(now / 1000, UTC).astimezone(PARIS).date().isoformat()


class LiveRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeSchedule,
        trading: BitvavoTrading,
        rules: Mapping[str, MarketRules],
        basket: list[str],
        max_capital: Decimal,
        broker: BitvavoBroker | None = None,
    ) -> None:
        self.conn, self.fees, self.basket = conn, fees, basket
        self.max_capital = max_capital
        self.broker = broker or BitvavoBroker(conn, fees, trading, rules)
        self.broker.guard = self.guard
        self.positions = SqlitePositionStore(conn, "live_positions")
        self.states = SimStateStore(conn, "live_sim_state")
        self.sim: Simulation | None = None
        self.choice_id: int | None = None
        self.held = False  # achats en attente déjà annulés pour le Hold en cours
        self.now = 0

    # --- Alignement sur l'état demandé -----------------------------------------------------

    async def sync(self, running: bool, ctx: MarketContext | None, now: int) -> None:
        """Applique l'état demandé par le dashboard. `running` : les simulations tournent."""
        self.now = now
        live = production.read_live(self.conn)
        choice = production.active_choice(self.conn)
        if live.status is LiveStatus.OFF or choice is None:
            if self.sim is not None:
                self.save(now)
                self.sim = self.choice_id = None
            return
        if self.sim is None or self.choice_id != choice.id:
            switching = self.sim is not None
            self._build(choice, live.capital_applied, now)
            assert self.sim is not None
            if switching:
                await self.sim.resync_take_profits(now)
            else:
                await self.sim.restore_take_profits(now)
            self.save(now)
        if live.emergency_requested_at is not None:
            await self.emergency(ctx, now)
            return
        if not running and not self.held:
            await self._cancel_entries(ctx, now)
            self.held = True
        elif running:
            self.held = False
        await self._finish_wind_down(now)

    def _build(self, choice: production.Choice, applied: Decimal | None, now: int) -> None:
        self.broker.open_account(SIM_ID, choice.capital)
        sim = Simulation(
            SIM_ID,
            config_from_json(choice.config_json),
            self.broker,
            self.fees,
            self.basket,
            choice.capital,
            store=self.positions,
            lift_to_minimum=True,
        )
        sim.positions = self.positions.load(SIM_ID)
        self.states.restore(sim)
        if applied is not None and applied != choice.capital:
            sim.peak_value += choice.capital - applied
            log.info("Réel : capital %s € → %s €.", applied, choice.capital)
        production.set_live_capital_applied(self.conn, choice.capital, now)
        for order in self.broker.open_orders(SIM_ID):
            if order.side is Side.BUY and order.order_type is OrderType.LIMIT:
                sim.pending[order.market] = PendingEntry(order.id, order.market in sim.positions)
        self.sim, self.choice_id = sim, choice.id
        log.warning(
            "Réel : scénario S-%03d, capital %s € (plafond %s €), %d positions.",
            choice.sim_id,
            choice.capital,
            self.max_capital,
            len(sim.positions),
        )

    # --- Garde-fous ------------------------------------------------------------------------

    def guard(self, sim_id: int, cost: Decimal) -> str | None:
        """Motif de refus d'un achat réel, ou None s'il est permis."""
        live = production.read_live(self.conn)
        if live.status is not LiveStatus.ON or live.emergency_requested_at is not None:
            return f"mode réel {production.LIVE_LABELS[live.status].lower()}"
        if live.buys_blocked_day is not None and live.buys_blocked_day == paris_day(self.now):
            return "perte maximale du jour atteinte, achats bloqués jusqu'à demain"
        if self.sim is None:
            return "mode réel non initialisé"
        engaged = sum((p.invested_cost for p in self.sim.positions.values()), Decimal(0))
        engaged += sum(
            (o.reserved or Decimal(0) for o in self.broker.open_orders(sim_id)), Decimal(0)
        )
        if engaged + cost > self.max_capital:
            return f"plafond {self.max_capital} € (engagé {engaged:.2f} €, achat {cost:.2f} €)"
        return None

    def check_daily_loss(self, ctx: MarketContext, now: int) -> None:
        if self.sim is not None:
            production.update_live_day(
                self.conn, paris_day(now), self.sim.portfolio_value(ctx), now
            )

    # --- Événements ------------------------------------------------------------------------

    async def on_candle(self, candle: Candle, now: int) -> None:
        """Bougie 1 min : stop déclenché → vente réelle au marché."""
        self.now = now
        sim = self.sim
        if (
            sim is not None
            and candle.market in sim.positions
            and await sim.on_price(candle.market, candle.low, now)
        ):
            self.save(now)

    async def tick(self, ctx: MarketContext, now: int) -> None:
        """Chaque minute : suivi des ordres chez Bitvavo, expirations, perte du jour."""
        self.now = now
        if self.sim is None:
            return
        for order in await self.broker.poll(now):
            await self._dispatch(order, ctx, now)
        for order in await self.broker.expire_orders(now):
            await self._dispatch(order, ctx, now)
        self.check_daily_loss(ctx, now)
        await self._finish_wind_down(now)
        self.save(now)

    async def on_4h_close(self, ctx: MarketContext, now: int) -> None:
        self.now = now
        if self.sim is not None:
            await self.sim.on_4h_close(now, ctx)
            self.save(now)

    async def _dispatch(self, order: Order, ctx: MarketContext | None, now: int) -> None:
        sim = self.sim
        assert sim is not None
        if order.status is not OrderStatus.FILLED:
            sim.on_order_closed(order)
            return
        if ctx is None:
            log.error("Réel : exécution %d reçue sans contexte de marché.", order.id)
            return
        if order.order_type is OrderType.MARKET:
            if order.side is Side.BUY:
                # Achat au marché confirmé après coup (réponse perdue, redémarrage) :
                # la position est ouverte comme pour un ordre limite.
                sim.pending[order.market] = PendingEntry(order.id, order.market in sim.positions)
            else:
                production.record_live_event(
                    self.conn,
                    f"Vente {order.market} confirmée après coup : position à vérifier.",
                    now,
                )
        await sim.on_order_filled(order, ctx, now)

    async def _cancel_entries(self, ctx: MarketContext | None, now: int) -> None:
        """Hold : achats en attente annulés, take profit conservés."""
        for order in self.broker.open_orders(SIM_ID):
            if order.side is Side.BUY:
                await self._dispatch(await self.broker.cancel_order(order.id, now), ctx, now)
        self.save(now)

    async def _finish_wind_down(self, now: int) -> None:
        live = production.read_live(self.conn)
        sim = self.sim
        if (
            live.status is LiveStatus.WINDING_DOWN
            and sim is not None
            and not sim.positions
            and not self.broker.open_orders(SIM_ID)
        ):
            production.set_live_status(
                self.conn, LiveStatus.OFF, "Arrêt progressif terminé : plus de position.", now
            )
            self.save(now)
            self.sim = self.choice_id = None
            log.warning("Réel : arrêt progressif terminé.")

    async def emergency(self, ctx: MarketContext | None, now: int) -> None:
        """Annule tous les ordres, vend au marché les positions du moteur, coupe le mode."""
        sim = self.sim
        assert sim is not None
        log.warning("Réel : ARRÊT D'URGENCE.")
        for order in self.broker.open_orders(SIM_ID):
            try:
                await self._dispatch(await self.broker.cancel_order(order.id, now), ctx, now)
            except Exception:
                log.exception("Réel : annulation de l'ordre %d impossible.", order.id)
        unsold = []
        for market in sorted(sim.positions):
            try:
                if not await sim.exit(market, Reason.EMERGENCY, now):
                    unsold.append(market)
            except Exception:
                log.exception("Réel : vente d'urgence %s impossible.", market)
                unsold.append(market)
        self.save(now)
        # Une seule tentative : ce qui reste (refus de Bitvavo, montant sous le minimum,
        # coupure réseau) est signalé pour une vente manuelle, sans boucle de nouvelles
        # tentatives.
        event = "Arrêt d'urgence exécuté : positions du moteur vendues."
        if unsold:
            event = (
                f"Arrêt d'urgence : vente impossible pour {', '.join(unsold)}, à vendre à la main."
            )
        production.set_live_status(self.conn, LiveStatus.OFF, event, now)
        self.sim = self.choice_id = None

    # --- Persistance -----------------------------------------------------------------------

    def save(self, now: int) -> None:
        if self.sim is not None:
            self.states.save([self.sim], now)

    def write_value(self, ts: int, value: Decimal) -> None:
        assert self.sim is not None
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO live_values (ts, total_value, cash) VALUES (?, ?, ?)
                   ON CONFLICT (ts) DO UPDATE SET total_value = excluded.total_value,
                       cash = excluded.cash""",
                (ts, value, self.broker.total_cash(SIM_ID)),
            )
