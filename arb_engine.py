"""
arb_engine.py — Arbitrage detection loop.

Each tick:
  1. Snapshot the PriceCache.
  2. Update the coincursive graph.
  3. Run tri-arb + cross-exchange scans.
  4. Filter by min profit, notional, blacklists.
  5. Push results into a thread-safe result buffer.
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Any

from models import PriceCache, ArbOpportunity, Leg

log = logging.getLogger(__name__)


# ── Coincursive stub ─────────────────────────────────────────────────────────
# Replace with your real `coincursive` import.
# The stub below documents the expected interface so you can wire it in.

class _CoincursiveStub:
    """
    Placeholder for the real coincursive library.

    Expected interface:
        .update_graph(prices: dict[exch][symbol] -> {bid, ask, ts})
        .find_tri_arbs()   -> list[dict]   (raw cycle dicts)
        .find_cross_exchange_arbs() -> list[dict]
    """

    def update_graph(self, prices: dict) -> None:
        pass

    def find_tri_arbs(self) -> list[dict]:
        return []

    def find_cross_exchange_arbs(self) -> list[dict]:
        return []


# ── Attempt real import, fall back to stub ───────────────────────────────────

try:
    import coincursive as _coincursive_mod       # type: ignore[import]
    coincursive: Any = _coincursive_mod
    log.info("coincursive library loaded")
except ImportError:
    coincursive = _CoincursiveStub()
    log.warning("coincursive not found — using stub (no arb detection)")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _estimate_slippage(legs: list[Leg]) -> str:
    """Heuristic slippage tag based on depth."""
    min_depth = min((l.depth_usd for l in legs), default=0)
    if min_depth >= 50_000:
        return "LOW"
    if min_depth >= 10_000:
        return "MED"
    return "HIGH"


def _raw_to_opp(raw: dict, opp_type: str, cfg: dict) -> ArbOpportunity | None:
    """
    Convert a raw dict from coincursive into a canonical ArbOpportunity.

    Expected raw keys (adapt to your coincursive output):
        symbols: list[str]
        legs: list[dict]  — each with pair, exchange, side, price, fee_pct, depth_usd
        profit_pct_gross: float
    """
    try:
        symbols = raw["symbols"]
        legs = [Leg(**l) for l in raw["legs"]]
        gross = raw["profit_pct_gross"]

        # Net = gross minus total fees minus fee_buffer
        total_fee = sum(l.fee_pct for l in legs) + cfg.get("fee_buffer", 0.001)
        net = gross - total_fee

        exchanges = list({l.exchange for l in legs})

        return ArbOpportunity(
            type=opp_type,
            symbols=symbols,
            legs=legs,
            exchanges=exchanges,
            profit_pct_gross=gross,
            profit_pct_net=net,
            slippage_tag=_estimate_slippage(legs),
        )
    except (KeyError, TypeError) as exc:
        log.debug("Skipping malformed raw opp: %s — %s", exc, raw)
        return None


# ── Engine ───────────────────────────────────────────────────────────────────

class ArbEngine:
    """
    Runs in its own daemon thread.  Wakes every `tick` seconds,
    snapshots prices, scans for arbs, and stores results.
    """

    def __init__(self, cfg: dict, cache: PriceCache, tick: float = 1.0):
        self.cfg = cfg
        self.cache = cache
        self.tick = tick

        self._lock = threading.Lock()
        self._opps: list[ArbOpportunity] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="arb-engine")

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def latest(self, n: int = 5, filter_type: str = "all") -> list[ArbOpportunity]:
        """Return the most recent N filtered opportunities (newest first)."""
        with self._lock:
            out = self._opps
            if filter_type == "tri-arb":
                out = [o for o in out if o.type == "tri-arb"]
            elif filter_type == "cross-exchange":
                out = [o for o in out if o.type == "cross-exchange"]
            return out[-n:][::-1]

    def stats(self) -> dict:
        """Quick aggregates for the status bar."""
        with self._lock:
            if not self._opps:
                return {"total": 0, "avg_net": 0.0, "per_hour": 0.0}
            total = len(self._opps)
            avg = sum(o.profit_pct_net for o in self._opps) / total
            span = max(1.0, self._opps[-1].ts - self._opps[0].ts)
            per_hour = total / (span / 3600) if span > 0 else 0
            return {"total": total, "avg_net": avg, "per_hour": per_hour}

    # ── loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:
                log.exception("arb-engine scan error")
            self._stop.wait(self.tick)

    def _scan(self) -> None:
        prices = self.cache.snapshot()
        if not prices:
            return

        coincursive.update_graph(prices)

        new_opps: list[ArbOpportunity] = []

        # Triangular arbs
        for raw in coincursive.find_tri_arbs():
            opp = _raw_to_opp(raw, "tri-arb", self.cfg)
            if opp:
                new_opps.append(opp)

        # Cross-exchange arbs
        for raw in coincursive.find_cross_exchange_arbs():
            opp = _raw_to_opp(raw, "cross-exchange", self.cfg)
            if opp:
                new_opps.append(opp)

        # ── filters ───────────────────────────────────────────────────────
        min_pct = self.cfg.get("min_profit_pct", 0.002)
        blacklist = set(self.cfg.get("coin_blacklist", []))

        filtered = []
        for opp in new_opps:
            if opp.profit_pct_net < min_pct:
                continue
            if any(s in blacklist for s in opp.symbols):
                continue
            filtered.append(opp)

        if filtered:
            with self._lock:
                self._opps.extend(filtered)
                # Keep only last 500 for memory
                if len(self._opps) > 500:
                    self._opps = self._opps[-500:]
