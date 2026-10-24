"""Spread estimé par paire : médiane sur 24 h des écarts meilleur bid / meilleur ask relevés
chaque minute (onglet « Marchés du panier » et pénalité des stops déclenchés).

Les relevés sont gardés en mémoire : après un redémarrage, la médiane se reconstitue."""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from statistics import median

WINDOW_MS = 24 * 3_600_000


class SpreadTracker:
    def __init__(self, window_ms: int = WINDOW_MS) -> None:
        self.window_ms = window_ms
        self._samples: dict[str, deque[tuple[int, Decimal]]] = {}

    def record(self, market: str, bid: Decimal, ask: Decimal, ts: int) -> None:
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        samples = self._samples.setdefault(market, deque())
        samples.append((ts, (ask - bid) / ((ask + bid) / 2)))
        while samples and samples[0][0] <= ts - self.window_ms:
            samples.popleft()

    def median_pct(self, market: str) -> Decimal | None:
        """Spread médian en % (0,05 = 0,05 %), ou None sans relevé."""
        samples = self._samples.get(market)
        if not samples:
            return None
        return (median(s for _, s in samples) * 100).quantize(Decimal("0.0001"))

    def count(self, market: str) -> int:
        return len(self._samples.get(market, ()))
