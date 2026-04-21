"""
main.py — Entry point for the Crypto Arb Monitor.

Wires together:  config → PriceCache → FeedManager → ArbEngine → UI

Usage:
    python main.py                  # uses config.json in same directory
    python main.py --config my.json # custom config path
"""

from __future__ import annotations

import json
import logging
import argparse
import sys
from pathlib import Path

from models import PriceCache
from price_feed import FeedManager
from arb_engine import ArbEngine
from arb_logger import ArbLogger
from ui import ArbMonitorUI


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[WARN] Config not found at {p}, using defaults")
        return {
            "exchanges": ["binance"],
            "watchlist": ["BTC/USDT", "ETH/USDT", "ETH/BTC"],
            "fee_buffer": 0.001,
            "min_profit_pct": 0.002,
            "poll_interval_sec": 2.0,
            "ui_refresh_ms": 1000,
            "max_display_rows": 5,
            "ui_position": "bottom-right",
            "ui_width": 520,
            "ui_height": 200,
            "show_filter": "all",
            "alert_sound_threshold_pct": 0.005,
            "log_to_csv": True,
            "log_file": "arb_log.csv",
            "fee_overrides": {},
            "coin_blacklist": [],
            "exchange_blacklist": [],
        }
    with open(p) as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Arb Monitor")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    log = logging.getLogger("main")

    cfg = load_config(args.config)
    log.info("Config loaded: %d exchanges, %d symbols",
             len(cfg["exchanges"]), len(cfg["watchlist"]))

    # 1. Shared price cache
    cache = PriceCache()

    # 2. Start exchange feeds (one thread per exchange)
    feeds = FeedManager(cfg, cache)
    feeds.start_all()

    # 3. Start arb detection engine
    engine = ArbEngine(cfg, cache, tick=cfg.get("poll_interval_sec", 2.0))
    engine.start()

    # 4. Optional CSV logger
    logger = None
    if cfg.get("log_to_csv"):
        logger = ArbLogger(cfg.get("log_file", "arb_log.csv"))
        log.info("CSV logging to %s", cfg["log_file"])

    # 5. Launch UI (blocks on mainloop)
    log.info("Launching monitor UI…")
    try:
        ui = ArbMonitorUI(engine, cache, cfg, logger=logger)
        ui.run()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down…")
        engine.stop()
        feeds.stop_all()
        log.info("Done.")


if __name__ == "__main__":
    main()
