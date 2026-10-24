from decimal import Decimal

import pytest

from mcs.market_data import Candle, aggregate, closed_only, floor_ts, is_closed
from tests.conftest import make_candle

H = 3_600_000


def test_is_closed_at_exact_end() -> None:
    assert not is_closed(0, "4h", 4 * H - 1)
    assert is_closed(0, "4h", 4 * H)


def test_floor_ts_aligns_on_utc() -> None:
    # Bougie 4 h observée chez Bitvavo : 1792857600000 (alignée sur minuit UTC).
    assert floor_ts(1792861693000, "4h") == 1792857600000
    assert floor_ts(1792861693000, "1d") == 1792800000000


def test_closed_only_drops_running_candle() -> None:
    candles = [make_candle("BTC-EUR", "1h", 0), make_candle("BTC-EUR", "1h", H)]
    assert [c.ts for c in closed_only(candles, H + 10)] == [0]


def test_aggregate_minutes_with_gaps() -> None:
    def c(ts_min: int, o: str, h: str, low: str, cl: str, v: str) -> Candle:
        return Candle(
            "BTC-EUR",
            "1m",
            ts_min * 60_000,
            Decimal(o),
            Decimal(h),
            Decimal(low),
            Decimal(cl),
            Decimal(v),
        )

    # Minute 1 absente (aucune transaction) ; minute 60 appartient à l'heure suivante.
    minutes = [
        c(2, "11", "15", "10", "14", "2"),
        c(0, "10", "12", "8", "11", "1"),
        c(60, "20", "20", "20", "20", "5"),
    ]
    first, second = aggregate(minutes, "1h")
    assert (first.ts, first.open, first.high, first.low, first.close, first.volume) == (
        0,
        Decimal(10),
        Decimal(15),
        Decimal(8),
        Decimal(14),
        Decimal(3),
    )
    assert second.ts == H


def test_aggregate_rejects_coarser_source() -> None:
    with pytest.raises(ValueError):
        aggregate([make_candle("BTC-EUR", "1d", 0)], "4h")
