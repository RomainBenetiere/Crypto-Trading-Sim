"""Collecte des bougies et des caractéristiques des marchés.

    python -m mcs.collector markets                 # caractéristiques, historique, volume 24 h
    python -m mcs.collector backfill [options]      # historique des bougies
    python -m mcs.collector live                    # collecte continue (REST + WebSocket)

Seules les bougies clôturées sont stockées. Historique : tout ce qui est disponible en 1 j, 4 h
et 1 h ; 90 jours en 1 min (puis collecte continue).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from mcs.bitvavo.rest import BitvavoRest
from mcs.bitvavo.ws import BitvavoWebSocket
from mcs.config import Settings, load_settings
from mcs.logging_setup import configure_logging
from mcs.market_data import INTERVAL_MS, INTERVALS, MINUTE_MS, aggregate, floor_ts
from mcs.storage.candles import load_candles, ts_bounds, upsert_candles
from mcs.storage.db import connect, now_ms
from mcs.storage.markets import (
    LatestPrice,
    update_market_stats,
    upsert_latest_prices,
    upsert_market_info,
)

log = logging.getLogger("mcs.collector")

DAY_MS = INTERVAL_MS["1d"]
DEFAULT_MINUTE_DAYS = 90
BACKFILL_ORDER = ("1d", "4h", "1h", "1m")
MINUTE_DELAY_MS = 3_000  # laisse à Bitvavo le temps de finaliser la bougie qui vient de clore
TICKER_STALE_MS = 120_000
MARKETS_REFRESH_MS = DAY_MS


@dataclass(frozen=True, slots=True)
class SyncResult:
    market: str
    interval: str
    recent: int
    older: int


def history_start(interval: str, now: int, minute_days: int) -> int | None:
    """Borne basse de l'historique à récupérer (None = tout l'historique disponible)."""
    if interval == "1m":
        return floor_ts(now - minute_days * DAY_MS, "1m")
    return None


async def sync_candles(
    conn: sqlite3.Connection,
    rest: BitvavoRest,
    market: str,
    interval: str,
    now: int,
    start: int | None = None,
) -> SyncResult:
    """Complète la base pour (market, interval) : d'abord les bougies récentes manquantes
    (de la plus récente stockée jusqu'à maintenant, trous compris), puis l'historique plus
    ancien que la plus ancienne stockée, jusqu'à `start` ou au début de l'historique."""
    end = floor_ts(now, interval)  # `end` exclu : la bougie en cours n'est jamais demandée
    if start is None and interval != "1d":
        # Le début de l'historique journalier borne la recherche : sans cette borne, la
        # pagination s'arrêterait à la première fenêtre sans transaction.
        daily = ts_bounds(conn, market, "1d")
        start = daily[0] if daily else None
    bounds = ts_bounds(conn, market, interval)
    recent = older = 0
    if bounds is not None:
        async for page in rest.iter_candles_backward(market, interval, end=end, start=bounds[1]):
            recent += upsert_candles(conn, page)
        older_end = bounds[0]
    else:
        older_end = end
    if start is None or older_end > start:
        async for page in rest.iter_candles_backward(market, interval, end=older_end, start=start):
            older += upsert_candles(conn, page)
    return SyncResult(market, interval, recent, older)


def check_consistency(conn: sqlite3.Connection, market: str, interval: str, ts: int) -> list[str]:
    """Compare la bougie officielle `interval` à l'agrégation des bougies 1 min stockées."""
    official = load_candles(conn, market, interval, ts, ts + 1)
    minutes = load_candles(conn, market, "1m", ts, ts + INTERVAL_MS[interval])
    if not official or not minutes:
        return []
    built = aggregate(minutes, interval)[0]
    reference = official[0]
    return [
        f"{field} : officiel {getattr(reference, field)} / 1 min {getattr(built, field)}"
        for field in ("open", "high", "low", "close", "volume")
        if getattr(reference, field) != getattr(built, field)
    ]


async def refresh_markets(
    conn: sqlite3.Connection, rest: BitvavoRest, basket: Sequence[str]
) -> None:
    """Caractéristiques des marchés, jours d'historique (bougies 1 j) et volume 24 h."""
    infos = await rest.get_markets(basket)
    missing = sorted(set(basket) - {i.market for i in infos})
    if missing:
        log.warning("Paires du panier inconnues chez Bitvavo : %s", ", ".join(missing))
    for info in infos:
        if info.status != "trading":
            log.warning("%s : statut %s.", info.market, info.status)
    upsert_market_info(conn, infos, basket)
    now = now_ms()
    for info in infos:
        await sync_candles(conn, rest, info.market, "1d", now)
        bounds = ts_bounds(conn, info.market, "1d")
        first = bounds[0] if bounds else None
        days = (floor_ts(now, "1d") - first) // DAY_MS if first is not None else 0
        ticker = await rest.get_ticker_24h(info.market)
        update_market_stats(
            conn,
            info.market,
            first_daily_ts=first,
            history_days=days,
            volume_24h_eur=ticker.volume_quote,
        )
        log.info(
            "%s : %d jours d'historique, volume 24 h %s €.", info.market, days, ticker.volume_quote
        )


async def backfill(
    conn: sqlite3.Connection,
    rest: BitvavoRest,
    markets: Sequence[str],
    intervals: Sequence[str] = BACKFILL_ORDER,
    minute_days: int = DEFAULT_MINUTE_DAYS,
) -> list[SyncResult]:
    results = []
    for market in markets:
        for interval in intervals:
            now = now_ms()
            result = await sync_candles(
                conn, rest, market, interval, now, history_start(interval, now, minute_days)
            )
            log.info(
                "%s %s : %d récentes, %d anciennes.", market, interval, result.recent, result.older
            )
            results.append(result)
    return results


class LiveCollector:
    """Collecte continue : bougies clôturées via REST à chaque frontière d'intervalle,
    derniers cours via le WebSocket (repli REST si le flux est muet)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        rest: BitvavoRest,
        basket: Sequence[str],
        minute_days: int = DEFAULT_MINUTE_DAYS,
    ) -> None:
        self.conn, self.rest, self.basket = conn, rest, list(basket)
        self.minute_days = minute_days
        self.ws = BitvavoWebSocket(self.basket, on_reconnect=self._on_reconnect)
        self._synced_end: dict[tuple[str, str], int] = {}
        self._markets_refreshed_at = 0

    async def _on_reconnect(self, downtime_s: float) -> None:
        # Le rattrapage REST du prochain tick comble les bougies manquées pendant la coupure.
        log.info("Rattrapage REST prévu après %.0f s de coupure WebSocket.", downtime_s)

    async def tick(self, now: int) -> None:
        if now - self._markets_refreshed_at >= MARKETS_REFRESH_MS:
            await refresh_markets(self.conn, self.rest, self.basket)
            self._markets_refreshed_at = now
        for market in self.basket:
            for interval in INTERVALS:
                end = floor_ts(now, interval)
                if self._synced_end.get((market, interval)) == end:
                    continue
                await sync_candles(
                    self.conn,
                    self.rest,
                    market,
                    interval,
                    now,
                    history_start(interval, now, self.minute_days),
                )
                self._synced_end[(market, interval)] = end
                if interval != "1m":
                    differences = check_consistency(
                        self.conn, market, interval, end - INTERVAL_MS[interval]
                    )
                    if differences:
                        log.info(
                            "%s %s : écart officiel / 1 min : %s",
                            market,
                            interval,
                            "; ".join(differences),
                        )
        await self.write_latest_prices(now)

    async def write_latest_prices(self, now: int) -> None:
        prices = []
        for market in self.basket:
            state = self.ws.tickers.get(market)
            if state and state.last_price and now - state.updated_ms < TICKER_STALE_MS:
                prices.append(
                    LatestPrice(market, state.last_price, state.best_bid, state.best_ask, now)
                )
                continue
            ticker = await self.rest.get_ticker_24h(market)
            price = ticker.last or _mid(ticker.bid, ticker.ask)
            if price is not None:
                prices.append(LatestPrice(market, price, ticker.bid, ticker.ask, now))
        upsert_latest_prices(self.conn, prices)

    async def run(self) -> None:
        ws_task = asyncio.create_task(self.ws.run(), name="bitvavo-ws")
        try:
            while True:
                now = now_ms()
                try:
                    await self.tick(now)
                except Exception:
                    log.exception("Échec du cycle de collecte, nouvel essai à la minute suivante.")
                next_tick = floor_ts(now_ms(), "1m") + MINUTE_MS + MINUTE_DELAY_MS
                await asyncio.sleep(max(next_tick - now_ms(), 0) / 1000)
        finally:
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ws_task


def _mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    return (bid + ask) / 2 if bid is not None and ask is not None else None


def _parse_list(raw: str | None, allowed: Sequence[str] | None = None) -> list[str] | None:
    if raw is None:
        return None
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if allowed is not None:
        invalid = [x for x in items if x not in allowed]
        if invalid:
            raise SystemExit(f"Valeurs invalides : {', '.join(invalid)} (permises : {allowed}).")
    return items


async def main_async(args: argparse.Namespace, settings: Settings) -> None:
    conn = connect(settings.db_path, settings.journal_mode)
    try:
        async with BitvavoRest() as rest:
            if args.command == "markets":
                await refresh_markets(conn, rest, settings.basket)
            elif args.command == "backfill":
                markets = _parse_list(args.markets) or list(settings.basket)
                intervals = _parse_list(args.intervals, INTERVALS) or list(BACKFILL_ORDER)
                await refresh_markets(conn, rest, settings.basket)
                await backfill(conn, rest, markets, intervals, args.minute_days)
            elif args.command == "live":
                await LiveCollector(conn, rest, settings.basket, args.minute_days).run()
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.collector",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("markets", "backfill", "live"))
    parser.add_argument("--markets", help="paires séparées par des virgules (défaut : panier)")
    parser.add_argument("--intervals", help="ex. 1d,4h (défaut : 1d,4h,1h,1m)")
    parser.add_argument(
        "--minute-days",
        type=int,
        default=DEFAULT_MINUTE_DAYS,
        help="profondeur d'historique en bougies 1 min (jours)",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_dir, "collector", settings.secrets)
    log.info("Base : %s (journal %s).", settings.db_path, settings.journal_mode.value)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args, settings))


if __name__ == "__main__":
    main()
