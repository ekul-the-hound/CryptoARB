# Crypto Arb Monitor

A real-time, always-on-top desktop monitor for triangular and cross-exchange crypto arbitrage opportunities. Built on `ccxt` (price feeds) and `coincursive` (graph-based arb detection).

```
python main.py                  # default config.json
python main.py --config my.json # custom config
```

**Requirements:** `pip install ccxt` + your local `coincursive` library.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        main.py                               │
│  load config → PriceCache → FeedManager → ArbEngine → UI    │
└──────────────────────────────────────────────────────────────┘

                    ┌─────────────┐
                    │ config.json │
                    └──────┬──────┘
                           │
         ┌─────────────────┼──────────────────┐
         ▼                 ▼                  ▼
  ┌─────────────┐   ┌───────────┐   ┌──────────────┐
  │ ExchangeFeed│   │ExchangeFeed│  │ ExchangeFeed │  (1 thread each)
  │  (binance)  │   │  (kucoin)  │  │   (bybit)    │
  └──────┬──────┘   └─────┬─────┘  └──────┬───────┘
         │                │               │
         └────────────────┼───────────────┘
                          ▼
                   ┌─────────────┐
                   │ PriceCache  │  thread-safe dict
                   │ [exch][sym] │  bid/ask/ts
                   └──────┬──────┘
                          │  .snapshot()
                          ▼
                   ┌─────────────┐
                   │  ArbEngine  │  daemon thread
                   │             │  coincursive.update_graph()
                   │             │  find_tri_arbs()
                   │             │  find_cross_exchange_arbs()
                   └──────┬──────┘
                          │  .latest(n, filter)
                          ▼
                   ┌─────────────┐
                   │ ArbMonitorUI│  tkinter mainloop
                   │  (topmost)  │  root.after() refresh
                   └──────┬──────┘
                          │
                          ▼
                   ┌─────────────┐
                   │  ArbLogger  │  append-only CSV
                   └─────────────┘
