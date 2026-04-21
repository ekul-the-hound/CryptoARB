"""
models.py — Canonical data structures for the arb monitor.

ArbOpportunity is the universal container passed between engine → UI → logger.
PriceCache is the thread-safe shared state between the feed and the engine.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Literal


# ── Opportunity ──────────────────────────────────────────────────────────────

@dataclass
class Leg:
    pair: str               # e.g. "BTC/USDT"
    exchange: str           # e.g. "binance"
    side: str               # "buy" or "sell"
    price: float            # bid or ask used
    fee_pct: float          # taker fee assumed
    depth_usd: float = 0.0  # top-of-book depth in USD (optional)

@dataclass
class ArbOpportunity:
    type: Literal["tri-arb", "cross-exchange"]
    symbols: list[str]          # cycle path, e.g. ["USDT","BTC","ETH","USDT"]
    legs: list[Leg]
    exchanges: list[str]        # unique exchanges involved
    profit_pct_gross: float
    profit_pct_net: float       # after fees + fee_buffer
    ts: float = field(default_factory=time.time)
    slippage_tag: str = "LOW"   # "LOW" / "MED" / "HIGH"

    # ── display helpers ──────────────────────────────────────────────────
    @property
    def cycle_str(self) -> str:
        """USDT→BTC→ETH→USDT"""
        return "→".join(self.symbols)

    @property
    def type_label(self) -> str:
        return "tri-arb" if self.type == "tri-arb" else "cross-X"

    @property
    def exchanges_str(self) -> str:
        return " / ".join(sorted(set(self.exchanges)))

    @property
    def age_str(self) -> str:
        delta = time.time() - self.ts
        if delta < 60:
            return f"{int(delta)}s"
        return f"{int(delta // 60)}m"

    def __repr__(self) -> str:
        return (
            f"<Opp {self.cycle_str}  {self.type_label}  "
            f"net={self.profit_pct_net:+.3%}  {self.exchanges_str}>"
        )


# ── Price cache ──────────────────────────────────────────────────────────────

class PriceEntry:
    __slots__ = ("bid", "ask", "ts")

    def __init__(self, bid: float, ask: float, ts: float | None = None):
        self.bid = bid
        self.ask = ask
        self.ts = ts or time.time()


class PriceCache:
    """
    Thread-safe nested dict:  cache[exchange][symbol] → PriceEntry

    Writers call  .update(exchange, symbol, bid, ask)
    Readers call  .snapshot() → plain dict copy for the engine.
    """

    def __init__(self):
        self._data: dict[str, dict[str, PriceEntry]] = {}
        self._lock = threading.Lock()

    def update(self, exchange: str, symbol: str, bid: float, ask: float) -> None:
        with self._lock:
            self._data.setdefault(exchange, {})[symbol] = PriceEntry(bid, ask)

    def snapshot(self) -> dict[str, dict[str, dict]]:
        """Return a plain-dict snapshot safe for cross-thread reads."""
        with self._lock:
            return {
                exch: {
                    sym: {"bid": e.bid, "ask": e.ask, "ts": e.ts}
                    for sym, e in pairs.items()
                }
                for exch, pairs in self._data.items()
            }

    def health(self) -> dict[str, str]:
        """Per-exchange staleness indicator: green / yellow / red."""
        now = time.time()
        result = {}
        with self._lock:
            for exch, pairs in self._data.items():
                if not pairs:
                    result[exch] = "red"
                    continue
                newest = max(e.ts for e in pairs.values())
                age = now - newest
                if age < 5:
                    result[exch] = "green"
                elif age < 15:
                    result[exch] = "yellow"
                else:
                    result[exch] = "red"
        return result
