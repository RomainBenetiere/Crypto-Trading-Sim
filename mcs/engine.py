"""Moteur live : un flux de données partagé, 300 simulations, commandes du dashboard.

    python -m mcs.engine

Le moteur intègre la collecte (décision 31) : WebSocket (ticker, bougies 1 min) et REST
(bougies clôturées). Événements :
- bougie 1 min (en cours, à chaque transaction) : stops, ordres limite ;
- chaque minute : expiration des ordres limite, signe de vie ;
- clôture 1 h : nouvelles bougies chargées, trailing horaire, valeur des portefeuilles ;
- clôture 4 h : sorties, trailings et entrées des 300 simulations.

Mode ombre (SPEC §15) : le scénario choisi tourne en plus sur son propre portefeuille
(`ShadowRunner`), aux mêmes événements, tant que les simulations tournent ; le solde réel est
lu toutes les 5 minutes si une clé en lecture seule est configurée. Aucun ordre envoyé.

Machine à états (SPEC §8) pilotée par la table `commands`. En Hold, aucun ordre : les
entrées en attente et les take profit sont annulés, puis les take profit sont re-posés à la
reprise (décision 28). Une coupure non prévue est enregistrée comme interruption, sans
rejouer les décisions manquées (décision 29). L'optimisation tourne dans un processus séparé
(décision 30).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

from mcs import production
from mcs.backtest import Dataset, load_dataset
from mcs.bitvavo.account import BitvavoAccount
from mcs.bitvavo.rest import BitvavoError, BitvavoRest
from mcs.bitvavo.trading import BitvavoTrading
from mcs.bitvavo.ws import TickerState
from mcs.broker.base import Liquidity, Side
from mcs.broker.book import BookProvider, CachedBookProvider, max_quantity_for_budget, walk_quantity
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import PaperBroker
from mcs.broker.rules import load_rules
from mcs.collector import LiveCollector
from mcs.config import load_settings
from mcs.live.context import LiveContext
from mcs.live.production import ShadowRunner
from mcs.live.real import LiveRunner
from mcs.live.state import (
    Command,
    SimStateStore,
    Status,
    complete_command,
    heartbeat,
    next_command,
    open_period,
    read_state,
    record_outage,
    set_status,
)
from mcs.logging_setup import configure_logging
from mcs.market_data import MINUTE_MS, Candle, floor_ts
from mcs.metrics import (
    SimMetrics,
    buy_and_hold_value,
    compute_metrics,
    load_buy_and_hold,
    load_metrics,
    write_metrics,
)
from mcs.sampling import load_simulations
from mcs.storage.db import connect, now_ms, transaction
from mcs.storage.markets import selected_markets
from mcs.storage.positions import SqlitePositionStore
from mcs.strategy.config import StopRecalc
from mcs.strategy.simulation import PendingEntry, Simulation

log = logging.getLogger("mcs.engine")

INITIAL_CAPITAL = Decimal(1000)
OUTAGE_AFTER_MS = 5 * MINUTE_MS
COMMAND_POLL_S = 2.0
HEARTBEAT_S = 30.0
TICK_DELAY_MS = 3_000
BOOK_TTL_S = 30.0
BALANCE_POLL_S = 300.0
BALANCE_RETRY_S = 30.0  # nouvel essai rapide après un échec passager (DNS, réseau)
RESET_TABLES = (
    "orders",
    "positions",
    "portfolio_values",
    "metrics",
    "buy_and_hold",
    "run_periods",
    "sim_state",
)


class EngineError(RuntimeError):
    """Commande refusée (message en français, renvoyé au dashboard)."""


def launch_optimizer(workers: int, recalibrate: bool) -> subprocess.Popen[bytes]:
    command = [sys.executable, "-m", "mcs.optimizer", "run", "--workers", str(workers)]
    return subprocess.Popen([*command, "--all"] if recalibrate else command)


class Engine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeSchedule,
        books: BookProvider,
        *,
        tickers: Mapping[str, TickerState] | None = None,
        clock: Callable[[], int] = now_ms,
        data_loader: Callable[[sqlite3.Connection, list[str]], Dataset] = load_dataset,
        optimizer_workers: int = 4,
        optimizer_launcher: Callable[[int, bool], Any] = launch_optimizer,
        trading: BitvavoTrading | None = None,
        max_capital: Decimal | None = None,
    ) -> None:
        self.conn, self.fees, self.books = conn, fees, books
        self.tickers = tickers if tickers is not None else {}
        self.clock, self.data_loader = clock, data_loader
        self.optimizer_workers, self.optimizer_launcher = optimizer_workers, optimizer_launcher
        self.optimizer_process: Any = None
        self.positions = SqlitePositionStore(conn)
        self.sim_states = SimStateStore(conn)
        self.status = read_state(conn).status
        self.broker: PaperBroker | None = None
        self.ctx: LiveContext | None = None
        self.sims: dict[int, Simulation] = {}
        self.shadow: ShadowRunner | None = None
        # Mode réel (palier 3) : seulement si le double verrou du `.env` est levé.
        self.trading = trading if max_capital is not None else None
        self.max_capital = max_capital
        self.live: LiveRunner | None = None
        self.metrics: dict[int, SimMetrics] = {}
        self.last_1h = self.last_4h = 0
        # Les commandes, les bougies 1 min et les clôtures ne s'entrelacent jamais.
        self.lock = asyncio.Lock()

    # --- Démarrage et état en mémoire -----------------------------------------------------

    def startup(self) -> None:
        """Recharge l'état après un redémarrage ; enregistre une éventuelle coupure."""
        now = self.clock()
        state = read_state(self.conn)
        self.status = state.status
        armed = self.trading is not None
        production.set_live_armed(self.conn, armed, self.max_capital, now)
        if not armed and production.read_live(self.conn).status is not production.LiveStatus.OFF:
            log.error("Mode réel demandé mais verrouillé (.env) : positions réelles non gérées.")
            production.record_live_event(
                self.conn, "Moteur sans PRODUCTION_LIVE ni clé : positions réelles non gérées.", now
            )
        if state.status is not Status.NOT_STARTED:
            self._load_runtime(list(state.basket))
            if state.heartbeat_at is not None and now - state.heartbeat_at > OUTAGE_AFTER_MS:
                kind = "active" if state.status is Status.RUNNING else "hold"
                record_outage(self.conn, state.heartbeat_at, now, kind)
                log.warning(
                    "Interruption enregistrée depuis le dernier signe de vie (%d min).",
                    (now - state.heartbeat_at) // MINUTE_MS,
                )
        self.last_1h, self.last_4h = floor_ts(now, "1h"), floor_ts(now, "4h")
        heartbeat(self.conn, now)

    def _load_runtime(self, basket: list[str]) -> None:
        if not basket:
            raise EngineError("Panier vide.")
        self.ctx = LiveContext(self.data_loader(self.conn, basket), self.tickers, self.clock)
        rules = load_rules(self.conn)
        self.broker = PaperBroker(
            self.conn, self.fees, self.books, rules={m: rules[m] for m in basket}
        )
        self.shadow = ShadowRunner(
            self.conn, self.fees, self.books, {m: rules[m] for m in basket}, basket
        )
        if self.trading is not None and self.max_capital is not None:
            self.live = LiveRunner(
                self.conn,
                self.fees,
                self.trading,
                {m: rules[m] for m in basket},
                basket,
                self.max_capital,
            )
        self.sims = {}
        self.metrics = load_metrics(self.conn)
        for stored in load_simulations(self.conn):
            self.broker.open_account(stored.id, INITIAL_CAPITAL)
            sim = Simulation(
                stored.id,
                stored.config,
                self.broker,
                self.fees,
                basket,
                INITIAL_CAPITAL,
                store=self.positions,
            )
            sim.positions = self.positions.load(stored.id)
            self.sim_states.restore(sim)
            for order in self.broker.open_orders(stored.id):
                if order.side is Side.BUY:
                    sim.pending[order.market] = PendingEntry(
                        order.id, order.market in sim.positions
                    )
            self.sims[stored.id] = sim
        log.info("%d simulations chargées sur %d paires.", len(self.sims), len(basket))

    def _clear_runtime(self) -> None:
        self.broker, self.ctx, self.sims, self.metrics = None, None, {}, {}
        self.shadow = self.live = None

    # --- Commandes ------------------------------------------------------------------------

    async def process_commands(self) -> bool:
        """Exécute la plus ancienne commande en attente, puis aligne le mode ombre sur l'état
        et le choix du dashboard. Retourne True si une commande a été traitée."""
        async with self.lock:
            processed = await self._process_next_command()
            await self.sync_shadow()
            await self.sync_live()
            return processed

    async def sync_shadow(self) -> None:
        if production.read_state(self.conn).reset_requested_at is not None:
            now = self.clock()
            if self.shadow is not None:
                await self.shadow.pause(now)
            production.reset_shadow(self.conn, now)
            log.info("Ombre remise à zéro.")
        if self.shadow is not None:
            await self.shadow.sync(self.status is Status.RUNNING, self.clock())

    async def sync_live(self) -> None:
        if self.live is not None:
            try:
                await self.live.sync(self.status is Status.RUNNING, self.ctx, self.clock())
            except Exception:
                log.exception("Réel : échec de l'alignement sur l'état demandé.")

    async def _process_next_command(self) -> bool:
        command = next_command(self.conn)
        if command is None:
            return False
        now = self.clock()
        handlers = {
            Command.OPTIMIZE: self._optimize,
            Command.START: self._start,
            Command.STOP: self._stop,
            Command.RESUME: self._resume,
            Command.RESET: self._reset,
        }
        try:
            handler = handlers[Command(command.command)]
            result = await handler(now)
        except (EngineError, ValueError) as exc:
            result = f"refusée : {exc}"
        log.info("Commande %s : %s", command.command, result)
        complete_command(self.conn, command.id, self.clock(), result)
        return True

    def _require(self, allowed: Status, action: str) -> None:
        if self.status is not allowed:
            raise EngineError(f"« {action} » impossible à l'état {self.status.value}.")

    async def _optimize(self, now: int) -> str:
        self._require(Status.NOT_STARTED, "Optimiser")
        optimization = read_state(self.conn).optimization_status
        if optimization == "RUNNING":
            raise EngineError("une optimisation est déjà en cours.")
        if not self.conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]:
            raise EngineError("aucun plan de simulations (python -m mcs.sampling generate).")
        recalibrate = optimization == "DONE"
        self.optimizer_process = self.optimizer_launcher(self.optimizer_workers, recalibrate)
        what = "recalibrage de toutes les jumelles" if recalibrate else "optimisation"
        return f"{what} lancée ({self.optimizer_workers} cœurs)"

    async def _start(self, now: int) -> str:
        self._require(Status.NOT_STARTED, "Lancer")
        if read_state(self.conn).optimization_status != "DONE":
            raise EngineError("l'optimisation doit être terminée.")
        basket = selected_markets(self.conn)
        if not self.conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]:
            raise EngineError("aucun plan de simulations.")
        self._load_runtime(basket)
        assert self.broker is not None
        await self._buy_and_hold(basket, now)
        with transaction(self.conn):
            open_period(self.conn, "active", now)
        set_status(self.conn, Status.RUNNING, now, started_at=now, basket_json=json.dumps(basket))
        self.status = Status.RUNNING
        self.sim_states.save(list(self.sims.values()), now)
        self.last_1h, self.last_4h = floor_ts(now, "1h"), floor_ts(now, "4h")
        return f"lancé : {len(self.sims)} simulations, {len(basket)} paires"

    async def _stop(self, now: int) -> str:
        self._require(Status.RUNNING, "Arrêter")
        assert self.broker is not None
        cancelled = 0
        for sim in self.sims.values():
            for order in self.broker.open_orders(sim.sim_id):
                closed = await self.broker.cancel_order(order.id, now)
                sim.on_order_closed(closed)
                cancelled += 1
        with transaction(self.conn):
            open_period(self.conn, "hold", now)
        set_status(self.conn, Status.HOLD, now)
        self.status = Status.HOLD
        self.sim_states.save(list(self.sims.values()), now)
        return f"en Hold ({cancelled} ordres annulés)"

    async def _resume(self, now: int) -> str:
        self._require(Status.HOLD, "Reprendre")
        with transaction(self.conn):
            open_period(self.conn, "active", now)
        set_status(self.conn, Status.RUNNING, now)
        self.status = Status.RUNNING
        for sim in self.sims.values():
            await sim.restore_take_profits(now)
        return "reprise"

    async def _reset(self, now: int) -> str:
        self._require(Status.HOLD, "Remettre à zéro")
        if production.read_live(self.conn).status is not production.LiveStatus.OFF:
            raise EngineError("arrêtez d'abord le mode réel (positions réelles en cours).")
        with transaction(self.conn):
            for table in RESET_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
        set_status(self.conn, Status.NOT_STARTED, now, started_at=None, basket_json=None)
        self.status = Status.NOT_STARTED
        self._clear_runtime()
        return "remis à zéro (configurations conservées)"

    async def _buy_and_hold(self, basket: list[str], now: int) -> None:
        """1 000 € à poids égaux, frais taker, au carnet réel, achat unique (SPEC §6)."""
        assert self.broker is not None
        weight = Decimal(1) / len(basket)
        budget = INITIAL_CAPITAL * weight
        rows = []
        for market in basket:
            rules = self.broker.rules[market]
            book = await self.books.get_book(market)
            quantity = rules.round_quantity(
                max_quantity_for_budget(book.asks, budget, self.fees.taker)
            )
            while quantity > 0:
                execution = walk_quantity(book.asks, quantity)
                fee = self.fees.fee(execution.notional, Liquidity.TAKER)
                if execution.notional + fee <= budget:
                    break
                quantity -= rules.quantity_step
            if quantity <= 0:
                log.warning("Buy & hold : %s ignorée, budget %s € insuffisant.", market, budget)
                continue
            rows.append((market, weight, budget, fee, execution.average_price, quantity, now))
        with transaction(self.conn):
            self.conn.executemany(
                """INSERT INTO buy_and_hold (market, weight, allocated_eur, buy_fee, start_price,
                       quantity, bought_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    # --- Événements de marché ---------------------------------------------------------------

    async def on_minute_candle(self, candle: Candle) -> None:
        """Bougie 1 min reçue du WebSocket (en cours) : stops puis ordres limite."""
        async with self.lock:
            await self._on_minute_candle(candle)

    async def _on_minute_candle(self, candle: Candle) -> None:
        live = self.live
        if live is not None and live.sim is not None and self.ctx is not None:
            # Stops réels surveillés même en Hold (décision 45).
            try:
                self.ctx.set_now(self.clock())
                await live.on_candle(candle, self.clock())
            except Exception:
                log.exception("Réel : échec du traitement de la bougie %s.", candle.market)
        if self.status is not Status.RUNNING or self.broker is None or self.ctx is None:
            return
        now = self.clock()
        self.ctx.set_now(now)
        changed = []
        for sim in self.sims.values():
            if candle.market in sim.positions and await sim.on_price(
                candle.market, candle.low, now
            ):
                changed.append(sim)
        for order in self.broker.on_candle(candle):
            await self.sims[order.sim_id].on_order_filled(order, self.ctx, now)
        if changed:
            self.sim_states.save(changed, now)
        shadow = self.shadow
        if shadow is not None and shadow.sim is not None:
            moved = candle.market in shadow.sim.positions and await shadow.sim.on_price(
                candle.market, candle.low, now
            )
            for order in shadow.broker.on_candle(candle):
                await shadow.sim.on_order_filled(order, self.ctx, now)
            if moved:
                shadow.save(now)

    async def on_tick(self, now: int, data_ok: bool = True) -> None:
        """Chaque minute, après la collecte des bougies clôturées. `data_ok` est faux si la
        collecte a échoué (coupure réseau) : la clôture 4 h est alors reportée, jamais prise
        sur des bougies incomplètes ; le reste (valeurs, expirations) continue."""
        async with self.lock:
            four_hour_close = await self._on_tick(now, data_ok)
        if four_hour_close:
            # Hors du verrou global : stops et ordres limite continuent d'être traités
            # entre deux simulations pendant la clôture 4 h.
            await self._four_hourly(now)

    async def _on_tick(self, now: int, data_ok: bool = True) -> bool:
        """Partie minute et heure ; retourne True si une clôture 4 h est à traiter."""
        heartbeat(self.conn, now)
        if self.broker is None or self.ctx is None:
            return False
        self.ctx.set_now(now)
        shadow = self.shadow.sim if self.shadow is not None else None
        if self.status is Status.RUNNING:
            for order in self.broker.expire_orders(now):
                self.sims[order.sim_id].on_order_closed(order)
            if shadow is not None and self.shadow is not None:
                for order in self.shadow.broker.expire_orders(now):
                    shadow.on_order_closed(order)
        if self.live is not None and self.live.sim is not None:
            try:
                await self.live.tick(self.ctx, now)
            except Exception:
                log.exception("Réel : échec du suivi des ordres.")
        hour = floor_ts(now, "1h")
        if hour > self.last_1h:
            self.last_1h = hour
            for market in self.ctx.data.markets:
                self.ctx.append_closed(self.conn, market)
            self.ctx.set_now(now)
            if self.status is Status.RUNNING:
                await self._hourly(now)
            elif self.live is not None and self.live.sim is not None:
                await self._hourly_sims(now, [self.live.sim])  # stops réels gérés en Hold
            self.write_portfolio_values(hour)
            self.update_metrics(now)
            if self.shadow is not None and self.shadow.sim is not None:
                self.shadow.write_value(hour, self.shadow.sim.portfolio_value(self.ctx))
            if self.live is not None and self.live.sim is not None:
                self.live.write_value(hour, self.live.sim.portfolio_value(self.ctx))
        bar = floor_ts(now, "4h")
        if bar > self.last_4h:
            if not data_ok:
                log.warning("Clôture 4 h reportée : bougies non récupérées (collecte en échec).")
                return False
            self.last_4h = bar
            return self.status is Status.RUNNING
        return False

    async def _hourly(self, now: int) -> None:
        sims = list(self.sims.values())
        for runner in (self.shadow, self.live):
            if runner is not None and runner.sim is not None:
                sims.append(runner.sim)
        await self._hourly_sims(now, sims)

    async def _hourly_sims(self, now: int, sims: list[Simulation]) -> None:
        assert self.ctx is not None
        self.ctx.set_now(now)
        highs = {}
        for market, data in self.ctx.data.markets.items():
            if len(data.h1):
                highs[market] = Decimal(repr(float(data.h1["high"].iloc[-1])))
        for sim in sims:
            if sim.config.exits.recalc is StopRecalc.HOURLY and sim.positions:
                await sim.on_1h_close(now, self.ctx, highs)

    async def _four_hourly(self, now: int) -> None:
        assert self.ctx is not None
        ctx = self.ctx
        started = self.clock()
        ctx.set_now(now)
        configs = [s.config for s in self.sims.values()]
        for runner in (self.shadow, self.live):
            if runner is not None and runner.sim is not None:
                configs.append(runner.sim.config)
        await asyncio.to_thread(ctx.warm, configs)
        for sim_id in sorted(self.sims):
            # Une simulation à la fois sous le verrou ; entre deux, la boucle traite les
            # bougies 1 min (stops), les commandes et les pings du WebSocket.
            async with self.lock:
                if self.status is not Status.RUNNING or self.ctx is not ctx:
                    log.info("Clôture 4 h interrompue (état %s).", self.status.value)
                    return
                ctx.set_now(now)
                await self.sims[sim_id].on_4h_close(now, ctx)
            await asyncio.sleep(0)
        async with self.lock:
            self.sim_states.save(list(self.sims.values()), now)
            shadow = self.shadow
            if self.status is Status.RUNNING and shadow is not None and shadow.sim is not None:
                ctx.set_now(now)
                await shadow.sim.on_4h_close(now, ctx)
                shadow.save(now)
            live = self.live
            if self.status is Status.RUNNING and live is not None and live.sim is not None:
                ctx.set_now(now)
                try:
                    await live.on_4h_close(ctx, now)
                except Exception:
                    log.exception("Réel : échec de la clôture 4 h.")
        log.info("Clôture 4 h traitée en %.1f s.", (self.clock() - started) / 1000)

    def update_metrics(self, now: int) -> None:
        """Métriques horaires de chaque simulation (SPEC §6)."""
        if self.broker is None or self.ctx is None:
            return
        markets = list(self.ctx.data.markets)
        prices = {m: p for m in markets if (p := self.ctx.last_price(m)) is not None}
        bh_value = buy_and_hold_value(load_buy_and_hold(self.conn), prices, self.fees.taker)
        bh_pnl = bh_value - INITIAL_CAPITAL if bh_value is not None else None
        for sim in self.sims.values():
            self.metrics[sim.sim_id] = compute_metrics(
                sim.sim_id,
                sim.portfolio_value(self.ctx),
                sim.trade_pnls,
                self.broker.account(sim.sim_id).fees_paid,
                len(sim.positions),
                bh_pnl,
                self.metrics.get(sim.sim_id),
            )
        write_metrics(self.conn, self.metrics.values(), now)

    def write_portfolio_values(self, ts: int) -> None:
        """Valeur de chaque portefeuille (positions nettes des frais de vente estimés)."""
        if self.broker is None or self.ctx is None:
            return
        rows = [
            (sim.sim_id, ts, sim.portfolio_value(self.ctx), self.broker.total_cash(sim.sim_id))
            for sim in self.sims.values()
        ]
        with transaction(self.conn):
            self.conn.executemany(
                """INSERT INTO portfolio_values (sim_id, ts, total_value, cash) VALUES (?, ?, ?, ?)
                   ON CONFLICT (sim_id, ts) DO UPDATE SET total_value = excluded.total_value,
                       cash = excluded.cash""",
                rows,
            )


async def run_engine() -> None:
    settings = load_settings()
    configure_logging(settings.log_dir, "engine", settings.secrets)
    conn = connect(settings.db_path, settings.journal_mode)
    fees = FeeSchedule(settings.fee_maker, settings.fee_taker)
    async with BitvavoRest() as rest:
        engine: Engine | None = None

        async def on_candle(candle: Candle) -> None:
            if engine is not None:
                await engine.on_minute_candle(candle)

        collector = LiveCollector(conn, rest, settings.basket, on_candle=on_candle)
        trading = None
        if (
            settings.production_live
            and settings.production_max_capital is not None
            and settings.api_key
            and settings.api_secret
        ):
            trading = BitvavoTrading(settings.api_key, settings.api_secret, settings.operator_id)
            log.warning(
                "Mode réel déverrouillé (plafond %s €) : ordres réels possibles sur demande du "
                "dashboard.",
                settings.production_max_capital,
            )
        engine = Engine(
            conn,
            fees,
            CachedBookProvider(rest, ttl_s=BOOK_TTL_S),
            tickers=collector.ws.tickers,
            optimizer_workers=settings.optimizer_workers,
            trading=trading,
            max_capital=settings.production_max_capital,
        )
        engine.startup()
        log.info("Moteur démarré (état %s).", engine.status.value)

        async def heartbeats() -> None:
            # Indépendant de la collecte : un long rattrapage ne fait pas croire à un arrêt.
            while True:
                heartbeat(conn, now_ms())
                await asyncio.sleep(HEARTBEAT_S)

        async def commands() -> None:
            # Traitées toutes les 2 s, même pendant un rattrapage de bougies.
            while True:
                try:
                    await engine.process_commands()
                except Exception:
                    log.exception("Échec du traitement d'une commande.")
                await asyncio.sleep(COMMAND_POLL_S)

        tasks = [
            asyncio.create_task(collector.ws.run(), name="bitvavo-ws"),
            asyncio.create_task(heartbeats(), name="heartbeat"),
            asyncio.create_task(commands(), name="commands"),
        ]
        if settings.api_key and settings.api_secret:
            account = BitvavoAccount(settings.api_key, settings.api_secret)
            tasks.append(asyncio.create_task(read_balances(conn, account), name="balance"))
        try:
            while True:
                now = now_ms()
                data_ok = True
                try:
                    await collector.tick(now)
                except (httpx.TransportError, BitvavoError) as exc:
                    # Coupure réseau ou DNS : une ligne, sans trace complète à chaque minute.
                    data_ok = False
                    log.warning(
                        "Collecte impossible (%s : %s), nouvel essai à la minute suivante.",
                        type(exc).__name__,
                        exc,
                    )
                except Exception:
                    data_ok = False
                    log.exception("Échec de la collecte, nouvel essai à la minute suivante.")
                try:
                    await engine.on_tick(now, data_ok)
                except Exception:
                    log.exception("Échec du cycle, nouvel essai à la minute suivante.")
                next_tick = floor_ts(now_ms(), "1m") + MINUTE_MS + TICK_DELAY_MS
                await asyncio.sleep(max(next_tick - now_ms(), 0) / 1000)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if trading is not None:
                await trading.aclose()
            conn.close()


async def read_balances(conn: sqlite3.Connection, account: BitvavoAccount) -> None:
    """Solde réel (lecture seule) toutes les 5 minutes, 30 s après un échec."""
    try:
        while True:
            ok = await poll_balance(conn, account, now_ms())
            await asyncio.sleep(BALANCE_POLL_S if ok else BALANCE_RETRY_S)
    finally:
        await account.aclose()


async def poll_balance(conn: sqlite3.Connection, account: BitvavoAccount, now: int) -> bool:
    """Lit et enregistre le solde ; False si la lecture a échoué (dernier solde conservé)."""
    try:
        balances = await account.balances()
    except (httpx.HTTPError, BitvavoError, ValueError) as exc:
        log.warning("Lecture du solde impossible (%s : %s).", type(exc).__name__, exc)
        production.record_balances(conn, None, f"{type(exc).__name__} : {exc}", now)
        return False
    production.record_balances(
        conn, {b.symbol: (b.available, b.in_order) for b in balances}, None, now
    )
    return True


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_engine())


if __name__ == "__main__":
    main()