```

### Data flow per tick

1. Each `ExchangeFeed` thread calls `ccxt.fetch_tickers()` → writes into `PriceCache`.
2. `ArbEngine` wakes, calls `cache.snapshot()` → feeds `coincursive.update_graph(prices)`.
3. Engine runs `find_tri_arbs()` + `find_cross_exchange_arbs()`, wraps results as `ArbOpportunity` objects, filters by min profit / blacklists.
4. UI calls `engine.latest()` on a `root.after()` timer, renders rows, updates footer stats + exchange health dots.
5. Logger writes qualifying opps to CSV.

### Key design decisions

- **Threads, not async.** Simpler to debug; tkinter's mainloop isn't async-friendly. Each exchange gets its own thread with independent backoff.
- **Snapshot pattern.** The engine never reads the cache under lock during its scan—it takes a dict snapshot and works with the copy.
- **coincursive as a black box.** The engine calls `update_graph`, `find_tri_arbs`, `find_cross_exchange_arbs` and normalizes the output. A stub is provided so the app runs (with no detections) if coincursive isn't installed.

---

## UI Wireframe

```
┌─────────────────────────────────────────────────────────┐
│ CRYPTO ARB MONITOR • ≥0.2% THRESHOLD                 ✕ │
├─────────────────────────────────────────────────────────┤
│ USDT→BTC→ETH→USDT      tri-arb   +0.32%  binance  12s │
│ BTC→SOL→ETH→BTC        tri-arb   +0.27%  kucoin    8s │
│ BTC vs ETH              cross-X   +0.21%  bin/kuc  15s │
│ SOL vs AVAX             cross-X   +0.18%  byb/bin  22s │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ [ALL] [TRI] [X-EX]   ≥0.2% ▲▼                          │
├─────────────────────────────────────────────────────────┤
│ 142 opps | ~38/hr | avg net 0.24%  │ ● bin ● kuc ○ byb │
└─────────────────────────────────────────────────────────┘
```

- **Rows** are color-coded: green (≥0.5%), yellow (≥0.2%), dim (below).
- **Click a row** → detail popup with legs, fees, slippage.
- **Health dots**: ● green (<5s stale), ◐ yellow (<15s), ○ red (>15s).
- **Drag** anywhere to reposition; **✕** to close.

---

## Module Reference

| File | Purpose |
|---|---|
| `config.json` | All tunable parameters |
| `models.py` | `ArbOpportunity`, `Leg`, `PriceCache` |
| `price_feed.py` | Threaded ccxt polling, rate-limit backoff |
| `arb_engine.py` | Scans via coincursive, filters, buffers results |
| `arb_logger.py` | Append-only CSV logger |
| `ui.py` | Tkinter always-on-top corner window |
| `main.py` | Entry point, wiring |

---

## Task 4 — Design Notes for Optional Layers

### 4.1 Risk & Filters

Add to `ArbEngine._scan()` after generating raw opps:

- **Per-exchange fees:** Already in `config.json` under `fee_overrides`. The `_raw_to_opp` function sums `leg.fee_pct` values. To make this more accurate, load maker/taker fees from `ccxt.exchange.fees` at startup and store in a `fee_table[exchange][symbol]` dict.
- **Minimum order-book depth:** Extend `ExchangeFeed._tick()` to optionally call `fetch_order_book(symbol, limit=5)` and store `depth_usd` (sum of top-5 bids × price) in the cache. Add a `min_depth_usd` config key; filter in the engine.
- **Minimum notional:** Already stubbed as `min_notional_usd` in config. Calculate per-leg notional from depth or a fixed trade size and reject if any leg < threshold.
- **Whitelist/blacklist:** Coin blacklist is implemented. Add `coin_whitelist`—if non-empty, only allow symbols where all assets are in the whitelist.

### 4.2 Fee-Aware and Slippage-Aware Profit

- **Gross vs net** is already computed. The UI shows net; the detail popup shows both.
- **Slippage estimation:** The `_estimate_slippage()` function uses depth as a heuristic. For a better model, simulate walking the order book: for a given trade size, sum fills from L2 data until the size is met, compute the effective average price vs. top-of-book, and tag LOW (<0.05% slip), MED (<0.2%), HIGH (≥0.2%).

### 4.3 Alerts and Logging

- **Visual flash:** Implemented—rows with net ≥ `alert_sound_threshold_pct` flash green briefly.
- **Sound alert:** Add `winsound.Beep(1000, 200)` (Windows) or `os.system("afplay /System/Library/Sounds/Ping.aiff")` (macOS) in the refresh loop when a high-value opp first appears. Gate with a cooldown (e.g., max 1 beep per 30s).
- **CSV logging:** Implemented in `arb_logger.py`. The `traded` column defaults to `False`—flip it when auto-trade is added.

### 4.4 Historical Context

- **Side-panel on click:** Extend `_on_row_click` to query `engine._opps` for all opps sharing the same `symbols` set, compute a sparkline of `profit_pct_net` over time, and render as a `tk.Canvas` line chart in the popup.
- **Stats bar:** Already in the footer (`total`, `per_hour`, `avg_net`). Add a `win_rate` field once trade tracking is wired: `wins / total_trades` where a win = actual fill profit > 0.

### 4.5 Config Persistence

- On startup, `main.py` already reads `config.json`.
- On exit (or on threshold change), serialize the current `cfg` dict back to `config.json`. Use `atexit.register(save_config)`.
- For UI position, store `root.winfo_x()` and `root.winfo_y()` into `cfg["ui_x"]` / `cfg["ui_y"]` and restore on next launch.

### 4.6 Resilience and Ops

- **Health indicators:** Implemented via `PriceCache.health()` → colored dots in footer.
- **Rate-limit backoff:** Implemented in `ExchangeFeed.run()` with exponential backoff capped at 60s.
- **Crash-resistant restart:** Wrap `main()` in a `while True` with a top-level `try/except` that logs the crash, waits 5s, and re-enters. Alternatively, use `supervisord` or a systemd unit.

### 4.7 Future Auto-Trade Extensibility

- **Dry-run vs live-run toggle:** Add `"mode": "dry"` to config. In the detail popup, add an "Execute" button. If `mode == "dry"`, log the intent + would-be orders. If `mode == "live"`, call `ccxt.exchange.create_order()` for each leg sequentially (or in parallel with `concurrent.futures`).
- **One-click preview:** The detail popup already shows legs. Add a "Preview Trade" button that fetches current balances via `ccxt.fetch_balance()`, computes expected output per leg, and displays the breakdown before confirming.
- **External bot integration:** Expose opportunities as a local REST API (`flask` or `fastapi` on `localhost:8899`). The bot polls `/opportunities` for the latest filtered list. Alternatively, push to a Redis pub/sub or ZMQ socket for lower latency.

---

## Task 5 — Implementation Checklist

Priority order, starting from what you already have (`ccxt` + `coincursive`):

### Phase 1 — Working corner monitor (do first)

- [ ] Verify `coincursive` exposes `update_graph()`, `find_tri_arbs()`, `find_cross_exchange_arbs()` with the expected dict format. Adapt `_raw_to_opp()` in `arb_engine.py` to match your actual output schema.
- [ ] Install `ccxt` (`pip install ccxt`), confirm API connectivity to your target exchanges (no keys needed for public ticker data).
- [ ] Run `python main.py` — confirm the tkinter window appears, stays on top, and shows exchange health dots going green.
- [ ] Feed a few symbols, verify `PriceCache` is populating (add a quick `print(cache.snapshot())` in the engine loop temporarily).
- [ ] Confirm arb detections appear in the UI rows (if coincursive is producing them).

### Phase 2 — Polish and logging

- [ ] Tune `poll_interval_sec` and `ui_refresh_ms` for your machine and exchange rate limits.
- [ ] Enable CSV logging, verify `arb_log.csv` is accumulating rows.
- [ ] Adjust `fee_overrides` per exchange to match your actual fee tier.
- [ ] Test filter toggles (ALL / TRI / X-EX) and threshold ▲▼ buttons.
- [ ] Test the detail popup (click a row).

### Phase 3 — Depth and slippage

- [ ] Add L2 order-book fetching to `ExchangeFeed` (optional, increases API load).
- [ ] Wire `depth_usd` into `PriceCache` and `Leg`.
- [ ] Improve `_estimate_slippage()` with order-book simulation.
- [ ] Add `min_notional_usd` filtering in the engine.

### Phase 4 — Alerts and history

- [ ] Add sound alerts for high-value opps (platform-specific).
- [ ] Add historical sparkline in the detail popup.
- [ ] Add config persistence (save on exit, restore on launch).

### Phase 5 — Auto-trade readiness

- [ ] Add dry-run "Execute" button to detail popup.
- [ ] Wire `ccxt.create_order()` behind a live-run gate.
- [ ] Expose opportunities via local REST endpoint for external bot consumption.
- [ ] Add balance checks and pre-trade preview.
