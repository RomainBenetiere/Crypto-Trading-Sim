"""Mode ombre (SPEC §15, palier 2) : le scénario choisi tourne sur son propre portefeuille.

Le portefeuille démarre avec le capital engagé (300 € par défaut) ; le dimensionnement étant
exprimé en % du capital, les montants sont ceux de la simulation mis à l'échelle. Les achats
sous le minimum d'ordre sont relevés au minimum (décision 43). Les ordres sont exécutés au
carnet réel par un `PaperBroker` dédié et enregistrés dans `production_orders` : ce sont les
ordres que le mode réel enverrait, mais rien n'est envoyé.

Changement de scénario : positions et ordres conservés, gérés avec les nouveaux paramètres
(take profit recalculés) ; un changement de capital ajuste les liquidités de la différence.
L'ombre suit l'état des simulations : active seulement quand elles tournent.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from decimal import Decimal

from mcs import production
from mcs.broker.base import Side
from mcs.broker.book import BookProvider
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import PaperBroker
from mcs.broker.rules import MarketRules
from mcs.live.state import SimStateStore
from mcs.storage.db import transaction
from mcs.storage.positions import SqlitePositionStore
from mcs.strategy.serialize import config_from_json
from mcs.strategy.simulation import PendingEntry, Simulation

log = logging.getLogger(__name__)

SIM_ID = production.PRODUCTION_SIM_ID


class ShadowRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeSchedule,
        books: BookProvider,
        rules: Mapping[str, MarketRules],
        basket: list[str],
    ) -> None:
        self.conn, self.fees, self.basket = conn, fees, basket
        self.broker = PaperBroker(conn, fees, books, rules=rules, orders_table="production_orders")
        self.positions = SqlitePositionStore(conn, "production_positions")
        self.states = SimStateStore(conn, "production_sim_state")
        self.sim: Simulation | None = None
        self.choice_id: int | None = None

    async def sync(self, active: bool, now: int) -> None:
        """Aligne l'ombre sur le mode demandé et le scénario choisi. `active` : les
        simulations tournent (l'ombre suit leur état)."""
        state = production.read_state(self.conn)
        choice = production.active_choice(self.conn)
        if not active or state.mode is not production.Mode.SHADOW or choice is None:
            if self.sim is not None:
                await self.pause(now)
            return
        if self.sim is not None and self.choice_id == choice.id:
            return
        switching = self.sim is not None
        self._build(choice, state.capital_applied, now)
        assert self.sim is not None
        if switching:
            # Nouveaux paramètres : take profit recalculés sur les positions conservées.
            await self.sim.resync_take_profits(now)
        else:
            await self.sim.restore_take_profits(now)
        self.save(now)

    def _build(self, choice: production.Choice, applied: Decimal | None, now: int) -> None:
        self.broker.open_account(SIM_ID, choice.capital)  # liquidités = capital + flux
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
            sim.peak_value += choice.capital - applied  # pas de faux circuit breaker
            log.info("Ombre : capital %s € → %s €.", applied, choice.capital)
        production.set_capital_applied(self.conn, choice.capital, now)
        for order in self.broker.open_orders(SIM_ID):
            if order.side is Side.BUY:
                sim.pending[order.market] = PendingEntry(order.id, order.market in sim.positions)
        self.sim, self.choice_id = sim, choice.id
        log.info(
            "Ombre : scénario S-%03d, capital %s €, %d positions.",
            choice.sim_id,
            choice.capital,
            len(sim.positions),
        )

    async def pause(self, now: int) -> None:
        """Hold ou arrêt de l'ombre : ordres en attente annulés, positions conservées."""
        sim = self.sim
        if sim is None:
            return
        for order in self.broker.open_orders(SIM_ID):
            sim.on_order_closed(await self.broker.cancel_order(order.id, now))
        self.save(now)
        self.sim = self.choice_id = None
        log.info("Ombre en pause (%d positions conservées).", len(sim.positions))

    def save(self, now: int) -> None:
        if self.sim is not None:
            self.states.save([self.sim], now)

    def write_value(self, ts: int, value: Decimal) -> None:
        assert self.sim is not None
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO production_values (ts, total_value, cash) VALUES (?, ?, ?)
                   ON CONFLICT (ts) DO UPDATE SET total_value = excluded.total_value,
                       cash = excluded.cash""",
                (ts, value, self.broker.total_cash(SIM_ID)),
            )
