"""
arb_logger.py — Append-only CSV logger for arb opportunities.

Logs every opportunity that passes filters, with a `traded` flag for
future auto-trade tracking.
"""

from __future__ import annotations

import csv
import os
import time
import threading
from pathlib import Path

from models import ArbOpportunity


class ArbLogger:
    """Thread-safe CSV logger.  One row per opportunity."""

    HEADERS = [
        "timestamp", "iso_time", "type", "cycle", "exchanges",
        "profit_gross_pct", "profit_net_pct", "slippage", "legs_json", "traded",
    ]

    def __init__(self, path: str = "arb_log.csv"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.HEADERS)

    def log(self, opp: ArbOpportunity, traded: bool = False) -> None:
        import json

        legs_json = json.dumps([
            {"pair": l.pair, "exchange": l.exchange, "side": l.side,
             "price": l.price, "fee_pct": l.fee_pct}
            for l in opp.legs
        ])

        row = [
            f"{opp.ts:.3f}",
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(opp.ts)),
            opp.type,
            opp.cycle_str,
            opp.exchanges_str,
            f"{opp.profit_pct_gross:.5f}",
            f"{opp.profit_pct_net:.5f}",
            opp.slippage_tag,
            legs_json,
            str(traded),
        ]

        with self._lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow(row)
