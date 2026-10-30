"""Mode production, palier 3 : ordres réels, contre un faux Bitvavo (aucun appel réel).

Le faux échange (`FakeBitvavo`, `httpx.MockTransport`) vérifie la signature de chaque requête,
exécute les ordres au marché à un prix fixe, garde les ordres limite ouverts jusqu'à ce que le
test les remplisse, et tient les soldes du compte.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from decimal import ROUND_DOWN
from decimal import Decimal as D
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from mcs import production
from mcs.bitvavo.account import sign
from mcs.bitvavo.rest import BASE_URL
from mcs.bitvavo.trading import BitvavoTrading
from mcs.broker.base import Order, OrderStatus, Reason, Rejection, RejectReason, Side
from mcs.broker.fees import FeeSchedule
from mcs.broker.live import BitvavoBroker
from mcs.broker.rules import MarketRules
from mcs.engine import Engine
from mcs.live.real import paris_day
from mcs.live.state import Command
from mcs.production import LiveStatus
from mcs.storage.orders import load_orders
from tests.test_engine import FEES, MARKETS, Books, command, data, run_bars, setup  # noqa: F401
from tests.test_production import add_sim

KEY, SECRET = "test-key", "test-secret"
SIM = production.PRODUCTION_SIM_ID
RULES = {
    "AAA-EUR": MarketRules("AAA-EUR", "trading", D("0.01"), 6, D("0.0001"), D(5)),
}


class FakeBitvavo:
    def __init__(self, price_of: Callable[[str], D], euros: D = D(1000)) -> None:
        self.price_of = price_of
        self.balances: dict[str, D] = {"EUR": euros}
        self.orders: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.lose_next_response = False
        self.fail_post: dict[str, Any] | None = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handle))

    def trading(self) -> BitvavoTrading:
        return BitvavoTrading(KEY, SECRET, 7, client=self.client(), clock=lambda: 1_000)

    @property
    def posts(self) -> list[dict[str, Any]]:
        return [body for method, _, body in self.requests if method == "POST"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path.removeprefix("/v2")
        target = f"{path}?{url.query}" if url.query else path
        body = request.content.decode()
        expected = sign(SECRET, 1_000, request.method, target, body)
        assert request.headers["bitvavo-access-key"] == KEY
        assert request.headers["bitvavo-access-signature"] == expected
        params = dict(parse_qsl(url.query))
        payload = json.loads(body) if body else {}
        self.requests.append((request.method, path, payload or params))
        if path == "/balance":
            return httpx.Response(200, json=[
                {"symbol": s, "available": format(a, "f"), "inOrder": "0"}
                for s, a in self.balances.items()
            ])  # fmt: skip
        assert path == "/order"
        if request.method == "POST":
            if self.fail_post is not None:
                return httpx.Response(400, json=self.fail_post)
            order = self._create(payload)
            if self.lose_next_response:
                self.lose_next_response = False
                raise httpx.ReadTimeout("réponse perdue", request=request)
            return httpx.Response(200, json=order)
        order = self.orders.get(params["clientOrderId"])
        if order is None or (request.method == "DELETE" and order["status"] != "new"):
            return httpx.Response(404, json={"errorCode": 240, "error": "No order found."})
        if request.method == "DELETE":
            assert params["operatorId"] == "7"
            order["status"] = "canceled"
            return httpx.Response(200, json={"orderId": order["orderId"]})
        return httpx.Response(200, json=order)

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        assert body["operatorId"] == 7
        market, side = body["market"], body["side"]
        price = self.price_of(market)
        order: dict[str, Any] = {
            "orderId": f"ex-{len(self.orders) + 1}", "clientOrderId": body["clientOrderId"],
            "market": market, "side": side, "orderType": body["orderType"], "status": "new",
            "filledAmount": "0", "filledAmountQuote": "0", "feePaid": "0", "feeCurrency": "EUR",
        }  # fmt: skip
        self.orders[body["clientOrderId"]] = order
        if body["orderType"] == "market":
            if "amountQuote" in body:
                amount = (D(body["amountQuote"]) / price).quantize(D("0.000001"), ROUND_DOWN)
            else:
                amount = D(body["amount"])
            self._fill(order, amount, price, D("0.0025"))
        else:
            limit = D(body["price"])
            order["amount"], order["price"] = body["amount"], body["price"]
            crosses = limit >= price if side == "buy" else limit <= price
            if body.get("postOnly") and crosses:
                order["status"] = "canceled"
        return dict(order)

    def fill(self, client_id: str, fraction: D = D(1), done: bool = True) -> None:
        order = self.orders[client_id]
        amount = (D(order["amount"]) * fraction).quantize(D("0.000001"), ROUND_DOWN)
        self._fill(order, amount, D(order["price"]), D("0.0015"), done)

    def _fill(self, order: dict[str, Any], amount: D, price: D, rate: D, done: bool = True) -> None:
        quote = amount * price
        fee = (quote * rate).quantize(D("0.00000001"))
        base = order["market"].split("-")[0]
        sign_ = 1 if order["side"] == "buy" else -1
        self.balances["EUR"] -= sign_ * quote + fee
        self.balances[base] = self.balances.get(base, D(0)) + sign_ * amount
        order.update(
            status="filled" if done else "partiallyFilled",
            filledAmount=format(amount, "f"),
            filledAmountQuote=format(quote, "f"),
            feePaid=format(fee, "f"),
        )


def make_broker(
    conn: sqlite3.Connection,
    exchange: FakeBitvavo,
    guard: Callable[[int, D], str | None] | None = None,
) -> BitvavoBroker:
    async def no_sleep(_: float) -> None:
        return None

    ids = iter(f"cid-{n}" for n in range(1, 1000))
    broker = BitvavoBroker(
        conn, FeeSchedule(), exchange.trading(), RULES, guard=guard, sleep=no_sleep,
        new_client_id=lambda: next(ids),
    )  # fmt: skip
    broker.open_account(SIM, D(300))
    return broker


@pytest.fixture
def exchange() -> FakeBitvavo:
    return FakeBitvavo(lambda market: D(100))


# --- Client et broker -------------------------------------------------------------------------


async def test_market_buy_records_real_fill(
    conn: sqlite3.Connection, exchange: FakeBitvavo
) -> None:
    broker = make_broker(conn, exchange)
    order = await broker.buy_market(SIM, "AAA-EUR", D(50), Reason.SIGNAL, 10)
    assert isinstance(order, Order) and order.status is OrderStatus.FILLED
    # Budget envoyé hors frais : 50 / 1,0025 arrondi au centime inférieur.
    assert exchange.posts[0]["amountQuote"] == "49.87"
    assert order.quantity == D("0.4987") and order.notional == D("49.87")
    assert order.fee == D("0.12467500")
    assert broker.total_cash(SIM) == D(300) - D("49.87") - D("0.124675")
    assert broker.holdings(SIM) == {"AAA-EUR": D("0.4987")}
    row = conn.execute("SELECT client_order_id, exchange_order_id FROM live_orders").fetchone()
    assert tuple(row) == ("cid-1", "ex-1")
    assert [m for m, _, _ in exchange.requests] == ["GET", "POST"]  # solde, puis ordre


async def test_lost_response_is_recovered_without_resending(
    conn: sqlite3.Connection, exchange: FakeBitvavo
) -> None:
    broker = make_broker(conn, exchange)
    exchange.lose_next_response = True
    order = await broker.buy_market(SIM, "AAA-EUR", D(50), Reason.SIGNAL, 10)
    assert isinstance(order, Order) and order.status is OrderStatus.FILLED
    assert len(exchange.posts) == 1  # retrouvé par clientOrderId, jamais renvoyé


async def test_buys_refused_by_guard_real_balance_or_exchange(
    conn: sqlite3.Connection, exchange: FakeBitvavo
) -> None:
    broker = make_broker(conn, exchange, guard=lambda sim_id, cost: "plafond")
    result = await broker.buy_market(SIM, "AAA-EUR", D(50), Reason.SIGNAL, 10)
    assert isinstance(result, Rejection) and result.reason is RejectReason.RISK_LIMIT
    broker.guard = None
    exchange.balances["EUR"] = D(20)
    result = await broker.buy_market(SIM, "AAA-EUR", D(50), Reason.SIGNAL, 10)
    assert isinstance(result, Rejection) and result.reason is RejectReason.INSUFFICIENT_FUNDS
    assert exchange.posts == []
    exchange.balances["EUR"] = D(1000)
    exchange.fail_post = {"errorCode": 216, "error": "Insufficient balance."}
    result = await broker.buy_market(SIM, "AAA-EUR", D(50), Reason.SIGNAL, 10)
    assert isinstance(result, Rejection) and result.reason is RejectReason.EXCHANGE_ERROR
    assert broker.total_cash(SIM) == D(300) and not broker.open_orders(SIM)
    statuses = [o.status for o in load_orders(conn, table="live_orders")]
    assert statuses == [OrderStatus.CANCELLED]


async def test_limit_orders_post_only_poll_and_partial_cancel(
    conn: sqlite3.Connection, exchange: FakeBitvavo
) -> None:
    broker = make_broker(conn, exchange)
    crossing = await broker.place_limit_buy(
        SIM, "AAA-EUR", D(101), D(50), 99_999, Reason.SIGNAL, 10
    )
    assert isinstance(crossing, Rejection) and "postOnly" in crossing.detail
    buy = await broker.place_limit_buy(SIM, "AAA-EUR", D(99), D(50), 99_999, Reason.SIGNAL, 10)
    assert isinstance(buy, Order) and buy.status is OrderStatus.OPEN
    assert exchange.posts[-1]["postOnly"] is True and exchange.posts[-1]["price"] == "99"
    assert broker.cash(SIM) < D(300 - 49)  # liquidités réservées
    assert await broker.poll(20) == []
    exchange.fill(broker.client_ids[buy.id])
    (filled,) = await broker.poll(30)
    assert filled.status is OrderStatus.FILLED and filled.quantity == buy.quantity
    held = broker.holdings(SIM)["AAA-EUR"]

    tp = await broker.place_limit_sell(SIM, "AAA-EUR", D(120), held, Reason.TAKE_PROFIT, 40)
    assert isinstance(tp, Order) and tp.status is OrderStatus.OPEN
    exchange.fill(broker.client_ids[tp.id], D("0.5"), done=False)
    cancelled = await broker.cancel_order(tp.id, 50)
    # Moitié exécutée avant l'annulation : enregistrée comme un ordre rempli réduit.
    assert cancelled.status is OrderStatus.FILLED and cancelled.quantity < held
    rest = broker.holdings(SIM)["AAA-EUR"]
    sell = await broker.sell_market(SIM, "AAA-EUR", held, Reason.STOP, 60)  # ramenée au détenu
    assert isinstance(sell, Order) and sell.quantity == rest
    assert broker.holdings(SIM) == {}


async def test_expired_limit_buy_is_cancelled_on_exchange(
    conn: sqlite3.Connection, exchange: FakeBitvavo
) -> None:
    broker = make_broker(conn, exchange)
    buy = await broker.place_limit_buy(SIM, "AAA-EUR", D(99), D(50), 100, Reason.SIGNAL, 10)
    assert isinstance(buy, Order)
    (expired,) = await broker.expire_orders(100)
    assert expired.status is OrderStatus.EXPIRED
    assert exchange.orders["cid-1"]["status"] == "canceled"
    assert broker.cash(SIM) == D(300)
    # Reprise : les ordres ouverts sont rechargés avec leur clientOrderId.
    other = await broker.place_limit_buy(SIM, "AAA-EUR", D(98), D(50), 999, Reason.SIGNAL, 10)
    assert isinstance(other, Order)
    reloaded = make_broker(conn, exchange)
    assert reloaded.client_ids == {other.id: "cid-2"}


# --- État du mode réel ------------------------------------------------------------------------


def test_start_live_requires_lock_confirmation_and_cap(conn: sqlite3.Connection) -> None:
    add_sim(conn, 1)
    production.choose(conn, 1, D(300), "pnl", 1)
    conn.execute("UPDATE engine_state SET status = 'RUNNING'")
    with pytest.raises(ValueError, match="verrouillé"):
        production.start_live(conn, production.LIVE_CONFIRMATION, 2)
    production.set_live_armed(conn, True, D(200), 2)
    with pytest.raises(ValueError, match="Saisissez"):
        production.start_live(conn, "oui", 2)
    with pytest.raises(ValueError, match="plafond"):
        production.start_live(conn, production.LIVE_CONFIRMATION, 2)
    production.set_live_armed(conn, True, D(300), 2)
    production.start_live(conn, production.LIVE_CONFIRMATION, 3)
    live = production.read_live(conn)
    assert live.status is LiveStatus.ON and live.started_at == 3
    assert production.read_state(conn).mode is production.Mode.SHADOW  # ombre en parallèle


def test_daily_loss_blocks_buys_until_next_day(conn: sqlite3.Connection) -> None:
    live = production.update_live_day(conn, "2026-10-30", D(300), 1)
    assert live.day_start_value == D(300) and live.buys_blocked_day is None
    assert production.update_live_day(conn, "2026-10-30", D(271), 2).buys_blocked_day is None
    assert (
        production.update_live_day(conn, "2026-10-30", D(270), 3).buys_blocked_day == "2026-10-30"
    )
    live = production.update_live_day(conn, "2026-10-31", D(270), 4)
    assert live.day_start_value == D(270) and live.buys_blocked_day != live.day


def test_paris_day_uses_local_midnight() -> None:
    assert paris_day(1_761_778_800_000) == "2025-10-30"  # 30/10/2025 00:00 à Paris (23:00 UTC)
    assert paris_day(1_761_778_799_999) == "2025-10-29"


# --- Moteur -----------------------------------------------------------------------------------


async def test_engine_live_mode_end_to_end(setup, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, _, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    exchange = FakeBitvavo(lambda m: D(repr(float(data.markets[m].h1["close"].iloc[-1]))))

    def make_engine(armed: bool = True) -> Engine:
        async def no_sleep(_: float) -> None:
            return None

        engine = Engine(conn, FEES, Books(data), clock=clock, data_loader=lambda c, b: data,
                        trading=exchange.trading() if armed else None,
                        max_capital=D(300) if armed else None)  # fmt: skip
        engine.startup()
        return engine

    engine = make_engine()
    assert production.read_live(conn).armed
    await command(engine, Command.START)
    await run_bars(engine, clock, 6)
    busiest = conn.execute(
        "SELECT sim_id FROM orders GROUP BY sim_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    production.choose(conn, busiest, D(300), "pnl", clock())
    production.start_live(conn, production.LIVE_CONFIRMATION, clock())
    await engine.process_commands()
    assert engine.live is not None and engine.live.sim is not None
    assert engine.shadow is not None and engine.shadow.sim is not None  # en parallèle

    await run_bars(engine, clock, 8)
    live_orders = load_orders(conn, table="live_orders")
    assert exchange.posts, "aucun ordre réel en 8 clôtures 4 h"
    assert all(o.sim_id == SIM for o in live_orders)
    buys = [o for o in live_orders if o.side is Side.BUY and o.status is OrderStatus.FILLED]
    assert all(o.notional is not None and o.notional >= D(5) for o in buys)
    assert conn.execute("SELECT COUNT(*) FROM live_values").fetchone()[0] == 8 * 4
    engaged = sum((p.invested_cost for p in engine.live.sim.positions.values()), D(0))
    assert engaged <= D(300)

    # Remise à zéro impossible tant que le mode réel tourne.
    await command(engine, Command.STOP)
    assert "mode réel" in await command(engine, Command.RESET)
    # Hold : achats en attente annulés, stops toujours gérés.
    assert engine.live.sim is not None
    assert not [o for o in engine.live.broker.open_orders(SIM) if o.side is Side.BUY]
    await command(engine, Command.RESUME)

    # Redémarrage : portefeuille réel reconstruit à l'identique.
    snapshot = (engine.live.broker.total_cash(SIM), engine.live.broker.holdings(SIM))
    restarted = make_engine()
    await restarted.process_commands()
    assert restarted.live is not None and restarted.live.sim is not None
    assert (restarted.live.broker.total_cash(SIM), restarted.live.broker.holdings(SIM)) == snapshot

    # Perte du jour : achats refusés par le garde-fou.
    day = paris_day(clock())
    conn.execute("UPDATE live_state SET buys_blocked_day = ?", (day,))
    assert restarted.live.guard(SIM, D(10)) is not None
    conn.execute("UPDATE live_state SET buys_blocked_day = NULL")
    assert restarted.live.guard(SIM, D(10)) is None
    assert restarted.live.guard(SIM, D(301)) is not None  # plafond

    # Arrêt d'urgence : tout ce que le moteur détient est vendu, le mode est coupé.
    exchange.balances["XYZ"] = D(3)  # crypto détenue hors du moteur : jamais touchée
    production.request_emergency(conn, clock())
    await restarted.process_commands()
    live = production.read_live(conn)
    assert live.status is LiveStatus.OFF and live.emergency_requested_at is None
    assert restarted.live.sim is None
    assert conn.execute("SELECT COUNT(*) FROM live_positions").fetchone()[0] == 0
    assert all(b >= 0 for b in exchange.balances.values())
    assert exchange.balances["XYZ"] == D(3)
    assert not [o for o in load_orders(conn, table="live_orders") if o.status is OrderStatus.OPEN]

    # Moteur sans le double verrou : pas de runner, état signalé.
    locked = make_engine(armed=False)
    assert locked.live is None or locked.live.sim is None
    assert not production.read_live(conn).armed


async def test_wind_down_stops_buying_and_finishes(setup, data) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    conn, clock, _, _ = setup
    conn.execute("UPDATE engine_state SET optimization_status = 'DONE'")
    exchange = FakeBitvavo(lambda m: D(repr(float(data.markets[m].h1["close"].iloc[-1]))))
    engine = Engine(conn, FEES, Books(data), clock=clock, data_loader=lambda c, b: data,
                    trading=exchange.trading(), max_capital=D(300))  # fmt: skip
    engine.startup()
    await command(engine, Command.START)
    production.choose(conn, 1, D(300), "pnl", clock())
    production.start_live(conn, production.LIVE_CONFIRMATION, clock())
    await engine.process_commands()
    assert engine.live is not None and engine.live.sim is not None
    production.wind_down_live(conn, clock())
    assert engine.live.guard(SIM, D(10)) is not None  # plus d'achat
    await engine.process_commands()  # aucune position : arrêt immédiat
    assert production.read_live(conn).status is LiveStatus.OFF
    assert engine.live.sim is None
