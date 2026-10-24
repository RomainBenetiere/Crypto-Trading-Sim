"""PaperBroker : arrondis, minimums, frais, carnet, ordres limite, stops, persistance."""

import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from mcs.bitvavo.rest import OrderBook
from mcs.broker.base import Liquidity, Order, OrderStatus, Reason, Rejection, RejectReason, Side
from mcs.broker.book import max_quantity_for_budget, walk_quantity
from mcs.broker.fees import FeeSchedule
from mcs.broker.paper import PaperBroker
from mcs.broker.rules import MarketRules
from mcs.market_data import Candle
from mcs.spread import SpreadTracker
from mcs.storage.orders import load_orders

D = Decimal
M = 60_000
H = 60 * M

# Règles réelles relevées sur Bitvavo (octobre 2026).
BTC = MarketRules("BTC-EUR", "trading", D("1.00"), 8, D("0.00006942"), D("5.00"))
PEPE = MarketRules("PEPE-EUR", "trading", D("0.000000000100"), 0, D("1297431"), D("5.00"))
FEES = FeeSchedule(maker=D("0.0015"), taker=D("0.0025"))


class StaticBooks:
    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self.calls = 0

    def set(self, market: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> None:
        self.books[market] = OrderBook(
            market, [(D(p), D(q)) for p, q in bids], [(D(p), D(q)) for p, q in asks], 0
        )

    async def get_book(self, market: str) -> OrderBook:
        self.calls += 1
        return self.books[market]


@pytest.fixture
def books() -> StaticBooks:
    b = StaticBooks()
    b.set(
        "BTC-EUR",
        bids=[("71616", "0.07"), ("71615", "1")],
        asks=[("71617", "0.01"), ("71618", "1")],
    )
    b.set("PEPE-EUR", bids=[("0.0000038571", "50000000")], asks=[("0.0000038592", "90000000")])
    return b


@pytest.fixture
def broker(conn: sqlite3.Connection, books: StaticBooks) -> PaperBroker:
    for sim_id in (1, 2):
        conn.execute(
            "INSERT INTO simulations (id, pair_id, mode, family, config_json, seed, created_at)"
            " VALUES (?, ?, 'typical', 'ema', '{}', 1, 0)",
            (sim_id, sim_id),
        )
    paper = PaperBroker(
        conn,
        FEES,
        books,
        rules={"BTC-EUR": BTC, "PEPE-EUR": PEPE},
        spread=lambda m: D("0.0005") if m == "BTC-EUR" else None,
    )
    paper.open_account(1, D("1000"))
    paper.open_account(2, D("1000"))
    return paper


def minute(market: str, ts: int, low: str) -> Candle:
    p = D(low)
    return Candle(market, "1m", ts, p, p, p, p, D(1))


# --- Règles de marché et frais ------------------------------------------------------------


def test_price_rounding_never_worse_than_requested() -> None:
    assert BTC.round_price(D("71616.7"), Side.BUY) == D("71616")
    assert BTC.round_price(D("71616.2"), Side.SELL) == D("71617")
    assert PEPE.round_price(D("0.00000385927"), Side.BUY) == D("0.0000038592")
    assert PEPE.round_price(D("0.00000385921"), Side.SELL) == D("0.0000038593")


def test_quantity_rounding_down() -> None:
    assert BTC.round_quantity(D("0.123456789")) == D("0.12345678")
    assert PEPE.round_quantity(D("2591210.99")) == D("2591210")


def test_minimums() -> None:
    assert BTC.check_minimum(D("0.0001"), D("71617")) is None
    assert BTC.check_minimum(D("0.00006"), D("71617"))[0] is RejectReason.BELOW_MINIMUM
    # 1 300 000 PEPE ≈ 5,02 € : au-dessus des deux minimums ; 1 200 000 sous le minimum base.
    assert PEPE.check_minimum(D("1300000"), D("0.00000386")) is None
    assert PEPE.check_minimum(D("1200000"), D("0.000005")) is not None


def test_fee_schedule() -> None:
    assert FEES.fee(D("100"), Liquidity.TAKER) == D("0.25")
    assert FEES.fee(D("100"), Liquidity.MAKER) == D("0.15")
    # 0,000002 × 0,25 % = 0,000000005 : arrondi au plus proche, moitié vers le haut.
    assert FEES.fee(D("0.000002"), Liquidity.TAKER) == D("0.00000001")


# --- Carnet -------------------------------------------------------------------------------


def test_walk_across_levels_and_short_book() -> None:
    asks = [(D("10"), D("1")), (D("11"), D("2"))]
    execution = walk_quantity(asks, D("2"))
    assert execution.notional == D("21") and execution.levels_used == 2
    assert not execution.short
    short = walk_quantity(asks, D("5"))
    assert short.short and short.notional == D("10") + D("22") + D("22")


def test_max_quantity_for_budget() -> None:
    asks = [(D("10"), D("1")), (D("20"), D("10"))]
    # 30,075 € frais 0,25 % compris → 30 € hors frais : 1 à 10 € puis 1 à 20 €.
    assert max_quantity_for_budget(asks, D("30.075"), D("0.0025")) == D("2")


# --- Ordres au marché ---------------------------------------------------------------------


async def test_market_buy_walks_book_and_respects_budget(broker: PaperBroker) -> None:
    order = await broker.buy_market(1, "BTC-EUR", D("900"), Reason.SIGNAL, now=1)
    assert isinstance(order, Order)
    # 0,01 BTC (716,17 €) au premier niveau, le reste au second : prix moyen entre les deux.
    assert order.quantity == BTC.round_quantity(order.quantity) > D("0.01")
    assert D("71617") < order.price < D("71618")
    assert order.notional == D("716.17") + (order.quantity - D("0.01")) * D("71618")
    assert order.notional + order.fee <= D("900")
    # Un pas de quantité de plus dépasserait le budget.
    more = order.quantity + BTC.quantity_step
    more_notional = D("716.17") + (more - D("0.01")) * D("71618")
    assert more_notional + FEES.fee(more_notional, Liquidity.TAKER) > D("900")
    assert order.fee == FEES.fee(order.notional, Liquidity.TAKER)
    assert (order.book_bid, order.book_ask) == (D("71616"), D("71617"))
    assert broker.cash(1) == D("1000") - order.notional - order.fee
    assert broker.holdings(1) == {"BTC-EUR": order.quantity}
    assert broker.cash(2) == D("1000")  # comptes indépendants


async def test_market_buy_pepe_whole_units(broker: PaperBroker) -> None:
    order = await broker.buy_market(1, "PEPE-EUR", D("10"), Reason.SIGNAL, now=1)
    assert isinstance(order, Order)
    assert order.quantity == order.quantity.to_integral_value()
    assert order.notional + order.fee <= D("10")


async def test_market_buy_rejections(broker: PaperBroker) -> None:
    too_small = await broker.buy_market(1, "BTC-EUR", D("4"), Reason.SIGNAL, now=1)
    assert isinstance(too_small, Rejection) and too_small.reason is RejectReason.BELOW_MINIMUM
    too_big = await broker.buy_market(1, "BTC-EUR", D("1000.01"), Reason.SIGNAL, now=1)
    assert isinstance(too_big, Rejection)
    assert too_big.reason is RejectReason.INSUFFICIENT_FUNDS
    unknown = await broker.buy_market(1, "XXX-EUR", D("10"), Reason.SIGNAL, now=1)
    assert isinstance(unknown, Rejection) and unknown.reason is RejectReason.UNKNOWN_MARKET
    assert broker.cash(1) == D("1000")


async def test_full_position_sold_without_dust(broker: PaperBroker) -> None:
    buy = await broker.buy_market(1, "PEPE-EUR", D("20"), Reason.SIGNAL, now=1)
    assert isinstance(buy, Order)
    sell = await broker.sell_market(1, "PEPE-EUR", buy.quantity, Reason.TAKE_PROFIT, now=2)
    assert isinstance(sell, Order)
    assert broker.holdings(1) == {}
    expected = D("1000") - buy.notional - buy.fee + sell.notional - sell.fee
    assert broker.cash(1) == expected
    assert broker.account(1).fees_paid == buy.fee + sell.fee


async def test_cannot_sell_more_than_held(broker: PaperBroker) -> None:
    result = await broker.sell_market(1, "BTC-EUR", D("0.001"), Reason.SIGNAL, now=1)
    assert isinstance(result, Rejection)
    assert result.reason is RejectReason.INSUFFICIENT_QUANTITY


async def test_stop_penalty_is_one_estimated_spread(broker: PaperBroker) -> None:
    buy = await broker.buy_market(1, "BTC-EUR", D("100"), Reason.SIGNAL, now=1)
    assert isinstance(buy, Order)
    half = BTC.round_quantity(buy.quantity / 2)
    normal = await broker.sell_market(1, "BTC-EUR", half, Reason.REVERSE_SIGNAL, now=2)
    stopped = await broker.sell_market(1, "BTC-EUR", half, Reason.STOP, now=3)
    assert isinstance(normal, Order) and isinstance(stopped, Order)
    assert normal.price == D("71616")
    assert stopped.price == D("71616") * (1 - D("0.0005"))


async def test_stop_penalty_falls_back_to_book_spread(broker: PaperBroker) -> None:
    book = await broker.books.get_book("PEPE-EUR")
    penalty = broker.stop_penalty("PEPE-EUR", book)
    bid, ask = D("0.0000038571"), D("0.0000038592")
    assert penalty == (ask - bid) / ((ask + bid) / 2)


# --- Ordres limite ------------------------------------------------------------------------


async def place(broker: PaperBroker, price: str = "71000", amount: str = "300") -> Order:
    order = await broker.place_limit_buy(
        1, "BTC-EUR", D(price), D(amount), expires_at=10 * H, reason=Reason.SIGNAL, now=30_000
    )
    assert isinstance(order, Order)
    return order


async def test_limit_order_reserves_cash(broker: PaperBroker) -> None:
    order = await place(broker)
    assert order.status is OrderStatus.OPEN
    assert order.reserved == order.quantity * D("71000") + FEES.fee(
        order.quantity * D("71000"), Liquidity.MAKER
    )
    assert order.reserved <= D("300")
    assert broker.cash(1) == D("1000") - order.reserved
    rejected = await broker.buy_market(1, "BTC-EUR", D("800"), Reason.SIGNAL, now=40_000)
    assert isinstance(rejected, Rejection)


async def test_limit_touched_is_not_filled_crossed_is(broker: PaperBroker) -> None:
    order = await place(broker)
    assert broker.on_candle(minute("BTC-EUR", 2 * M, "71000")) == []  # touché seulement
    assert broker.on_candle(minute("ETH-EUR", 3 * M, "1")) == []  # autre marché
    (filled,) = broker.on_candle(minute("BTC-EUR", 3 * M, "70999"))
    assert filled.status is OrderStatus.FILLED and filled.liquidity is Liquidity.MAKER
    assert filled.price == D("71000") and filled.notional == order.quantity * D("71000")
    assert filled.fee == FEES.fee(filled.notional, Liquidity.MAKER)
    assert broker.holdings(1) == {"BTC-EUR": order.quantity}
    assert broker.cash(1) == D("1000") - filled.notional - filled.fee
    assert broker.open_orders(1) == []


async def test_limit_ignores_candle_containing_placement(broker: PaperBroker) -> None:
    await place(broker)  # placé à 30 s : la bougie de 0 à 60 s contient des échanges antérieurs
    assert broker.on_candle(minute("BTC-EUR", 0, "60000")) == []
    assert len(broker.on_candle(minute("BTC-EUR", M, "60000"))) == 1


async def test_limit_expiry_releases_reservation(broker: PaperBroker) -> None:
    await place(broker)
    assert broker.expire_orders(10 * H - 1) == []
    (expired,) = broker.expire_orders(10 * H)
    assert expired.status is OrderStatus.EXPIRED
    assert broker.cash(1) == D("1000")
    assert broker.on_candle(minute("BTC-EUR", 10 * H, "1")) == []


async def test_limit_cancel(broker: PaperBroker) -> None:
    order = await place(broker)
    cancelled = await broker.cancel_order(order.id, now=60_000)
    assert cancelled.status is OrderStatus.CANCELLED and broker.cash(1) == D("1000")


async def test_limit_price_rounded_and_minimum(broker: PaperBroker) -> None:
    order = await place(broker, price="71000.9")
    assert order.price == D("71000")
    too_small = await broker.place_limit_buy(
        1, "BTC-EUR", D("71000"), D("4"), expires_at=H, reason=Reason.SIGNAL, now=0
    )
    assert isinstance(too_small, Rejection)


# --- Persistance et reprise ---------------------------------------------------------------


async def test_orders_persisted_and_account_restored(
    broker: PaperBroker, conn: sqlite3.Connection, books: StaticBooks
) -> None:
    buy = await broker.buy_market(1, "BTC-EUR", D("200"), Reason.SIGNAL, now=1)
    limit = await place(broker, amount="100")
    assert isinstance(buy, Order)
    stored = load_orders(conn, 1)
    assert [o.id for o in stored] == [buy.id, limit.id]
    assert stored[0] == buy and stored[1] == limit

    restarted = PaperBroker(conn, FEES, books, rules={"BTC-EUR": BTC})
    account = restarted.open_account(1, D("1000"))
    assert account.cash_total == broker.account(1).cash_total
    assert account.holdings == broker.account(1).holdings
    assert restarted.cash(1) == broker.cash(1)
    assert list(account.open_orders) == [limit.id]


async def test_book_fetched_once_per_order(broker: PaperBroker, books: StaticBooks) -> None:
    await broker.buy_market(1, "BTC-EUR", D("50"), Reason.SIGNAL, now=1)
    assert books.calls == 1


# --- Spread estimé ------------------------------------------------------------------------


def test_spread_tracker_median_over_window() -> None:
    tracker = SpreadTracker(window_ms=3 * M)
    for i, (bid, ask) in enumerate([("99", "101"), ("99.9", "100.1"), ("99.5", "100.5")]):
        tracker.record("X", D(bid), D(ask), i * M)
    assert tracker.median_pct("X") == D("1.0000")
    tracker.record("X", D("99.95"), D("100.05"), 3 * M)  # le premier relevé sort de la fenêtre
    assert tracker.count("X") == 3
    assert tracker.median_pct("X") == D("0.2000")
    tracker.record("X", D("101"), D("100"), 4 * M)  # carnet croisé ignoré
    assert tracker.count("X") == 3
    assert tracker.median_pct("Y") is None


# --- Garde-fou : aucune conversion en float dans le broker ---------------------------


def test_no_float_in_broker() -> None:
    root = Path(__file__).resolve().parents[1] / "mcs" / "broker"
    for path in root.glob("*.py"):
        assert not re.search(r"\bfloat\(", path.read_text(encoding="utf-8")), path.name
