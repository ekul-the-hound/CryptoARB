"""
price_feed.py — Threaded price-fetching loop using ccxt.

One thread per exchange.  Each thread polls tickers for all watched symbols
on that exchange and pushes updates into the shared PriceCache.

Rate-limit aware: backs off on 429 / DDoSProtection errors.
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Any

import ccxt

from models import PriceCache

log = logging.getLogger(__name__)


class ExchangeFeed(threading.Thread):
    """Polls one exchange in a dedicated daemon thread."""

    def __init__(
        self,
        exchange_id: str,
        symbols: list[str],
        cache: PriceCache,
        poll_interval: float = 2.0,
        fee_overrides: dict | None = None,
    ):
        super().__init__(daemon=True, name=f"feed-{exchange_id}")
        self.exchange_id = exchange_id
        self.symbols = symbols
        self.cache = cache
        self.poll_interval = poll_interval
        self.fee_overrides = fee_overrides or {}

        self._stop_event = threading.Event()
        self._exchange: Any = None
        self._available_symbols: set[str] = set()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._connect()
        backoff = self.poll_interval

        while not self._stop_event.is_set():
            try:
                self._tick()
                backoff = self.poll_interval          # reset on success
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as exc:
                backoff = min(backoff * 2, 60)
                log.warning("%s rate-limited, backing off %.1fs: %s",
                            self.exchange_id, backoff, exc)
            except ccxt.NetworkError as exc:
                log.warning("%s network error: %s", self.exchange_id, exc)
            except Exception:
                log.exception("%s unexpected error in feed loop", self.exchange_id)

            self._stop_event.wait(backoff)

    # ── internals ─────────────────────────────────────────────────────────

    def _connect(self) -> None:
        """Instantiate the ccxt exchange and load markets once."""
        cls = getattr(ccxt, self.exchange_id)
        self._exchange = cls({"enableRateLimit": True})
        try:
            self._exchange.load_markets()
            self._available_symbols = set(self._exchange.symbols)
            log.info("%s connected — %d markets loaded",
                     self.exchange_id, len(self._available_symbols))
        except Exception:
            log.exception("%s failed to load markets", self.exchange_id)

    def _tick(self) -> None:
        """Fetch tickers for watched symbols and push into cache."""
        # Filter to symbols actually listed on this exchange
        active = [s for s in self.symbols if s in self._available_symbols]
        if not active:
            return

        # Use fetch_tickers for batch efficiency (if exchange supports it)
        try:
            tickers = self._exchange.fetch_tickers(active)
        except ccxt.NotSupported:
            # Fallback: fetch one-by-one
            tickers = {}
            for sym in active:
                try:
                    tickers[sym] = self._exchange.fetch_ticker(sym)
                except Exception:
                    pass

        for sym, t in tickers.items():
            bid = t.get("bid")
            ask = t.get("ask")
            if bid and ask and bid > 0 and ask > 0:
                self.cache.update(self.exchange_id, sym, float(bid), float(ask))


# ── Manager ──────────────────────────────────────────────────────────────────

class FeedManager:
    """
    Starts / stops one ExchangeFeed per configured exchange.

    Usage:
        mgr = FeedManager(cfg, cache)
        mgr.start_all()
        ...
        mgr.stop_all()
    """

    def __init__(self, cfg: dict, cache: PriceCache):
        self.feeds: list[ExchangeFeed] = []
        for exch_id in cfg["exchanges"]:
            if exch_id in cfg.get("exchange_blacklist", []):
                continue
            feed = ExchangeFeed(
                exchange_id=exch_id,
                symbols=cfg["watchlist"],
                cache=cache,
                poll_interval=cfg.get("poll_interval_sec", 2.0),
                fee_overrides=cfg.get("fee_overrides", {}).get(exch_id, {}),
            )
            self.feeds.append(feed)

    def start_all(self) -> None:
        for f in self.feeds:
            f.start()
            log.info("Started feed: %s", f.exchange_id)

    def stop_all(self) -> None:
        for f in self.feeds:
            f.stop()
        for f in self.feeds:
            f.join(timeout=5)
