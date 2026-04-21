"""
ui.py — Compact, always-on-top tkinter arb monitor.

Features:
  • Borderless, stays on top, snapped to a screen corner.
  • Shows last N opportunities with color-coded profit.
  • Filter toggle (tri-arb / cross-X / all) + threshold slider.
  • Exchange health dots in the footer.
  • Row click → detail popup (extensible to side-panel history).
  • Visual flash on high-value opportunities.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import font as tkfont

from models import ArbOpportunity, PriceCache
from arb_engine import ArbEngine
from arb_logger import ArbLogger


# ── Color palette ────────────────────────────────────────────────────────────

BG           = "#0D1117"
BG_HEADER    = "#161B22"
FG           = "#C9D1D9"
FG_DIM       = "#6E7681"
GREEN        = "#3FB950"
YELLOW       = "#D29922"
RED          = "#F85149"
ACCENT       = "#58A6FF"
ROW_HOVER    = "#21262D"
FLASH_BG     = "#1A3A1A"


# ── Main monitor window ─────────────────────────────────────────────────────

class ArbMonitorUI:
    """
    Compact corner monitor.

    Args:
        engine:  running ArbEngine instance
        cache:   shared PriceCache (for health dots)
        cfg:     config dict
        logger:  optional ArbLogger for CSV output
    """

    def __init__(
        self,
        engine: ArbEngine,
        cache: PriceCache,
        cfg: dict,
        logger: ArbLogger | None = None,
    ):
        self.engine = engine
        self.cache = cache
        self.cfg = cfg
        self.logger = logger

        self.filter_type = cfg.get("show_filter", "all")
        self.min_pct = cfg.get("min_profit_pct", 0.002)
        self.max_rows = cfg.get("max_display_rows", 5)
        self._last_logged_ts: set[float] = set()

        self._build()

    # ── window construction ───────────────────────────────────────────────

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.title("Arb Monitor")
        self.root.overrideredirect(True)          # borderless
        self.root.attributes("-topmost", True)    # always on top
        self.root.configure(bg=BG)

        w = self.cfg.get("ui_width", 520)
        h = self.cfg.get("ui_height", 200)
        self._snap_to_corner(w, h)

        # Allow dragging the borderless window
        self.root.bind("<Button-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._on_drag)

        # ── Header ────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG_HEADER, padx=8, pady=4)
        hdr.pack(fill=tk.X)

        mono = tkfont.Font(family="Consolas", size=9, weight="bold")
        self.hdr_label = tk.Label(
            hdr, text=self._header_text(), font=mono,
            fg=ACCENT, bg=BG_HEADER, anchor="w",
        )
        self.hdr_label.pack(side=tk.LEFT)

        # Close button
        tk.Label(
            hdr, text="✕", font=("Consolas", 10), fg=FG_DIM, bg=BG_HEADER,
            cursor="hand2",
        ).pack(side=tk.RIGHT)
        hdr.winfo_children()[-1].bind("<Button-1>", lambda e: self.root.destroy())

        # ── Opportunity list ──────────────────────────────────────────────
        self.list_frame = tk.Frame(self.root, bg=BG, padx=6, pady=2)
        self.list_frame.pack(fill=tk.BOTH, expand=True)

        self.row_labels: list[tk.Label] = []
        row_font = tkfont.Font(family="Consolas", size=8)
        for i in range(self.max_rows):
            lbl = tk.Label(
                self.list_frame, text="", font=row_font,
                fg=FG, bg=BG, anchor="w", padx=4, pady=1,
            )
            lbl.pack(fill=tk.X)
            lbl.bind("<Enter>", lambda e, l=lbl: l.config(bg=ROW_HOVER))
            lbl.bind("<Leave>", lambda e, l=lbl: l.config(bg=BG))
            lbl.bind("<Button-1>", lambda e, idx=i: self._on_row_click(idx))
            self.row_labels.append(lbl)

        # ── Control bar ───────────────────────────────────────────────────
        ctrl = tk.Frame(self.root, bg=BG_HEADER, padx=6, pady=3)
        ctrl.pack(fill=tk.X)

        btn_font = tkfont.Font(family="Consolas", size=7)
        for label, ftype in [("ALL", "all"), ("TRI", "tri-arb"), ("X-EX", "cross-exchange")]:
            b = tk.Label(
                ctrl, text=f" {label} ", font=btn_font,
                fg=ACCENT if self.filter_type == ftype else FG_DIM,
                bg=BG_HEADER, cursor="hand2", padx=2,
            )
            b.pack(side=tk.LEFT, padx=2)
            b.bind("<Button-1>", lambda e, ft=ftype: self._set_filter(ft))

        # Threshold display
        self.thresh_label = tk.Label(
            ctrl, text=f"≥{self.min_pct:.1%}", font=btn_font,
            fg=YELLOW, bg=BG_HEADER,
        )
        self.thresh_label.pack(side=tk.LEFT, padx=8)

        # ▲ / ▼ to adjust threshold
        for sym, delta in [("▲", 0.001), ("▼", -0.001)]:
            b = tk.Label(
                ctrl, text=sym, font=btn_font, fg=FG_DIM,
                bg=BG_HEADER, cursor="hand2",
            )
            b.pack(side=tk.LEFT)
            b.bind("<Button-1>", lambda e, d=delta: self._adjust_threshold(d))

        # ── Footer / status ───────────────────────────────────────────────
        self.footer = tk.Label(
            self.root, text="starting…", font=tkfont.Font(family="Consolas", size=7),
            fg=FG_DIM, bg=BG, anchor="w", padx=8, pady=2,
        )
        self.footer.pack(fill=tk.X)

        # Kick off the periodic refresh
        self._refresh()

    # ── Geometry ──────────────────────────────────────────────────────────

    def _snap_to_corner(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos = self.cfg.get("ui_position", "bottom-right")
        margin = 10
        if pos == "bottom-right":
            x, y = sw - w - margin, sh - h - 50
        elif pos == "bottom-left":
            x, y = margin, sh - h - 50
        elif pos == "top-right":
            x, y = sw - w - margin, margin
        else:
            x, y = margin, margin
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Dragging ──────────────────────────────────────────────────────────

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # ── Refresh loop ─────────────────────────────────────────────────────

    def _refresh(self) -> None:
        opps = self.engine.latest(n=self.max_rows, filter_type=self.filter_type)

        # Log new opps to CSV
        if self.logger:
            for o in opps:
                if o.ts not in self._last_logged_ts:
                    self.logger.log(o)
                    self._last_logged_ts.add(o.ts)
                    # Keep set bounded
                    if len(self._last_logged_ts) > 1000:
                        self._last_logged_ts = set(list(self._last_logged_ts)[-500:])

        # Update rows
        for i, lbl in enumerate(self.row_labels):
            if i < len(opps):
                opp = opps[i]
                lbl.config(text=self._format_row(opp), fg=self._row_color(opp))
                # Flash for high-value
                if opp.profit_pct_net >= self.cfg.get("alert_sound_threshold_pct", 0.005):
                    lbl.config(bg=FLASH_BG)
                    lbl.after(600, lambda l=lbl: l.config(bg=BG))
            else:
                lbl.config(text="", bg=BG)

        # Update header threshold
        self.hdr_label.config(text=self._header_text())

        # Update footer
        stats = self.engine.stats()
        health = self.cache.health()
        health_str = "  ".join(
            f"{'●' if s == 'green' else '◐' if s == 'yellow' else '○'} {ex}"
            for ex, s in health.items()
        )
        self.footer.config(
            text=(
                f"{stats['total']} opps | "
                f"~{stats['per_hour']:.0f}/hr | "
                f"avg net {stats['avg_net']:.3%}  │  {health_str}"
            )
        )

        interval = self.cfg.get("ui_refresh_ms", 1000)
        self.root.after(interval, self._refresh)

    # ── Formatting helpers ────────────────────────────────────────────────

    def _header_text(self) -> str:
        return f"CRYPTO ARB MONITOR • ≥{self.min_pct:.1%} THRESHOLD"

    @staticmethod
    def _format_row(opp: ArbOpportunity) -> str:
        cycle = opp.cycle_str.ljust(24)
        ttype = opp.type_label.ljust(8)
        profit = f"{opp.profit_pct_net:+.2%}".ljust(8)
        exch = opp.exchanges_str[:20].ljust(20)
        age = opp.age_str.rjust(4)
        return f" {cycle} {ttype} {profit} {exch} {age}"

    @staticmethod
    def _row_color(opp: ArbOpportunity) -> str:
        if opp.profit_pct_net >= 0.005:
            return GREEN
        if opp.profit_pct_net >= 0.002:
            return YELLOW
        return FG

    # ── Controls ──────────────────────────────────────────────────────────

    def _set_filter(self, ftype: str) -> None:
        self.filter_type = ftype
        # Re-color buttons
        for child in self.root.winfo_children():
            if isinstance(child, tk.Frame):
                for btn in child.winfo_children():
                    if isinstance(btn, tk.Label) and btn.cget("cursor") == "hand2":
                        text = btn.cget("text").strip()
                        mapping = {"ALL": "all", "TRI": "tri-arb", "X-EX": "cross-exchange"}
                        if mapping.get(text) == ftype:
                            btn.config(fg=ACCENT)
                        elif text in mapping:
                            btn.config(fg=FG_DIM)

    def _adjust_threshold(self, delta: float) -> None:
        self.min_pct = max(0.0, self.min_pct + delta)
        self.cfg["min_profit_pct"] = self.min_pct
        self.thresh_label.config(text=f"≥{self.min_pct:.1%}")

    # ── Row interaction ───────────────────────────────────────────────────

    def _on_row_click(self, idx: int) -> None:
        """Show a detail popup for the clicked opportunity."""
        opps = self.engine.latest(n=self.max_rows, filter_type=self.filter_type)
        if idx >= len(opps):
            return
        opp = opps[idx]

        popup = tk.Toplevel(self.root)
        popup.title("Opportunity Detail")
        popup.attributes("-topmost", True)
        popup.configure(bg=BG)
        popup.geometry("360x220")

        mono = tkfont.Font(family="Consolas", size=8)
        lines = [
            f"  Type:       {opp.type}",
            f"  Cycle:      {opp.cycle_str}",
            f"  Exchanges:  {opp.exchanges_str}",
            f"  Gross:      {opp.profit_pct_gross:+.4%}",
            f"  Net:        {opp.profit_pct_net:+.4%}",
            f"  Slippage:   {opp.slippage_tag}",
            f"  Age:        {opp.age_str}",
            "",
            "  ── Legs ──",
        ]
        for leg in opp.legs:
            lines.append(
                f"  {leg.side.upper():4} {leg.pair:12} @ {leg.price:.8g}  "
                f"fee={leg.fee_pct:.3%}  [{leg.exchange}]"
            )

        text = tk.Text(popup, font=mono, fg=FG, bg=BG, bd=0,
                       highlightthickness=0, wrap=tk.NONE)
        text.insert("1.0", "\n".join(lines))
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()
