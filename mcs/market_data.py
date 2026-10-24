"""Types de données de marché partagés : bougies et intervalles."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

MINUTE_MS = 60_000
INTERVAL_MS: dict[str, int] = {
    "1m": MINUTE_MS,
    "1h": 60 * MINUTE_MS,
    "4h": 240 * MINUTE_MS,
    "1d": 1440 * MINUTE_MS,
}
INTERVALS: tuple[str, ...] = tuple(INTERVAL_MS)


@dataclass(frozen=True, slots=True)
class Candle:
    """Bougie OHLCV. `ts` = début de la bougie, en ms UTC (alignée sur l'intervalle)."""

    market: str
    interval: str
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def end_ts(self) -> int:
        return self.ts + INTERVAL_MS[self.interval]

    def is_closed(self, now_ms: int) -> bool:
        return is_closed(self.ts, self.interval, now_ms)


def is_closed(ts: int, interval: str, now_ms: int) -> bool:
    """Une bougie est clôturée quand sa fin est atteinte."""
    return ts + INTERVAL_MS[interval] <= now_ms


def floor_ts(ts: int, interval: str) -> int:
    """Début de la bougie contenant `ts` (les bougies Bitvavo sont alignées sur UTC)."""
    step = INTERVAL_MS[interval]
    return ts - ts % step


def closed_only(candles: Iterable[Candle], now_ms: int) -> list[Candle]:
    return [c for c in candles if c.is_closed(now_ms)]


def aggregate(candles: Sequence[Candle], interval: str) -> list[Candle]:
    """Agrège des bougies plus fines (triées ou non) en bougies `interval`.

    Les bougies sans transaction n'existent pas chez Bitvavo : un groupe peut avoir des trous,
    il est agrégé tel quel (ouverture de la première, clôture de la dernière).
    """
    groups: dict[tuple[str, int], list[Candle]] = {}
    for candle in sorted(candles, key=lambda c: (c.market, c.ts)):
        if INTERVAL_MS[candle.interval] >= INTERVAL_MS[interval]:
            raise ValueError(f"Impossible d'agréger du {candle.interval} en {interval}.")
        groups.setdefault((candle.market, floor_ts(candle.ts, interval)), []).append(candle)
    return [
        Candle(
            market=market,
            interval=interval,
            ts=ts,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum((c.volume for c in group), Decimal(0)),
        )
        for (market, ts), group in groups.items()
    ]
