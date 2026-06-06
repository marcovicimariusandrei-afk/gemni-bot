"""
main.py — polybot_simple_v1, module 3 (signal + validation + dashboard).
v5.8.1 — applied 2026-04-29.

NEW IN v5.8.1: late-stage stop-loss (LATE-SL).

Designed for A/B comparison. Two modes:
  - SL_LATE_MODE=pct: bid <= entry × SL_LATE_PCT (e.g., 0.50 of entry)
  - SL_LATE_MODE=abs: bid <= SL_LATE_FLOOR (e.g., $0.10 absolute)
  - SL_LATE_MODE="" (default): late-SL disabled

Both modes additionally require:
  - time_remaining_s <= SL_LATE_WINDOW_S (default 60s)
  - condition holds for SL_LATE_PERSIST_S consecutive ticks (default 1s)

Late-SL fires after TP check, after entry-gated SL check (both inherited
from v5.8.0). Marker ',sl_late_pct:0.50' or ',sl_late_abs:0.10' appended
to trades CSV `notes` field. Dashboard shows red 'LATE' chip in resolution
column for SL_LATE-exited trades.

A/B test plan: two bot instances with identical config except SL_LATE_MODE:
  Variant A: SL_LATE_MODE=pct, SL_LATE_PCT=0.50, SL_LATE_WINDOW_S=60
  Variant B: SL_LATE_MODE=abs, SL_LATE_FLOOR=0.10, SL_LATE_WINDOW_S=60
Both keep TP +$0.15 / 5s and BLOCK_REENTRY_AFTER_EXIT=true. Both disable
the v5.8.0 entry-gated SL via STOP_LOSS_THRESHOLD=0.

DASHBOARD: new "Avg win / break-even" indicator on stats line.

DATA PRESERVATION: zero schema changes. Late-SL exits use the same
trades CSV with notes='pnl=-X.XXXX,sl_late_pct:0.50' or 'sl_late_abs:0.10'
markers.

CARRIED FORWARD FROM v5.8.0:
  Re-entry block after exit (any exit type adds market_id to set;
  re-entry refused with reason 'market_already_exited'). Entry-gated SL
  (defaults off in v5.8.1 A/B configs).

CARRIED FORWARD FROM v5.7.0:
  Take-profit early exit at entry+TAKE_PROFIT_THRESHOLD for
  TAKE_PROFIT_PERSIST_S consecutive seconds (DRY only). Backtest
  showed +$0.79/trade swing on n=202 v5.5.29+ trades.

CARRIED FORWARD FROM v5.6.0:

Two new daily-rotated CSVs are emitted alongside signal_log / trades /
binance_prices, both at ~1 Hz aligned to the main_loop tick (so they
join cleanly to signal_log on ts_ms):

  depth_log_<date>.csv  — 51 columns. Top-5 bid + top-5 ask levels for
    YES and NO sides as (price, size) pairs, plus aggregates:
    bid_depth_5, ask_depth_5, imbalance_5 = (bid_depth-ask_depth)/total,
    book_age_s. Source: existing Polymarket WS `book` events.

  flow_log_<date>.csv   — 25 columns. Per-side trade flow over rolling
    windows of 20s and 120s: n, buy_vol_usdc, sell_vol_usdc, net_flow,
    vwap, last_fill_ts_ms. Source: Polymarket WS `last_trade_price`
    events that v5.5.31's on_message silently dropped.

  No new HTTP calls. No new REST polling. Both feeds derive from the
  existing Polymarket Market WS subscription. Disk overhead ~80–100 MB/day.

  Side inference for trade events is defensive: use event.get('side') if
  Polymarket sends it; otherwise classify by price vs current top-of-book
  (price ≥ ask → BUY, ≤ bid → SELL, inside spread → nearer to mid). One
  diagnostic log line per token on the first trade event seen, then quiet.

  v5.6.0 was logging-only. Strategy/entry/exit paths were unchanged in
  that version — v5.7.0 is the first change to exit logic.

CARRIED FORWARD FROM v5.5.31:
  Two new columns in signal_log (market_open_btc, delta_from_start_pct)
  and CsvLogger schema-mismatch rotation to <name>_<date>.v1.csv.

CARRIED FORWARD FROM v5.5.30:
  Two bug fixes — Guard 1 slug-naming invariant fixed (drift now correctly
  computed against ts + MARKET_INTERVAL_S, not ts), and the resolution-
  thread hard timeout (1800s) moved to top of poll loop so it executes
  even when external API calls fail.

CARRIED FORWARD FROM v5.5.29:
  Three defensive guards (slug-naming invariant, pre-entry market-active
  gate, stuck-cycle detector). Dashboard CSV logs panel + endpoints.

CARRIED FORWARD FROM v5.5.28:
  _next_resolution_boundaries returns current_b (the active market's
  slug timestamp), not next_b.

CARRIED FORWARD FROM v5.5.27:
  MARKET_END_MAX_S = 300 (only active markets pass discovery filter).

CARRIED FORWARD FROM v5.5.26:
  Live books panel + open-position detail panel on the dashboard.

CARRIED FORWARD FROM v5.5.25:
  STICKY market_discovery_thread: stay on selected market until end_ts
  has passed (5s grace).

PRIOR FIXES (v5.5.24-fix, retained):
  1. compute_signal()         : snapshot binance_prices deque before iterating.
  2. _build_status_payload()  : same deque-snapshot fix.
  3. compute_signal()         : SIGNAL_INVERT env var (UP↔DOWN flip).

No other behaviour changes. Strategy gates, validation, exits, resolution
logic, dashboard — all untouched.

Module 2 adds three daemon threads to module 1's scaffolding:
  - Binance WS thread: btcusdt@trade → state.binance_prices
  - Market discovery thread: Gamma API → state.btc_5m_market
  - Polymarket WS thread: subscribed to current market's two tokens
                          → state.poly_books

Heartbeat is enriched to surface feed health:
  [heartbeat] uptime=120s mode=dry binance=OK(180ms,$108,432) poly_ws=OK(420ms)
              market='Bitcoin Up or Down ...' ends_in=85s books=2

Single-file by design.
"""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import signal as signal_module
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

# v5.5.23-cl: Chainlink price stream (Polymarket's free RTDS relay).
# Used as PRIMARY resolution source; Binance is last-resort fallback.
# Fail-safe import — if module missing, bot continues with Binance only.
try:
    import chainlink_stream_log
    _CHAINLINK_AVAILABLE = True
except Exception as _cl_e:
    chainlink_stream_log = None
    _CHAINLINK_AVAILABLE = False
    print(f"[chainlink] module unavailable: {_cl_e}", flush=True)


# v5.5.24-fix: Read SIGNAL_INVERT once at import time. Cheap to check per-tick.
_SIGNAL_INVERT = os.environ.get("SIGNAL_INVERT", "false").strip().lower() in ("1", "true", "yes")

# v5.7.0: single source of truth for the bot's version string. Used in the
# boot banner, /api/status payload, and dashboard header so all three stay
# in sync. Bump this on every release.
BOT_VERSION = "6.3.19-ppmp"


# ═══════════════════════════════════════════════════════════════════
# v6.5.11 TIERED EXIT LADDER — ported onto the v6.3.12 base.
# Replaces the single-threshold sell-loser (winner≥0.93) with a four-tier
# ladder gated on TTR + winner_ask + swing/dip/sustain guards. Backtested
# on TPS signal_log (~221 markets) to ~5.9 catastrophes/100 (vs ~19/100
# unguarded). When _BS_TIER_ENABLED is True (default), the tiered evaluator
# runs and the BTC late-fallback + late-conviction paths are skipped (the
# ladder's lower tiers handle held-both via pure numbers). Flip
# BS_TIER_ENABLED=false to restore the v621 stack — no redeploy.
# Self-contained parsing (the _f/_s/_b helpers aren't defined yet here).
# ═══════════════════════════════════════════════════════════════════
def _tier_env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _tier_env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        print(f"[boot][v6.5.11] warning: {name}={raw!r} not parseable; "
              f"using default {default}", flush=True)
        return default
    if v < lo or v > hi:
        print(f"[boot][v6.5.11] warning: {name}={v} outside [{lo},{hi}]; "
              f"clamping", flush=True)
        v = max(lo, min(hi, v))
    return v


_BS_TIER_ENABLED           = _tier_env_bool("BS_TIER_ENABLED", True)
_BS_TIER_T0_WINNER         = _tier_env_float("BS_TIER_T0_WINNER", 0.96, 0.50, 1.00)
_BS_TIER_T1_TTR            = _tier_env_float("BS_TIER_T1_TTR", 120.0, 0.0, 300.0)
_BS_TIER_T1_WINNER         = _tier_env_float("BS_TIER_T1_WINNER", 0.90, 0.50, 1.00)
_BS_TIER_T2_TTR            = _tier_env_float("BS_TIER_T2_TTR", 60.0, 0.0, 300.0)
_BS_TIER_T2_WINNER         = _tier_env_float("BS_TIER_T2_WINNER", 0.87, 0.50, 1.00)
_BS_TIER_T3_TTR            = _tier_env_float("BS_TIER_T3_TTR", 30.0, 0.0, 300.0)
_BS_TIER_T3_WINNER         = _tier_env_float("BS_TIER_T3_WINNER", 0.80, 0.50, 1.00)
_BS_TIER_PERSIST_S         = _tier_env_float("BS_TIER_PERSIST_S", 5.0, 0.0, 60.0)
_BS_TIER_T0_MAX_TTR        = _tier_env_float("BS_TIER_T0_MAX_TTR", 200.0, 0.0, 300.0)
_BS_TIER_T0_SUSTAIN_THRESH = _tier_env_float("BS_TIER_T0_SUSTAIN_THRESH", 0.94, 0.50, 1.00)
_BS_TIER_T0_SUSTAIN_S      = _tier_env_float("BS_TIER_T0_SUSTAIN_S", 30.0, 0.0, 120.0)
_BS_TIER_SWING_WINDOW_S    = _tier_env_float("BS_TIER_SWING_WINDOW_S", 30.0, 0.0, 120.0)
_BS_TIER_SWING_DRAWDOWN    = _tier_env_float("BS_TIER_SWING_DRAWDOWN", 0.05, 0.0, 0.50)
_BS_TIER_SWING_BOUNCE      = _tier_env_float("BS_TIER_SWING_BOUNCE", 0.02, 0.0, 0.50)
_BS_TIER_DIP_WINDOW_S      = _tier_env_float("BS_TIER_DIP_WINDOW_S", 60.0, 0.0, 120.0)
_BS_TIER_DIP_FLOOR         = _tier_env_float("BS_TIER_DIP_FLOOR", 0.65, 0.0, 1.00)
# Derived: how much ask-history to retain (covers the widest guard window).
_BS_TIER_HISTORY_MAX_S = max(_BS_TIER_SWING_WINDOW_S, _BS_TIER_DIP_WINDOW_S,
                             _BS_TIER_T0_SUSTAIN_S, _BS_TIER_PERSIST_S) + 5.0


# v5.6.0: depth + flow observability constants. All used only by new
# logging paths; entry/exit/validation logic does not consult them.
DEPTH_LEVELS = 5                 # top-N book levels captured per side
FLOW_WINDOW_SHORT_S = 20.0       # short rolling window for trade-flow aggregation
FLOW_WINDOW_LONG_S = 120.0       # long rolling window
POLY_TRADES_BUFFER = 5000        # per-token deque maxlen (>> 120s of trades on busy markets)


# v5.7.0: take-profit env vars + reserved hooks. All default to OFF so a
# deploy of v5.7.0 with no env-var changes is behaviorally identical to
# v5.6.0 (no early exits trigger). Set TAKE_PROFIT_THRESHOLD>0 to enable.
# v5.8.0: stop-loss env vars added; STOP_LOSS_THRESHOLD now actually
# implemented (was reserved hook in v5.7.0). BLOCK_REENTRY_AFTER_EXIT added.
def _read_tp_env() -> Tuple[float, float, float, float, float, float, bool,
                            str, float, float, float, float]:
    def _f(name: str, default: float, lo: float, hi: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            print(f"[boot] warning: {name}={raw!r} not parseable; using default {default}",
                  flush=True)
            return default
        if v < lo or v > hi:
            print(f"[boot] warning: {name}={v} outside safe range [{lo},{hi}]; "
                  f"clamping to {default}", flush=True)
            return default
        return v
    def _b(name: str, default: bool) -> bool:
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes")
    def _s(name: str, default: str, allowed: Tuple[str, ...]) -> str:
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            return default
        if raw not in allowed:
            print(f"[boot] warning: {name}={raw!r} not in {allowed}; using default {default!r}",
                  flush=True)
            return default
        return raw
    threshold = _f("TAKE_PROFIT_THRESHOLD", 0.0, 0.0, 0.5)
    persist_s = _f("TAKE_PROFIT_PERSIST_S", 5.0, 1.0, 60.0)
    stop_loss = _f("STOP_LOSS_THRESHOLD", 0.0, 0.0, 0.5)
    sl_persist = _f("STOP_LOSS_PERSIST_S", 5.0, 1.0, 60.0)
    sl_min_entry = _f("STOP_LOSS_MIN_ENTRY", 0.30, 0.0, 0.99)
    trail_drop = _f("TRAILING_DROP", 0.0, 0.0, 0.5)
    block_reentry = _b("BLOCK_REENTRY_AFTER_EXIT", True)
    # v5.8.1: late-stage stop-loss. Two modes (pct, abs) with shared time
    # window and persistence requirement. Default mode "" disables LATE-SL
    # entirely so v5.8.1 deploy with no env changes is identical to v5.8.0.
    sl_late_mode = _s("SL_LATE_MODE", "", ("", "pct", "abs"))
    sl_late_pct = _f("SL_LATE_PCT", 0.50, 0.0, 1.0)
    sl_late_floor = _f("SL_LATE_FLOOR", 0.10, 0.0, 0.99)
    sl_late_window = _f("SL_LATE_WINDOW_S", 60.0, 1.0, 300.0)
    sl_late_persist = _f("SL_LATE_PERSIST_S", 1.0, 1.0, 60.0)
    return (threshold, persist_s, stop_loss, sl_persist, sl_min_entry,
            trail_drop, block_reentry,
            sl_late_mode, sl_late_pct, sl_late_floor, sl_late_window, sl_late_persist)

(_TP_THRESHOLD, _TP_PERSIST_S, _STOP_LOSS_THRESHOLD,
 _SL_PERSIST_S, _SL_MIN_ENTRY, _TRAILING_DROP, _BLOCK_REENTRY,
 _SL_LATE_MODE, _SL_LATE_PCT, _SL_LATE_FLOOR,
 _SL_LATE_WINDOW_S, _SL_LATE_PERSIST_S) = _read_tp_env()


# v6.1.0: STRATEGY_MODE switch + both-sides strategy + multi-duration logging.
# Default is "lag_signal" so a v6.1.0 deploy with no STRATEGY_MODE env var is
# behaviorally byte-equivalent to v5.8.1 on the trading path. Setting
# STRATEGY_MODE="both_sides_btc" activates the new code path:
#   - 5m BTC markets: trade both YES + NO legs at entry, sell loser when
#     winner ask >= BS_SELL_LOSER_THRESHOLD with TTR floor + persistence +
#     min loser bid preconditions met. No directional signal.
#   - 15m + 60m BTC markets: pure logging — capture top-of-book at the
#     "entry window" TTR (10-15 min before resolution) into a new CSV.
#     No trades placed.
#   - Both 5m trading and 15m/60m logging are BTC-only.
# All v6.1.0 env vars are validated and clamped at boot. Bad values fall
# back to defaults and emit a warning. The new path runs alongside the
# v5.8.1 lag-signal path threads (signal/TP) which become no-ops via
# explicit early-return guards.
def _read_v610_env() -> Tuple[
    str,    # strategy_mode
    float, float,    # bs_lead_time_min_s, bs_lead_time_max_s
    float,           # bs_sum_ask_max
    float,           # bs_sell_loser_threshold
    float,           # bs_sell_loser_ttr_floor_s
    float,           # bs_sell_loser_persist_s
    float,           # bs_sell_loser_min_loser_bid
    float,           # bs_min_btc_delta_usd     (v6.2.0)
    float,           # bs_btc_late_threshold_usd (v6.2.0)
    float,           # bs_late_conv_ttr_s       (v6.2.1)
    float,           # bs_late_conv_winner_thresh (v6.2.1)
    float,           # bs_late_conv_min_btc_usd  (v6.2.1)
    str,             # bs_strategy              (v6.2.2)
    float,           # bs_vl_arm_threshold      (v6.2.4)
    float,           # bs_vl_drop_tolerance     (v6.2.4)
    str, str,        # log_15m_slug_prefix, log_60m_slug_prefix
    float,           # log_window_min_s
    float,           # log_window_max_s
    float,           # log_sample_interval_s
]:
    def _f(name: str, default: float, lo: float, hi: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            print(f"[boot][v6.1.0] warning: {name}={raw!r} not parseable; "
                  f"using default {default}", flush=True)
            return default
        if v < lo or v > hi:
            print(f"[boot][v6.1.0] warning: {name}={v} outside [{lo},{hi}]; "
                  f"clamping to default {default}", flush=True)
            return default
        return v
    def _s(name: str, default: str, allowed: Tuple[str, ...]) -> str:
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            return default
        if raw not in allowed:
            print(f"[boot][v6.1.0] warning: {name}={raw!r} not in {allowed}; "
                  f"using default {default!r}", flush=True)
            return default
        return raw
    def _str(name: str, default: str) -> str:
        raw = os.environ.get(name, "").strip()
        return raw if raw else default

    strategy_mode = _s("STRATEGY_MODE", "lag_signal",
                       ("lag_signal", "both_sides_btc"))

    # Lead-time window: enter both legs when 5m market TTR is in
    # [BS_LEAD_TIME_MIN_S, BS_LEAD_TIME_MAX_S]. Default 600-900 = 10-15 min
    # before resolution. Same window used as "entry-window" TTR for 15m/60m
    # logging (so the data we collect is what we'd have seen at entry time
    # if we were also trading those durations).
    bs_lead_min = _f("BS_LEAD_TIME_MIN_S", 600.0, 60.0, 3600.0)
    bs_lead_max = _f("BS_LEAD_TIME_MAX_S", 900.0, 60.0, 3600.0)
    if bs_lead_min >= bs_lead_max:
        print(f"[boot][v6.1.0] warning: BS_LEAD_TIME_MIN_S ({bs_lead_min}) "
              f">= BS_LEAD_TIME_MAX_S ({bs_lead_max}); resetting to 600/900",
              flush=True)
        bs_lead_min, bs_lead_max = 600.0, 900.0

    bs_sum_ask_max = _f("BS_SUM_ASK_MAX", 1.03, 1.00, 1.20)
    bs_sell_thresh = _f("BS_SELL_LOSER_THRESHOLD", 0.93, 0.50, 0.99)
    bs_sell_ttr_floor = _f("BS_SELL_LOSER_TTR_FLOOR_S", 75.0, 0.0, 300.0)
    bs_sell_persist = _f("BS_SELL_LOSER_PERSIST_S", 5.0, 0.0, 60.0)
    bs_sell_min_bid = _f("BS_SELL_LOSER_MIN_LOSER_BID", 0.05, 0.0, 0.50)
    # v6.2.0: BTC-confirmation guard. PROD's existing book-based sell-loser fire
    # additionally requires |btc_now - btc_strike| ≥ this many USD. Set to 0 to
    # disable (= v6.1.x behavior). Default 30 — derived from May 3 catastrophe
    # analysis where all 5 catastrophes fired with |delta| ≤ $30.
    bs_min_btc_delta = _f("BS_MIN_BTC_DELTA_USD", 30.0, 0.0, 500.0)
    # v6.2.0: BTC late-fallback sell-loser. When TTR ≤ 60s and
    # |btc_now - btc_strike| ≥ this many USD, fire on the BTC-implied loser side
    # regardless of book conviction. Set to 999999 to disable. Default 80 —
    # captures held-both markets with sharp final-minute BTC moves.
    bs_btc_late_thresh = _f("BS_BTC_LATE_THRESHOLD_USD", 80.0, 0.0, 999999.0)
    # v6.2.1: Late-conviction override sell-loser. When TTR is extremely short
    # AND book is overwhelmingly confident AND BTC weakly supports, fire and
    # bypass the main BTC guard ($30). Captures held-both markets where book
    # conviction is overwhelming but BTC is below the standard guard threshold.
    # Disable any of these by setting them to 0.
    bs_late_conv_ttr_s = _f("BS_LATE_CONV_TTR_S", 5.0, 0.0, 60.0)
    bs_late_conv_winner_thresh = _f("BS_LATE_CONV_WINNER_THRESHOLD", 0.98, 0.50, 1.00)
    bs_late_conv_min_btc = _f("BS_LATE_CONV_MIN_BTC_USD", 10.0, 0.0, 500.0)

    # v6.2.2: BS_STRATEGY selects which sell-loser logic is active.
    # Values:
    #   "v621" (default) — full v6.2.1 stack: PROD path + BTC late-fallback +
    #                       late-conviction override + BTC guard. Production bot.
    #   "verification_late" — pure BTC-tiered logic, no book check, no BTC
    #                          guard. Fires only:
    #                            - TTR ≤ 60s + |BTC Δ| ≥ $90  (Phase B)
    #                            - TTR ≤ 30s + |BTC Δ| ≥ $85  (Phase C)
    #                            - TTR ≤ 10s + |BTC Δ| ≥ $80  (Phase D)
    #                          Designed for side-by-side A/B testing on a second
    #                          Railway service (DRY only, same markets, same entry).
    #   "bss_entry" (v6.3.0) — Both-Sides See-Saw entry. REPLACES the standard
    #                          sum_ask<1.10 entry path AND replaces verification_late.
    #                          Bot watches every 5m market and waits for ONE side
    #                          to dip below BS_BSS_T_FIRST sustained for
    #                          BS_BSS_SUSTAIN_FIRST_S seconds → buy first leg.
    #                          Then waits for OTHER side to dip below
    #                          BS_BSS_T_SECOND_STRICT (or relaxed threshold after
    #                          BS_BSS_RELAX_AT_S) sustained for
    #                          BS_BSS_SUSTAIN_SECOND_S seconds → buy second leg.
    #                          Holds both to resolution. If second never confirms
    #                          by BS_BSS_ABORT_AT_S, sells first leg at current bid.
    #                          Mirror-of-verification_late structure: sustain →
    #                          fire pattern, just inverted (buy low instead of
    #                          confirm high). DRY-only.
    bs_strategy_raw = os.environ.get("BS_STRATEGY", "v621").strip().lower()
    if bs_strategy_raw not in ("v621", "verification_late", "bss_entry"):
        print(f"[boot][v6.2.2] warning: BS_STRATEGY={bs_strategy_raw!r} "
              f"not recognized; using default 'v621'", flush=True)
        bs_strategy_raw = "v621"
    bs_strategy = bs_strategy_raw

    # v6.2.4: verification_late freeze logic (whipsaw detection).
    # vl_arm_thresh: at TTR ≤ 60s, if winner_ask ≥ this, ARM the verification.
    #                Below this and the verification stays disarmed (= hold-both).
    # vl_drop_tol:   once armed, if winner_ask drops more than this many points
    #                below its peak (since arming), FREEZE permanently.
    # When BS_STRATEGY is not 'verification_late', these vars are inert.
    bs_vl_arm_thresh = _f("BS_VL_ARM_THRESHOLD", 0.70, 0.50, 0.99)
    bs_vl_drop_tol = _f("BS_VL_DROP_TOLERANCE", 0.03, 0.0, 0.50)

    # v6.3.0: BSS (Both-Sides See-Saw) parameters. Inert unless
    # BS_STRATEGY == 'bss_entry'.
    bs_bss_t_first         = _f("BS_BSS_T_FIRST",          0.45, 0.10, 0.50)
    bs_bss_sustain_first_s = _f("BS_BSS_SUSTAIN_FIRST_S",  4.0,  0.0,  30.0)
    bs_bss_t_second_strict = _f("BS_BSS_T_SECOND_STRICT",  0.50, 0.30, 0.99)
    bs_bss_t_second_relax  = _f("BS_BSS_T_SECOND_RELAXED", 0.62, 0.30, 0.99)
    bs_bss_sustain_2nd_s   = _f("BS_BSS_SUSTAIN_SECOND_S", 3.0,  0.0,  30.0)
    bs_bss_relax_at_s      = _f("BS_BSS_RELAX_AT_S",       240.0, 1.0, 280.0)
    bs_bss_abort_at_s      = _f("BS_BSS_ABORT_AT_S",       270.0, 5.0, 300.0)
    # v6.3.1: BTC-velocity first-leg filter. At first-leg fire moment,
    # compute BTC % change over last 30s. If BTC is moving WITH the
    # buy side (= we'd be buying the temporary winner), skip the fire.
    # On May 5-6 depth_log: 95 "with-BTC" fires (67% second-leg confirm)
    # vs 32 "against-BTC" fires (88% confirm) vs 36 "neutral" (78%).
    # Default 0.02 (= 2bps) blocks strong-with moves only. Set 0.0 to
    # disable the filter entirely (= v6.3.0 behavior).
    bs_bss_btc_vel_filter = _f("BS_BSS_BTC_VEL_FILTER_PCT", 0.02, 0.0, 1.0)
    bs_bss_btc_vel_lookback_s = _f("BS_BSS_BTC_VEL_LOOKBACK_S", 30.0, 5.0, 120.0)

    # v6.3.7: PATIENT SECOND LEG. When the opposite side hits the strict
    # threshold (0.50), don't fire immediately if the price is still
    # actively falling — wait one more tick for a better fill. The bot
    # checks the opposite-side ask velocity over the last
    # OPP_VEL_LOOKBACK_S seconds. If price has dropped by at least
    # OPP_VEL_PATIENT_DROP in that window, it's "still falling" and we
    # wait. If the price is flat or rising, we fire (we caught the bottom).
    #
    # Floor backstop: if opposite side ever hits T_SECOND_FLOOR or below
    # (default 0.40), fire IMMEDIATELY regardless of velocity. The dip is
    # so deep that risking a bounce above 0.50 is worse than firing now.
    #
    # Set OPP_VEL_PATIENT_DROP=0 to disable patience (= v6.3.6 behavior).
    bs_bss_t_second_floor = _f("BS_BSS_T_SECOND_FLOOR", 0.40, 0.10, 0.50)
    bs_bss_opp_vel_lookback_s = _f("BS_BSS_OPP_VEL_LOOKBACK_S", 10.0, 2.0, 60.0)
    bs_bss_opp_vel_patient_drop = _f("BS_BSS_OPP_VEL_PATIENT_DROP", 0.005, 0.0, 0.5)

    # v6.3.2: PRE-MARKET BSS phase. Polymarket creates 5m markets ~30 min
    # before the window opens. Books form, prices wobble, sometimes one
    # side drops below $0.49 in this period. We can buy then. To use this
    # phase you also need to extend BS_LEAD_TIME_MAX_S to 1800 (or
    # whatever pre-market window you want covered).
    #
    # Pre-market thresholds are LOOSER than live (0.49 vs 0.45 first leg)
    # because pre-market dips tend to be shallower — books are thinner,
    # market makers haven't tightened yet.
    #
    # No abort timer during pre-market (time is abundant). When the live
    # window opens (T=0) and we're still WAITING_2ND, the bot switches to
    # standard live thresholds (0.50/0.62) and starts the abort timer
    # from T=0 (not from pre-market first-leg fill).
    #
    # If neither side ever dipped below T_FIRST_PRE during the entire
    # pre-market period, the bot enters the live window in WATCH state
    # and runs standard BSS logic.
    bs_bss_t_first_pre   = _f("BS_BSS_T_FIRST_PRE",   0.49, 0.10, 0.99)
    bs_bss_t_second_pre  = _f("BS_BSS_T_SECOND_PRE",  0.49, 0.10, 0.99)
    bs_bss_sustain_first_pre_s  = _f("BS_BSS_SUSTAIN_FIRST_PRE_S", 4.0, 0.0, 60.0)
    bs_bss_sustain_second_pre_s = _f("BS_BSS_SUSTAIN_SECOND_PRE_S", 3.0, 0.0, 60.0)

    # v6.3.2: Fast BSS tick interval. The BSS evaluator runs in its own
    # thread at this cadence (default 20Hz = 50ms). The main_loop still
    # runs at 1Hz for everything else; only BSS gets the fast loop.
    bs_bss_tick_interval_s = _f("BS_BSS_TICK_INTERVAL_S", 0.05, 0.005, 1.0)

    # Slug prefixes for 15m + 60m. Polymarket convention from past sessions
    # is btc-updown-{Nm}-{ts}. The 60m slug is unverified — Railway env vars
    # let you correct it without code change if discovery returns 0 markets.
    log_15m_prefix = _str("LOG_15M_SLUG_PREFIX", "btc-updown-15m-")
    log_60m_prefix = _str("LOG_60M_SLUG_PREFIX", "btc-updown-60m-")

    # How wide a window around the "entry-window TTR" do we sample for
    # logging? Default: same as bs_lead_min..bs_lead_max above (so 15m/60m
    # books are captured at TTR 600-900s, the same lead-time we'd enter
    # if we were trading them). Overridable separately if needed.
    log_window_min = _f("LOG_WINDOW_MIN_S", bs_lead_min, 60.0, 3600.0)
    log_window_max = _f("LOG_WINDOW_MAX_S", bs_lead_max, 60.0, 3600.0)
    if log_window_min >= log_window_max:
        log_window_min, log_window_max = bs_lead_min, bs_lead_max

    # Sample once every N seconds while a market is in the logging window.
    # Default 30s = ~10-30 rows per (market × duration) over the full window.
    log_interval = _f("LOG_SAMPLE_INTERVAL_S", 30.0, 1.0, 600.0)

    return (strategy_mode,
            bs_lead_min, bs_lead_max,
            bs_sum_ask_max,
            bs_sell_thresh, bs_sell_ttr_floor, bs_sell_persist, bs_sell_min_bid,
            bs_min_btc_delta, bs_btc_late_thresh,
            bs_late_conv_ttr_s, bs_late_conv_winner_thresh, bs_late_conv_min_btc,
            bs_strategy,
            bs_vl_arm_thresh, bs_vl_drop_tol,
            bs_bss_t_first, bs_bss_sustain_first_s,
            bs_bss_t_second_strict, bs_bss_t_second_relax, bs_bss_sustain_2nd_s,
            bs_bss_relax_at_s, bs_bss_abort_at_s,
            bs_bss_btc_vel_filter, bs_bss_btc_vel_lookback_s,
            bs_bss_t_second_floor, bs_bss_opp_vel_lookback_s, bs_bss_opp_vel_patient_drop,
            bs_bss_t_first_pre, bs_bss_t_second_pre,
            bs_bss_sustain_first_pre_s, bs_bss_sustain_second_pre_s,
            bs_bss_tick_interval_s,
            log_15m_prefix, log_60m_prefix,
            log_window_min, log_window_max, log_interval)


(_STRATEGY_MODE,
 _BS_LEAD_MIN_S, _BS_LEAD_MAX_S,
 _BS_SUM_ASK_MAX,
 _BS_SELL_THRESH, _BS_SELL_TTR_FLOOR_S, _BS_SELL_PERSIST_S, _BS_SELL_MIN_BID,
 _BS_MIN_BTC_DELTA_USD, _BS_BTC_LATE_THRESHOLD_USD,
 _BS_LATE_CONV_TTR_S, _BS_LATE_CONV_WINNER_THRESHOLD, _BS_LATE_CONV_MIN_BTC_USD,
 _BS_STRATEGY,
 _BS_VL_ARM_THRESHOLD, _BS_VL_DROP_TOLERANCE,
 _BS_BSS_T_FIRST, _BS_BSS_SUSTAIN_FIRST_S,
 _BS_BSS_T_SECOND_STRICT, _BS_BSS_T_SECOND_RELAXED, _BS_BSS_SUSTAIN_SECOND_S,
 _BS_BSS_RELAX_AT_S, _BS_BSS_ABORT_AT_S,
 _BS_BSS_BTC_VEL_FILTER_PCT, _BS_BSS_BTC_VEL_LOOKBACK_S,
 _BS_BSS_T_SECOND_FLOOR, _BS_BSS_OPP_VEL_LOOKBACK_S, _BS_BSS_OPP_VEL_PATIENT_DROP,
 _BS_BSS_T_FIRST_PRE, _BS_BSS_T_SECOND_PRE,
 _BS_BSS_SUSTAIN_FIRST_PRE_S, _BS_BSS_SUSTAIN_SECOND_PRE_S,
 _BS_BSS_TICK_INTERVAL_S,
 _LOG_15M_PREFIX, _LOG_60M_PREFIX,
 _LOG_WINDOW_MIN_S, _LOG_WINDOW_MAX_S, _LOG_SAMPLE_INTERVAL_S
 ) = _read_v610_env()

_BS_ACTIVE = (_STRATEGY_MODE == "both_sides_btc")

# ── v6.3.14 PPMP: managed-bail config ───────────────────────────────
# Read directly from env (not threaded through the v610 config tuple) to
# keep the change isolated and avoid mis-aligning that large unpack.
def _ppmp_f_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except Exception:
        v = default
    return min(max(v, lo), hi)

# Managed-bail window after the live window opens (the first N seconds in
# which we try to sell-up an unpaired leg-1 before timing out to the bid).
_BS_BSS_BAIL_WINDOW_S  = _ppmp_f_env("BS_BSS_BAIL_WINDOW_S", 30.0, 1.0, 300.0)
# Hard cashout floor on leg-1's BID: drop below this → dump immediately.
_BS_BSS_BAIL_HARD_STOP = _ppmp_f_env("BS_BSS_BAIL_HARD_STOP", 0.46, 0.0, 0.99)
# Leg-2 "open grace": at the trade open we'll still complete the pair if
# the other side is available up to this ask (your "top 0.51 at start").
_BS_BSS_T_SECOND_OPEN  = _ppmp_f_env("BS_BSS_T_SECOND_OPEN", 0.51, 0.30, 0.99)
# Fee fidelity for the bail ONLY (rest of the DRY P&L is fee-free in this
# build). Polymarket crypto taker fee = rate * p * (1-p) per share; maker
# fills (sold-up) pay 0. Controlled by the existing env vars.
_PPMP_USE_FEE = (os.environ.get("BS_USE_POLYMARKET_FEE_FORMULA", "false")
                 .strip().lower() in ("1", "true", "yes", "on"))
_PPMP_TAKER_FEE_RATE = _ppmp_f_env("BS_POLYMARKET_TAKER_FEE_RATE", 0.07, 0.0, 1.0)

def _ppmp_taker_fee(qty: float, price: float) -> float:
    """USDC taker fee for a fill of `qty` shares at `price` (crypto curve).
    Zero when the fee formula is disabled. Peaks at p=0.50."""
    if not _PPMP_USE_FEE or qty <= 0 or price <= 0 or price >= 1:
        return 0.0
    return qty * _PPMP_TAKER_FEE_RATE * price * (1.0 - price)

# v6.3.16 PPMP strategy guard: leg-1 is a PRE-MARKET entry only. Never open a
# fresh first leg inside the live 5-min window (no time left to complete the
# pair → it would just force an immediate bail). Default True = on-strategy;
# set "false" only to restore the old behavior of also legging in live.
_BS_BSS_LEG1_PREMARKET_ONLY = (
    os.environ.get("BS_BSS_LEG1_PREMARKET_ONLY", "true").strip().lower()
    in ("1", "true", "yes", "on"))

# v6.3.18 real-dip filter: a 1¢ book always quotes one side at 0.495 and the
# other at 0.505 — that's structure, not opportunity. To filter the structural
# quote and only fire on actual price action, require the candidate side's
# current ask to be at least REAL_DIP_DROP below its own max ask within the
# last REAL_DIP_LOOKBACK_S seconds. A side sitting at 0.495 forever has
# max==0.495 → drop==0 → no fire. A side that was at 0.505 and dropped to
# 0.495 has drop==0.010 → fires. Set DROP=0 to disable.
_BS_BSS_REAL_DIP_DROP = _ppmp_f_env("BS_BSS_REAL_DIP_DROP", 0.010, 0.0, 0.50)
_BS_BSS_REAL_DIP_LOOKBACK_S = _ppmp_f_env("BS_BSS_REAL_DIP_LOOKBACK_S", 30.0, 1.0, 600.0)

def _bss_recent_max_ask(samples, side_idx: int, now: float,
                         lookback_s: float):
    """Max ask for one side within the last lookback_s seconds. side_idx
    1=YES, 2=NO. samples are (ts, yes_ask, no_ask) appended in order. Returns
    None if no samples fall in the window."""
    cutoff = now - lookback_s
    m = None
    for ts, ya, na in reversed(samples):
        if ts < cutoff:
            break
        a = ya if side_idx == 1 else na
        if m is None or a > m:
            m = a
    return m

# v6.3.17 PPMP fallback: if a market never gave a cheap leg-1 (still WATCH)
# by FALLBACK_LEAD_S seconds before the live window opens, take the pair at
# market anyway — buy BOTH sides at the current ask (a flat ~1.00 pair, e.g.
# 0.49+0.51 / 0.50+0.50 / 0.50+0.51) so we don't miss the market. The flat
# pair is then held to expiry on the existing loser-cut exit stack. Guarded
# by a max-sum so we never take a blown-out book.
_BS_BSS_FALLBACK_BOTH_ENABLED = (
    os.environ.get("BS_BSS_FALLBACK_BOTH_ENABLED", "true").strip().lower()
    in ("1", "true", "yes", "on"))
_BS_BSS_FALLBACK_LEAD_S = _ppmp_f_env("BS_BSS_FALLBACK_LEAD_S", 10.0, 1.0, 120.0)
_BS_BSS_FALLBACK_MAX_SUM = _ppmp_f_env("BS_BSS_FALLBACK_MAX_SUM", 1.04, 1.0, 1.20)





# ═══════════════════════════════════════════════════════════════════
# v6.2.5: BOT_NAME, SKIP_END_MINUTES, LOG_RETENTION_DAYS
# ═══════════════════════════════════════════════════════════════════
# All three env vars default to inert values, so a v6.2.5 deploy with no
# new env-vars set is behaviorally identical to v6.2.4.
#
# BOT_NAME: identifier folded into log subdir AND dashboard download
#   filename. Two bots running on shared infra (same volume / same chat
#   download dir) no longer overwrite each other's CSVs. Normalized to
#   lowercase [a-z0-9_]+; invalid chars stripped. Empty = legacy behavior.
#
# SKIP_END_MINUTES: comma-separated minutes (0-59 UTC) on which to refuse
#   both-sides entry. Filter applied early in _bs_should_enter, before
#   book lookup. Tracks May 3-5 catastrophe analysis findings:
#     Money Looser deploy:    SKIP_END_MINUTES=0
#     Wastefull Son deploy:   SKIP_END_MINUTES=10,20,25,45,50
#   Empty = no filter. Lag-signal mode ignores this var entirely.
#
# LOG_RETENTION_DAYS: int, default 0 = disabled (keep forever). When ≥ 1,
#   on boot and every 24h thereafter, deletes <dataset>_<YYYY-MM-DD>.csv
#   files in the bot's log_dir whose date is older than N days (UTC).
#   Hard safety buffer: never deletes today's or yesterday's files even
#   if LOG_RETENTION_DAYS=1.

def _normalize_bot_name(raw: str) -> str:
    """Return a filesystem-safe lowercase identifier or '' if input is empty.
    Strips characters outside [a-z0-9_]; if nothing usable remains, returns ''
    so we fall back to legacy non-isolated path."""
    raw = (raw or "").strip().lower()
    cleaned = "".join(c if (c.isalnum() or c == "_") else "" for c in raw)
    return cleaned

_BOT_NAME = _normalize_bot_name(os.environ.get("BOT_NAME", ""))
if os.environ.get("BOT_NAME", "").strip() and not _BOT_NAME:
    print(f"[boot][v6.2.5] warning: BOT_NAME={os.environ.get('BOT_NAME')!r} "
          f"contained no [a-z0-9_] chars after normalization; ignoring",
          flush=True)


def _parse_skip_end_minutes(raw: str) -> Set[int]:
    """Parse comma-separated minute values into a set of ints in [0,59].
    Tokens that don't parse or are out of range are dropped with a warning."""
    out: Set[int] = set()
    raw = (raw or "").strip()
    if not raw:
        return out
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            m = int(tok)
        except ValueError:
            print(f"[boot][v6.2.5] warning: SKIP_END_MINUTES token {tok!r} "
                  f"not an integer; ignoring", flush=True)
            continue
        if 0 <= m < 60:
            out.add(m)
        else:
            print(f"[boot][v6.2.5] warning: SKIP_END_MINUTES token {m} "
                  f"outside [0,59]; ignoring", flush=True)
    return out

_SKIP_END_MINUTES: Set[int] = _parse_skip_end_minutes(
    os.environ.get("SKIP_END_MINUTES", ""))


def _parse_retention_days(raw: str) -> int:
    """Return retention days as int >= 0. 0 = disabled. Out-of-range or
    unparseable values fall back to 0 with a warning."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
    except ValueError:
        print(f"[boot][v6.2.5] warning: LOG_RETENTION_DAYS={raw!r} "
              f"not parseable; using 0 (disabled)", flush=True)
        return 0
    if v < 0:
        print(f"[boot][v6.2.5] warning: LOG_RETENTION_DAYS={v} negative; "
              f"using 0 (disabled)", flush=True)
        return 0
    if v > 3650:
        print(f"[boot][v6.2.5] warning: LOG_RETENTION_DAYS={v} > 3650; "
              f"clamping to 3650", flush=True)
        return 3650
    return v

_LOG_RETENTION_DAYS: int = _parse_retention_days(
    os.environ.get("LOG_RETENTION_DAYS", ""))


# ═══════════════════════════════════════════════════════════════════
# CSV LOGGER (queued, non-blocking, daily rotation)
# ═══════════════════════════════════════════════════════════════════

class CsvLogger:
    """
    Queue-fed CSV logger. Producer threads call .log(row) — never blocks.
    A dedicated writer thread drains the queue and writes to a daily-rotated
    file under <base_dir>/<dataset>_<YYYY-MM-DD>.csv.

    Headers are written automatically when a new daily file is created.
    """

    def __init__(self, base_dir: Path, dataset: str, header: List[str],
                 max_queue: int = 20000):
        self.base_dir = Path(base_dir)
        self.dataset = dataset
        self.header = header
        self.queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._current_date: Optional[str] = None
        self._current_file = None
        self._current_writer = None
        self._dropped = 0
        self._written = 0
        self.enabled = True

    def log(self, row: List) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                print(f"[csv] {self.dataset}: queue full, dropped {self._dropped} rows total",
                      flush=True)

    def stats(self) -> Dict[str, int]:
        return {
            "written": self._written,
            "dropped": self._dropped,
            "queued": self.queue.qsize(),
        }

    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _path_for_date(self, date_str: str) -> Path:
        return self.base_dir / f"{self.dataset}_{date_str}.csv"

    def _ensure_writer(self) -> None:
        today = self._today_str()
        if today == self._current_date and self._current_writer is not None:
            return
        if self._current_file is not None:
            try:
                self._current_file.flush()
                self._current_file.close()
            except Exception:
                pass
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for_date(today)
        need_header = not path.exists() or path.stat().st_size == 0

        # v5.5.31: schema-mismatch detection. If the daily file already
        # exists with a DIFFERENT header (e.g. a deploy mid-day extended
        # the schema), rotate the old file to <name>_<date>.v1.csv and
        # start a fresh file with the new header. Otherwise we'd append
        # ragged-width rows to the existing CSV, breaking downstream parsers.
        if not need_header:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                expected_first_line = ",".join(self.header)
                if first_line and first_line != expected_first_line:
                    # Header changed. Rotate the file out of the way.
                    rotated = path.with_name(f"{path.stem}.v1{path.suffix}")
                    n = 1
                    while rotated.exists():
                        n += 1
                        rotated = path.with_name(f"{path.stem}.v{n}{path.suffix}")
                    path.rename(rotated)
                    print(f"[csv] {self.dataset}: header changed, rotated {path.name} → {rotated.name}",
                          flush=True)
                    need_header = True
            except Exception as e:
                print(f"[csv] {self.dataset}: header check failed ({e}); appending anyway",
                      flush=True)

        self._current_file = open(path, "a", newline="", encoding="utf-8")
        self._current_writer = csv.writer(self._current_file)
        if need_header:
            self._current_writer.writerow(self.header)
            self._current_file.flush()
        self._current_date = today

    def writer_loop(self, kill_check) -> None:
        last_flush = time.time()
        flush_interval = 1.0
        while not kill_check():
            try:
                row = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self._current_file is not None and time.time() - last_flush > flush_interval:
                    try:
                        self._current_file.flush()
                    except Exception:
                        pass
                    last_flush = time.time()
                continue
            try:
                self._ensure_writer()
                self._current_writer.writerow(row)
                self._written += 1
                if time.time() - last_flush > flush_interval:
                    self._current_file.flush()
                    last_flush = time.time()
            except Exception as e:
                print(f"[csv] {self.dataset} write error: {e}", flush=True)
        if self._current_file is not None:
            try:
                self._current_file.flush()
                self._current_file.close()
            except Exception:
                pass


def list_log_files(base_dir: Path) -> List[Dict[str, Any]]:
    if not base_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(base_dir.glob("*.csv"), reverse=True):
        m = re.match(r"^([a-z0-9_]+)_(\d{4}-\d{2}-\d{2})\.csv$", f.name)
        if not m:
            continue
        try:
            stat = f.stat()
        except Exception:
            continue
        out.append({
            "dataset": m.group(1),
            "date": m.group(2),
            "filename": f.name,
            "size_bytes": stat.st_size,
            "modified_ts": stat.st_mtime,
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# v6.1.3: CLOB HTTP HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════════
# Tracks Polymarket REST response codes (gamma + clob) in a rolling
# in-memory deque. Exposed via /api/status as `clob_health`. Dashboard
# shows a red banner when 425-rate (rate-limit signal) > 5% over the
# last 60s. Module-level state because the helper fetch functions
# (_fetch_event_by_slug, _fetch_market_resolution) don't have a state
# parameter — refactoring them all would be a larger change than this
# tracking mechanism warrants.

_CLOB_HTTP_RECENT: Deque[Tuple[float, int]] = deque(maxlen=1000)
_CLOB_HTTP_LOCK: threading.Lock = threading.Lock()


def _record_clob_status(status_code: int) -> None:
    """v6.1.3: append a Polymarket REST response code to the rolling tracker.
    status_code 0 is reserved for network exceptions (request raised before
    a response was received).
    """
    with _CLOB_HTTP_LOCK:
        _CLOB_HTTP_RECENT.append((time.time(), int(status_code)))


def _compute_clob_health(window_s: float = 60.0) -> Dict[str, Any]:
    """v6.1.3: snapshot the CLOB HTTP tracker and compute health stats
    for the dashboard. Returns a dict suitable for direct JSON serialization.
    """
    now = time.time()
    cutoff = now - window_s
    with _CLOB_HTTP_LOCK:
        snapshot = [(ts, s) for ts, s in _CLOB_HTTP_RECENT if ts >= cutoff]
    total = len(snapshot)
    if total == 0:
        return {
            "window_s": window_s, "total": 0,
            "rate_200": 0.0, "rate_425": 0.0, "rate_5xx": 0.0,
            "rate_4xx_other": 0.0, "rate_network_err": 0.0,
            "n_425": 0, "n_5xx": 0, "n_4xx_other": 0, "n_network_err": 0,
            "alert_425": False, "alert_5xx": False,
        }
    n_200 = sum(1 for _, s in snapshot if s == 200)
    n_425 = sum(1 for _, s in snapshot if s == 425)
    n_5xx = sum(1 for _, s in snapshot if 500 <= s < 600)
    n_4xx_other = sum(1 for _, s in snapshot if 400 <= s < 500 and s != 425)
    n_net = sum(1 for _, s in snapshot if s == 0)
    rate_425 = n_425 / total * 100.0
    rate_5xx = n_5xx / total * 100.0
    return {
        "window_s": window_s, "total": total,
        "rate_200": round(n_200 / total * 100.0, 1),
        "rate_425": round(rate_425, 1),
        "rate_5xx": round(rate_5xx, 1),
        "rate_4xx_other": round(n_4xx_other / total * 100.0, 1),
        "rate_network_err": round(n_net / total * 100.0, 1),
        "n_425": n_425,
        "n_5xx": n_5xx,
        "n_4xx_other": n_4xx_other,
        "n_network_err": n_net,
        # Alert: rate > 5% AND >= 3 hits in the window. The count guard
        # prevents a single 425 in a quiet 60s window from triggering
        # the banner (1 of 5 = 20% rate, but only 1 actual hit).
        "alert_425": rate_425 > 5.0 and n_425 >= 3,
        "alert_5xx": rate_5xx > 5.0 and n_5xx >= 3,
    }


# ═══════════════════════════════════════════════════════════════════
# DOMAIN TYPES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MarketInfo:
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_ts: float
    market_url: str
    # v5.5.31: BTC price at market scoring start (end_ts - MARKET_INTERVAL_S).
    # Populated when the market is first selected. None if we couldn't
    # determine it (no Binance samples old enough). Used purely for
    # logging delta_from_start in signal_log; not consulted by entry logic.
    open_btc_price: Optional[float] = None


@dataclass
class PolyBook:
    token_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    last_update_ts: float
    # v5.6.0: depth-logging additions. Populated from `book` event arrays;
    # NOT updated by `price_change` events (next book snapshot resets them).
    # last_book_snapshot_ts is age-of-snapshot, distinct from last_update_ts
    # which also bumps on price_change. Default empty so existing constructor
    # call sites that don't pass these keep working unchanged.
    bid_levels: List[Tuple[float, float]] = field(default_factory=list)
    ask_levels: List[Tuple[float, float]] = field(default_factory=list)
    last_book_snapshot_ts: float = 0.0


@dataclass
class Signal:
    coin: str
    direction: str
    delta_pct: float
    binance_price_now: float
    binance_price_then: float
    computed_ts: float


@dataclass
class Position:
    trade_id: str
    coin: str
    direction: str
    market_id: str
    market_url: str
    token_id: str
    entry_price: float
    size_usdc: float
    entry_ts: float
    edge_at_entry: float
    delta_pct_at_entry: float
    resolution_ts: float
    # v5.7.0: take-profit state tracking. tp_consecutive_ticks counts how
    # many consecutive main_loop ticks the held-side bid has been at or
    # above (entry_price + TAKE_PROFIT_THRESHOLD). Resets to 0 when bid
    # drops below target. When count >= TAKE_PROFIT_PERSIST_S, TP fires.
    # peak_mark tracks the highest bid seen since entry (for TRAILING_DROP
    # hook; unused when trailing is disabled). Defaults make existing
    # Position(...) constructor calls continue to work unchanged.
    tp_consecutive_ticks: int = 0
    peak_mark: float = 0.0
    # v5.8.0: stop-loss state tracking. Counts consecutive ticks where
    # bid <= STOP_LOSS_THRESHOLD (absolute floor). Resets when bid recovers
    # above the floor. When count >= STOP_LOSS_PERSIST_S, SL fires.
    sl_consecutive_ticks: int = 0
    # v5.8.1: late-stage SL state tracking. Independent counter from
    # sl_consecutive_ticks (different rule, different conditions).
    # Increments only when both time-window AND threshold conditions are met.
    sl_late_consecutive_ticks: int = 0


# ─────────────────────────────────────────────────────────────────────
# v6.1.0: both-sides + multi-duration logging dataclasses
# ─────────────────────────────────────────────────────────────────────

@dataclass
class BothSidesLeg:
    """One leg (YES or NO) of a both-sides position. Mirrors the relevant
    subset of Position fields but is part of a BothSidesPosition aggregate."""
    side: str                       # 'YES' or 'NO'
    token_id: str
    entry_ask: float                # the ask we filled at
    entry_bid: float                # bid at entry time (for slippage diag)
    size_usdc: float
    qty_shares: float
    entry_ts: float
    closed: bool = False            # True once leg is settled (sell-loser or resolution)
    close_reason: str = ""          # 'sell_loser' | 'resolved_win' | 'resolved_loss' | 'voided'
    close_price: float = 0.0        # bid we sold at, or 1.0/0.0 on resolution
    close_ts: float = 0.0
    pnl_usdc: float = 0.0           # realized at close
    # v6.1.4: peak bid observed during the leg's lifetime (entry → close).
    # Updated on every tick by both_sides_tick. Diagnostic for sell-loser
    # timing quality: for losers, was there a moment we could have sold
    # higher? For winners, did the bid climb steady or have a drawdown?
    # peak_bid_ts captures WHEN the peak occurred (relative to entry_ts +
    # end_ts in the dashboard) so we can infer trajectory shape.
    peak_bid: float = 0.0           # max bid seen during leg lifetime
    peak_bid_ts: float = 0.0        # when peak_bid was observed


@dataclass
class BothSidesPosition:
    """Both-sides position spanning ONE market. Holds yes_leg + no_leg.
    Lives in state.both_sides_positions keyed by market_id. Removed only
    when both legs are closed (after settle) — a per-cycle settlement
    purge handles cleanup."""
    market_id: str
    market_url: str
    market_question: str
    slug: str
    duration_s: int                 # always 300 for v6.1.0 (5m only); reserved for future
    end_ts: float                   # market resolution timestamp
    entry_ts: float                 # when both legs were placed
    sum_ask_at_entry: float         # yes_ask + no_ask at entry (for diag/CSV)
    yes_leg: BothSidesLeg
    no_leg: BothSidesLeg
    # Sell-loser persistence counter — increments while sell preconditions
    # all pass. Resets to 0 when any precondition fails. When >=
    # _BS_SELL_PERSIST_S (≈5), sell_loser fires.
    sell_loser_consecutive_ticks: int = 0
    # Loser-side identification: cached after the first tick where
    # winner-side ask >= threshold (i.e. the "winner" is whichever side's
    # ask is high). Once set, it stays set — the loser doesn't flip
    # back-and-forth tick-to-tick.
    identified_loser_side: str = ""  # '' | 'YES' | 'NO'
    # Diagnostic: most recent reason a precondition blocked sell-loser, for
    # heartbeat / dashboard.
    sell_loser_status: str = "preconditions_pending"
    # v6.1.2: last-known live book cache (updated every tick before end_ts).
    # Polymarket clears WS books within ~1-2s of end_ts, so reading the
    # current book at settle time (end_ts+2s) returns empty and the settle
    # code falls into the both_zero→VOID branch even when the real outcome
    # was a clean resolution. We cache the last book state observed before
    # end_ts and use that for settlement instead.
    last_yes_ask: float = 0.0
    last_yes_bid: float = 0.0
    last_no_ask: float = 0.0
    last_no_bid: float = 0.0
    last_book_ts: float = 0.0       # 0.0 means no book ever seen
    # v6.1.2: pending-resolution state. When the cache + live + chainlink +
    # gamma cascade in _bs_settle_position all return None, the position is
    # marked pending and stays in state.both_sides_positions for retry every
    # tick. There is NO hard timeout and NO void path — for binary BTC
    # up/down markets, the underlying always resolves (BTC moved or didn't),
    # so we keep retrying until a source returns. Dashboard flags positions
    # pending >= 600s as STUCK so the user can investigate.
    pending_since: float = 0.0          # 0 = not pending; else first-pending ts
    pending_attempts: int = 0           # diagnostic counter
    # v6.5.11 tiered exit ladder state
    tier_ask_history: List[Tuple[float, float, float]] = field(default_factory=list)
    fire_tier: str = ""                 # set to T0/T1/T2/T3 when the ladder fires
    tier_last_eval_status: str = "preconditions_pending"
    last_gamma_fetch_ts: float = 0.0    # throttle Gamma API to 30s/market
    last_pending_log_ts: float = 0.0    # throttle pending log lines
    # v6.1.7: tracks the first time winner_ask crossed _BS_SELL_THRESH for
    # this position. Used to compute "lead duration" at sell-loser fire
    # time for diagnostic CSV logging. Reset to 0 when winner_ask drops
    # back below threshold (winner-side flips or weakens). 0 means
    # winner_ask has not yet crossed threshold.
    winner_first_seen_ts: float = 0.0
    # v6.2.4: verification_late freeze state (whipsaw detection).
    # Only mutated when _BS_STRATEGY == 'verification_late'.
    # vl_armed: set True the first tick TTR ≤ 60s AND winner_ask ≥ ARM_THRESH.
    # vl_armed_side: 'YES' or 'NO' — locked at moment of arming.
    # vl_peak_winner_ask: highest winner_ask seen since arming (only the
    #   armed side counts; if other side becomes leader, vl_frozen is set).
    # vl_frozen: True if (a) side flipped, or (b) winner_ask dropped > DROP_TOL
    #   below vl_peak_winner_ask. Permanent for life of position.
    # vl_freeze_reason: human-readable string describing why we froze.
    # vl_freeze_ts: timestamp of the freeze event for diagnostic.
    # When vl_frozen, the verification-late evaluator will not fire on this
    # market regardless of any other conditions.
    vl_armed: bool = False
    vl_armed_side: str = ""
    vl_peak_winner_ask: float = 0.0
    vl_frozen: bool = False
    vl_freeze_reason: str = ""
    vl_freeze_ts: float = 0.0
    # v6.2.5: arming-time timestamp + peak-update counter. Both feed the
    # SELL_LOSER notes diagnostic. vl_peak_update_count is the 1Hz-leak
    # signal — low values vs how long we've been armed implies the main
    # loop's 1s tick is missing intra-tick peaks (architectural concern
    # logged in journal; data needed before refactoring sampling rate).
    vl_armed_ts: float = 0.0
    vl_peak_update_count: int = 0
    # SPARKLINE: live (t_rel_s, yes_bid, no_bid) samples taken during the
    # life of the position. Used by the dashboard to draw a live sparkline
    # on each open position. Sampled approximately every 3s (rate-limited
    # by last_live_sample_ts below), capped at 120 entries (~6min coverage,
    # enough for a 5min market lifetime).
    live_bid_history: List[Tuple[float, float, float]] = field(default_factory=list)
    last_live_sample_ts: float = 0.0
    # SOLD-LEG MARKER: stamped at the moment sell-loser fires, so the
    # dashboard can draw a vertical line on the sparkline where the sell
    # happened. All zero / empty means sell-loser hasn't fired yet.
    sold_at_ts: float = 0.0
    sold_side: str = ""              # '' | 'YES' | 'NO'
    sold_price: float = 0.0
    sold_ttr_s: float = 0.0          # TTR at the moment of sell
    sold_reason: str = ""            # fire_source ('prod' / 'btc_late' / etc.)


@dataclass
class MultiDurationMarket:
    """Wraps a MarketInfo with the duration tag (5m/15m/60m) — used by
    v6.1.0 discovery to keep all three duration sets distinguishable in
    one structure. The base MarketInfo carries condition_id, slug,
    yes/no token IDs, end_ts, etc."""
    duration_label: str             # '5m' | '15m' | '60m'
    duration_s: int                 # 300 / 900 / 3600
    market: MarketInfo
    # Pre-market markets may have books that are sparse or absent. We
    # track the last sample time so the logger doesn't write rows faster
    # than _LOG_SAMPLE_INTERVAL_S even if the main loop runs at 1 Hz.
    last_logged_ts: float = 0.0
    # Subscription state — set True once the poly_ws thread has issued
    # the subscription. Used by the bs_discovery thread to know when to
    # ask for a re-subscribe.
    ws_subscribed: bool = False

    # ─── v6.3.0: BSS (Both-Sides See-Saw) per-market state ─────────────
    # Inert unless _BS_STRATEGY == 'bss_entry'. Mirrors the pattern of
    # BothSidesPosition.vl_* fields — sustain-and-fire state for entry,
    # rather than for sell. Lifecycle:
    #   bss_state='WATCH'        → looking for first-leg sustain
    #   bss_state='WAITING_2ND'  → first leg "filled"; looking for second
    #   bss_state='BOTH'         → both legs filled; held to resolution
    #                              (a real BothSidesPosition has been
    #                              created and lives in
    #                              state.both_sides_positions)
    #   bss_state='ABORT'        → second leg never confirmed; first leg
    #                              "sold" at last bid; done
    #   bss_state='RESOLVED'     → terminal (only used for ABORT path;
    #                              BOTH path is resolved by the existing
    #                              both_sides_positions resolution flow)
    bss_state: str = "WATCH"
    bss_yes_below_first_start_ts: Optional[float] = None
    bss_no_below_first_start_ts: Optional[float] = None
    bss_first_side: Optional[str] = None
    bss_first_price: Optional[float] = None
    bss_first_fill_ts: Optional[float] = None
    bss_other_below_strict_start_ts: Optional[float] = None
    bss_other_below_relax_start_ts: Optional[float] = None
    bss_second_price: Optional[float] = None
    bss_second_fill_ts: Optional[float] = None
    bss_second_phase: Optional[str] = None     # 'strict' | 'relaxed' | 'pre'
    bss_abort_sold_at: Optional[float] = None
    bss_abort_ts: Optional[float] = None
    # v6.3.14 PPMP: managed-bail state. bail_started_ts = first tick of the
    # live-window bail; bail_sell_target = the resting sell-up offer price
    # (leg-1 ask captured at bail start); abort_reason = sold_up|hard_stop|
    # timeout|market_ended.
    bss_bail_started_ts: Optional[float] = None
    bss_bail_sell_target: Optional[float] = None
    bss_abort_reason: Optional[str] = None

    # v6.3.2: pre-market streak tracking. Independent counters for the
    # looser pre-market threshold (T_FIRST_PRE, T_SECOND_PRE). When the
    # live window opens these get cleared and the live-strict/relaxed
    # streaks take over.
    bss_yes_below_pre_start_ts: Optional[float] = None
    bss_no_below_pre_start_ts: Optional[float] = None
    bss_other_below_pre_start_ts: Optional[float] = None
    bss_first_filled_in_pre: bool = False     # flips True if first leg
                                                # fired before window open

    # v6.3.3: price-history samples for dashboard rendering. Appended at
    # ~1Hz by the BSS evaluator. Capped at 1800 entries (~30min). Each
    # entry: (ts, yes_ask, no_ask).
    bss_price_samples: List[Tuple[float, float, float]] = field(default_factory=list)
    bss_last_sample_ts: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

@dataclass
class BotConfig:
    mode: str
    private_key: str
    proxy_wallet: str
    force_signature_type: int
    position_size_usdc: float
    daily_loss_limit_usdc: float
    delta_threshold_pct: float
    lookback_s: int
    entry_price_min: float
    entry_price_max: float
    edge_min: float
    spread_max: float
    ws_freshness_s: int
    ws_rest_tolerance_pct: float
    binance_tolerance_pct: float
    port: int
    data_dir: str
    log_to_disk: bool
    validation_mode: bool
    resolution_poll_s: float

    def threshold_fraction(self) -> float:
        return self.delta_threshold_pct / 100.0


# ═══════════════════════════════════════════════════════════════════
# RUNTIME STATE
# ═══════════════════════════════════════════════════════════════════

@dataclass
class BotState:
    config: BotConfig
    boot_ts: float

    clob_client: Optional[Any] = None

    binance_prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=12000))
    binance_ws_connected: bool = False
    binance_last_msg_ts: float = 0.0

    poly_books: Dict[str, PolyBook] = field(default_factory=dict)
    poly_ws_connected: bool = False
    poly_last_msg_ts: float = 0.0
    poly_ws_handle: Optional[Any] = None

    btc_5m_market: Optional[MarketInfo] = None
    open_position: Optional[Position] = None

    trades_today: int = 0
    pnl_today_usdc: float = 0.0
    skips_today: int = 0
    last_signal: Optional[Signal] = None

    live_delta_pct: Optional[float] = None
    live_lookback_s: Optional[float] = None
    signal_status_msg: str = "starting"
    last_validation_ok: Optional[bool] = None
    last_validation_reason: str = ""
    skips_by_reason: Dict[str, int] = field(default_factory=dict)

    kill_flag: bool = False

    binance_logger: Optional[Any] = None
    signal_logger: Optional[Any] = None
    trades_logger: Optional[Any] = None
    log_dir: Optional[str] = None

    # v5.6.0: per-token deques of (ts, price, size, side) populated from
    # Polymarket WS `last_trade_price` events. Used by flow_log only;
    # entry/exit logic does NOT consult this.
    poly_trades: Dict[str, Deque[Tuple[float, float, float, str]]] = field(default_factory=dict)
    depth_logger: Optional[Any] = None
    flow_logger: Optional[Any] = None
    # One-shot diag set: tracks tokens for which we've logged the first
    # `last_trade_price` event seen. Used to confirm WS wiring during
    # verify-deploy; not consulted by any logic.
    _first_trade_logged_tokens: Set[str] = field(default_factory=set)

    # v5.8.0: market_ids the bot has already exited a position on this
    # session. Once a market is in this set, compute_strategy_decision
    # will refuse to enter again (skip reason: 'market_already_exited').
    # Cleared on bot restart. Required because TP/SL exits leave the
    # market still active and the bot would otherwise re-enter.
    exited_market_ids: Set[str] = field(default_factory=set)

    trade_history: List[Dict[str, Any]] = field(default_factory=list)
    trades_won: int = 0
    trades_lost: int = 0
    last_decision_reason: str = ""
    pending_resolutions: List[Dict[str, Any]] = field(default_factory=list)

    # ─── v6.1.0: both-sides + multi-duration logging ───────────────────
    # Open both-sides positions, keyed by market_id. Single-position rule
    # is REPLACED in this mode by a soft per-market constraint: at most one
    # both-sides position per market. Multiple markets in flight is the
    # whole point — at any given moment there are typically 2-3 5m markets
    # in the lead-time window.
    both_sides_positions: Dict[str, BothSidesPosition] = field(default_factory=dict)
    # Session-scoped set of market_ids the bot has already entered both-
    # sides on. Prevents accidental re-entry on the same market if it
    # somehow re-appears in candidates (slug timestamp aliasing, retry
    # loops, etc.). Cleared on bot restart.
    bs_entered_market_ids: Set[str] = field(default_factory=set)
    # Active multi-duration market sets, refreshed by both_sides_discovery
    # thread. Keys are market_id. Used by:
    #   - poly_ws subscription (it subscribes to ALL token_ids across all
    #     three sets when v6.1.0 is active)
    #   - both_sides_tick (uses 5m_in_window to decide which markets to
    #     enter both-sides on)
    #   - pre_market_books_log_tick (uses all three to write CSV rows)
    bs_5m_in_window: Dict[str, MultiDurationMarket] = field(default_factory=dict)
    bs_15m_in_window: Dict[str, MultiDurationMarket] = field(default_factory=dict)
    bs_60m_in_window: Dict[str, MultiDurationMarket] = field(default_factory=dict)
    # Per-cycle counters (lifetime, not daily). Incremented in both_sides_tick
    # and exposed in /api/status for the dashboard.
    bs_total_entered: int = 0
    bs_total_sold_loser: int = 0
    bs_total_resolved: int = 0
    # v6.1.2: bs_total_voided REMOVED. VOID is not a valid concept for
    # BTC up/down binary markets — the underlying always resolves. The
    # dashboard now tracks bs_total_pending (computed live from positions
    # with pending_since > 0). True voids would only occur if Polymarket
    # itself canceled a market (essentially never for these crypto markets).
    bs_pnl_today_usdc: float = 0.0
    # v6.1.2: in-memory rolling list of resolved both-sides trades for the
    # dashboard "Last 5 trades" panel. Each entry holds both legs' final
    # state plus aggregate fields. Trimmed to last 100 entries to bound
    # memory. Not persisted across bot restarts (CSV is the persistent
    # source of truth — bs_trades_<date>.csv has every entry/exit row).
    bs_trade_history: List[Dict[str, Any]] = field(default_factory=list)
    # Cumulative diag: which 60m / 15m slug formats actually returned
    # markets. Lets us see in /api/status whether the env-var prefixes
    # are right or need tweaking.
    bs_discovery_diag: Dict[str, int] = field(default_factory=dict)
    # New CSV logger for pre_market_books_<date>.csv (5m/15m/60m books).
    pre_market_books_logger: Optional[Any] = None
    # New CSV logger for bs_trades_<date>.csv (both-sides entry/exit events).
    bs_trades_logger: Optional[Any] = None

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def uptime_s(self) -> float:
        return time.time() - self.boot_ts


# ═══════════════════════════════════════════════════════════════════
# CONFIG LOADING
# ═══════════════════════════════════════════════════════════════════

def _required_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(
            f"Missing required env var: {name}\n"
            f"Set this on Railway → Variables. PRIVATE_KEY and PROXY_WALLET are "
            f"required EVEN IN DRY MODE for boot parity with LIVE."
        )
    return v


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        raise RuntimeError(f"Env var {name}={v!r} is not a valid float.")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise RuntimeError(f"Env var {name}={v!r} is not a valid int.")


def load_config() -> BotConfig:
    mode = os.environ.get("MODE", "dry").strip().lower()
    if mode not in ("dry", "live"):
        raise RuntimeError(f"MODE must be 'dry' or 'live', got: {mode!r}")

    validation_mode = os.environ.get("VALIDATION_MODE", "false").strip().lower() in ("1", "true", "yes")

    delta_threshold_pct = _env_float("SIGNAL_DELTA_THRESHOLD_PCT", 0.4)
    entry_price_min = _env_float("ENTRY_PRICE_MIN", 0.35)
    entry_price_max = _env_float("ENTRY_PRICE_MAX", 0.65)
    edge_min = _env_float("EDGE_MIN", 0.10)
    spread_max = _env_float("SPREAD_MAX", 0.05)
    ws_freshness_s = _env_int("WS_FRESHNESS_S", 3)

    if validation_mode:
        if mode == "live":
            raise RuntimeError(
                "Refusing to boot: VALIDATION_MODE=true with MODE=live. "
                "Validation mode loosens all gates and is DRY-only by design."
            )
        delta_threshold_pct = 0.02
        entry_price_min = 0.05
        entry_price_max = 0.95
        edge_min = 0.02
        spread_max = 0.20
        ws_freshness_s = 5

    cfg = BotConfig(
        mode=mode,
        private_key=_required_env("PRIVATE_KEY"),
        proxy_wallet=_required_env("PROXY_WALLET"),
        force_signature_type=_env_int("FORCE_SIGNATURE_TYPE", 1),
        position_size_usdc=_env_float("POSITION_SIZE_USDC", 1.0),
        daily_loss_limit_usdc=_env_float("DAILY_LOSS_LIMIT_USDC", 10.0),
        delta_threshold_pct=delta_threshold_pct,
        lookback_s=_env_int("SIGNAL_LOOKBACK_S", 30),
        entry_price_min=entry_price_min,
        entry_price_max=entry_price_max,
        edge_min=edge_min,
        spread_max=spread_max,
        ws_freshness_s=ws_freshness_s,
        ws_rest_tolerance_pct=_env_float("WS_REST_TOLERANCE_PCT", 0.5),
        binance_tolerance_pct=_env_float("BINANCE_TOLERANCE_PCT", 0.1),
        port=_env_int("PORT", 8080),
        data_dir=os.environ.get("DATA_DIR", "/data").strip() or "/data",
        log_to_disk=os.environ.get("LOG_TO_DISK", "true").strip().lower() in ("1", "true", "yes"),
        validation_mode=validation_mode,
        resolution_poll_s=_env_float("RESOLUTION_POLL_S", 10.0),
    )

    if not (0 < cfg.entry_price_min < cfg.entry_price_max < 1):
        raise RuntimeError(
            f"Bad entry band: min={cfg.entry_price_min} max={cfg.entry_price_max}"
        )
    if cfg.position_size_usdc <= 0:
        raise RuntimeError(
            f"POSITION_SIZE_USDC must be > 0, got {cfg.position_size_usdc}"
        )
    if cfg.force_signature_type != 1:
        raise RuntimeError(
            f"FORCE_SIGNATURE_TYPE must be 1 (Polymarket native wallet). "
            f"Got {cfg.force_signature_type}. Refusing to boot."
        )
    if cfg.daily_loss_limit_usdc <= 0:
        raise RuntimeError(
            f"DAILY_LOSS_LIMIT_USDC must be > 0, got {cfg.daily_loss_limit_usdc}"
        )

    return cfg


# ═══════════════════════════════════════════════════════════════════
# DATA DIR CHECK
# ═══════════════════════════════════════════════════════════════════

def verify_data_dir_writable(path_str: str) -> None:
    p = Path(path_str)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise RuntimeError(
            f"Cannot create data_dir {p!s}: {e}\n"
            f"On Railway: attach a persistent volume mounted at {p!s}."
        )
    probe = p / ".write_probe"
    try:
        probe.write_text(str(time.time()))
        probe.unlink()
    except Exception as e:
        raise RuntimeError(
            f"data_dir {p!s} exists but is not writable: {e}\n"
            f"On Railway: check the volume is attached and the mount path matches."
        )


# ═══════════════════════════════════════════════════════════════════
# CLOB CLIENT INIT
# ═══════════════════════════════════════════════════════════════════

def init_clob_client(cfg: BotConfig):
    try:
        from py_clob_client.client import ClobClient
    except ImportError as e:
        raise RuntimeError(f"py-clob-client not installed: {e}")

    CLOB_HOST = "https://clob.polymarket.com"
    POLYGON_CHAIN_ID = 137

    try:
        client = ClobClient(
            CLOB_HOST,
            key=cfg.private_key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=cfg.force_signature_type,
            funder=cfg.proxy_wallet,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        raise RuntimeError(
            f"CLOB client init failed: {e}\n"
            f"Check PRIVATE_KEY matches PROXY_WALLET and that the wallet is "
            f"a Polymarket native wallet (signature_type=1)."
        )

    return client


# ═══════════════════════════════════════════════════════════════════
# FEED THREAD: BINANCE WS
# ═══════════════════════════════════════════════════════════════════

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"


def binance_ws_thread(state: BotState) -> None:
    import websocket

    backoff = 1.0
    backoff_max = 60.0

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            price = float(data.get("p") or 0)
            qty = float(data.get("q") or 0)
            ts_ms = data.get("T") or data.get("E") or 0
            ts = (ts_ms / 1000.0) if ts_ms else time.time()
            if price > 0:
                state.binance_prices.append((ts, price))
                state.binance_last_msg_ts = time.time()
                state.binance_ws_connected = True
                if state.binance_logger is not None:
                    state.binance_logger.log([
                        int(ts * 1000),
                        f"{price:.2f}",
                        f"{qty:.8f}",
                    ])
        except Exception:
            pass

    def on_error(ws, error):
        state.binance_ws_connected = False
        print(f"[binance_ws] error: {error}", flush=True)

    def on_close(ws, code, msg):
        state.binance_ws_connected = False
        print(f"[binance_ws] closed code={code} msg={msg}", flush=True)

    def on_open(ws):
        nonlocal backoff
        backoff = 1.0
        state.binance_ws_connected = True
        print("[binance_ws] connected", flush=True)

    while not state.kill_flag:
        try:
            ws = websocket.WebSocketApp(
                BINANCE_WS_URL,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10, reconnect=0)
        except Exception as e:
            print(f"[binance_ws] crash: {e}", flush=True)

        state.binance_ws_connected = False
        if state.kill_flag:
            break
        print(f"[binance_ws] reconnecting in {backoff:.1f}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, backoff_max)


# ═══════════════════════════════════════════════════════════════════
# FEED THREAD: MARKET DISCOVERY (Gamma API)
# ═══════════════════════════════════════════════════════════════════

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
DISCOVERY_INTERVAL_S = 30.0
MARKET_END_MIN_S = 60
# v5.5.27: was 15 * 60 = 900s. Capped at 300s = exactly one 5-min market
# duration so the bot ONLY selects markets that have already started their
# scoring window. Pre-market slugs (TTR 300-900) had queryable books from
# market-maker speculation but no active scoring — entries there were
# placing bets before the market began. Cross-bot policy: never enter
# non-active trades.
MARKET_END_MAX_S = 300

MARKET_INTERVAL_S = 300
MARKET_SLUG_PREFIX = "btc-updown-5m-"
SLUG_LOOKAHEAD_BOUNDARIES = 3

# v6.1.0: 15m + 60m duration constants. Used only by the both_sides
# discovery thread when STRATEGY_MODE=both_sides_btc. Slug prefixes
# are env-var configurable (LOG_15M_SLUG_PREFIX / LOG_60M_SLUG_PREFIX)
# in case Polymarket uses a different convention than expected.
MARKET_INTERVAL_15M_S = 900
MARKET_INTERVAL_60M_S = 3600
# How many future boundaries to scan per tick. 5m needs more (markets
# resolve every 5 min so window covers ~3 markets concurrently); 15m
# needs ~3 to cover the lead-time window; 60m needs 1-2.
def _ppmp_int_env(name: str, default: int, lo: int, hi: int) -> int:
    # v6.3.14: env-read int with clamp. Used to widen the 5m scan-ahead
    # horizon for PPMP (12 boundaries = 60min of pre-market markets).
    try:
        v = int(float(os.environ.get(name, "") or default))
    except Exception:
        v = default
    return min(max(v, lo), hi)
# v6.3.14 PPMP: was hard-coded 4 (~20min). Env-configurable so PPMP can
# widen to 12 (=60min) for the patient pre-market pair build. Default
# unchanged at 4 so a deploy with no env var behaves exactly as before.
SLUG_LOOKAHEAD_5M = _ppmp_int_env("SLUG_LOOKAHEAD_5M", 4, 1, 24)   # 4=~20min, 12=60min
SLUG_LOOKAHEAD_15M = 3
SLUG_LOOKAHEAD_60M = 2


def _parse_iso_to_ts(iso_str: str) -> Optional[float]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _next_resolution_boundaries(now: float, count: int) -> List[int]:
    # v5.5.28: start from CURRENT 5-min boundary (the active market's slug
    # timestamp), not the next one. Previously this returned [next_b,
    # next_b+300, next_b+600] — all pre-market slugs — which meant the
    # active market was NEVER in the candidate set. Pre-v5.5.27 with
    # MAX=900 the bot still functioned by picking pre-market slugs and
    # waiting them out, but every entry was effectively a pre-market bet.
    # v5.5.27 then hard-rejected pre-market entries (MAX=300), but with
    # this function still skipping the active slug, ALL candidates failed
    # → bot stuck with no market for 47+ minutes.
    current_b = int((now // MARKET_INTERVAL_S) * MARKET_INTERVAL_S)
    return [current_b + i * MARKET_INTERVAL_S for i in range(count)]


def _parse_event_to_market(event: dict, now: float,
                            ttr_min_s: Optional[float] = None,
                            ttr_max_s: Optional[float] = None) -> Tuple[Optional[MarketInfo], str]:
    """Parse a Gamma API event into a MarketInfo.

    v6.1.0: ttr_min_s / ttr_max_s let callers override MARKET_END_MIN_S /
    MARKET_END_MAX_S. The defaults preserve v5.8.1 behavior — the
    market_discovery_thread (lag_signal path) calls without args and gets
    [60, 300] = active markets only. The both_sides_discovery_thread
    passes wider TTR bounds to capture pre-market 5m candidates and
    longer 15m/60m markets.
    """
    if ttr_min_s is None:
        ttr_min_s = float(MARKET_END_MIN_S)
    if ttr_max_s is None:
        ttr_max_s = float(MARKET_END_MAX_S)

    if not isinstance(event, dict):
        return None, "not_dict"
    markets = event.get("markets") or []
    if not markets:
        return None, "no_markets_in_event"

    m = markets[0]
    question = (m.get("question") or event.get("title") or "").strip()

    end_ts = _parse_iso_to_ts(m.get("endDate") or event.get("endDate"))
    if not end_ts:
        return None, "no_end_date"
    time_left = end_ts - now
    if time_left < ttr_min_s:
        return None, f"end_too_close:{time_left:.0f}s"
    if time_left > ttr_max_s:
        return None, f"end_too_far:{time_left:.0f}s"

    token_ids_raw = m.get("clobTokenIds")
    if not token_ids_raw:
        return None, "no_token_ids"
    try:
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    except Exception:
        return None, "token_ids_parse_error"
    if not isinstance(token_ids, list) or len(token_ids) != 2:
        return None, "token_ids_wrong_count"

    outcomes_raw = m.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except Exception:
        return None, "outcomes_parse_error"
    if not isinstance(outcomes, list) or len(outcomes) != 2:
        return None, "outcomes_wrong_count"

    o0 = (outcomes[0] or "").strip().lower()
    o1 = (outcomes[1] or "").strip().lower()
    if o0 in ("up", "yes"):
        yes_id, no_id = token_ids[0], token_ids[1]
    elif o1 in ("up", "yes"):
        yes_id, no_id = token_ids[1], token_ids[0]
    else:
        print(f"[market_disc] unknown outcomes={outcomes}, defaulting to order", flush=True)
        yes_id, no_id = token_ids[0], token_ids[1]

    cond_id = m.get("conditionId") or m.get("id") or event.get("id") or ""
    event_slug = (event.get("slug") or m.get("slug") or "").strip().lower()

    return MarketInfo(
        condition_id=str(cond_id),
        question=question,
        slug=event_slug,
        yes_token_id=str(yes_id),
        no_token_id=str(no_id),
        end_ts=end_ts,
        market_url=f"https://polymarket.com/event/{event_slug}" if event_slug else "",
    ), "ok"


def _fetch_event_by_slug(slug: str) -> Optional[dict]:
    import requests

    headers = {
        "User-Agent": "polybot-simple-v1/0.3 (+https://polymarket.com)",
        "Accept": "application/json",
    }
    try:
        r = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, headers=headers, timeout=8)
    except Exception as e:
        _record_clob_status(0)  # v6.1.3: 0 = network exception
        print(f"[market_disc] slug {slug} fetch error: {e}", flush=True)
        return None
    _record_clob_status(r.status_code)  # v6.1.3
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    items = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
    if not items:
        return None
    return items[0]


def _diag_dump_bitcoin_markets(now: float) -> None:
    print("[market_disc][diag] === Slug-based discovery (5-min boundaries) ===", flush=True)
    boundaries = _next_resolution_boundaries(now, SLUG_LOOKAHEAD_BOUNDARIES)
    found = 0
    for ts in boundaries:
        slug = f"{MARKET_SLUG_PREFIX}{ts}"
        eta = ts - now
        ev = _fetch_event_by_slug(slug)
        if ev is None:
            print(f"[market_disc][diag] slug={slug} eta={eta:.0f}s → NOT FOUND", flush=True)
            continue
        mi, reason = _parse_event_to_market(ev, now)
        if mi:
            found += 1
            print(
                f"[market_disc][diag] slug={slug} eta={eta:.0f}s → ACCEPT "
                f"q={mi.question!r} yes={mi.yes_token_id[:12]}…",
                flush=True,
            )
        else:
            inner_markets = ev.get("markets") or []
            print(
                f"[market_disc][diag] slug={slug} eta={eta:.0f}s → REJECT[{reason}] "
                f"event_id={ev.get('id')} markets_in_event={len(inner_markets)}",
                flush=True,
            )
    print(f"[market_disc][diag] === total ACCEPT: {found}/{len(boundaries)} ===", flush=True)


def _fetch_btc_5min_candidates() -> List[MarketInfo]:
    candidates: List[MarketInfo] = []
    now = time.time()
    for ts in _next_resolution_boundaries(now, SLUG_LOOKAHEAD_BOUNDARIES):
        slug = f"{MARKET_SLUG_PREFIX}{ts}"
        ev = _fetch_event_by_slug(slug)
        if ev is None:
            continue
        mi, _reason = _parse_event_to_market(ev, now)
        if mi:
            # v5.5.30 (fixed): slug-naming invariant.
            # Polymarket convention (per v5.5.28 finding from boot logs):
            # slug btc-updown-5m-{ts} represents a market that STARTS at ts
            # and ENDS at ts + MARKET_INTERVAL_S (300s for 5-min markets).
            #
            # v5.5.29 had this BACKWARDS — assumed mi.end_ts ≈ ts, leading
            # to drift = 300s on every candidate, which exceeded the 30s
            # tolerance and caused EVERY candidate to be silently rejected.
            # The bug only showed when the bot crashed (Polymarket flake,
            # 09:51:40 UTC 2026-04-28): on restart, btc_5m_market = None,
            # discovery never picked any market again. Fixed.
            expected_end = float(ts) + float(MARKET_INTERVAL_S)
            drift = abs(mi.end_ts - expected_end)
            if drift > 30.0:
                print(
                    f"[market_disc][CRITICAL] slug-naming invariant VIOLATED for {slug}: "
                    f"slug_ts={ts} expected end_ts={expected_end:.0f} but "
                    f"endDate={mi.end_ts:.0f} (drift={drift:.0f}s). "
                    f"Polymarket may have changed slug convention. Discarding candidate.",
                    flush=True,
                )
                continue
            candidates.append(mi)
    return candidates


def _resolve_market_open_btc(state: BotState, market: MarketInfo) -> Optional[float]:
    """v5.5.31: find the BTC price closest to the market's scoring start
    (end_ts - MARKET_INTERVAL_S) from state.binance_prices ring buffer.

    Returns:
      - exact-or-near match if a sample exists within ±10s of start_ts → that price
      - None otherwise (caller should leave open_btc_price as None and try again later
        once the deque has accumulated enough recent samples)

    This is best-effort and used only for delta_from_start logging. Entry
    logic does NOT consult open_btc_price, so a None result is harmless.
    """
    start_ts = market.end_ts - MARKET_INTERVAL_S
    snapshot = list(state.binance_prices)  # snapshot to avoid race with WS thread
    if not snapshot:
        return None
    best_price = None
    best_diff = float("inf")
    for ts, price in snapshot:
        d = abs(ts - start_ts)
        if d < best_diff:
            best_diff = d
            best_price = price
    # Require a sample within 10s of the actual start. If the bot just booted
    # mid-market and only has post-start samples, the closest sample will be
    # > start_ts, but if it's within 10s the price is still a usable proxy.
    if best_diff > 10.0:
        return None
    return best_price


def market_discovery_thread(state: BotState) -> None:
    diag_done = False
    consecutive_empty_cycles = 0  # v5.5.29 guard 3: stuck detector
    last_stuck_warning_at = 0.0
    while not state.kill_flag:
        try:
            if not diag_done:
                _diag_dump_bitcoin_markets(time.time())
                diag_done = True

            candidates = _fetch_btc_5min_candidates()
            now = time.time()
            old = state.btc_5m_market

            # v5.5.29 guard 3: Stuck-cycle detector.
            # If `state.btc_5m_market is None` AND we get no candidates for
            # many consecutive cycles, the bot is silently dead. Log CRITICAL
            # so it's visible in Railway logs immediately rather than only
            # detected by user noticing zero trades over many minutes.
            no_market = state.btc_5m_market is None and not candidates
            if no_market:
                consecutive_empty_cycles += 1
            else:
                if consecutive_empty_cycles >= 5:
                    print(
                        f"[market_disc] recovered after {consecutive_empty_cycles} "
                        f"empty cycles", flush=True,
                    )
                consecutive_empty_cycles = 0

            # Warn at 5 cycles (~2.5 min) and re-warn every 10 cycles thereafter.
            if (consecutive_empty_cycles == 5
                    or (consecutive_empty_cycles > 5
                        and consecutive_empty_cycles % 10 == 0)):
                print(
                    f"[market_disc][CRITICAL] STUCK: no market selected for "
                    f"{consecutive_empty_cycles} cycles ({consecutive_empty_cycles * DISCOVERY_INTERVAL_S:.0f}s). "
                    f"Discovery is failing — running diag dump to investigate.",
                    flush=True,
                )
                # Re-run diag to see what slugs are returning what
                _diag_dump_bitcoin_markets(now)

            # v5.5.25: STICKY market selection.
            # Stay on the current market until its end_ts has actually passed.
            # Only switch when:
            #   (a) old is None (initial pick), or
            #   (b) old.end_ts <= now - 5 (current market actually expired)
            # The previous behaviour (switching on chosen.condition_id !=
            # old.condition_id) caused the bot to abandon the active market
            # the moment its TTR dropped below MARKET_END_MIN_S=60s, never
            # observing the final-minute price-discovery window.
            old_still_valid = old is not None and old.end_ts > now - 5

            if old_still_valid:
                # Keep current market. Do not switch even if filter rejected
                # it from candidates. Discovery thread continues to refresh
                # candidates (so we have the next market warmed up in case
                # of a fast handoff), but state.btc_5m_market stays put.
                # v5.5.31: if we still don't have an open_btc_price for this
                # market (e.g. we picked it up mid-life and the deque didn't
                # have an old-enough sample yet), retry now.
                if old.open_btc_price is None:
                    p = _resolve_market_open_btc(state, old)
                    if p is not None:
                        old.open_btc_price = p
                        print(
                            f"[market_disc] late-resolved open_btc=${p:.2f} "
                            f"for {old.condition_id[:10]}",
                            flush=True,
                        )
            elif candidates:
                # Old expired or never set — pick soonest valid candidate.
                candidates.sort(key=lambda mi: mi.end_ts)
                chosen = candidates[0]
                # v5.5.31: try to resolve open_btc_price NOW, before assigning.
                # If unsuccessful (deque too short), the assignment still proceeds
                # with open_btc_price=None and the sticky-stay branch above will
                # retry on subsequent cycles.
                chosen.open_btc_price = _resolve_market_open_btc(state, chosen)
                state.btc_5m_market = chosen
                open_str = (f"open_btc=${chosen.open_btc_price:.2f}"
                            if chosen.open_btc_price is not None else "open_btc=pending")
                print(
                    f"[market_disc] selected '{chosen.question}' "
                    f"ends_in={chosen.end_ts - now:.0f}s "
                    f"yes={chosen.yes_token_id[:10]}… {open_str}",
                    flush=True,
                )
                if len(candidates) > 1:
                    rest = [round(c.end_ts - now) for c in candidates[1:4]]
                    print(
                        f"[market_disc] {len(candidates)} candidates total; "
                        f"others queued at {rest}s",
                        flush=True,
                    )
                _force_poly_ws_resubscribe(state)
            else:
                # No candidates available and old is expired or None.
                if old is not None:
                    print(
                        f"[market_disc] current market expired: {old.question}",
                        flush=True,
                    )
                    state.btc_5m_market = None
                    _force_poly_ws_resubscribe(state)
                else:
                    print("[market_disc] no BTC 5-min candidates found", flush=True)
        except Exception as e:
            print(f"[market_disc] crash: {e}", flush=True)
            traceback.print_exc()

        slept = 0.0
        while slept < DISCOVERY_INTERVAL_S and not state.kill_flag:
            time.sleep(1.0)
            slept += 1.0


def _force_poly_ws_resubscribe(state: BotState) -> None:
    handle = state.poly_ws_handle
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# FEED THREAD: POLYMARKET WS
# ═══════════════════════════════════════════════════════════════════

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def poly_ws_thread(state: BotState) -> None:
    import websocket

    backoff_max = 30.0
    nonlocal_ref = {"backoff": 1.0}

    def _build_subscribe_msg(market: MarketInfo) -> str:
        return json.dumps({
            "type": "Market",
            "assets_ids": [market.yes_token_id, market.no_token_id],
        })

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
        except Exception:
            return

        events = data if isinstance(data, list) else [data]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("event_type")
            asset_id = event.get("asset_id") or event.get("market")
            if not asset_id:
                continue

            now = time.time()

            if event_type == "book":
                bids = event.get("bids") or []
                asks = event.get("asks") or []
                best_bid = max((float(b["price"]) for b in bids if "price" in b), default=0.0)
                best_ask = min((float(a["price"]) for a in asks if "price" in a), default=0.0)
                bid_size = sum(float(b.get("size", 0)) for b in bids
                               if "price" in b and float(b["price"]) == best_bid) if best_bid else 0.0
                ask_size = sum(float(a.get("size", 0)) for a in asks
                               if "price" in a and float(a["price"]) == best_ask) if best_ask else 0.0

                # v5.6.0: capture top-N levels for depth_log. Polymarket may
                # return rows unsorted; sort here so [0] is always best.
                # Skips malformed rows silently rather than raising — same
                # philosophy as best_bid/best_ask above.
                bid_levels: List[Tuple[float, float]] = []
                for b in bids:
                    try:
                        p = float(b.get("price", 0))
                        s = float(b.get("size", 0))
                    except (ValueError, TypeError):
                        continue
                    if p > 0 and s > 0:
                        bid_levels.append((p, s))
                bid_levels.sort(key=lambda x: x[0], reverse=True)
                bid_levels = bid_levels[:DEPTH_LEVELS]

                ask_levels: List[Tuple[float, float]] = []
                for a in asks:
                    try:
                        p = float(a.get("price", 0))
                        s = float(a.get("size", 0))
                    except (ValueError, TypeError):
                        continue
                    if p > 0 and s > 0:
                        ask_levels.append((p, s))
                ask_levels.sort(key=lambda x: x[0])
                ask_levels = ask_levels[:DEPTH_LEVELS]

                state.poly_books[asset_id] = PolyBook(
                    token_id=asset_id,
                    bid=best_bid,
                    ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    last_update_ts=now,
                    bid_levels=bid_levels,
                    ask_levels=ask_levels,
                    last_book_snapshot_ts=now,
                )
                state.poly_last_msg_ts = now
                state.poly_ws_connected = True

            elif event_type == "price_change":
                book = state.poly_books.get(asset_id)
                if book:
                    changes = event.get("changes") or event.get("price_changes") or []
                    for ch in changes:
                        try:
                            side = (ch.get("side") or "").upper()
                            price = float(ch.get("price"))
                            size = float(ch.get("size"))
                        except Exception:
                            continue
                        if side == "BUY" and price > book.bid:
                            book.bid = price
                            book.bid_size = size
                        elif side == "SELL" and (book.ask == 0 or price < book.ask):
                            book.ask = price
                            book.ask_size = size
                    book.last_update_ts = now
                state.poly_last_msg_ts = now

            elif event_type == "last_trade_price":
                # v5.6.0: trade-flow capture. v5.5.31 silently dropped these.
                # Defensive: tolerate missing/malformed fields, clamp ts to now.
                try:
                    price = float(event.get("price", 0))
                    size = float(event.get("size", 0))
                except (ValueError, TypeError):
                    continue
                if price <= 0 or size <= 0:
                    continue
                trade_ts_raw = event.get("timestamp")
                try:
                    trade_ts = (float(trade_ts_raw) / 1000.0
                                if trade_ts_raw not in (None, "") else now)
                except (ValueError, TypeError):
                    trade_ts = now
                # Sanity clamp: if Polymarket sends a stale or future ts,
                # use server-receive time instead.
                if abs(trade_ts - now) > 600:
                    trade_ts = now
                book = state.poly_books.get(asset_id)
                side = _infer_trade_side(event, book)
                deque_for_token = state.poly_trades.setdefault(
                    asset_id, deque(maxlen=POLY_TRADES_BUFFER))
                deque_for_token.append((trade_ts, price, size, side))
                state.poly_last_msg_ts = now  # trades count as keepalive
                # One-shot wiring confirmation per token.
                if asset_id not in state._first_trade_logged_tokens:
                    state._first_trade_logged_tokens.add(asset_id)
                    print(
                        f"[poly_ws] first last_trade_price for token={asset_id[:10]}… "
                        f"price={price:.4f} size={size:.2f} side={side} "
                        f"event_keys={sorted(event.keys())}",
                        flush=True,
                    )

    def on_error(ws, error):
        state.poly_ws_connected = False
        print(f"[poly_ws] error: {error}", flush=True)

    def on_close(ws, code, msg):
        state.poly_ws_connected = False
        print(f"[poly_ws] closed code={code} msg={msg}", flush=True)

    def make_on_open(market: Optional[MarketInfo], v610_token_ids: Optional[List[str]] = None):
        def on_open(ws):
            nonlocal_ref["backoff"] = 1.0
            try:
                if v610_token_ids:
                    # v6.1.0: multi-market subscription. Send all token IDs
                    # across 5m/15m/60m discovery sets in one Market message.
                    msg = json.dumps({
                        "type": "Market",
                        "assets_ids": v610_token_ids,
                    })
                    ws.send(msg)
                    state.poly_ws_connected = True
                    print(
                        f"[poly_ws] connected; v6.1.0 multi-market subscribed "
                        f"({len(v610_token_ids)} tokens)",
                        flush=True,
                    )
                elif market is not None:
                    ws.send(_build_subscribe_msg(market))
                    state.poly_ws_connected = True
                    print(
                        f"[poly_ws] connected; subscribed to '{market.question[:40]}' "
                        f"(yes={market.yes_token_id[:10]}…, no={market.no_token_id[:10]}…)",
                        flush=True,
                    )
            except Exception as e:
                print(f"[poly_ws] subscribe failed: {e}", flush=True)
        return on_open

    while not state.kill_flag:
        # v6.1.0: dual subscribe-source logic.
        # If both_sides is active, build the union of token IDs across the
        # three duration sets (5m/15m/60m) and subscribe to all in one
        # connection. Otherwise fall back to v5.8.1 single-market behavior
        # using state.btc_5m_market.
        v610_token_ids: Optional[List[str]] = None
        market: Optional[MarketInfo] = None
        if _BS_ACTIVE:
            v610_token_ids = _bs_compute_subscribe_token_ids(state)
            if not v610_token_ids:
                time.sleep(2.0)
                continue
        else:
            market = state.btc_5m_market
            if market is None:
                time.sleep(2.0)
                continue

        try:
            ws = websocket.WebSocketApp(
                POLY_WS_URL,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=make_on_open(market, v610_token_ids),
            )
            state.poly_ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10, reconnect=0)
        except Exception as e:
            print(f"[poly_ws] crash: {e}", flush=True)
        finally:
            state.poly_ws_handle = None
            state.poly_ws_connected = False
            state.poly_books.clear()

        if state.kill_flag:
            break

        b = nonlocal_ref["backoff"]
        print(f"[poly_ws] reconnecting in {b:.1f}s", flush=True)
        slept = 0.0
        while slept < b and not state.kill_flag:
            time.sleep(0.5)
            slept += 0.5
        nonlocal_ref["backoff"] = min(b * 2, backoff_max)


# ═══════════════════════════════════════════════════════════════════
# HTTP LISTENER + DASHBOARD (truncated dashboard HTML — same as v5.5.23)
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>polybot simple</title>
<style id="theme-style">
:root{--bg:#0a0a0a;--panel:#141414;--border:#262626;--border-soft:#1f1f1f;--text:#e0e0e0;--muted:#7a7a7a;--green:#5cbd5c;--yellow:#e0b340;--red:#d96666;--blue:#7aa5d2;--mono:ui-monospace,Menlo,Consolas,monospace;}
/* v6.2.3: verification bot theme — burgundy/amber to differentiate from production */
body.theme-verification{--bg:#1a0808;--panel:#241010;--border:#3d1818;--border-soft:#2a1010;--text:#f0d8c0;--muted:#a08070;--blue:#d97a4a;}
body.theme-verification header{background:linear-gradient(180deg,#3d1818 0%,#1a0808 100%);}
body.theme-verification .badge-version{background:rgba(217,122,74,.18);color:#e8a070;border:1px solid rgba(217,122,74,.4);}
body.theme-verification .badge-bs{background:rgba(217,122,74,.18);color:#e8a070;border:1px solid rgba(217,122,74,.4);}
:root{--bg:#0a0a0a;--panel:#141414;--border:#262626;--border-soft:#1f1f1f;--text:#e0e0e0;--muted:#7a7a7a;--green:#5cbd5c;--yellow:#e0b340;--red:#d96666;--blue:#7aa5d2;--mono:ui-monospace,Menlo,Consolas,monospace;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.45;}
header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
header h1{margin:0;font-size:18px;font-weight:600;}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:600;text-transform:uppercase;}
.badge-dry{background:rgba(88,166,255,.15);color:var(--blue);}
.badge-live{background:rgba(248,81,73,.15);color:var(--red);}
.badge-invert{background:rgba(210,153,34,.18);color:var(--yellow);border:1px solid rgba(210,153,34,.4);}
.badge-version{background:rgba(88,166,255,.12);color:var(--blue);border:1px solid rgba(88,166,255,.3);font-family:var(--mono);}
.badge-tp{background:rgba(63,185,80,.15);color:var(--green);border:1px solid rgba(63,185,80,.4);}
.badge-sl{background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.4);}
.badge-sl-late{background:rgba(255,140,0,.15);color:#ff9c3a;border:1px solid rgba(255,140,0,.4);}
.uptime{margin-left:auto;color:var(--muted);font-family:var(--mono);font-size:12px;}
main{padding:20px;max-width:1100px;margin:0 auto;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:20px;}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;}
.card-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;}
.card-value{font-size:22px;font-weight:600;font-family:var(--mono);}
.card-detail{margin-top:4px;font-size:12px;color:var(--muted);font-family:var(--mono);}
.signal-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:20px;}
.signal-panel.is-valid{border-color:var(--green);}
.signal-panel.is-skip{border-color:var(--yellow);}
.signal-row{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}
.signal-direction{font-size:28px;font-weight:700;font-family:var(--mono);}
.signal-direction.up{color:var(--green);}
.signal-direction.down{color:var(--red);}
.signal-direction.neutral{color:var(--muted);}
.signal-detail{margin-top:8px;color:var(--muted);font-family:var(--mono);font-size:13px;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;}
.stat{background:#1a1a1a;border:1px solid var(--border);border-radius:8px;padding:10px 12px;text-align:center;}
.stat-label{font-size:10px;text-transform:uppercase;color:var(--muted);}
.stat-value{font-size:18px;font-family:var(--mono);font-weight:600;margin-top:4px;}
.up{color:var(--green)!important;}.down{color:var(--red)!important;}
.invert-banner{background:rgba(210,153,34,.15);border:1px solid var(--yellow);color:var(--yellow);padding:8px 14px;border-radius:8px;font-family:var(--mono);font-size:12px;margin-bottom:16px;text-align:center;}
.clob-warning{background:rgba(248,81,73,.15);border:1px solid var(--red);color:var(--red);padding:8px 14px;border-radius:8px;font-family:var(--mono);font-size:12px;margin-bottom:16px;text-align:center;}
.books-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}
.books-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:10px;}
.books-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:600px){.books-grid{grid-template-columns:1fr;}}
.book-side{background:#1a1a1a;border:1px solid var(--border);border-radius:8px;padding:10px 12px;}
.book-side.yes{border-left:3px solid var(--green);}
.book-side.no{border-left:3px solid var(--red);}
.book-name{font-size:11px;text-transform:uppercase;color:var(--muted);margin-bottom:6px;font-weight:600;letter-spacing:.05em;}
.book-quote{font-family:var(--mono);font-size:13px;line-height:1.7;}
.book-quote .qkey{color:var(--muted);display:inline-block;width:62px;}
.book-quote .stale{color:var(--red);}
.book-quote .qval{font-variant-numeric:tabular-nums;}
.position-detail{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}
.position-detail.up{border-left:3px solid var(--green);}
.position-detail.down{border-left:3px solid var(--red);}
.pos-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:10px;}
.pos-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;font-family:var(--mono);font-size:13px;}
.pos-cell .pos-key{font-size:10px;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:3px;letter-spacing:.05em;}
.pos-cell .pos-val{font-weight:600;font-variant-numeric:tabular-nums;}
.pos-link{color:var(--blue);font-family:var(--mono);font-size:12px;text-decoration:none;display:inline-block;margin-top:8px;}
.pos-link:hover{text-decoration:underline;}
.trades-section{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-top:20px;}
.trades-title{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.06em;margin-bottom:10px;font-weight:600;}
.trades-empty{color:var(--muted);font-family:var(--mono);font-size:12px;padding:8px 0;}
.trades-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
.trades-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px;min-width:520px;}
.trades-table th,.trades-table td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap;}
.trades-table th{color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em;}
.trades-table td.num{text-align:right;font-variant-numeric:tabular-nums;}
.trades-table tr:last-child td{border-bottom:none;}
.pill{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;}
.pill-win{background:rgba(63,185,80,.15);color:var(--green);}
.pill-loss{background:rgba(248,81,73,.15);color:var(--red);}
.pill-void{background:rgba(139,148,158,.15);color:var(--muted);}
.logs-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-top:20px;}
.recent-trades{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}
.recent-trades-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;font-weight:600;}
.recent-trades-list{display:flex;flex-direction:column;gap:6px;}
.trade-row{display:grid;grid-template-columns:50px 1fr 80px 80px 70px;gap:10px;align-items:center;padding:8px 10px;background:#1a1a1a;border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:12px;}
.trade-row.up{border-left:3px solid var(--green);}
.trade-row.down{border-left:3px solid var(--red);}
.trade-dir{font-weight:600;}
.trade-dir.up{color:var(--green);}
.trade-dir.down{color:var(--red);}
.trade-prices{color:var(--text);font-variant-numeric:tabular-nums;}
.trade-prices .arrow{color:var(--muted);margin:0 4px;}
.trade-res{text-align:center;font-weight:600;}
.trade-res.up{color:var(--green);}
.trade-res.down{color:var(--red);}
.trade-res.tp{color:var(--green);background:rgba(63,185,80,.12);padding:2px 6px;border-radius:3px;font-size:10px;}
.trade-res.sl{color:var(--red);background:rgba(248,81,73,.12);padding:2px 6px;border-radius:3px;font-size:10px;}
.trade-res.sl-late{color:#ff9c3a;background:rgba(255,140,0,.12);padding:2px 6px;border-radius:3px;font-size:10px;}
.trade-res.void{color:var(--muted);font-size:10px;}
.trade-pnl{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;}
.trade-pnl.up{color:var(--green);}
.trade-pnl.down{color:var(--red);}
.trade-time{color:var(--muted);text-align:right;font-size:11px;}
@media(max-width:640px){.trade-row{grid-template-columns:42px 1fr 60px 70px;font-size:11px;}.trade-row .trade-time{display:none;}}
.logs-title{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.06em;margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:10px;justify-content:space-between;}
.logs-list{display:flex;flex-direction:column;gap:6px;}
.log-row{display:flex;align-items:center;gap:12px;padding:8px 10px;background:#1a1a1a;border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:12px;}
.log-row .log-dataset{font-weight:600;color:var(--blue);min-width:90px;}
.log-row .log-date{color:var(--muted);min-width:90px;}
.log-row .log-size{color:var(--muted);min-width:80px;font-variant-numeric:tabular-nums;text-align:right;}
.log-row .log-rows{color:var(--muted);min-width:80px;font-variant-numeric:tabular-nums;text-align:right;}
.log-row a.log-dl{margin-left:auto;color:var(--green);text-decoration:none;padding:3px 10px;border:1px solid var(--green);border-radius:4px;font-size:11px;font-weight:600;}
.log-row a.log-dl:hover{background:rgba(63,185,80,.15);}
@media(max-width:700px){.log-row{flex-wrap:wrap;}.log-row .log-rows,.log-row .log-size{min-width:60px;}}
.footer{text-align:center;color:var(--muted);font-size:11px;padding:24px 20px;font-family:var(--mono);}
/* v6.1.0: both-sides panel */
.badge-bs{background:rgba(122,165,210,.15);color:var(--blue);border:1px solid rgba(122,165,210,.4);font-family:var(--mono);}
.bs-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}
.bs-panel.active{border-left:3px solid var(--muted);}
.bs-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:10px;justify-content:space-between;}
.bs-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:14px;}
.bs-stat{background:#1a1a1a;border:1px solid var(--border);border-radius:6px;padding:8px 10px;text-align:center;}
.bs-stat-label{font-size:9px;text-transform:uppercase;color:var(--muted);letter-spacing:.05em;}
.bs-stat-value{font-size:15px;font-family:var(--mono);font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums;}
.bs-positions{display:flex;flex-direction:column;gap:8px;}
.bs-empty{color:var(--muted);font-family:var(--mono);font-size:12px;padding:8px 0;text-align:center;}
.bs-pos{background:#1a1a1a;border:1px solid var(--border);border-radius:8px;padding:10px 12px;border-left:3px solid var(--muted);}
.bs-pos-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:8px;}
.bs-pos-id{font-weight:600;color:var(--text);}
.bs-pos-ttr{color:var(--yellow);font-weight:600;}
.bs-pos-status{color:var(--blue);font-size:10px;}
.bs-legs{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
@media(max-width:600px){.bs-legs{grid-template-columns:1fr;}}
.bs-leg{padding:8px 10px;background:#181818;border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:11px;}
.bs-leg.yes{border-left:2px solid var(--green);}
.bs-leg.no{border-left:2px solid var(--red);}
.bs-leg.closed{opacity:.7;}
.bs-leg-head{display:flex;justify-content:space-between;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:4px;}
.bs-leg-row{display:flex;justify-content:space-between;padding:1px 0;font-variant-numeric:tabular-nums;}
.bs-leg-key{color:var(--muted);}
.bs-leg-pnl.up{color:var(--green);font-weight:600;}
.bs-leg-pnl.down{color:var(--red);font-weight:600;}
.bs-leg-closed-tag{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.04em;}
</style></head><body>
<header><h1 id="header-title">polybot simple v1</h1><span id="version-badge" class="badge badge-version">v—</span><span id="mode-badge" class="badge badge-dry">DRY</span><span id="strategy-badge" class="badge badge-bs" style="display:none">BOTH-SIDES</span><span id="variant-badge" class="badge badge-bs" style="display:none">v621</span><span class="uptime" id="uptime">uptime —</span></header>
<main>
<div id="clob-warning" class="clob-warning" style="display:none">⚠ Polymarket rate-limiting detected — <span id="clob-warning-detail"></span></div>
<div class="grid">
<div class="card"><div class="card-title">Binance feed</div><div class="card-value" id="binance-price">—</div><div class="card-detail" id="binance-detail">no data</div></div>
<div class="card"><div class="card-title">Polymarket WS</div><div class="card-value" id="poly-state">—</div><div class="card-detail" id="poly-detail">no data</div></div>
</div>
<div class="bs-panel" id="bs-panel" style="display:none">
<div class="bs-title"><span id="bs-config-line" style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0"></span></div>
<div class="bs-stats">
<div class="bs-stat"><div class="bs-stat-label">Open</div><div class="bs-stat-value" id="bs-open">0</div></div>
<div class="bs-stat"><div class="bs-stat-label">Entered</div><div class="bs-stat-value" id="bs-entered">0</div></div>
<div class="bs-stat"><div class="bs-stat-label">Sold loser</div><div class="bs-stat-value" id="bs-sold">0</div></div>
<div class="bs-stat"><div class="bs-stat-label">Resolved</div><div class="bs-stat-value" id="bs-resolved">0</div></div>
<div class="bs-stat"><div class="bs-stat-label">Pending</div><div class="bs-stat-value" id="bs-pending">0</div></div>
<div class="bs-stat"><div class="bs-stat-label">P&amp;L today</div><div class="bs-stat-value" id="bs-pnl">$0.00</div></div>
<div class="bs-stat"><div class="bs-stat-label">5m / 15m / 60m</div><div class="bs-stat-value" id="bs-disc">0/0/0</div></div>
</div>
<div class="bs-positions" id="bs-positions"><div class="bs-empty">no open both-sides positions</div></div>
</div>
<div class="recent-trades" id="recent-trades-panel">
<div class="recent-trades-title">Last 10 trades</div>
<div class="recent-trades-list" id="recent-trades-list"><div class="trades-empty">no closed trades yet</div></div>
</div>
<div class="logs-panel">
<div class="logs-title"><span>CSV logs</span><span id="logs-meta" style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0"></span><button id="csv-del-btn" onclick="deleteOldCsv()" style="font-size:10px;margin-left:12px;padding:3px 9px;background:rgba(248,81,73,0.10);color:var(--red);border:1px solid rgba(248,81,73,0.30);border-radius:4px;cursor:pointer;">delete CSVs &gt;7d</button></div>
<div class="logs-list" id="logs-list"><div class="trades-empty">loading…</div></div>
</div>
<div id="bss-watch-bottom" style="margin-top:6px;"></div>
<div class="footer">Polling /api/status every 1s · <a href="/api/status" target="_blank" style="color:var(--blue)">view JSON</a> · <a href="/api/datasets" target="_blank" style="color:var(--blue)">view datasets JSON</a></div>
</main>
<script>
const $ = id => document.getElementById(id);
function fmtUptime(s){if(s==null)return '—';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=Math.floor(s%60);if(h)return `${h}h ${m}m ${sec}s`;if(m)return `${m}m ${sec}s`;return `${sec}s`;}
function fmtBytes(b){if(b==null)return '—';if(b<1024)return b+' B';if(b<1048576)return (b/1024).toFixed(1)+' KB';if(b<1073741824)return (b/1048576).toFixed(1)+' MB';return (b/1073741824).toFixed(2)+' GB';}
async function deleteOldCsv(){
  if(!confirm("Delete CSV log files older than 7 days? Today's files are kept. This cannot be undone."))return;
  const btn=$('csv-del-btn'); if(btn){btn.disabled=true;btn.textContent='deleting…';}
  try{
    const r=await fetch('/api/delete_old_csv?days=7',{cache:'no-store'});
    const d=await r.json();
    alert('Deleted '+(d.deleted_count||0)+' file(s); kept '+(d.kept||0)+'.'+((d.errors&&d.errors.length)?('\nErrors: '+d.errors.join('; ')):''));
  }catch(e){alert('delete failed: '+e);}
  if(btn){btn.disabled=false;btn.innerHTML='delete CSVs &gt;7d';}
  tickDatasets();
}
async function tickDatasets(){
try{
const r=await fetch('/api/datasets',{cache:'no-store'});
if(!r.ok)throw 0;
const d=await r.json();
const files=d.files||[];
const stats=d.writer_stats||{};
const list=$('logs-list');
if(!files.length){list.innerHTML='<div class="trades-empty">no log files yet</div>';$('logs-meta').textContent='';return;}
let totalRows=0;
const rowsHtml=files.map(f=>{
const wstats=stats[f.dataset]||{};
const rows=wstats.rows_written;
if(rows!=null)totalRows+=rows;
return `<div class="log-row">`
+`<span class="log-dataset">${f.dataset}</span>`
+`<span class="log-date">${f.date}</span>`
+`<span class="log-size">${fmtBytes(f.size_bytes)}</span>`
+`<span class="log-rows">${rows!=null?rows.toLocaleString()+' rows':'—'}</span>`
+`<a class="log-dl" href="/api/download/${f.filename}" download>download</a>`
+`</div>`;
}).join('');
list.innerHTML=rowsHtml;
$('logs-meta').textContent=files.length+' file'+(files.length===1?'':'s')+(totalRows?' · '+totalRows.toLocaleString()+' rows total':'');
}catch(e){$('logs-list').innerHTML='<div class="trades-empty">error loading logs list</div>';}
}
async function tick(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw 0;const s=await r.json();render(s);}catch(e){$('uptime').textContent='connection lost';}}
function render(s){
$('uptime').textContent='uptime '+fmtUptime(s.uptime_s);
const badge=$('mode-badge');badge.textContent=(s.mode||'dry').toUpperCase();badge.className='badge '+(s.mode==='live'?'badge-live':'badge-dry');
const vbadge=$('version-badge');if(s.bot_version){vbadge.textContent='v'+s.bot_version;vbadge.style.display='inline-block';}else{vbadge.style.display='none';}
if(s.binance&&s.binance.latest_price){$('binance-price').textContent='$'+s.binance.latest_price.toLocaleString(undefined,{maximumFractionDigits:0});$('binance-detail').textContent=`${s.binance.last_msg_age_s||'—'}s ago · ${s.binance.samples} samples`;}
$('poly-state').textContent=s.polymarket_ws&&s.polymarket_ws.books_tracked?(s.polymarket_ws.books_tracked+' books'):'—';
$('poly-detail').textContent=s.polymarket_ws&&s.polymarket_ws.last_msg_age_s!=null?(s.polymarket_ws.last_msg_age_s+'s ago'):'disconnected';
renderRecentTrades(s);
renderBothSides(s);
renderClobHealth(s);
}
function renderClobHealth(s){
  // v6.1.3: CLOB rate-limit banner — visible only when alert_425 is true.
  // alert_425 fires when 425-rate over the last 60s is >5% AND >=3 hits
  // (count guard prevents single-hit spurious alerts in low-traffic windows).
  const ch=s.clob_health||{};
  const w=$('clob-warning');
  if(ch.alert_425){
    w.style.display='block';
    $('clob-warning-detail').textContent=`${ch.n_425}/${ch.total} requests in last ${ch.window_s}s returned HTTP 425 (${ch.rate_425}%)`;
  }else if(ch.alert_5xx){
    w.style.display='block';
    $('clob-warning-detail').textContent=`${ch.n_5xx}/${ch.total} requests in last ${ch.window_s}s returned 5xx errors (${ch.rate_5xx}%)`;
  }else{
    w.style.display='none';
  }
}
function fmtAge(a){if(a==null)return '—';if(a<60)return a.toFixed(1)+'s';if(a<3600)return Math.floor(a/60)+'m '+Math.floor(a%60)+'s';return Math.floor(a/3600)+'h '+Math.floor((a%3600)/60)+'m';}
function fmtCountdown(s){if(s==null)return '—';const x=Math.round(s);if(x<=0)return 'now';if(x<60)return x+'s';return Math.floor(x/60)+'m '+(x%60)+'s';}
function fmtBookCell(v,digits){return v==null?'—':v.toFixed(digits);}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtAgeShort(sec){if(sec==null||!isFinite(sec))return '';if(sec<60)return Math.round(sec)+'s';if(sec<3600)return Math.round(sec/60)+'m';if(sec<86400)return Math.round(sec/3600)+'h';return Math.round(sec/86400)+'d';}
function fmtPrice(p){if(p==null)return '—';return Number(p).toFixed(2);}
function renderRecentTrades(s){
const list=$('recent-trades-list');
// v6.1.9: sparkline trades panel.
//   Schema per row (single line):
//     [pill] [sold|resolved · winner_badge]   [sparkline 100x24]   [+$X.XX BTC]   [pnl]
//   Color rule (sparkline + tinted bg):
//     outcome === 'LOSS'                    → yellow (attention)
//     market_winner === 'YES' (good outcome)→ green
//     market_winner === 'NO'  (good outcome)→ red
//     else                                  → muted gray
//   Backend supplies btc_strike + btc_samples (30 floats over trade lifetime,
//   computed in _bs_collect_btc_samples). On insufficient data → "—" placeholder.
const hist=((s.bs_state&&s.bs_state.trade_history)||[]).slice(-10).reverse();
if(!hist.length){
  list.innerHTML='<div class="trades-empty" style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:8px 0;">no resolved trades yet</div>';
  return;
}
const nowSec=Date.now()/1000;
list.innerHTML=hist.map(tr=>{
  // Color by outcome priority: LOSS yellow > winner green/red > muted
  let lineCol='#7a7a7a';
  if(tr.outcome==='LOSS') lineCol='#e0b340';
  else if(tr.market_winner==='YES') lineCol='#5cbd5c';
  else if(tr.market_winner==='NO') lineCol='#d96666';

  // Sparkline: 100x24 SVG, samples scaled into [4,20] band, dashed strike line at strike y.
  let sparkSvg='';
  let deltaStr='';
  const samples=tr.btc_samples||[];
  const strike=tr.btc_strike;
  if(samples.length>=2 && strike!=null){
    const lo=Math.min(strike, ...samples);
    const hi=Math.max(strike, ...samples);
    const range=Math.max(hi-lo, 1.0);
    const yFor=v=>(20-((v-lo)/range)*16).toFixed(1);
    const pts=samples.map((v,i)=>`${((i/(samples.length-1))*100).toFixed(1)},${yFor(v)}`).join(' ');
    const yStrike=yFor(strike);
    sparkSvg=`<svg viewBox="0 0 100 24" width="100" height="24" style="display:block;">`
      +`<line x1="0" y1="${yStrike}" x2="100" y2="${yStrike}" stroke="#444" stroke-width="0.5" stroke-dasharray="2,2"/>`
      +`<polyline fill="none" stroke="${lineCol}" stroke-width="1.4" points="${pts}"/>`
      +`</svg>`;
    const deltaUsd=samples[samples.length-1]-strike;
    deltaStr=(deltaUsd>=0?'+$':'-$')+Math.abs(deltaUsd).toFixed(2)+' BTC';
  }else{
    sparkSvg='<div style="width:100px;height:24px;color:var(--muted);font-size:10px;text-align:center;line-height:24px;">—</div>';
  }

  // Outcome pill (preserved from v6.1.8)
  let resHtml='';
  if(tr.outcome==='WIN') resHtml='<span class="trade-res up">WIN</span>';
  else if(tr.outcome==='LOSS') resHtml='<span class="trade-res down">LOSS</span>';
  else if(tr.market_winner==='YES') resHtml='<span class="trade-res up">YES WON</span>';
  else if(tr.market_winner==='NO') resHtml='<span class="trade-res down">NO WON</span>';
  else resHtml='<span class="trade-res void">EVEN</span>';
  const sold=tr.had_sell_loser?'sold':'resolved';
  const winnerBadge=(tr.market_winner && tr.outcome!=='EVEN')
    ? ` · <span style="color:var(--muted);">${tr.market_winner} won</span>` : '';
  const tot=tr.total_pnl;
  const pnlCls=tot>0.0001?'up':tot<-0.0001?'down':'';
  const pnlStr=(tot>=0?'+$':'-$')+Math.abs(tot||0).toFixed(2);
  const ageSec=tr.close_ts?(nowSec-tr.close_ts):null;
  // v6.2.3: clickable Polymarket link (↗ icon)
  const pmUrl=tr.market_url||'';
  const pmLink=pmUrl
    ? `<a href="${pmUrl}" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:none;font-size:11px;margin-left:6px;" title="Open on Polymarket">↗</a>`
    : '';

  return `<div class="trade-row" style="display:grid;grid-template-columns:55px 130px 100px 80px 90px 24px;gap:10px;align-items:center;padding:6px 10px;background:transparent;border:none;border-bottom:0.5px solid var(--border-soft);border-radius:0;font-family:var(--mono);font-size:11px;">`
    +`<span>${resHtml}</span>`
    +`<span style="color:var(--muted);font-size:10px;">${sold}${winnerBadge}</span>`
    +sparkSvg
    +`<span style="color:var(--muted);font-size:10px;text-align:right;">${deltaStr}</span>`
    +`<span class="${pnlCls}" style="text-align:right;font-weight:600;">${pnlStr} <span style="color:var(--muted);font-weight:400;">· ${fmtAgeShort(ageSec)}</span></span>`
    +`<span style="text-align:center;">${pmLink}</span>`
    +`</div>`;
}).join('');
}
function renderBothSides(s){
const panel=$('bs-panel');const sb=$('strategy-badge');const vb=$('variant-badge');
if(!s.bs_active){panel.style.display='none';sb.style.display='none';return;}
sb.style.display='inline-block';sb.textContent='BOTH-SIDES';sb.title='STRATEGY_MODE='+(s.strategy_mode||'?')+': trade 5m, log 15m+60m';
// v6.2.3: dashboard theming + branding for verification_late variant
const variant=(s.bs_strategy||'v621');
vb.style.display='inline-block';
vb.textContent=variant;
if(variant==='verification_late'){
  document.body.classList.add('theme-verification');
  const h1=$('header-title'); if(h1) h1.textContent='The Money Looser';
  document.title='The Money Looser';
  vb.style.background='rgba(217,122,74,.25)';vb.style.color='#e8a070';
  vb.style.border='1px solid rgba(217,122,74,.5)';
}else{
  document.body.classList.remove('theme-verification');
  const h1=$('header-title'); if(h1) h1.textContent='polybot simple v1';
  document.title='polybot simple';
}
panel.style.display='block';panel.classList.add('active');
const cfg=s.bs_config||{};
$('bs-config-line').textContent='lead '+(cfg.lead_min_s||0)+'-'+(cfg.lead_max_s||0)+'s · sum_ask≤'+(cfg.sum_ask_max||0).toFixed(2)+' · sell≥'+(cfg.sell_threshold||0).toFixed(2)+' (TTR≤'+(cfg.sell_ttr_floor_s||0)+'s, '+(cfg.sell_persist_s||0)+'s persist)';
const st=s.bs_state||{};
$('bs-open').textContent=st.open_count||0;
$('bs-entered').textContent=st.total_entered||0;
$('bs-sold').textContent=st.total_sold_loser||0;
$('bs-resolved').textContent=st.total_resolved||0;
$('bs-pending').textContent=st.total_pending||0;
const pendingClass=(st.total_pending||0)>0?'down':'';
$('bs-pending').className='bs-stat-value '+pendingClass;
const bp=st.pnl_today_usdc||0;$('bs-pnl').textContent=(bp>=0?'+$':'-$')+Math.abs(bp).toFixed(2);$('bs-pnl').className='bs-stat-value '+(bp>0?'up':bp<0?'down':'');
const d=st.discovery||{};
$('bs-disc').textContent=(d['5m_in_window']||0)+'/'+(d['15m_in_window']||0)+'/'+(d['60m_in_window']||0);
const positions=st.open_positions||[];
const watching=st.bss_watching||[];
const aborted=st.bss_aborted_today||[];
const bssActive=!!st.bss_strategy_active;
const list=$('bs-positions');
// v6.3.1: BSS-aware rendering. When bss_entry strategy active, show
// BSS state cards (WATCH / WAITING_2ND / ABORT) alongside BOTH-state
// positions. Old-style sum_ask@entry hidden in bss mode.
if(bssActive){
  const cards=[];
  if(positions.length||watching.length||aborted.length){
    cards.push(`<div style="font-size:11px;color:var(--muted);margin-bottom:6px;letter-spacing:0.5px;">BSS · ${watching.length} watching · ${positions.length} held · ${aborted.length} aborted today</div>`);
  }
  // v6.3.3: Design 3 — TWO-PANEL CHART for active trade (first item)
  function renderActiveChart(p){
    const samples=p.price_samples||[];
    if(samples.length<2){
      return `<div class="bs-pos" style="border-left:3px solid var(--yellow);padding:12px;">
        <div style="font-size:12px;color:var(--muted);">${escapeHtml((p.market_id||'').slice(0,12))}… — collecting price history…</div></div>`;
    }
    const t0=samples[0].ts, t1=samples[samples.length-1].ts, span=Math.max(1,t1-t0);
    const pmin=0.40, pmax=0.62; // y-axis range; clamp prices into here
    const W=280, H=60;
    const xy=(s,k)=>{const x=((s.ts-t0)/span)*W; const v=Math.max(pmin,Math.min(pmax,s[k])); const y=H - ((v-pmin)/(pmax-pmin))*(H-10) - 5; return [x,y];};
    const ptsY=samples.map(s=>xy(s,'y').join(',')).join(' ');
    const ptsN=samples.map(s=>xy(s,'n').join(',')).join(' ');
    // Marker positions
    const ttr=p.ttr_s||0;
    const nowTs=t1;
    const wOpenTs=p.window_open_ts;
    const f1Ts=p.first_fill_ts_abs;
    const f2Ts=p.second_fill_ts_abs;
    function vlineAt(ts,color,label){
      if(!ts) return '';
      const x=((ts-t0)/span)*W;
      if(x<0||x>W) return '';
      return `<line x1="${x}" y1="0" x2="${x}" y2="${H}" stroke="${color}" stroke-width="0.5" stroke-dasharray="1 2"/>`+
             (label?`<text x="${x+2}" y="9" font-size="9" fill="${color}" font-weight="500">${label}</text>`:'');
    }
    function fillMarkerAt(ts,k,samples,color){
      if(!ts) return '';
      // Find sample closest to ts
      let best=samples[0],bd=1e9;
      for(const s of samples){const d=Math.abs(s.ts-ts);if(d<bd){bd=d;best=s;}}
      const [x,y]=xy(best,k);
      return `<circle cx="${x}" cy="${y}" r="4" fill="${color}" stroke="white" stroke-width="1.5"/>`;
    }
    const yesFirst=p.first_side==='YES';
    const phase=p.first_filled_in_pre?'pre':'live';
    const yesIs=yesFirst?'first leg':(p.bss_state==='BOTH'||p.bss_state==='WAITING_2ND'?'second leg':'YES');
    const noIs=yesFirst?(p.bss_state==='BOTH'||p.bss_state==='WAITING_2ND'?'second leg':'NO'):'first leg';
    const stateBadge=p.bss_state==='BOTH'?`<span style="background:rgba(63,185,80,0.15);color:var(--green);padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;">BOTH · held</span>`:
                    p.bss_state==='WAITING_2ND'?`<span style="background:rgba(255,193,7,0.15);color:var(--yellow);padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;">WAITING_2ND</span>`:
                    `<span style="background:rgba(255,255,255,0.06);color:var(--muted);padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;">WATCH</span>`;
    return `<div class="bs-pos" style="border-left:3px solid ${p.bss_state==='BOTH'?'var(--green)':p.bss_state==='WAITING_2ND'?'var(--yellow)':'var(--muted)'};padding:12px;">
      <div style="display:flex;align-items:center;gap:10px;font-size:13px;margin-bottom:10px;padding-bottom:8px;border-bottom:0.5px solid rgba(255,255,255,0.08);">
        <span style="font-family:monospace;color:var(--muted);">${escapeHtml((p.market_id||'').slice(0,12))}…</span>
        <span style="color:var(--text);">${escapeHtml((p.slug||'').slice(0,42))}</span>
        <span style="margin-left:auto;color:var(--muted);">TTR ${fmtCountdown(ttr)}</span>
        ${stateBadge}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:8px;">
        <div style="background:rgba(56,139,253,0.08);border-radius:6px;padding:8px 10px;">
          <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;">
            <span style="font-weight:600;color:#88c0f9;">YES · ${escapeHtml(yesIs)}</span>
            <span style="color:var(--muted);">${yesFirst&&p.first_filled_in_pre?'★ pre':''}</span>
          </div>
          <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:60px;display:block;" aria-hidden="true">
            ${vlineAt(wOpenTs,'#888','window')}
            ${vlineAt(nowTs,'#888','now')}
            <polyline fill="none" stroke="#388bfd" stroke-width="1.5" points="${ptsY}"/>
            ${yesFirst?fillMarkerAt(f1Ts,'y',samples,'#388bfd'):fillMarkerAt(f2Ts,'y',samples,'#388bfd')}
          </svg>
          <div style="display:flex;justify-content:space-between;font-size:11px;margin-top:2px;color:#88c0f9;">
            <span>${yesFirst?(p.first_price?'@'+p.first_price.toFixed(3):''):(p.second_price?'@'+p.second_price.toFixed(3):'need <'+(p.current_threshold||0.50).toFixed(2))}</span>
            <span>now ${(p.yes_ask||0).toFixed(3)}</span>
          </div>
        </div>
        <div style="background:rgba(248,81,73,0.08);border-radius:6px;padding:8px 10px;">
          <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;">
            <span style="font-weight:600;color:#f9a3b3;">NO · ${escapeHtml(noIs)}</span>
            <span style="color:var(--muted);">${(!yesFirst)&&p.first_filled_in_pre?'★ pre':''}</span>
          </div>
          <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:60px;display:block;" aria-hidden="true">
            ${vlineAt(wOpenTs,'#888','window')}
            ${vlineAt(nowTs,'#888','now')}
            <polyline fill="none" stroke="#d4537e" stroke-width="1.5" points="${ptsN}"/>
            ${(!yesFirst)?fillMarkerAt(f1Ts,'n',samples,'#d4537e'):fillMarkerAt(f2Ts,'n',samples,'#d4537e')}
          </svg>
          <div style="display:flex;justify-content:space-between;font-size:11px;margin-top:2px;color:#f9a3b3;">
            <span>${!yesFirst?(p.first_price?'@'+p.first_price.toFixed(3):''):(p.second_price?'@'+p.second_price.toFixed(3):'need <'+(p.current_threshold||0.50).toFixed(2))}</span>
            <span>now ${(p.no_ask||0).toFixed(3)}</span>
          </div>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);">
        ${p.bss_state==='WAITING_2ND'?`<span>${p.in_pre_market?`pre-market · live opens in <b style="color:var(--yellow);">${fmtCountdown(p.pre_market_remaining_s||0)}</b> · `:`elapsed <b style="color:var(--text);">${(p.elapsed_s||0).toFixed(0)}s</b> · `}phase <b>${escapeHtml(p.phase||'')}</b> · sustain ${(p.second_sustain_s||0).toFixed(1)}/${(p.sustain_second_s||3).toFixed(0)}s</span><span>${p.in_pre_market?`<span style="color:var(--muted);">no abort during pre-market</span>`:`abort in <b style="color:${(p.abort_in_s||0)<30?'var(--red)':'var(--yellow)'};">${(p.abort_in_s||0).toFixed(0)}s</b>`}</span>`:''}
        ${p.bss_state==='BOTH'?`<span>cost <b style="color:var(--text);">$${((p.first_price||0)+(p.second_price||0)).toFixed(4)}</b></span><span>if win: <b style="color:var(--green);">+$${(1.0/Math.max(p.first_price||1,p.second_price||1) - 2.0).toFixed(4)}</b></span>`:''}
        ${p.bss_state==='WATCH'?`<span>need either side &lt;${(p.t_first||0.45).toFixed(2)} for ${(p.sustain_first_s||4).toFixed(0)}s · YES sus ${(p.yes_sustain_s||0).toFixed(1)}s · NO sus ${(p.no_sustain_s||0).toFixed(1)}s</span>`:''}
      </div>
    </div>`;
  }
  // Compact row for non-active items
  function renderCompact(p){
    const ttrStr=fmtCountdown(p.ttr_s);
    const isWaiting=p.bss_state==='WAITING_2ND';
    const stateColor=isWaiting?'var(--yellow)':'var(--muted)';
    const detail=isWaiting
      ? `1st ${escapeHtml(p.first_side||'')}@${(p.first_price||0).toFixed(3)} · 2nd ${escapeHtml(p.first_side==='YES'?'NO':'YES')} ${(p.other_ask||0).toFixed(3)}/<${(p.current_threshold||0).toFixed(2)} · ${p.in_pre_market?'pre':'abort '+(p.abort_in_s||0).toFixed(0)+'s'}`
      : `YES ${(p.yes_ask||0).toFixed(3)} · NO ${(p.no_ask||0).toFixed(3)} · need <${(p.t_first||0.45).toFixed(2)}`;
    return `<div style="padding:6px 10px;background:rgba(255,255,255,0.02);border-radius:4px;margin-top:4px;font-size:11px;display:flex;gap:10px;align-items:center;">
      <span style="font-family:monospace;color:var(--muted);">${escapeHtml((p.market_id||'').slice(0,10))}…</span>
      <span style="color:${stateColor};font-weight:600;min-width:80px;">${escapeHtml(p.bss_state)}</span>
      <span style="color:var(--muted);">TTR ${ttrStr}</span>
      <span style="margin-left:auto;color:var(--text);">${detail}</span>
    </div>`;
  }
  // v6.3.14 PPMP: watching/upcoming markets render in the QUIET bottom panel
  // (#bss-watch-bottom), not the main list. With a 60-min scan there can be
  // many; they stay visible-but-unobtrusive. Pre-market WATCH rows are now
  // shown here too (previously hidden up top as clutter).
  const watchCards=[];
  if(watching.length){
    const active=watching[0];
    // v6.3.19 fix: the active trade (watching[0], with chart if applicable)
    // belongs in the MAIN top list — it's the focal panel. Only the rest of
    // the watching markets render in the quiet bottom panel. (Regression from
    // 6.3.14 which sent the whole list to the bottom and made the active
    // trade display look like it was missing.)
    cards.push(active.chart_active?renderActiveChart(active):renderCompact(active));
    watching.slice(1).forEach(p=>watchCards.push(renderCompact(p)));
  }
  // BOTH state held (no chart, just summary)
  positions.forEach(p=>{
    const ttrStr=fmtCountdown(p.ttr_s);
    const yes=p.yes_leg||{},no=p.no_leg||{};
    const totalCost=(yes.entry_ask||0)+(no.entry_ask||0);
    const winnerPrice=Math.max(yes.entry_ask||0,no.entry_ask||0);
    const projGain=winnerPrice>0?(1.0/winnerPrice - 2.0):0;
    cards.push(`<div style="padding:8px 12px;background:rgba(63,185,80,0.06);border-radius:4px;margin-top:6px;border-left:3px solid var(--green);font-size:11px;display:flex;gap:10px;align-items:center;">
      <span style="font-family:monospace;color:var(--muted);">${escapeHtml((p.market_id||'').slice(0,10))}…</span>
      <span style="color:var(--green);font-weight:600;">BOTH · held</span>
      <span style="color:var(--muted);">TTR ${ttrStr}</span>
      <span style="margin-left:auto;color:var(--text);">YES@${(yes.entry_ask||0).toFixed(3)} NO@${(no.entry_ask||0).toFixed(3)} · cost $${totalCost.toFixed(4)} · win <b class="${projGain>0?'up':'down'}">${projGain>=0?'+':''}${projGain.toFixed(4)}</b></span>
    </div>`);
  });
  // Recent aborts (compact strip)
  if(aborted.length){
    cards.push(`<div style="font-size:10px;color:var(--muted);margin-top:8px;text-transform:uppercase;letter-spacing:0.5px;">recent aborts</div>`);
    aborted.slice(-5).forEach(a=>{
      const pnl=a.abort_pnl_usdc||0;
      cards.push(`<div style="padding:4px 8px;background:rgba(248,81,73,0.04);border-radius:3px;margin-top:3px;font-size:11px;display:flex;justify-content:space-between;">`
        +`<span>${escapeHtml((a.slug||'').slice(0,40))}</span>`
        +`<span style="color:var(--muted);">first ${escapeHtml(a.first_side||'')}@${(a.first_price||0).toFixed(3)} → sold@${(a.abort_sold_at||0).toFixed(3)}</span>`
        +`<span class="${pnl>=0?'up':'down'}">${pnl>=0?'+$':'-$'}${Math.abs(pnl).toFixed(2)}</span>`
        +`</div>`);
    });
  }
  const _wb=$('bss-watch-bottom');
  if(_wb){_wb.innerHTML = watchCards.length
    ? `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin:14px 0 6px;opacity:0.75;">upcoming · watching · ${watching.length}</div>`+watchCards.join('')
    : '';}
  if(!cards.length){list.innerHTML='<div class="bs-empty">BSS active · no markets in window yet</div>';return;}
  list.innerHTML=cards.join('');
  return;
}
{const _wbc=$('bss-watch-bottom');if(_wbc)_wbc.innerHTML='';}
if(!positions.length){list.innerHTML='<div class="bs-empty">no open both-sides positions</div>';return;}
// SPARKLINE: renders a 320x44 SVG of the YES (green) and NO (red) bid
// trajectories. If sell-loser has fired, draws a vertical dashed marker
// at sold_ttr_s elapsed seconds and a small dot at that y-value.
function renderLiveSparkline(p){
  const h=(p.live_bid_history||[]);
  if(h.length<2){return '<div style="height:44px;"></div>';}
  const W=320, H=44, padX=4, padY=4;
  const ts=h.map(s=>s[0]);
  const tMin=ts[0], tMax=ts[ts.length-1];
  const tRange=Math.max(1,tMax-tMin);
  const yMin=0, yMax=1;
  const xOf=t=>padX+((t-tMin)/tRange)*(W-2*padX);
  const yOf=v=>padY+(1-(v-yMin)/(yMax-yMin))*(H-2*padY);
  const yesPath=h.map((s,i)=>`${i?'L':'M'}${xOf(s[0]).toFixed(1)},${yOf(s[1]).toFixed(1)}`).join('');
  const noPath =h.map((s,i)=>`${i?'L':'M'}${xOf(s[0]).toFixed(1)},${yOf(s[2]).toFixed(1)}`).join('');
  const ref50y=yOf(0.5).toFixed(1);
  let marker='';
  if(p.sold_at_ts&&p.sold_side){
    // sold_ttr_s is TTR remaining at moment of sale; t_rel of sale = tMax - sold_ttr_s only approximate
    // better: compute t_rel from sold_at_ts - entry_ts; since we don't have entry_ts as t_rel=0, use the last sample as proxy
    // We have a tighter relation: sold_at_ts is wall-clock; but we kept t_rel relative to entry_ts in the buffer.
    // tMax = (sold_at_ts - entry_ts) approximately, since last sample is near-current. So tSold ≈ tMax.
    // For better accuracy when buffer continues past sell, compute tSold = (sold_at_ts - entry_ts).
    // We have sold_at_ts but not entry_ts here. Use tMax as approximation.
    const tSold=tMax;
    const xSold=xOf(tSold).toFixed(1);
    const sCol=p.sold_side==='YES'?'#d96666':'#5cbd5c';
    const ySold=yOf(p.sold_price||0).toFixed(1);
    marker=`<line x1="${xSold}" y1="${padY}" x2="${xSold}" y2="${H-padY}" stroke="#888" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>`
      +`<circle cx="${xSold}" cy="${ySold}" r="3" fill="${sCol}"/>`;
  }
  return `<div style="margin-top:8px;height:44px;">`
    +`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block;">`
    +`<line x1="${padX}" y1="${ref50y}" x2="${W-padX}" y2="${ref50y}" stroke="#444" stroke-width="0.5" stroke-dasharray="2,3"/>`
    +`<path d="${yesPath}" fill="none" stroke="#5cbd5c" stroke-width="1.5"/>`
    +`<path d="${noPath}"  fill="none" stroke="#d96666" stroke-width="1.5"/>`
    +marker
    +`</svg></div>`;
}
list.innerHTML=positions.map(p=>{
  const ttrStr=fmtCountdown(p.ttr_s);
  const yes=p.yes_leg||{};const no=p.no_leg||{};
  const yPnl=yes.closed?yes.pnl_usdc:yes.mark_pnl_usdc;
  const nPnl=no.closed?no.pnl_usdc:no.mark_pnl_usdc;
  const yCls=yPnl>0?'up':yPnl<0?'down':'';
  const nCls=nPnl>0?'up':nPnl<0?'down':'';
  const yClosedTag=yes.closed?` <span class="bs-leg-closed-tag">${escapeHtml(yes.close_reason||'closed')}</span>`:'';
  const nClosedTag=no.closed?` <span class="bs-leg-closed-tag">${escapeHtml(no.close_reason||'closed')}</span>`:'';
  // v6.1.2: pending/stuck chip — shown when settle cascade hasn't found a source yet
  const pendingAge=p.pending_age_s||0;
  let pendingChip='';
  if(pendingAge>0){
    const isStuck=pendingAge>=600;
    const tag=isStuck?'STUCK':'PENDING';
    const ageStr=pendingAge<60?Math.round(pendingAge)+'s':Math.floor(pendingAge/60)+'m';
    const chipColor=isStuck?'var(--red)':'var(--yellow)';
    pendingChip=`<span style="color:${chipColor};font-weight:600;background:rgba(248,81,73,0.12);padding:2px 8px;border-radius:3px;font-size:10px;">${tag} ${ageStr}</span>`;
  }
  return `<div class="bs-pos">`
    +`<div class="bs-pos-head">`
    +`<span class="bs-pos-id">${escapeHtml((p.market_id||'').slice(0,12))}…</span>`
    +`<span class="bs-pos-ttr">TTR ${ttrStr}</span>`
    +`${pendingChip}`
    +`<span class="bs-pos-status">${escapeHtml(p.sell_loser_status||'')}</span>`
    +`<span style="color:var(--muted);">sum_ask@entry: ${(p.sum_ask_at_entry||0).toFixed(4)}</span>`
    +`</div>`
    +`<div class="bs-legs">`
    +`<div class="bs-leg yes ${yes.closed?'closed':''}">`
    +`<div class="bs-leg-head"><span>YES</span>${yClosedTag}</div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">entry ask</span><span>${(yes.entry_ask||0).toFixed(4)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">qty</span><span>${(yes.qty_shares||0).toFixed(3)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">${yes.closed?'close':'mark'}</span><span>${(yes.closed?yes.close_price:yes.current_bid||0).toFixed(4)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">P&L</span><span class="bs-leg-pnl ${yCls}">${yPnl>=0?'+$':'-$'}${Math.abs(yPnl||0).toFixed(4)}</span></div>`
    +`</div>`
    +`<div class="bs-leg no ${no.closed?'closed':''}">`
    +`<div class="bs-leg-head"><span>NO</span>${nClosedTag}</div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">entry ask</span><span>${(no.entry_ask||0).toFixed(4)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">qty</span><span>${(no.qty_shares||0).toFixed(3)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">${no.closed?'close':'mark'}</span><span>${(no.closed?no.close_price:no.current_bid||0).toFixed(4)}</span></div>`
    +`<div class="bs-leg-row"><span class="bs-leg-key">P&L</span><span class="bs-leg-pnl ${nCls}">${nPnl>=0?'+$':'-$'}${Math.abs(nPnl||0).toFixed(4)}</span></div>`
    +`</div>`
    +`</div>`
    +renderLiveSparkline(p)
    +`</div>`;
}).join('');
}
tick();setInterval(tick,1000);
tickDatasets();setInterval(tickDatasets,10000);
</script></body></html>
"""


def _build_books_block(state: BotState, now: float) -> Optional[dict]:
    """v5.5.26: serialize current YES/NO books for dashboard display.
    Returns None when no market is selected. Each side has bid/ask/spread/
    edge/age_s; missing fields are None when book or quotes are unavailable."""
    market = state.btc_5m_market
    if market is None:
        return None

    def _side(token_id: str) -> dict:
        b = state.poly_books.get(token_id)
        if b is None:
            return {
                "bid": None, "ask": None, "spread": None, "edge": None,
                "bid_size": None, "ask_size": None, "age_s": None,
            }
        bid_v = b.bid if b.bid > 0 else None
        ask_v = b.ask if b.ask > 0 else None
        spread = (ask_v - bid_v) if (bid_v is not None and ask_v is not None) else None
        edge = (1.0 - ask_v) if ask_v is not None else None
        age = (now - b.last_update_ts) if b.last_update_ts else None
        return {
            "bid": round(bid_v, 4) if bid_v is not None else None,
            "ask": round(ask_v, 4) if ask_v is not None else None,
            "spread": round(spread, 4) if spread is not None else None,
            "edge": round(edge, 4) if edge is not None else None,
            "bid_size": round(b.bid_size, 2) if b.bid_size else None,
            "ask_size": round(b.ask_size, 2) if b.ask_size else None,
            "age_s": round(age, 2) if age is not None else None,
        }

    return {
        "yes": _side(market.yes_token_id),
        "no": _side(market.no_token_id),
    }


def _build_status_payload(state: BotState) -> dict:
    now = time.time()
    cfg = state.config

    # v5.5.24-fix: snapshot deque before iterating to avoid "deque mutated during iteration"
    cutoff = now - 60.0
    prices_snapshot = list(state.binance_prices)
    recent = [(t, p) for t, p in prices_snapshot if t >= cutoff]
    if len(recent) > 120:
        step = max(1, len(recent) // 120)
        recent = recent[::step]

    open_pos = None
    if state.open_position:
        p = state.open_position
        held_s = now - p.entry_ts
        # v5.5.26: current mark = best bid for the held token (what we'd net if we sold now).
        # If book is missing/stale or has no bid, fall back to entry price (mark = 0 PnL).
        current_book = state.poly_books.get(p.token_id)
        if current_book and current_book.bid > 0:
            current_mark = current_book.bid
        else:
            current_mark = p.entry_price
        qty = p.size_usdc / p.entry_price if p.entry_price > 0 else 0.0
        mark_pnl = qty * current_mark - p.size_usdc
        mark_pnl_pct = ((current_mark - p.entry_price) / p.entry_price * 100.0
                        if p.entry_price > 0 else 0.0)
        open_pos = {
            "direction": p.direction,
            "entry_price": p.entry_price,
            "size_usdc": p.size_usdc,
            "trade_id": p.trade_id,
            "edge_at_entry": p.edge_at_entry,
            "held_s": round(held_s, 1),
            "resolves_in_s": round(p.resolution_ts - now, 1),
            "market_url": p.market_url,
            "current_mark": round(current_mark, 4),
            "mark_pnl_usdc": round(mark_pnl, 4),
            "mark_pnl_pct": round(mark_pnl_pct, 3),
        }

    # v6.1.0: build both-sides snapshot block for the dashboard
    bs_open_positions = []
    for mid, pos in state.both_sides_positions.items():
        yes_book = state.poly_books.get(pos.yes_leg.token_id)
        no_book = state.poly_books.get(pos.no_leg.token_id)
        yes_mark_bid = float(yes_book.bid) if yes_book else 0.0
        no_mark_bid = float(no_book.bid) if no_book else 0.0
        # Per-leg mark P&L: if leg already closed, use its locked-in pnl_usdc.
        # Otherwise mark-to-bid (what we'd net selling now).
        if pos.yes_leg.closed:
            yes_mark_pnl = pos.yes_leg.pnl_usdc
        else:
            yes_mark_pnl = pos.yes_leg.qty_shares * yes_mark_bid - pos.yes_leg.size_usdc
        if pos.no_leg.closed:
            no_mark_pnl = pos.no_leg.pnl_usdc
        else:
            no_mark_pnl = pos.no_leg.qty_shares * no_mark_bid - pos.no_leg.size_usdc
        bs_open_positions.append({
            "market_id": mid,
            "market_question": pos.market_question[:60],
            "market_url": pos.market_url,
            "slug": pos.slug,
            "ttr_s": round(pos.end_ts - now, 1),
            "sum_ask_at_entry": round(pos.sum_ask_at_entry, 4),
            "yes_leg": {
                "side": "YES",
                "entry_ask": round(pos.yes_leg.entry_ask, 4),
                "qty_shares": round(pos.yes_leg.qty_shares, 4),
                "current_bid": round(yes_mark_bid, 4),
                "mark_pnl_usdc": round(yes_mark_pnl, 4),
                "closed": pos.yes_leg.closed,
                "close_reason": pos.yes_leg.close_reason,
                "close_price": round(pos.yes_leg.close_price, 4),
                "pnl_usdc": round(pos.yes_leg.pnl_usdc, 4),
            },
            "no_leg": {
                "side": "NO",
                "entry_ask": round(pos.no_leg.entry_ask, 4),
                "qty_shares": round(pos.no_leg.qty_shares, 4),
                "current_bid": round(no_mark_bid, 4),
                "mark_pnl_usdc": round(no_mark_pnl, 4),
                "closed": pos.no_leg.closed,
                "close_reason": pos.no_leg.close_reason,
                "close_price": round(pos.no_leg.close_price, 4),
                "pnl_usdc": round(pos.no_leg.pnl_usdc, 4),
            },
            "sell_loser_status": pos.sell_loser_status,
            "sell_loser_consecutive_ticks": pos.sell_loser_consecutive_ticks,
            # v6.1.2: pending state info for STUCK/PENDING chips on dashboard.
            # pending_age_s = 0 means not pending (still pre-resolution); else
            # the seconds elapsed since first pending detection.
            "pending_age_s": (round(now - pos.pending_since, 1)
                                if pos.pending_since > 0 else 0.0),
            "pending_attempts": pos.pending_attempts,
            # SPARKLINE: live (t_rel_s, yes_bid, no_bid) samples; capped 120
            "live_bid_history": list(pos.live_bid_history),
            # SOLD-LEG MARKER: empty until sell-loser fires; then non-zero
            "sold_at_ts": pos.sold_at_ts,
            "sold_side": pos.sold_side,
            "sold_price": pos.sold_price,
            "sold_ttr_s": pos.sold_ttr_s,
            "sold_reason": pos.sold_reason,
        })
    # Sort by TTR ascending (closest to resolution first — most actionable)
    bs_open_positions.sort(key=lambda x: x["ttr_s"])

    # v6.3.1: BSS watching list — markets in WATCH/WAITING_2ND/ABORT state
    # that don't yet have a BothSidesPosition. Surfaces what BSS is actually
    # doing in real-time. Only populated when _BS_STRATEGY=='bss_entry'.
    bss_watching = []
    bss_aborted_today = []
    if _BS_STRATEGY == "bss_entry":
        for cid, mdm in state.bs_5m_in_window.items():
            if mdm.duration_s != 300:
                continue
            ttr_s = mdm.market.end_ts - now
            yb = state.poly_books.get(mdm.market.yes_token_id)
            nb = state.poly_books.get(mdm.market.no_token_id)
            ya = float(yb.ask) if yb else 0.0
            na = float(nb.ask) if nb else 0.0
            yb_bid = float(yb.bid) if yb else 0.0
            nb_bid = float(nb.bid) if nb else 0.0
            yes_sus_s = (now - mdm.bss_yes_below_first_start_ts
                          if mdm.bss_yes_below_first_start_ts else 0.0)
            no_sus_s = (now - mdm.bss_no_below_first_start_ts
                         if mdm.bss_no_below_first_start_ts else 0.0)
            # v6.3.15: show the threshold that actually applies right now —
            # pre-market markets enter leg-1 at T_FIRST_PRE (0.49), not the
            # live-window T_FIRST (0.45). The dashboard was always showing
            # 0.45, which is unreachable pre-market and misleading.
            _wopen_disp = mdm.market.end_ts - mdm.duration_s
            _t_first_eff = (_BS_BSS_T_FIRST_PRE if now < _wopen_disp
                            else _BS_BSS_T_FIRST)
            entry = {
                "market_id": cid,
                "market_question": mdm.market.question[:60],
                "market_url": mdm.market.market_url,
                "slug": mdm.market.slug,
                "ttr_s": round(ttr_s, 1),
                "bss_state": mdm.bss_state,
                "yes_ask": round(ya, 4), "no_ask": round(na, 4),
                "yes_bid": round(yb_bid, 4), "no_bid": round(nb_bid, 4),
                "first_side": mdm.bss_first_side,
                "first_price": (round(mdm.bss_first_price, 4)
                                 if mdm.bss_first_price else None),
                "first_fill_ago_s": (round(now - mdm.bss_first_fill_ts, 1)
                                       if mdm.bss_first_fill_ts else None),
                "yes_sustain_s": round(yes_sus_s, 1),
                "no_sustain_s": round(no_sus_s, 1),
                "t_first": _t_first_eff,
                "sustain_first_s": _BS_BSS_SUSTAIN_FIRST_S,
            }
            if mdm.bss_state == "WAITING_2ND":
                elapsed = now - (mdm.bss_first_fill_ts or now)
                # v6.3.5: pre-market-aware phase determination. If we're
                # currently before window-open (or first fill happened in
                # pre AND we're still in pre), use pre-market threshold +
                # pre-market sustain counter; abort timer doesn't apply.
                window_open_ts = mdm.market.end_ts - mdm.duration_s
                in_pre_market_now = now < window_open_ts
                other_ask = na if mdm.bss_first_side == "YES" else ya
                if in_pre_market_now:
                    phase = "pre"
                    cur_thr = _BS_BSS_T_SECOND_PRE
                    sus = (now - mdm.bss_other_below_pre_start_ts
                            if mdm.bss_other_below_pre_start_ts else 0.0)
                    sustain_max = _BS_BSS_SUSTAIN_SECOND_PRE_S
                    abort_in = None  # no abort during pre-market
                    # In pre-market, the "elapsed since first fill" can
                    # include time before live window. The visible timer
                    # should be "live window opens in X" instead.
                    pre_market_remaining_s = window_open_ts - now
                else:
                    in_strict = elapsed <= _BS_BSS_RELAX_AT_S
                    cur_thr = (_BS_BSS_T_SECOND_STRICT if in_strict
                               else _BS_BSS_T_SECOND_RELAXED)
                    phase = "strict" if in_strict else "relaxed"
                    if in_strict:
                        sus = (now - mdm.bss_other_below_strict_start_ts
                                if mdm.bss_other_below_strict_start_ts else 0.0)
                    else:
                        sus = (now - mdm.bss_other_below_relax_start_ts
                                if mdm.bss_other_below_relax_start_ts else 0.0)
                    sustain_max = _BS_BSS_SUSTAIN_SECOND_S
                    # If first leg fired in pre-market, abort timer starts
                    # at window_open_ts (matches evaluator behavior)
                    if mdm.bss_first_filled_in_pre:
                        timer_start = max(mdm.bss_first_fill_ts or now, window_open_ts)
                    else:
                        timer_start = mdm.bss_first_fill_ts or now
                    abort_in = round(_BS_BSS_ABORT_AT_S - (now - timer_start), 1)
                    pre_market_remaining_s = None
                entry.update({
                    "elapsed_s": round(elapsed, 1),
                    "phase": phase,
                    "current_threshold": cur_thr,
                    "other_ask": round(other_ask, 4),
                    "second_sustain_s": round(sus, 1),
                    "sustain_second_s": sustain_max,
                    "abort_at_s": _BS_BSS_ABORT_AT_S,
                    "abort_in_s": abort_in,
                    "in_pre_market": in_pre_market_now,
                    "pre_market_remaining_s": (round(pre_market_remaining_s, 1)
                                                if pre_market_remaining_s is not None else None),
                })
            if mdm.bss_state in ("ABORT", "RESOLVED") and mdm.bss_abort_sold_at:
                # Compute realized P&L from abort
                fp = mdm.bss_first_price or 0.0
                size = state.config.position_size_usdc
                pnl = size * (mdm.bss_abort_sold_at / fp - 1.0) if fp > 0 else 0.0
                entry.update({
                    "abort_sold_at": round(mdm.bss_abort_sold_at, 4),
                    "abort_pnl_usdc": round(pnl, 4),
                })
                bss_aborted_today.append(entry)
            else:
                bss_watching.append(entry)
        # Sort: WAITING_2ND (most urgent) first, then WATCH by TTR ascending
        bss_watching.sort(key=lambda x: (
            0 if x["bss_state"] == "WAITING_2ND" else 1,
            x.get("abort_in_s", 1e9),
            x["ttr_s"],
        ))
        # v6.3.3: active trade gets full chart data (Design 3). Identified
        # as the FIRST item after sorting (WAITING_2ND with lowest abort
        # timer, or first WATCH by TTR if no WAITING_2ND). Other entries
        # get the compact view only.
        if bss_watching:
            active_cid = bss_watching[0]["market_id"]
            active_mdm = state.bs_5m_in_window.get(active_cid)
            if active_mdm is not None:
                wopen = active_mdm.market.end_ts - active_mdm.duration_s
                samples = list(active_mdm.bss_price_samples)
                # downsample to ≤300 points to keep payload manageable
                if len(samples) > 300:
                    step = len(samples) // 300
                    samples = samples[::step]
                bss_watching[0]["chart_active"] = True
                bss_watching[0]["window_open_ts"] = wopen
                bss_watching[0]["price_samples"] = [
                    {"ts": round(t, 1), "y": round(ya, 4), "n": round(na, 4)}
                    for t, ya, na in samples
                ]
                bss_watching[0]["first_fill_ts_abs"] = active_mdm.bss_first_fill_ts
                bss_watching[0]["second_fill_ts_abs"] = active_mdm.bss_second_fill_ts
                bss_watching[0]["second_price"] = active_mdm.bss_second_price
                bss_watching[0]["second_phase"] = active_mdm.bss_second_phase
                bss_watching[0]["first_filled_in_pre"] = active_mdm.bss_first_filled_in_pre

    return {
        "status": "ok",
        "uptime_s": round(state.uptime_s, 1),
        "mode": state.mode,
        "bot_version": BOT_VERSION,        # v5.7.0
        "validation_mode": cfg.validation_mode,
        "signal_invert": _SIGNAL_INVERT,
        "take_profit_threshold": _TP_THRESHOLD,    # v5.7.0
        "take_profit_persist_s": _TP_PERSIST_S,    # v5.7.0
        "stop_loss_threshold": _STOP_LOSS_THRESHOLD,  # v5.8.0
        "stop_loss_persist_s": _SL_PERSIST_S,         # v5.8.0
        "stop_loss_min_entry": _SL_MIN_ENTRY,         # v5.8.0 (revised)
        "block_reentry": _BLOCK_REENTRY,              # v5.8.0
        "exited_market_count": len(state.exited_market_ids),  # v5.8.0
        "sl_late_mode": _SL_LATE_MODE,                # v5.8.1
        "sl_late_pct": _SL_LATE_PCT,                  # v5.8.1
        "sl_late_floor": _SL_LATE_FLOOR,              # v5.8.1
        "sl_late_window_s": _SL_LATE_WINDOW_S,        # v5.8.1
        "sl_late_persist_s": _SL_LATE_PERSIST_S,      # v5.8.1
        # v6.1.0: strategy mode + both-sides config + state
        "strategy_mode": _STRATEGY_MODE,
        "bs_active": _BS_ACTIVE,
        # v6.2.2: strategy variant (selects sell-loser logic)
        "bs_strategy": _BS_STRATEGY,
        # v6.1.3: CLOB HTTP health snapshot for the dashboard banner
        "clob_health": _compute_clob_health(60.0),
        "bs_config": {
            "lead_min_s": _BS_LEAD_MIN_S,
            "lead_max_s": _BS_LEAD_MAX_S,
            "sum_ask_max": _BS_SUM_ASK_MAX,
            "sell_threshold": _BS_SELL_THRESH,
            "sell_ttr_floor_s": _BS_SELL_TTR_FLOOR_S,
            "sell_persist_s": _BS_SELL_PERSIST_S,
            "sell_min_loser_bid": _BS_SELL_MIN_BID,
            "tier_enabled": _BS_TIER_ENABLED,
            "tier_ladder": (
                f"T0(any,≥{_BS_TIER_T0_WINNER:.2f},strict) "
                f"T1(≤{_BS_TIER_T1_TTR:.0f}s,≥{_BS_TIER_T1_WINNER:.2f}) "
                f"T2(≤{_BS_TIER_T2_TTR:.0f}s,≥{_BS_TIER_T2_WINNER:.2f}) "
                f"T3(≤{_BS_TIER_T3_TTR:.0f}s,≥{_BS_TIER_T3_WINNER:.2f}) "
                f"persist={_BS_TIER_PERSIST_S:.0f}s"
            ) if _BS_TIER_ENABLED else "disabled (legacy v621 active)",
            "log_15m_prefix": _LOG_15M_PREFIX,
            "log_60m_prefix": _LOG_60M_PREFIX,
            "log_window_min_s": _LOG_WINDOW_MIN_S,
            "log_window_max_s": _LOG_WINDOW_MAX_S,
            "log_sample_interval_s": _LOG_SAMPLE_INTERVAL_S,
        },
        "bs_state": {
            "open_count": len(state.both_sides_positions),
            "total_entered": state.bs_total_entered,
            "total_sold_loser": state.bs_total_sold_loser,
            "total_resolved": state.bs_total_resolved,
            # v6.1.2: total_voided REMOVED. VOID is not a valid concept
            # for binary BTC up/down markets. Replaced by total_pending
            # (live count of positions stuck in resolution cascade) and
            # max_pending_age_s (longest-pending position's elapsed time).
            "total_pending": sum(1 for p in state.both_sides_positions.values()
                                  if p.pending_since > 0),
            "max_pending_age_s": (
                round(max((time.time() - p.pending_since
                            for p in state.both_sides_positions.values()
                            if p.pending_since > 0), default=0.0), 1)),
            "pnl_today_usdc": round(state.bs_pnl_today_usdc, 4),
            "discovery": dict(state.bs_discovery_diag),
            "open_positions": bs_open_positions,
            # v6.3.1: BSS state surfaces — only populated in bss_entry mode
            "bss_watching": bss_watching,
            "bss_aborted_today": bss_aborted_today[-20:],
            "bss_strategy_active": _BS_STRATEGY == "bss_entry",
            # v6.1.2: rolling history for "Last 5 trades" panel.
            "trade_history": state.bs_trade_history[-15:],
        },
        "config": {
            "delta_threshold_pct": cfg.delta_threshold_pct,
            "lookback_s": cfg.lookback_s,
            "ws_freshness_s": cfg.ws_freshness_s,
            "position_size_usdc": cfg.position_size_usdc,
            "entry_price_min": cfg.entry_price_min,
            "entry_price_max": cfg.entry_price_max,
            "edge_min": cfg.edge_min,
            "spread_max": cfg.spread_max,
        },
        "binance": {
            "connected": state.binance_ws_connected,
            "last_msg_age_s": (round(now - state.binance_last_msg_ts, 2)
                               if state.binance_last_msg_ts else None),
            "latest_price": (prices_snapshot[-1][1] if prices_snapshot else None),
            "samples": len(prices_snapshot),
            "recent_prices": recent,
        },
        "polymarket_ws": {
            "connected": state.poly_ws_connected,
            "last_msg_age_s": (round(now - state.poly_last_msg_ts, 2)
                               if state.poly_last_msg_ts else None),
            "books_tracked": len(state.poly_books),
        },
        # v5.5.26: live book panel data
        "books": _build_books_block(state, now),
        "market": (
            {
                "question": state.btc_5m_market.question,
                "ends_in_s": round(state.btc_5m_market.end_ts - now, 1),
                "url": state.btc_5m_market.market_url,
            } if state.btc_5m_market else None
        ),
        "signal": {
            "status": state.signal_status_msg,
            "live_delta_pct": (round(state.live_delta_pct, 4)
                               if state.live_delta_pct is not None else None),
            "lookback_s": (round(state.live_lookback_s, 2)
                           if state.live_lookback_s is not None else None),
            "last_validation_ok": state.last_validation_ok,
            "last_validation_reason": state.last_validation_reason,
            "last_decision_reason": state.last_decision_reason,
        },
        "position": ("OPEN" if state.open_position else "NONE"),
        "open_position": open_pos,
        "trades_today": state.trades_today,
        "trades_won": state.trades_won,
        "trades_lost": state.trades_lost,
        "pnl_today_usdc": round(state.pnl_today_usdc, 4),
        "skips_today": state.skips_today,
        "skips_by_reason": dict(state.skips_by_reason),
        "trade_history": state.trade_history[-15:],
    }


def http_server_thread(state: BotState) -> None:
    cfg = state.config

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/status" or path == "/api/status/":
                payload = _build_status_payload(state)
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/datasets":
                files = []
                if state.log_dir:
                    files = list_log_files(Path(state.log_dir))
                stats = {}
                # v5.6.0: include depth_logger and flow_logger
                for ldr in (state.binance_logger, state.signal_logger,
                            state.trades_logger, state.depth_logger,
                            state.flow_logger):
                    if ldr is not None:
                        stats[ldr.dataset] = ldr.stats()
                payload = {"files": files, "writer_stats": stats}
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            m = re.match(r"^/api/download/([a-z0-9_]+_\d{4}-\d{2}-\d{2}\.csv)$", path)
            if m and state.log_dir:
                filename = m.group(1)
                file_path = Path(state.log_dir) / filename
                try:
                    file_path = file_path.resolve()
                    if not str(file_path).startswith(str(Path(state.log_dir).resolve())):
                        raise ValueError("path escape")
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return
                if not file_path.exists() or not file_path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return

                # v5.6.0: include depth_logger and flow_logger so a download
                # of a partially-written daily file gets the latest buffered rows.
                for ldr in (state.binance_logger, state.signal_logger,
                            state.trades_logger, state.depth_logger,
                            state.flow_logger):
                    if ldr is not None and filename.startswith(ldr.dataset + "_"):
                        if ldr._current_file is not None:
                            try: ldr._current_file.flush()
                            except Exception: pass

                try:
                    body = file_path.read_bytes()
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f"read error: {e}".encode())
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # v6.2.5: prefix download filename with BOT_NAME so two bots'
                # CSVs don't overwrite each other in the user's local
                # Downloads folder. Server-side filename is unchanged.
                download_name = f"{_BOT_NAME}_{filename}" if _BOT_NAME else filename
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{download_name}"')
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/delete_old_csv" and state.log_dir:
                # v6.3.14: delete CSV log files older than ?days=N (default 7).
                # Only files matching dataset_YYYY-MM-DD.csv; never anything
                # modified within the cutoff (today's active files are safe).
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                days = 7
                for part in qs.split("&"):
                    if part.startswith("days="):
                        try:
                            days = max(1, min(365, int(float(part[5:]))))
                        except Exception:
                            days = 7
                cutoff = time.time() - days * 86400.0
                base = Path(state.log_dir)
                deleted: List[str] = []
                kept = 0
                errs: List[str] = []
                try:
                    for f in base.glob("*.csv"):
                        try:
                            if not re.match(r"^[a-z0-9_]+_\d{4}-\d{2}-\d{2}\.csv$",
                                            f.name):
                                kept += 1
                                continue
                            if f.stat().st_mtime < cutoff:
                                f.unlink()
                                deleted.append(f.name)
                            else:
                                kept += 1
                        except Exception as e:
                            errs.append(f"{f.name}: {type(e).__name__}")
                except Exception as e:
                    errs.append(str(e))
                payload = {"deleted": deleted, "deleted_count": len(deleted),
                           "kept": kept, "older_than_days": days,
                           "errors": errs}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                print(f"[dashboard] delete_old_csv days={days} "
                      f"deleted={len(deleted)} kept={kept}", flush=True)
                return

            if path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found\n")

        def log_message(self, fmt, *args):
            return

    addr = ("0.0.0.0", cfg.port)
    while not state.kill_flag:
        try:
            server = ThreadingHTTPServer(addr, _Handler)
            print(f"[http] listening on {addr[0]}:{addr[1]}", flush=True)
            server.serve_forever(poll_interval=1.0)
        except Exception as e:
            print(f"[http] crash: {e}", flush=True)
            time.sleep(2.0)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL COMPUTE + VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _bump_skip(state: BotState, reason: str) -> None:
    state.skips_today += 1
    state.skips_by_reason[reason] = state.skips_by_reason.get(reason, 0) + 1


def compute_signal(state: BotState) -> Tuple[Optional[Signal], str]:
    """
    v5.5.24-fix:
    1. Snapshot binance_prices deque before iterating (avoid race with WS thread).
    2. If SIGNAL_INVERT env var is true, flip the resulting direction UP↔DOWN.
    """
    cfg = state.config
    state.live_delta_pct = None
    state.live_lookback_s = None

    # FIX 1: snapshot deque to avoid "deque mutated during iteration"
    prices = list(state.binance_prices)

    if len(prices) < 2:
        return None, "insufficient_data"

    now = time.time()
    target_ts = now - cfg.lookback_s
    latest_ts, latest_price = prices[-1]

    then_ts, then_price = prices[0]
    for ts, price in reversed(prices):
        if ts <= target_ts:
            then_ts, then_price = ts, price
            break

    actual_lookback = latest_ts - then_ts
    if actual_lookback < cfg.lookback_s * 0.7:
        state.live_lookback_s = actual_lookback
        return None, f"lookback_too_short:{actual_lookback:.1f}s"

    if then_price <= 0:
        return None, "bad_then_price"

    delta_pct = (latest_price - then_price) / then_price * 100.0
    state.live_delta_pct = delta_pct
    state.live_lookback_s = actual_lookback

    if abs(delta_pct) < cfg.delta_threshold_pct:
        return None, f"below_threshold:{delta_pct:+.3f}%"

    direction = "UP" if delta_pct > 0 else "DOWN"

    # FIX 2: SIGNAL_INVERT — flip after raw direction is computed.
    # Everything downstream (Signal.direction, logs, trade_history) uses inverted dir.
    if _SIGNAL_INVERT:
        direction = "DOWN" if direction == "UP" else "UP"

    return Signal(
        coin="BTC",
        direction=direction,
        delta_pct=delta_pct,
        binance_price_now=latest_price,
        binance_price_then=then_price,
        computed_ts=now,
    ), "ok"


def validate_for_entry(state: BotState, signal: Signal) -> Tuple[bool, str]:
    cfg = state.config
    now = time.time()

    if state.binance_last_msg_ts <= 0:
        return False, "binance_no_msgs"
    age = now - state.binance_last_msg_ts
    if age > 3.0:
        return False, f"binance_stale:{age:.1f}s"

    market = state.btc_5m_market
    if market is None:
        return False, "no_market"

    yes_book = state.poly_books.get(market.yes_token_id)
    no_book = state.poly_books.get(market.no_token_id)
    if yes_book is None:
        return False, "yes_book_missing"
    if no_book is None:
        return False, "no_book_missing"

    yes_age = now - yes_book.last_update_ts
    no_age = now - no_book.last_update_ts
    if yes_age > cfg.ws_freshness_s:
        return False, f"yes_book_stale:{yes_age:.1f}s"
    if no_age > cfg.ws_freshness_s:
        return False, f"no_book_stale:{no_age:.1f}s"

    target_book = yes_book if signal.direction == "UP" else no_book
    if target_book.bid <= 0 or target_book.ask <= 0:
        return False, "book_no_quotes"
    if target_book.ask <= target_book.bid:
        return False, f"book_crossed:bid={target_book.bid:.3f},ask={target_book.ask:.3f}"

    return True, "ok"


def signal_tick(state: BotState) -> None:
    signal, status = compute_signal(state)
    state.signal_status_msg = status

    if signal is None:
        state.last_validation_ok = None
        state.last_validation_reason = ""
    else:
        state.last_signal = signal
        valid, reason = validate_for_entry(state, signal)
        state.last_validation_ok = valid
        state.last_validation_reason = reason

        # v6.1.0: in both_sides_btc mode, the lag-signal path does NOT drive
        # entries. The signal is still computed and (below) written to
        # signal_log_<date>.csv for after-the-fact analysis. Entries are
        # placed by both_sides_tick instead. We skip the entry decision
        # path entirely while leaving signal_log writing intact.
        if _BS_ACTIVE:
            pass
        elif valid:
            decision = compute_strategy_decision(state, signal)
            state.last_decision_reason = decision.reason

            if decision.should_trade:
                pos = place_entry(state, signal, decision)
                if pos is not None:
                    state.open_position = pos
                    state.trades_today += 1
                    print(
                        f"[trade] OPEN trade_id={pos.trade_id} dir={pos.direction} "
                        f"entry={pos.entry_price:.3f} size=${pos.size_usdc:.2f} "
                        f"resolution_in={pos.resolution_ts - time.time():.0f}s",
                        flush=True,
                    )
            else:
                _bump_skip(state, decision.reason.split(":", 1)[0])
                print(
                    f"[strategy] SKIP {signal.direction} delta={signal.delta_pct:+.3f}% "
                    f"reason={decision.reason} ask={decision.ask:.3f} edge={decision.edge:.3f}",
                    flush=True,
                )
        else:
            _bump_skip(state, reason.split(":", 1)[0])
            print(
                f"[signal] SKIP {signal.direction} delta={signal.delta_pct:+.3f}% "
                f"reason={reason}",
                flush=True,
            )

    if state.signal_logger is not None:
        _log_signal_tick(state)


def _log_signal_tick(state: BotState) -> None:
    now = time.time()

    prices_snapshot = list(state.binance_prices)
    binance_price = prices_snapshot[-1][1] if prices_snapshot else None
    binance_age = (now - state.binance_last_msg_ts) if state.binance_last_msg_ts else None

    market = state.btc_5m_market
    yes_book = state.poly_books.get(market.yes_token_id) if market else None
    no_book = state.poly_books.get(market.no_token_id) if market else None

    # v5.5.31: compute delta_from_start_pct = (binance_price - market_open_btc) / market_open_btc * 100
    # Both fields blank when market is None or open_btc isn't yet resolved.
    market_open_btc = market.open_btc_price if market else None
    if (market_open_btc is not None and binance_price is not None
            and market_open_btc > 0):
        delta_from_start_pct = (binance_price - market_open_btc) / market_open_btc * 100.0
    else:
        delta_from_start_pct = None

    def _f(v, fmt="{:.4f}"):
        return fmt.format(v) if v is not None else ""

    row = [
        int(now * 1000),
        f"{state.uptime_s:.1f}",
        _f(binance_price, "{:.2f}"),
        _f(binance_age, "{:.3f}"),
        len(prices_snapshot),
        _f(state.live_lookback_s, "{:.2f}"),
        _f(state.live_delta_pct, "{:.5f}"),
        state.signal_status_msg,
        _f(yes_book.bid if yes_book else None, "{:.4f}"),
        _f(yes_book.ask if yes_book else None, "{:.4f}"),
        _f((now - yes_book.last_update_ts) if yes_book else None, "{:.3f}"),
        _f(no_book.bid if no_book else None, "{:.4f}"),
        _f(no_book.ask if no_book else None, "{:.4f}"),
        _f((now - no_book.last_update_ts) if no_book else None, "{:.3f}"),
        market.question if market else "",
        _f((market.end_ts - now) if market else None, "{:.1f}"),
        _f(market_open_btc, "{:.2f}"),                  # v5.5.31
        _f(delta_from_start_pct, "{:.5f}"),             # v5.5.31
        "" if state.last_validation_ok is None else ("1" if state.last_validation_ok else "0"),
        state.last_validation_reason or "",
    ]
    state.signal_logger.log(row)


# ═══════════════════════════════════════════════════════════════════
# v5.6.0: DEPTH + FLOW LOGGING (no strategy logic)
# ═══════════════════════════════════════════════════════════════════

def _infer_trade_side(event: dict, book: Optional[PolyBook]) -> str:
    """v5.6.0: classify a `last_trade_price` event as BUY/SELL/UNKNOWN.

    Uses an explicit `side` field if Polymarket sends one. Otherwise infers
    from fill price vs current top-of-book:
      - price >= ask  → buyer aggressed   (BUY)
      - price <= bid  → seller aggressed  (SELL)
      - inside spread → attribute to nearer of bid/ask via mid

    Returns 'UNKNOWN' if the book is missing or has no quotes (rare; mostly
    at session start before the first `book` snapshot arrives).
    """
    explicit = str(event.get("side") or "").upper()
    if explicit in ("BUY", "SELL"):
        return explicit
    try:
        price = float(event.get("price", 0))
    except (ValueError, TypeError):
        return "UNKNOWN"
    if book is None or price <= 0:
        return "UNKNOWN"
    if book.ask > 0 and price >= book.ask:
        return "BUY"
    if book.bid > 0 and price <= book.bid:
        return "SELL"
    if book.bid > 0 and book.ask > 0:
        mid = (book.bid + book.ask) / 2.0
        return "BUY" if price >= mid else "SELL"
    return "UNKNOWN"


def _compute_flow_window(trades_deque: Optional[Deque],
                         now: float,
                         window_s: float) -> Dict[str, Any]:
    """v5.6.0: aggregate trade flow over the last `window_s` seconds.

    All volumes are USDC notional (price * size). UNKNOWN-side trades
    contribute to n and vwap but to neither buy_vol nor sell_vol.
    """
    empty = {"n": 0, "buy_vol": 0.0, "sell_vol": 0.0,
             "net": 0.0, "vwap": None, "last_fill_ts_ms": None}
    if trades_deque is None or not trades_deque:
        return empty
    cutoff = now - window_s
    n = 0
    buy_vol = 0.0
    sell_vol = 0.0
    notional_total = 0.0
    size_total = 0.0
    last_fill_ts = 0.0
    # Snapshot to avoid mutation during iteration (WS thread is producer).
    snapshot = list(trades_deque)
    for ts, price, size, side in snapshot:
        if ts < cutoff:
            continue
        notional = price * size
        n += 1
        notional_total += notional
        size_total += size
        if ts > last_fill_ts:
            last_fill_ts = ts
        if side == "BUY":
            buy_vol += notional
        elif side == "SELL":
            sell_vol += notional
    vwap = (notional_total / size_total) if size_total > 0 else None
    return {
        "n": n,
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "net": buy_vol - sell_vol,
        "vwap": vwap,
        "last_fill_ts_ms": int(last_fill_ts * 1000) if last_fill_ts > 0 else None,
    }


def _log_depth_tick(state: BotState) -> None:
    """v5.6.0: emit one row to depth_log per main_loop tick.

    v6.2.0: emit one row PER in-flight bs_position (was: only btc_5m_market).
    Previously this logged only the soonest 5m market, leaving older
    positions with zero depth coverage during their firing window — caused
    a 45% gap in held-both depth data observed May 2-3. Now iterates over
    state.both_sides_positions and additionally logs btc_5m_market if it
    is not already a position (covers the entry-window candidate).

    Volume impact: ~3-4× rows/tick when bs_open is healthy. Storage is
    proportional (~130MB/day vs prior ~30MB/day), still well within budget.
    """
    if state.depth_logger is None:
        return

    # Build target list: all in-flight bs_positions, plus btc_5m_market
    # if it's not already represented (covers the not-yet-entered candidate).
    targets: List[Tuple[str, str, str, str]] = []  # (mid, slug, yes_tid, no_tid)
    seen_mids: set = set()
    for pos in state.both_sides_positions.values():
        targets.append((pos.market_id, pos.slug,
                        pos.yes_leg.token_id, pos.no_leg.token_id))
        seen_mids.add(pos.market_id)

    market = state.btc_5m_market
    if market is not None and market.condition_id not in seen_mids:
        targets.append((market.condition_id, market.slug,
                        market.yes_token_id, market.no_token_id))

    if not targets:
        return

    now = time.time()

    def _level_cells(levels: List[Tuple[float, float]]) -> List[str]:
        # Interleaved layout [p1, s1, p2, s2, ..., p5, s5]. CSV header
        # below in boot() interleaves to match.
        out: List[str] = []
        for i in range(DEPTH_LEVELS):
            if i < len(levels):
                p, s = levels[i]
                out.append(f"{p:.4f}")
                out.append(f"{s:.2f}")
            else:
                out.append("")
                out.append("")
        return out

    def _book_cells(book: Optional[PolyBook]) -> Tuple[List[str], float, float, float]:
        if book is None:
            empty = [""] * (DEPTH_LEVELS * 2)
            return empty + empty, 0.0, 0.0, 0.0
        bid_cells = _level_cells(book.bid_levels)
        ask_cells = _level_cells(book.ask_levels)
        bid_depth = sum(s for (_, s) in book.bid_levels)
        ask_depth = sum(s for (_, s) in book.ask_levels)
        denom = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / denom if denom > 0 else 0.0
        return bid_cells + ask_cells, bid_depth, ask_depth, imbalance

    def _f(v, fmt="{:.4f}"):
        return fmt.format(v) if v is not None else ""

    for mid, slug, yes_tid, no_tid in targets:
        yes_book = state.poly_books.get(yes_tid)
        no_book = state.poly_books.get(no_tid)
        yes_cells, yes_bid_depth, yes_ask_depth, yes_imb = _book_cells(yes_book)
        no_cells, no_bid_depth, no_ask_depth, no_imb = _book_cells(no_book)

        yes_age = ((now - yes_book.last_book_snapshot_ts)
                   if yes_book and yes_book.last_book_snapshot_ts else None)
        no_age = ((now - no_book.last_book_snapshot_ts)
                  if no_book and no_book.last_book_snapshot_ts else None)

        row = [
            int(now * 1000),
            mid,
            slug,
            *yes_cells,            # 20 cells: yes_bid p1..s5 + yes_ask p1..s5
            *no_cells,             # 20 cells: no_bid + no_ask
            f"{yes_bid_depth:.2f}",
            f"{yes_ask_depth:.2f}",
            f"{no_bid_depth:.2f}",
            f"{no_ask_depth:.2f}",
            f"{yes_imb:+.4f}",
            f"{no_imb:+.4f}",
            _f(yes_age, "{:.3f}"),
            _f(no_age, "{:.3f}"),
        ]
        state.depth_logger.log(row)


def _log_flow_tick(state: BotState) -> None:
    """v5.6.0: emit one row to flow_log per main_loop tick.

    Skips when no market is selected. Emits zeros (rather than empty cells)
    for the n/vol fields when the deque is empty, so downstream code never
    has to special-case missing trades. last_fill_ts_ms is empty when no
    trades have been seen in the long window.
    """
    if state.flow_logger is None:
        return
    market = state.btc_5m_market
    if market is None:
        return
    now = time.time()

    def _flow_cells(token_id: str) -> List[Any]:
        d = state.poly_trades.get(token_id)
        short = _compute_flow_window(d, now, FLOW_WINDOW_SHORT_S)
        long = _compute_flow_window(d, now, FLOW_WINDOW_LONG_S)
        last_fill = long["last_fill_ts_ms"]
        return [
            short["n"],
            f"{short['buy_vol']:.4f}",
            f"{short['sell_vol']:.4f}",
            f"{short['net']:+.4f}",
            f"{short['vwap']:.4f}" if short["vwap"] is not None else "",
            long["n"],
            f"{long['buy_vol']:.4f}",
            f"{long['sell_vol']:.4f}",
            f"{long['net']:+.4f}",
            f"{long['vwap']:.4f}" if long["vwap"] is not None else "",
            last_fill if last_fill is not None else "",
        ]

    yes_cells = _flow_cells(market.yes_token_id)
    no_cells = _flow_cells(market.no_token_id)
    row = [
        int(now * 1000),
        market.condition_id,
        market.slug,
        *yes_cells,
        *no_cells,
    ]
    state.flow_logger.log(row)


# ═══════════════════════════════════════════════════════════════════
# STRATEGY GATES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TradeDecision:
    should_trade: bool
    reason: str
    direction: str = ""
    target_token_id: str = ""
    ask: float = 0.0
    bid: float = 0.0
    spread: float = 0.0
    edge: float = 0.0
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0


def compute_strategy_decision(state: BotState, signal: Signal) -> TradeDecision:
    cfg = state.config
    market = state.btc_5m_market
    if market is None:
        return TradeDecision(False, "no_market")

    yes_book = state.poly_books.get(market.yes_token_id)
    no_book = state.poly_books.get(market.no_token_id)
    if yes_book is None or no_book is None:
        return TradeDecision(False, "books_missing")

    if signal.direction == "UP":
        target_id = market.yes_token_id
        target_book = yes_book
    elif signal.direction == "DOWN":
        target_id = market.no_token_id
        target_book = no_book
    else:
        return TradeDecision(False, f"unknown_direction:{signal.direction}")

    ask, bid = target_book.ask, target_book.bid
    spread = (ask - bid) if (ask > 0 and bid > 0) else 999.0
    edge = (1.0 - ask) if ask > 0 else 0.0

    base = TradeDecision(
        should_trade=False,
        reason="",
        direction=signal.direction,
        target_token_id=target_id,
        ask=ask, bid=bid, spread=spread, edge=edge,
        yes_bid=yes_book.bid, yes_ask=yes_book.ask,
        no_bid=no_book.bid, no_ask=no_book.ask,
    )

    if ask <= 0 or bid <= 0:
        base.reason = "no_quotes"
        return base
    if ask <= bid:
        base.reason = f"crossed_book:bid={bid:.3f}/ask={ask:.3f}"
        return base
    if ask < cfg.entry_price_min:
        base.reason = f"ask_below_band:{ask:.3f}<{cfg.entry_price_min}"
        return base
    if ask > cfg.entry_price_max:
        base.reason = f"ask_above_band:{ask:.3f}>{cfg.entry_price_max}"
        return base
    if edge < cfg.edge_min:
        base.reason = f"edge_too_low:{edge:.3f}<{cfg.edge_min}"
        return base
    if spread > cfg.spread_max:
        base.reason = f"spread_too_wide:{spread:.3f}>{cfg.spread_max}"
        return base
    if state.open_position is not None:
        base.reason = "position_already_open"
        return base
    # v5.8.0: refuse to re-enter a market we've already exited a position on.
    # Production observation: bot took profit on a market, re-entered same
    # market while still active, second entry went to zero. Block on by
    # default; can be disabled via BLOCK_REENTRY_AFTER_EXIT=false.
    if _BLOCK_REENTRY and market.condition_id in state.exited_market_ids:
        base.reason = "market_already_exited"
        return base
    if cfg.mode == "live" and state.pnl_today_usdc <= -cfg.daily_loss_limit_usdc:
        base.reason = f"daily_loss_limit_hit:{state.pnl_today_usdc:.2f}"
        return base

    base.should_trade = True
    base.reason = "ok"
    return base


# ═══════════════════════════════════════════════════════════════════
# EXECUTION
# ═══════════════════════════════════════════════════════════════════

def _gen_trade_id() -> str:
    import secrets
    return f"{int(time.time()*1000)}_{secrets.token_hex(2)}"


def place_entry(state: BotState, signal: Signal, decision: TradeDecision) -> Optional[Position]:
    cfg = state.config
    market = state.btc_5m_market
    if market is None:
        return None

    # v5.5.29 guard 2: Pre-entry market-active gate.
    # Final defense before money moves: confirm the market is currently
    # within its scoring window. start_ts = end_ts - 300 (5-min markets).
    # If we somehow arrive here with a pre-market candidate (discovery
    # bug, sticky bug, anything), refuse to enter rather than place the
    # bet pre-market. Cross-bot policy: do not enter non-active trades.
    now_check = time.time()
    market_start_ts = market.end_ts - MARKET_INTERVAL_S
    if now_check < market_start_ts - 1.0:
        ttr = market.end_ts - now_check
        print(
            f"[trade][BLOCKED] pre-entry active-market gate REFUSED entry: "
            f"market starts at {market_start_ts:.0f} (in {market_start_ts - now_check:.0f}s), "
            f"now={now_check:.0f}, market_end={market.end_ts:.0f} (TTR={ttr:.0f}). "
            f"Cross-bot policy violation prevented. Discovery layer has a bug.",
            flush=True,
        )
        return None
    if now_check >= market.end_ts:
        print(
            f"[trade][BLOCKED] pre-entry active-market gate REFUSED entry: "
            f"market already ended at {market.end_ts:.0f}, now={now_check:.0f}. "
            f"Sticky-market layer let an expired market through.",
            flush=True,
        )
        return None

    trade_id = _gen_trade_id()
    size_usdc = cfg.position_size_usdc
    qty_shares = size_usdc / decision.ask if decision.ask > 0 else 0.0
    now = time.time()

    if cfg.mode == "dry":
        position = Position(
            trade_id=trade_id,
            coin="BTC",
            direction=signal.direction,
            market_id=market.condition_id,
            market_url=market.market_url,
            token_id=decision.target_token_id,
            entry_price=decision.ask,
            size_usdc=size_usdc,
            entry_ts=now,
            edge_at_entry=decision.edge,
            delta_pct_at_entry=signal.delta_pct,
            resolution_ts=market.end_ts,
        )
        print(
            f"[execution] DRY fill: trade_id={trade_id} {signal.direction} "
            f"ask={decision.ask:.3f} edge={decision.edge:.3f} size=${size_usdc:.2f} "
            f"qty={qty_shares:.4f} → resolves in {market.end_ts - now:.0f}s",
            flush=True,
        )
        _log_trade_event(state, "OPEN_DRY", position, decision, signal, fill_price=decision.ask, error="")
        return position

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        client = state.clob_client
        if client is None:
            print("[execution] LIVE: clob_client missing, refusing to trade", flush=True)
            _log_trade_event(state, "ENTRY_FAIL", None, decision, signal, fill_price=0.0, error="no_clob_client")
            return None

        order_args = OrderArgs(
            price=decision.ask,
            size=qty_shares,
            side=BUY,
            token_id=decision.target_token_id,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.FAK)
    except Exception as e:
        print(f"[execution] LIVE order error: {e}", flush=True)
        _log_trade_event(state, "ENTRY_FAIL", None, decision, signal, fill_price=0.0, error=str(e)[:200])
        return None

    success = bool(resp and resp.get("success", False))
    fill_price = decision.ask
    if not success:
        err = (resp or {}).get("errorMsg") or (resp or {}).get("error") or "rejected"
        print(f"[execution] LIVE order not filled: {err} resp={resp}", flush=True)
        _log_trade_event(state, "ENTRY_FAIL", None, decision, signal, fill_price=0.0, error=str(err)[:200])
        return None

    position = Position(
        trade_id=trade_id,
        coin="BTC",
        direction=signal.direction,
        market_id=market.condition_id,
        market_url=market.market_url,
        token_id=decision.target_token_id,
        entry_price=fill_price,
        size_usdc=size_usdc,
        entry_ts=now,
        edge_at_entry=decision.edge,
        delta_pct_at_entry=signal.delta_pct,
        resolution_ts=market.end_ts,
    )
    print(
        f"[execution] LIVE fill: trade_id={trade_id} {signal.direction} "
        f"@${fill_price:.3f} qty={qty_shares:.4f} size=${size_usdc:.2f}",
        flush=True,
    )
    _log_trade_event(state, "OPEN_LIVE", position, decision, signal, fill_price=fill_price, error="")
    return position


def _log_trade_event(state: BotState, event: str,
                     position: Optional[Position], decision: TradeDecision,
                     signal: Signal, fill_price: float, error: str) -> None:
    if state.trades_logger is None:
        return
    row = [
        int(time.time() * 1000),
        event,
        position.trade_id if position else "",
        signal.direction,
        f"{signal.delta_pct:+.5f}",
        decision.target_token_id if decision else "",
        decision.ask if decision else 0.0,
        decision.bid if decision else 0.0,
        decision.spread if decision else 0.0,
        decision.edge if decision else 0.0,
        f"{fill_price:.4f}",
        position.size_usdc if position else 0.0,
        state.btc_5m_market.condition_id if state.btc_5m_market else "",
        state.btc_5m_market.question if state.btc_5m_market else "",
        state.config.mode,
        error,
    ]
    state.trades_logger.log(row)


# ═══════════════════════════════════════════════════════════════════
# v6.1.0: BOTH-SIDES STRATEGY + MULTI-DURATION LOGGING
# ═══════════════════════════════════════════════════════════════════
# All code in this section is gated behind STRATEGY_MODE=both_sides_btc
# (env var). When STRATEGY_MODE=lag_signal (default), every entry point
# below short-circuits via early-return guards. The v5.8.1 lag-signal
# path is byte-equivalent unchanged.
#
# Strategy summary:
#   - 5m BTC markets: enter both YES + NO when TTR is in [BS_LEAD_MIN,
#     BS_LEAD_MAX] (defaults 600-900s = 10-15 min before resolution),
#     gated by sum(yes_ask, no_ask) <= BS_SUM_ASK_MAX (default 1.03).
#     One both-sides position per market_id (re-entry blocked by
#     bs_entered_market_ids set).
#   - Sell-loser tick: when winner-side ask >= BS_SELL_LOSER_THRESHOLD
#     (default 0.93) AND TTR <= BS_SELL_LOSER_TTR_FLOOR_S (default 120s)
#     AND condition persists BS_SELL_LOSER_PERSIST_S ticks (default 5)
#     AND loser bid >= BS_SELL_LOSER_MIN_LOSER_BID (default 0.05),
#     close loser leg at its current bid. Winner leg held to resolution.
#   - Settle: at end_ts, both legs settle. Winner pays $1/share
#     (size_usdc/entry_ask shares × $1), loser pays $0/share.
#   - 15m + 60m markets: pure logging — top-of-book sampled every
#     LOG_SAMPLE_INTERVAL_S into pre_market_books CSV. No trades.
#
# Functions in this section (top-down):
#   _bs_fetch_candidates(...)           — discovery for one duration
#   both_sides_discovery_thread(...)    — refreshes bs_*_in_window dicts
#   _bs_compute_subscribe_token_ids(...) — assets_ids list for poly_ws
#   _bs_should_enter(...)               — entry preconditions per market
#   _bs_place_entry(...)                — DRY/LIVE both-leg fill
#   _bs_log_trade_event(...)            — write entry/exit row to bs_trades CSV
#   _bs_evaluate_sell_loser(...)        — preconditions check + state update
#   _bs_close_leg(...)                  — close one leg at price
#   _bs_settle_position(...)            — handle resolution of full position
#   both_sides_tick(...)                — main_loop hook: entry + sell-loser
#   _bs_resolution_tick(...)            — main_loop hook: settle expired positions
#   pre_market_books_log_tick(...)      — main_loop hook: write CSV rows


def _bs_fetch_candidates(duration_label: str,
                          duration_s: int,
                          slug_prefix: str,
                          lookahead: int,
                          ttr_min_s: float,
                          ttr_max_s: float) -> List[MarketInfo]:
    """Discover BTC markets for a given duration whose TTR is in the
    requested window. Used by both_sides_discovery_thread for all three
    duration sets (5m / 15m / 60m).

    Note on slug timestamps: Polymarket convention from v5.5.28 finding
    is that slug 'btc-updown-{Nm}-{ts}' represents a market that STARTS
    at ts and ENDS at ts + duration_s. We probe `lookahead` boundaries
    starting from the current one.
    """
    candidates: List[MarketInfo] = []
    now = time.time()
    current_b = int((now // duration_s) * duration_s)
    boundaries = [current_b + i * duration_s for i in range(lookahead)]
    for ts in boundaries:
        slug = f"{slug_prefix}{ts}"
        ev = _fetch_event_by_slug(slug)
        if ev is None:
            continue
        # Pass widened TTR window so pre-market 5m and full 15m/60m markets
        # both pass. Note: end_ts > now is always required (no past markets).
        mi, _reason = _parse_event_to_market(ev, now, ttr_min_s, ttr_max_s)
        if mi is None:
            continue
        # Enforce slug-naming invariant: if endDate doesn't match
        # ts + duration_s within 30s tolerance, the slug convention has
        # changed and we should NOT trust this market. Logged loud.
        expected_end = float(ts) + float(duration_s)
        drift = abs(mi.end_ts - expected_end)
        if drift > 30.0:
            print(
                f"[bs_disc][{duration_label}][CRITICAL] slug-naming invariant "
                f"violated for {slug}: expected end_ts={expected_end:.0f}, "
                f"got endDate={mi.end_ts:.0f} (drift={drift:.0f}s). "
                f"Discarding candidate.",
                flush=True,
            )
            continue
        candidates.append(mi)
    return candidates


def both_sides_discovery_thread(state: BotState) -> None:
    """v6.1.0 discovery thread. Maintains state.bs_5m_in_window /
    bs_15m_in_window / bs_60m_in_window. Runs ONLY when STRATEGY_MODE is
    both_sides_btc — exits immediately otherwise. Refresh interval is
    DISCOVERY_INTERVAL_S (30s) same as legacy discovery."""
    if not _BS_ACTIVE:
        print("[bs_disc] STRATEGY_MODE != both_sides_btc — discovery thread idle",
              flush=True)
        return

    print(f"[bs_disc] starting. lead_window=[{_BS_LEAD_MIN_S:.0f}s,"
          f"{_BS_LEAD_MAX_S:.0f}s] log_window=[{_LOG_WINDOW_MIN_S:.0f}s,"
          f"{_LOG_WINDOW_MAX_S:.0f}s] 15m_prefix={_LOG_15M_PREFIX!r} "
          f"60m_prefix={_LOG_60M_PREFIX!r}", flush=True)

    diag_done = False
    prev_token_set: Optional[frozenset] = None
    while not state.kill_flag:
        try:
            now = time.time()

            # ── 5m markets in entry-window TTR for both-sides trading ──
            # TTR window: BS_LEAD_MIN .. BS_LEAD_MAX (default 600-900s).
            # Markets are entered when they first appear in this window
            # AND haven't been entered before (bs_entered_market_ids).
            new_5m = _bs_fetch_candidates(
                "5m", MARKET_INTERVAL_S, MARKET_SLUG_PREFIX,
                SLUG_LOOKAHEAD_5M,
                ttr_min_s=_BS_LEAD_MIN_S,
                ttr_max_s=_BS_LEAD_MAX_S,
            )
            # Also keep already-entered 5m markets in the dict until
            # resolution so poly_ws stays subscribed and sell-loser
            # logic can run. We add active positions' markets back even
            # if their TTR has dropped below the lead window.
            updated_5m: Dict[str, MultiDurationMarket] = {}
            for mi in new_5m:
                key = mi.condition_id
                existing = state.bs_5m_in_window.get(key)
                if existing is not None:
                    # Carry forward last_logged_ts and ws_subscribed so
                    # the WS subscription doesn't churn.
                    existing.market = mi
                    updated_5m[key] = existing
                else:
                    updated_5m[key] = MultiDurationMarket(
                        duration_label="5m", duration_s=MARKET_INTERVAL_S,
                        market=mi,
                    )
            # Keep markets we still hold positions on, regardless of TTR
            for mid in list(state.both_sides_positions.keys()):
                if mid not in updated_5m:
                    old = state.bs_5m_in_window.get(mid)
                    if old is not None:
                        updated_5m[mid] = old
            # v6.3.12: also keep BSS-watched markets through pre-market AND
            # live phases until they actually end. Previously the [600s, 1800s]
            # discovery filter caused markets to drop OFF the watch list at
            # TTR=600s — which is 5 minutes BEFORE the live window even opens
            # (live window opens at TTR=300s for 5m markets). Result: bot
            # would only watch markets in pre-pre-market state, never during
            # actual pre-market or live phase, and could never fire.
            now_ts = time.time()
            for mid, mdm in list(state.bs_5m_in_window.items()):
                if mid not in updated_5m and mdm.market.end_ts > now_ts:
                    updated_5m[mid] = mdm
            state.bs_5m_in_window = updated_5m

            # ── 15m logging-only markets in log-window TTR ──
            new_15m = _bs_fetch_candidates(
                "15m", MARKET_INTERVAL_15M_S, _LOG_15M_PREFIX,
                SLUG_LOOKAHEAD_15M,
                ttr_min_s=_LOG_WINDOW_MIN_S,
                ttr_max_s=_LOG_WINDOW_MAX_S,
            )
            updated_15m: Dict[str, MultiDurationMarket] = {}
            for mi in new_15m:
                key = mi.condition_id
                existing = state.bs_15m_in_window.get(key)
                if existing is not None:
                    existing.market = mi
                    updated_15m[key] = existing
                else:
                    updated_15m[key] = MultiDurationMarket(
                        duration_label="15m", duration_s=MARKET_INTERVAL_15M_S,
                        market=mi,
                    )
            state.bs_15m_in_window = updated_15m

            # ── 60m logging-only markets in log-window TTR ──
            new_60m = _bs_fetch_candidates(
                "60m", MARKET_INTERVAL_60M_S, _LOG_60M_PREFIX,
                SLUG_LOOKAHEAD_60M,
                ttr_min_s=_LOG_WINDOW_MIN_S,
                ttr_max_s=_LOG_WINDOW_MAX_S,
            )
            updated_60m: Dict[str, MultiDurationMarket] = {}
            for mi in new_60m:
                key = mi.condition_id
                existing = state.bs_60m_in_window.get(key)
                if existing is not None:
                    existing.market = mi
                    updated_60m[key] = existing
                else:
                    updated_60m[key] = MultiDurationMarket(
                        duration_label="60m", duration_s=MARKET_INTERVAL_60M_S,
                        market=mi,
                    )
            state.bs_60m_in_window = updated_60m

            # Diag counters for /api/status
            state.bs_discovery_diag = {
                "5m_in_window": len(state.bs_5m_in_window),
                "15m_in_window": len(state.bs_15m_in_window),
                "60m_in_window": len(state.bs_60m_in_window),
                "5m_new_this_cycle": len(new_5m),
                "15m_new_this_cycle": len(new_15m),
                "60m_new_this_cycle": len(new_60m),
            }

            if not diag_done:
                print(f"[bs_disc] first cycle: 5m={len(new_5m)} "
                      f"15m={len(new_15m)} 60m={len(new_60m)}", flush=True)
                if len(new_15m) == 0:
                    print(f"[bs_disc][warn] 0 15m candidates — slug prefix "
                          f"{_LOG_15M_PREFIX!r} may be wrong. Override with "
                          f"LOG_15M_SLUG_PREFIX env var.", flush=True)
                if len(new_60m) == 0:
                    print(f"[bs_disc][warn] 0 60m candidates — slug prefix "
                          f"{_LOG_60M_PREFIX!r} may be wrong. Override with "
                          f"LOG_60M_SLUG_PREFIX env var.", flush=True)
                diag_done = True

            # Only resubscribe poly_ws when the token set actually changed.
            # Compute current token-id set and compare to the snapshot from
            # the previous cycle. Avoids tearing down the WS every 30s.
            current_token_set = frozenset(_bs_compute_subscribe_token_ids(state))
            if current_token_set != prev_token_set:
                if prev_token_set is not None:
                    added = current_token_set - prev_token_set
                    removed = prev_token_set - current_token_set
                    print(f"[bs_disc] token set changed: +{len(added)} -{len(removed)} "
                          f"(now={len(current_token_set)} tokens)", flush=True)
                _force_poly_ws_resubscribe(state)
                prev_token_set = current_token_set

        except Exception as e:
            print(f"[bs_disc] crash: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

        slept = 0.0
        while slept < DISCOVERY_INTERVAL_S and not state.kill_flag:
            time.sleep(1.0)
            slept += 1.0


def _bs_compute_subscribe_token_ids(state: BotState) -> List[str]:
    """Return the union of all token_ids across the three v6.1.0
    duration sets. The poly_ws thread reads this when v6.1.0 is active
    and subscribes to all of them in one Market message."""
    token_ids: List[str] = []
    seen: Set[str] = set()
    for duration_dict in (state.bs_5m_in_window,
                          state.bs_15m_in_window,
                          state.bs_60m_in_window):
        for md in duration_dict.values():
            for tid in (md.market.yes_token_id, md.market.no_token_id):
                if tid and tid not in seen:
                    token_ids.append(tid)
                    seen.add(tid)
    return token_ids


def _bs_should_enter(state: BotState, mdm: MultiDurationMarket,
                       now: float) -> Tuple[bool, str, float, float, float]:
    """Decide whether to enter both-sides on this 5m market right now.

    Returns: (should_enter, reason, yes_ask, no_ask, sum_ask)
    """
    market = mdm.market
    # 1) Re-entry block
    if market.condition_id in state.bs_entered_market_ids:
        return False, "already_entered", 0.0, 0.0, 0.0
    if market.condition_id in state.both_sides_positions:
        return False, "position_open", 0.0, 0.0, 0.0
    # v6.2.5: end-minute skip filter (configurable via SKIP_END_MINUTES env).
    # Catastrophe analysis (May 3-5, n=194 fires) showed strong end-minute
    # clustering of catastrophes. Markets ending on minutes in the configured
    # set are refused entry entirely. Empty set = no filter (current default).
    if _SKIP_END_MINUTES:
        end_minute = datetime.fromtimestamp(market.end_ts, tz=timezone.utc).minute
        if end_minute in _SKIP_END_MINUTES:
            return False, f"skip_end_minute:{end_minute:02d}", 0.0, 0.0, 0.0
    # 2) TTR must be in lead window
    ttr = market.end_ts - now
    if ttr < _BS_LEAD_MIN_S:
        return False, f"ttr_below_min:{ttr:.0f}s", 0.0, 0.0, 0.0
    if ttr > _BS_LEAD_MAX_S:
        return False, f"ttr_above_max:{ttr:.0f}s", 0.0, 0.0, 0.0
    # 3) Both books must be present and fresh
    yes_book = state.poly_books.get(market.yes_token_id)
    no_book = state.poly_books.get(market.no_token_id)
    if yes_book is None or no_book is None:
        return False, "no_book", 0.0, 0.0, 0.0
    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        return False, f"book_stale:{book_age_max:.0f}s", 0.0, 0.0, 0.0
    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    # 4) Both asks must be non-zero (no offers = no fill possible)
    if yes_ask <= 0.0 or no_ask <= 0.0:
        return False, "zero_ask", yes_ask, no_ask, 0.0
    # 5) Sum-ask gate (the core both-sides risk control)
    sum_ask = yes_ask + no_ask
    if sum_ask > _BS_SUM_ASK_MAX:
        return False, f"sum_ask_too_high:{sum_ask:.4f}", yes_ask, no_ask, sum_ask
    # 6) Sanity: each side must individually be within a reasonable price
    # band. Polymarket binary markets sum to ~1.00 in equilibrium; if one
    # side is 0.99 and the other 0.04 (sum=1.03), that's a degenerate
    # market we don't want to take. Require each side >= 0.05.
    if yes_ask < 0.05 or no_ask < 0.05:
        return False, "side_too_thin", yes_ask, no_ask, sum_ask
    return True, "ok", yes_ask, no_ask, sum_ask


def _bs_log_trade_event(state: BotState, event: str,
                         pos: BothSidesPosition, leg: Optional[BothSidesLeg],
                         note: str = "") -> None:
    """Write a row to bs_trades_<date>.csv. Schema:

      ts_ms, event, market_id, slug, end_ts, side, token_id,
      entry_ask, entry_bid, size_usdc, qty_shares,
      close_price, close_ts, pnl_usdc, sum_ask_at_entry, mode, notes

    `event` is one of: ENTRY_YES_DRY/LIVE, ENTRY_NO_DRY/LIVE,
    SELL_LOSER_DRY/LIVE, RESOLVE_WIN, RESOLVE_LOSS, VOID, ENTRY_FAIL.
    For market-level events (ENTRY_FAIL pre-leg), pass leg=None.
    """
    if state.bs_trades_logger is None:
        return
    if leg is not None:
        row = [
            int(time.time() * 1000),
            event,
            pos.market_id,
            pos.slug,
            pos.market_url,
            f"{pos.end_ts:.0f}",
            leg.side,
            leg.token_id,
            f"{leg.entry_ask:.4f}",
            f"{leg.entry_bid:.4f}",
            f"{leg.size_usdc:.4f}",
            f"{leg.qty_shares:.4f}",
            f"{leg.close_price:.4f}",
            f"{leg.close_ts:.0f}" if leg.close_ts else "",
            f"{leg.pnl_usdc:+.4f}",
            f"{pos.sum_ask_at_entry:.4f}",
            state.config.mode,
            note,
        ]
    else:
        row = [
            int(time.time() * 1000),
            event,
            pos.market_id,
            pos.slug,
            pos.market_url,
            f"{pos.end_ts:.0f}",
            "", "",
            "", "",
            "0.0000", "0.0000",
            "", "",
            "0.0000",
            f"{pos.sum_ask_at_entry:.4f}",
            state.config.mode,
            note,
        ]
    state.bs_trades_logger.log(row)


def _bs_place_entry(state: BotState, mdm: MultiDurationMarket,
                     yes_ask: float, no_ask: float,
                     sum_ask: float) -> Optional[BothSidesPosition]:
    """Place both YES and NO legs of a both-sides position. In DRY mode
    creates the position object directly. In LIVE mode (gated — DRY-only
    in v6.1.0 default), would call CLOB. v6.1.0 ships DRY-only by intent;
    LIVE both-sides path is reserved for v6.2.x after data validates EV.
    """
    cfg = state.config
    market = mdm.market
    now = time.time()
    size_usdc = cfg.position_size_usdc

    yes_book = state.poly_books.get(market.yes_token_id)
    no_book = state.poly_books.get(market.no_token_id)
    yes_bid = float(yes_book.bid) if yes_book else 0.0
    no_bid = float(no_book.bid) if no_book else 0.0

    yes_qty = size_usdc / yes_ask if yes_ask > 0 else 0.0
    no_qty = size_usdc / no_ask if no_ask > 0 else 0.0

    if cfg.mode != "dry":
        # v6.1.0: refuse LIVE both-sides entries by design. Reserved for
        # v6.2.x after a few hundred DRY trades validate EV.
        print(f"[bs_entry][BLOCKED] LIVE both-sides not implemented in v6.1.0 — "
              f"refusing entry on market_id={market.condition_id[:10]}", flush=True)
        return None

    yes_leg = BothSidesLeg(
        side="YES", token_id=market.yes_token_id,
        entry_ask=yes_ask, entry_bid=yes_bid,
        size_usdc=size_usdc, qty_shares=yes_qty,
        entry_ts=now,
        # v6.1.4: seed peak with entry_bid so peak ≥ entry by definition.
        peak_bid=yes_bid, peak_bid_ts=now,
    )
    no_leg = BothSidesLeg(
        side="NO", token_id=market.no_token_id,
        entry_ask=no_ask, entry_bid=no_bid,
        size_usdc=size_usdc, qty_shares=no_qty,
        entry_ts=now,
        # v6.1.4: seed peak with entry_bid so peak ≥ entry by definition.
        peak_bid=no_bid, peak_bid_ts=now,
    )
    pos = BothSidesPosition(
        market_id=market.condition_id,
        market_url=market.market_url,
        market_question=market.question,
        slug=market.slug,
        duration_s=mdm.duration_s,
        end_ts=market.end_ts,
        entry_ts=now,
        sum_ask_at_entry=sum_ask,
        yes_leg=yes_leg,
        no_leg=no_leg,
    )
    state.both_sides_positions[market.condition_id] = pos
    state.bs_entered_market_ids.add(market.condition_id)
    state.bs_total_entered += 1

    print(
        f"[bs_entry] DRY fill market={market.condition_id[:10]}… "
        f"YES@{yes_ask:.3f} NO@{no_ask:.3f} sum_ask={sum_ask:.4f} "
        f"yes_qty={yes_qty:.4f} no_qty={no_qty:.4f} "
        f"TTR={market.end_ts - now:.0f}s slug={market.slug[:30]}",
        flush=True,
    )
    _bs_log_trade_event(state, "ENTRY_YES_DRY", pos, yes_leg, note="both_sides_v610")
    _bs_log_trade_event(state, "ENTRY_NO_DRY", pos, no_leg, note="both_sides_v610")
    return pos


def _btc_closest(state: BotState, target_ts: float,
                  tol_s: float) -> Optional[float]:
    """v6.2.0: helper — find binance price closest to target_ts within tol_s.

    Used by both the BTC-confirmation guard (in _bs_evaluate_sell_loser) and
    the BTC late-fallback (in _bs_evaluate_btc_late_fallback) and the existing
    diagnostic emitter. Snapshots state.binance_prices to avoid race with
    the WS thread. Returns None if no sample within tol_s of target_ts.
    """
    snapshot = list(state.binance_prices)
    best = None
    best_dt = float('inf')
    for ts, price in snapshot:
        dt = abs(ts - target_ts)
        if dt < best_dt and dt <= tol_s:
            best_dt = dt
            best = price
    return best


def _btc_velocity_pct(state: BotState, now: float,
                        lookback_s: float) -> Optional[float]:
    """v6.3.1: helper for BSS BTC-velocity filter. Returns the % change
    in BTC price between (now - lookback_s) and now. Positive = up,
    negative = down. Returns None if insufficient data.

    Snapshots state.binance_prices once at call time to avoid races.
    """
    snapshot = list(state.binance_prices)
    if len(snapshot) < 3:
        return None
    end_ts = now
    start_ts = now - lookback_s
    # Find prices closest to start_ts and end_ts within ±2s tolerance
    best_start = best_end = None
    best_start_dt = best_end_dt = float('inf')
    for ts, price in snapshot:
        dt_s = abs(ts - start_ts)
        if dt_s < best_start_dt and dt_s <= 5.0:
            best_start_dt = dt_s; best_start = price
        dt_e = abs(ts - end_ts)
        if dt_e < best_end_dt and dt_e <= 5.0:
            best_end_dt = dt_e; best_end = price
    if best_start is None or best_end is None or best_start <= 0:
        return None
    return (best_end - best_start) / best_start * 100.0


def _opposite_side_drop(mdm, fire_side: str, now: float,
                          lookback_s: float) -> Optional[float]:
    """v6.3.7: returns the absolute price drop of the OPPOSITE side over the
    last `lookback_s` seconds. Used by patient second-leg logic.

    Args:
      mdm        : MultiDurationMarket — has bss_price_samples (1Hz, last 30min)
      fire_side  : "YES" or "NO" (the side we already own; opposite is what
                    we're checking velocity on)
      now        : current ts
      lookback_s : how far back to look

    Returns:
      Absolute drop = price_old - price_new
      Positive value = price has FALLEN by that much
      Negative value = price has RISEN
      None if insufficient samples
    """
    samples = list(mdm.bss_price_samples)
    if len(samples) < 3:
        return None
    target_old_ts = now - lookback_s
    # Find samples closest to target_old_ts and to now (within 3s tolerance)
    best_old_dt = best_new_dt = float('inf')
    best_old_yes = best_old_no = None
    best_new_yes = best_new_no = None
    for ts, yes_ask, no_ask in samples:
        dt_old = abs(ts - target_old_ts)
        if dt_old < best_old_dt and dt_old <= 3.0:
            best_old_dt = dt_old
            best_old_yes, best_old_no = yes_ask, no_ask
        dt_new = abs(ts - now)
        if dt_new < best_new_dt and dt_new <= 3.0:
            best_new_dt = dt_new
            best_new_yes, best_new_no = yes_ask, no_ask
    if best_old_yes is None or best_new_yes is None:
        return None
    # Opposite side relative to first leg
    if fire_side == "YES":
        old_p, new_p = best_old_no, best_new_no  # opposite is NO
    else:
        old_p, new_p = best_old_yes, best_new_yes  # opposite is YES
    return old_p - new_p  # positive = falling


def _bs_compute_sell_loser_diagnostics(state: BotState, pos: BothSidesPosition,
                                          now: float) -> str:
    """v6.1.7: build a comma-separated key=value string of diagnostics for
    the SELL_LOSER_DRY notes column. Used for retrospective analysis of
    catastrophic vs winning sell-loser fires.

    Fields:
      ttr_s              — time to resolution at fire time (seconds)
      btc_now            — BTC price at fire time (closest binance sample within 5s)
      btc_strike         — BTC at start_ts (= end_ts - duration_s; tol 30s)
      btc_dist_pct       — signed % distance of btc_now from btc_strike
      btc_30s_range      — max - min over BTC samples in the last 30s ($)
      btc_30s_dir        — "rising" / "falling" / "wandering" based on net move
      lead_dur_s         — how long winner_ask has been ≥ _BS_SELL_THRESH

    v6.2.5 additions (all from in-memory state — no extra API calls):
      winner_bid         — current bid on the winner side (book-width sanity)
      vl_peak_winner_ask — peak winner_ask since arming (verification_late)
      vl_peak_updates    — count of peak updates (1Hz-leak diagnostic; low
                           value relative to vl_armed_for_s = main loop is
                           sampling-limited and missing intra-tick peaks)
      vl_armed_for_s     — seconds since vl_armed flipped True
      depth_winner_ask   — top-5 ask-side depth on winner (size we're fading)
      depth_loser_bid    — top-5 bid-side depth on loser (what we'd fill into)
      btc_60s_range      — max - min over BTC samples in last 60s ($)
      btc_120s_range     — max - min over BTC samples in last 120s ($)
      book_age_s         — max(yes_book.last_update_ts, no_book.last_update_ts)
                           age at fire moment

    Each field uses "na" if the underlying data is unavailable (e.g.
    Binance feed disconnected). All numeric formats are stable so a
    downstream parser can safely split on "," and "=".
    """
    parts: List[str] = []
    ttr = pos.end_ts - now
    parts.append(f"ttr_s={ttr:.0f}")

    btc_now = _btc_closest(state, now, 5.0)
    start_ts = pos.end_ts - pos.duration_s
    btc_strike = _btc_closest(state, start_ts, 30.0)

    parts.append(f"btc_now={btc_now:.2f}" if btc_now is not None else "btc_now=na")
    parts.append(f"btc_strike={btc_strike:.2f}" if btc_strike is not None
                 else "btc_strike=na")

    if btc_now is not None and btc_strike is not None and btc_strike > 0:
        dist_pct = (btc_now - btc_strike) / btc_strike * 100
        parts.append(f"btc_dist_pct={dist_pct:+.4f}")
    else:
        parts.append("btc_dist_pct=na")

    # BTC range windows (30s legacy + 60s/120s v6.2.5 additions). One
    # snapshot, three sub-windows — cheaper than three snapshots.
    snapshot = list(state.binance_prices)
    cutoff_30 = now - 30.0
    cutoff_60 = now - 60.0
    cutoff_120 = now - 120.0
    win_30: List[Tuple[float, float]] = []
    win_60: List[Tuple[float, float]] = []
    win_120: List[Tuple[float, float]] = []
    for ts, p in snapshot:
        if ts < cutoff_120 or ts > now:
            continue
        win_120.append((ts, p))
        if ts >= cutoff_60:
            win_60.append((ts, p))
            if ts >= cutoff_30:
                win_30.append((ts, p))
    win_30.sort()
    win_60.sort()
    win_120.sort()

    # 30s window — range + direction (legacy fields preserved as-is)
    if len(win_30) >= 2:
        prices_30 = [p for _, p in win_30]
        rng_30 = max(prices_30) - min(prices_30)
        net_30 = prices_30[-1] - prices_30[0]
        if abs(net_30) < 5.0:
            direction = "wandering"
        elif net_30 > 0:
            direction = "rising"
        else:
            direction = "falling"
        parts.append(f"btc_30s_range={rng_30:.2f}")
        parts.append(f"btc_30s_dir={direction}")
    else:
        parts.append("btc_30s_range=na")
        parts.append("btc_30s_dir=na")

    if pos.winner_first_seen_ts > 0.0:
        lead_dur = now - pos.winner_first_seen_ts
        parts.append(f"lead_dur_s={lead_dur:.1f}")
    else:
        parts.append("lead_dur_s=na")

    # ── v6.2.5 fields below ──

    # Re-derive winner side from current books to compute winner_bid +
    # depth fields. Cheap (just dict lookup + a comparison) and avoids
    # changing this function's signature so all callers stay unchanged.
    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is not None and no_book is not None:
        ya = float(yes_book.ask); yb = float(yes_book.bid)
        na = float(no_book.ask); nb = float(no_book.bid)
        # Winner = side with higher ask (matches sell-loser fire logic).
        # Tie → YES (matches the `>=` comparator used in evaluator paths).
        if ya >= na and ya > 0:
            winner_book = yes_book
            loser_book = no_book
            winner_bid = yb
        elif na > 0:
            winner_book = no_book
            loser_book = yes_book
            winner_bid = nb
        else:
            winner_book = None
            loser_book = None
            winner_bid = None
        parts.append(f"winner_bid={winner_bid:.4f}" if winner_bid is not None
                     else "winner_bid=na")
        if winner_book is not None and loser_book is not None:
            depth_winner_ask = sum(s for (_, s) in winner_book.ask_levels)
            depth_loser_bid = sum(s for (_, s) in loser_book.bid_levels)
            parts.append(f"depth_winner_ask={depth_winner_ask:.2f}")
            parts.append(f"depth_loser_bid={depth_loser_bid:.2f}")
        else:
            parts.append("depth_winner_ask=na")
            parts.append("depth_loser_bid=na")
        book_age_s = max(now - yes_book.last_update_ts,
                         now - no_book.last_update_ts)
        parts.append(f"book_age_s={book_age_s:.2f}")
    else:
        parts.append("winner_bid=na")
        parts.append("depth_winner_ask=na")
        parts.append("depth_loser_bid=na")
        parts.append("book_age_s=na")

    # vl_* state — only meaningful in verification_late mode but always
    # logged so the CSV schema stays uniform regardless of strategy variant.
    parts.append(f"vl_peak_winner_ask={pos.vl_peak_winner_ask:.4f}")
    parts.append(f"vl_peak_updates={pos.vl_peak_update_count}")
    if pos.vl_armed_ts > 0.0:
        parts.append(f"vl_armed_for_s={now - pos.vl_armed_ts:.1f}")
    else:
        parts.append("vl_armed_for_s=na")

    # 60s + 120s BTC ranges — wider context than legacy 30s window
    if len(win_60) >= 2:
        prices_60 = [p for _, p in win_60]
        parts.append(f"btc_60s_range={max(prices_60) - min(prices_60):.2f}")
    else:
        parts.append("btc_60s_range=na")
    if len(win_120) >= 2:
        prices_120 = [p for _, p in win_120]
        parts.append(f"btc_120s_range={max(prices_120) - min(prices_120):.2f}")
    else:
        parts.append("btc_120s_range=na")

    return ",".join(parts)


# ───────────────────────────────────────────────────────────────────────
# v6.5.11 — TIERED EXIT LADDER helpers (verbatim from test_tier_v6_5_11.py)
# ───────────────────────────────────────────────────────────────────────
def _bs_tier_match(ttr, winner_ask):
    if ttr <= _BS_TIER_T3_TTR and winner_ask >= _BS_TIER_T3_WINNER:
        return "T3", _BS_TIER_T3_WINNER
    if ttr <= _BS_TIER_T2_TTR and winner_ask >= _BS_TIER_T2_WINNER:
        return "T2", _BS_TIER_T2_WINNER
    if ttr <= _BS_TIER_T1_TTR and winner_ask >= _BS_TIER_T1_WINNER:
        return "T1", _BS_TIER_T1_WINNER
    if winner_ask >= _BS_TIER_T0_WINNER:
        return "T0", _BS_TIER_T0_WINNER
    return None, None


def _bs_tier_detect_swing(history, winner_side, now):
    cutoff = now - _BS_TIER_SWING_WINDOW_S
    window = [t for t in history if t[0] >= cutoff]
    if len(window) < 3:
        return False
    asks = [w[1] if winner_side == "YES" else w[2] for w in window]
    peak_idx = max(range(len(asks)), key=lambda i: asks[i])
    if peak_idx >= len(asks) - 1:
        return False
    after_peak = asks[peak_idx:]
    trough_off = min(range(len(after_peak)), key=lambda i: after_peak[i])
    if trough_off >= len(after_peak) - 1:
        return False
    recovery = max(after_peak[trough_off:])
    drawdown = asks[peak_idx] - after_peak[trough_off]
    bounce = recovery - after_peak[trough_off]
    return drawdown >= _BS_TIER_SWING_DRAWDOWN and bounce >= _BS_TIER_SWING_BOUNCE


def _bs_tier_no_dip(history, winner_side, now):
    cutoff = now - _BS_TIER_DIP_WINDOW_S
    window = [t for t in history if t[0] >= cutoff]
    if not window:
        return True
    asks = [w[1] if winner_side == "YES" else w[2] for w in window]
    return min(asks) >= _BS_TIER_DIP_FLOOR


def _bs_tier_sustained_above(history, winner_side, threshold, sustain_s, now):
    if sustain_s <= 0:
        return True
    asks_ts = sorted(
        ((t[0], t[1] if winner_side == "YES" else t[2]) for t in history),
        key=lambda x: x[0],
    )
    if not asks_ts:
        return False
    for ts, ask in reversed(asks_ts):
        if ask < threshold:
            return (now - ts) >= sustain_s
    return (now - asks_ts[0][0]) >= sustain_s


def _bs_evaluate_sell_loser_tiered(
        state: BotState, pos: BothSidesPosition,
        now: float) -> Tuple[bool, str, str, float, float]:
    """v6.5.11 tiered exit ladder. Same return contract as the legacy
    evaluator: (should_sell, reason, loser_side, loser_bid, winner_ask).

    Ladder (most-specific TTR window wins):
      T0  any TTR, winner_ask≥0.96, TTR≤200s, sustained≥0.94 for 30s,
          AND-guard (no_swing AND no_dip)
      T1  TTR≤120s, winner_ask≥0.90, OR-guard (no_swing OR no_dip)
      T2  TTR≤60s,  winner_ask≥0.87, OR-guard
      T3  TTR≤30s,  winner_ask≥0.80, OR-guard
    Cross-tier: winner_ask must hold ≥ tier_threshold for BS_TIER_PERSIST_S.
    """
    ttr = pos.end_ts - now
    if ttr < 0.0:
        pos.tier_last_eval_status = f"ttr_negative:{ttr:.0f}s"
        return False, pos.tier_last_eval_status, "", 0.0, 0.0

    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        pos.tier_last_eval_status = "no_book"
        return False, "no_book", "", 0.0, 0.0

    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        pos.tier_last_eval_status = f"book_stale:{book_age_max:.0f}s"
        return False, pos.tier_last_eval_status, "", 0.0, 0.0

    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    yes_bid = float(yes_book.bid)
    no_bid = float(no_book.bid)

    # Maintain winner-ask history for the guards; trim to the widest window.
    pos.tier_ask_history.append((now, yes_ask, no_ask))
    cutoff = now - _BS_TIER_HISTORY_MAX_S
    if pos.tier_ask_history and pos.tier_ask_history[0][0] < cutoff:
        pos.tier_ask_history = [t for t in pos.tier_ask_history if t[0] >= cutoff]

    # Winner = side with HIGHER ask (same identification as legacy).
    if yes_ask >= no_ask and yes_ask > 0:
        winner_side = "YES"; winner_ask = yes_ask
        loser_side = "NO";   loser_bid = no_bid
    elif no_ask > 0:
        winner_side = "NO";  winner_ask = no_ask
        loser_side = "YES";  loser_bid = yes_bid
    else:
        pos.tier_last_eval_status = "both_asks_zero"
        return False, "both_asks_zero", "", 0.0, 0.0

    tier_label, tier_thresh = _bs_tier_match(ttr, winner_ask)
    if tier_label is None:
        pos.tier_last_eval_status = f"no_tier:ttr={ttr:.0f}s,wa={winner_ask:.3f}"
        return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    # Need a real bid to sell into (don't dump at $0).
    if loser_bid <= 0.0:
        pos.tier_last_eval_status = f"loser_bid_zero:{loser_bid:.3f}"
        return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    # Cross-tier persistence at the matched tier's threshold.
    if not _bs_tier_sustained_above(pos.tier_ask_history, winner_side,
                                    tier_thresh, _BS_TIER_PERSIST_S, now):
        pos.tier_last_eval_status = f"{tier_label}_persist_pending"
        return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    no_swing = not _bs_tier_detect_swing(pos.tier_ask_history, winner_side, now)
    no_dip = _bs_tier_no_dip(pos.tier_ask_history, winner_side, now)

    if tier_label == "T0":
        if ttr > _BS_TIER_T0_MAX_TTR:
            pos.tier_last_eval_status = f"T0_ttr_over_max:{ttr:.0f}s"
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
        if not _bs_tier_sustained_above(pos.tier_ask_history, winner_side,
                                        _BS_TIER_T0_SUSTAIN_THRESH,
                                        _BS_TIER_T0_SUSTAIN_S, now):
            pos.tier_last_eval_status = "T0_sustain_pending"
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
        if not (no_swing and no_dip):  # T0 AND-guard
            pos.tier_last_eval_status = (f"T0_guard_block:swing={not no_swing},"
                                         f"dip={not no_dip}")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
    else:
        if not (no_swing or no_dip):  # T1/T2/T3 OR-guard
            pos.tier_last_eval_status = (f"{tier_label}_guard_block:swing={not no_swing},"
                                         f"dip={not no_dip}")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    pos.identified_loser_side = loser_side
    pos.fire_tier = tier_label
    reason = f"fire_{tier_label}_LADDER:wa={winner_ask:.3f},ttr={ttr:.0f}s"
    pos.tier_last_eval_status = reason
    return True, reason, loser_side, loser_bid, winner_ask


def _bs_evaluate_sell_loser(state: BotState, pos: BothSidesPosition,
                            now: float) -> Tuple[bool, str, str, float, float]:
    """v6.5.11 dispatcher: tiered ladder (default) or legacy v6.2.x evaluator.
    Routes on _BS_TIER_ENABLED so rollback is a single env flip, no redeploy."""
    if _BS_TIER_ENABLED:
        return _bs_evaluate_sell_loser_tiered(state, pos, now)
    return _bs_evaluate_sell_loser_legacy(state, pos, now)


def _bs_evaluate_sell_loser_legacy(state: BotState, pos: BothSidesPosition,
                              now: float) -> Tuple[bool, str, str, float, float]:
    """Check the four sell-loser preconditions. Returns:
        (should_sell, reason, loser_side, loser_bid, winner_ask)

    Preconditions (ALL must be true to fire):
      A) TTR <= BS_SELL_LOSER_TTR_FLOOR_S       (default 120s)
      B) winner_ask >= BS_SELL_LOSER_THRESHOLD  (default 0.93)
      C) sustained for BS_SELL_LOSER_PERSIST_S consecutive ticks (default 5s)
      D) loser_bid >= BS_SELL_LOSER_MIN_LOSER_BID (default 0.05)

    Loser identification: whichever side has the LOWER current ask is
    "winning" — its ask is approaching $1 because resolution is heading
    its way. The OTHER side (with higher ask, lower bid) is the loser.
    Once identified, cached on pos.identified_loser_side so it doesn't
    flip turn-to-turn.
    """
    # A) TTR floor
    ttr = pos.end_ts - now
    if ttr > _BS_SELL_TTR_FLOOR_S:
        pos.sell_loser_consecutive_ticks = 0
        return False, f"ttr_above_floor:{ttr:.0f}s", "", 0.0, 0.0

    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        pos.sell_loser_consecutive_ticks = 0
        return False, "no_book", "", 0.0, 0.0

    # Stale book guard — same threshold as entry
    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        pos.sell_loser_consecutive_ticks = 0
        return False, f"book_stale:{book_age_max:.0f}s", "", 0.0, 0.0

    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    yes_bid = float(yes_book.bid)
    no_bid = float(no_book.bid)

    # Identify winner side: in a binary Up/Down market, YES → 1.00 if
    # the outcome resolves Up, NO → 1.00 if Down. The "winning" side's
    # ask climbs toward $1 as confidence grows; the losing side's ask
    # falls toward $0. So winner = side with HIGHER ask.
    if yes_ask >= no_ask and yes_ask > 0:
        winner_side = "YES"
        winner_ask = yes_ask
        loser_side = "NO"
        loser_bid = no_bid
    elif no_ask > 0:
        winner_side = "NO"
        winner_ask = no_ask
        loser_side = "YES"
        loser_bid = yes_bid
    else:
        pos.sell_loser_consecutive_ticks = 0
        return False, "both_asks_zero", "", 0.0, 0.0

    # v6.1.7: track when winner_ask first crossed threshold (for lead-duration
    # diagnostic at fire time). Set on first crossing, reset when it drops back.
    if winner_ask >= _BS_SELL_THRESH:
        if pos.winner_first_seen_ts == 0.0:
            pos.winner_first_seen_ts = now
    else:
        pos.winner_first_seen_ts = 0.0

    # B) winner_ask >= threshold
    if winner_ask < _BS_SELL_THRESH:
        pos.sell_loser_consecutive_ticks = 0
        return False, f"winner_ask_below_thresh:{winner_ask:.3f}", \
               loser_side, loser_bid, winner_ask

    # E) v6.2.0: BTC-confirmation guard. The book can be overconfident
    # (winner_ask ≥ threshold based on momentum forecasting) when BTC is
    # barely separated from strike. Today's catastrophes all fired with
    # |btc_delta| ≤ $30 — pure noise zone where BTC could reverse before
    # settle. This guard requires real BTC commitment before trusting the
    # book. Set BS_MIN_BTC_DELTA_USD=0 to disable (= v6.1.x behavior).
    # Fail-open: if BTC sample unavailable (binance feed hiccup), skip the
    # guard so the bot keeps firing on book conviction alone.
    if _BS_MIN_BTC_DELTA_USD > 0.0:
        btc_now_g = _btc_closest(state, now, 5.0)
        btc_strike_g = _btc_closest(state, pos.end_ts - pos.duration_s, 30.0)
        if btc_now_g is not None and btc_strike_g is not None:
            btc_delta_g = btc_now_g - btc_strike_g
            if abs(btc_delta_g) < _BS_MIN_BTC_DELTA_USD:
                pos.sell_loser_consecutive_ticks = 0
                return False, (f"btc_delta_below_min:"
                               f"{btc_delta_g:+.0f}/{_BS_MIN_BTC_DELTA_USD:.0f}"), \
                       loser_side, loser_bid, winner_ask

    # D) loser_bid >= min (price floor — don't sell at $0.01 because
    # the loser is already collapsing — let it resolve)
    if loser_bid < _BS_SELL_MIN_BID:
        pos.sell_loser_consecutive_ticks = 0
        return False, f"loser_bid_below_min:{loser_bid:.3f}", \
               loser_side, loser_bid, winner_ask

    # All instantaneous gates pass — increment persistence counter
    pos.sell_loser_consecutive_ticks += 1
    pos.identified_loser_side = loser_side

    # C) persistence
    if pos.sell_loser_consecutive_ticks < int(_BS_SELL_PERSIST_S):
        return False, (f"persisting:{pos.sell_loser_consecutive_ticks}/"
                       f"{int(_BS_SELL_PERSIST_S)}"), \
               loser_side, loser_bid, winner_ask

    return True, "fire", loser_side, loser_bid, winner_ask


# ───────────────────────────────────────────────────────────────────────
# v6.2.0 — BTC late-fallback sell-loser
# ───────────────────────────────────────────────────────────────────────
# Independent fire path that triggers on BTC fundamentals alone (no book
# conviction required). Fires when:
#   - TTR ≤ 60s
#   - |btc_now - btc_strike| ≥ BS_BTC_LATE_THRESHOLD_USD (default $80)
#   - Books are present and not stale
#   - Loser bid > 0
#
# Direction: BTC up → NO is loser (sell NO). BTC down → YES is loser.
#
# Designed to capture held-both markets where BTC made a sharp final-minute
# commit but the order book never reached the 0.93 conviction threshold.
# The $80 threshold + 60s window combination is empirically robust: in May 3
# data, BTC crossing |Δ|≥$80 in the last 60s never reversed by settle.
#
# This path runs ONLY when the PROD path didn't fire on this market AND
# neither leg has been closed yet (idempotent — no double-fire).
# ───────────────────────────────────────────────────────────────────────
def _bs_evaluate_btc_late_fallback(
        state: BotState, pos: BothSidesPosition,
        now: float) -> Tuple[bool, str, str, float, float]:
    """Returns (should_fire, reason, loser_side, loser_bid, winner_ask).
    Disabled when _BS_BTC_LATE_THRESHOLD_USD is at its sentinel (999999)."""
    if _BS_BTC_LATE_THRESHOLD_USD >= 999999.0:
        return False, "disabled", "", 0.0, 0.0

    ttr = pos.end_ts - now
    if ttr > 60.0 or ttr < 0.0:
        return False, "ttr_outside_60s", "", 0.0, 0.0

    btc_now = _btc_closest(state, now, 5.0)
    btc_strike = _btc_closest(state, pos.end_ts - pos.duration_s, 30.0)
    if btc_now is None or btc_strike is None:
        return False, "btc_unavailable", "", 0.0, 0.0

    delta = btc_now - btc_strike
    if abs(delta) < _BS_BTC_LATE_THRESHOLD_USD:
        return False, f"btc_delta_below:{delta:+.0f}", "", 0.0, 0.0

    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        return False, "no_book", "", 0.0, 0.0

    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        return False, f"book_stale:{book_age_max:.0f}s", "", 0.0, 0.0

    # BTC up → NO is loser; BTC down → YES is loser
    if delta > 0:
        loser_side = "NO"
        loser_bid = float(no_book.bid)
        winner_ask = float(yes_book.ask)
    else:
        loser_side = "YES"
        loser_bid = float(yes_book.bid)
        winner_ask = float(no_book.ask)

    if loser_bid <= 0.0:
        return False, f"loser_bid_zero:{loser_bid:.3f}", loser_side, loser_bid, winner_ask

    reason = (f"btc_late_fallback:delta={delta:+.0f},"
              f"thr={_BS_BTC_LATE_THRESHOLD_USD:.0f},ttr={ttr:.0f}s")
    return True, reason, loser_side, loser_bid, winner_ask


# ───────────────────────────────────────────────────────────────────────
# v6.2.1 — Late-conviction sell-loser override
# ───────────────────────────────────────────────────────────────────────
# Bypass the standard $30 BTC guard when book conviction is overwhelming AND
# TTR is extremely short AND BTC at least weakly supports the book direction.
# Rationale: at TTR ≤ 5s with winner_ask ≥ 0.98, the book has fully committed
# and BTC has no time left to reverse $10+. The standard $30 guard is
# appropriate for normal TTR (60-240s) where reversals are still possible,
# but is too restrictive at TTR ≤ 5s. This path captures held-both markets
# where v6.2.0's main guard is too conservative.
#
# May 3 modeling: 14 fires, 14 correct, 0 catastrophes, +$1.40/day vs the
# held-both baseline. With the BTC support requirement (≥$10), reversal risk
# in the remaining few seconds is effectively zero.
#
# This path only fires when:
#   - PROD path didn't fire (e.g. blocked by BTC guard or loser_bid floor)
#   - BTC late-fallback didn't fire
#   - Neither leg has been closed yet (idempotent — no double-fire)
# ───────────────────────────────────────────────────────────────────────
def _bs_evaluate_late_conviction(
        state: BotState, pos: BothSidesPosition,
        now: float) -> Tuple[bool, str, str, float, float]:
    """Returns (should_fire, reason, loser_side, loser_bid, winner_ask).
    Disabled when any of the three thresholds are at sentinel values
    (TTR_S=0, WINNER_THRESHOLD>=1.0, MIN_BTC_USD>=999)."""
    # Disable check
    if (_BS_LATE_CONV_TTR_S <= 0.0
            or _BS_LATE_CONV_WINNER_THRESHOLD >= 1.0
            or _BS_LATE_CONV_MIN_BTC_USD >= 999.0):
        return False, "disabled", "", 0.0, 0.0

    ttr = pos.end_ts - now
    if ttr > _BS_LATE_CONV_TTR_S or ttr < 0.0:
        return False, "ttr_outside_window", "", 0.0, 0.0

    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        return False, "no_book", "", 0.0, 0.0

    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        return False, f"book_stale:{book_age_max:.0f}s", "", 0.0, 0.0

    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    yes_bid = float(yes_book.bid)
    no_bid = float(no_book.bid)

    # Identify winner side (highest ask)
    if yes_ask >= no_ask and yes_ask > 0:
        winner_side = "YES"
        winner_ask = yes_ask
        loser_side = "NO"
        loser_bid = no_bid
    elif no_ask > 0:
        winner_side = "NO"
        winner_ask = no_ask
        loser_side = "YES"
        loser_bid = yes_bid
    else:
        return False, "both_asks_zero", "", 0.0, 0.0

    # Conviction check
    if winner_ask < _BS_LATE_CONV_WINNER_THRESHOLD:
        return False, f"winner_ask_below:{winner_ask:.3f}", \
               loser_side, loser_bid, winner_ask

    # BTC support check — must agree with book direction
    btc_now = _btc_closest(state, now, 5.0)
    btc_strike = _btc_closest(state, pos.end_ts - pos.duration_s, 30.0)
    if btc_now is None or btc_strike is None:
        return False, "btc_unavailable", loser_side, loser_bid, winner_ask
    btc_delta = btc_now - btc_strike

    # Book says winner=YES → BTC delta must be ≥ +threshold (BTC up supports YES)
    # Book says winner=NO → BTC delta must be ≤ -threshold (BTC down supports NO)
    if winner_side == "YES":
        if btc_delta < _BS_LATE_CONV_MIN_BTC_USD:
            return False, f"btc_no_support:delta={btc_delta:+.0f}", \
                   loser_side, loser_bid, winner_ask
    else:  # winner_side == "NO"
        if btc_delta > -_BS_LATE_CONV_MIN_BTC_USD:
            return False, f"btc_no_support:delta={btc_delta:+.0f}", \
                   loser_side, loser_bid, winner_ask

    # Loser bid > 0 (need something to sell at)
    if loser_bid <= 0.0:
        return False, f"loser_bid_zero:{loser_bid:.3f}", \
               loser_side, loser_bid, winner_ask

    reason = (f"late_conv:winner_ask={winner_ask:.3f},"
              f"btc_delta={btc_delta:+.0f},ttr={ttr:.1f}s")
    return True, reason, loser_side, loser_bid, winner_ask


# ───────────────────────────────────────────────────────────────────────
# v6.2.3 — VERIFICATION-LATE strategy (CORRECTED — book cents, not BTC delta)
# ───────────────────────────────────────────────────────────────────────
# Pure book-based tiered logic. NO BTC check, NO BTC-confirmation guard,
# NO loser_bid floor (>0 only), NO persistence.
# Designed for side-by-side A/B comparison against v6.2.1's full stack.
#
# Activated when env var BS_STRATEGY=verification_late. When active, this
# REPLACES all v6.2.1 sell-loser paths (PROD, BTC late-fallback, late-conv).
#
# Fire conditions (winner_ask is the higher of yes_ask, no_ask):
#   - TTR ≤ 60s  AND  winner_ask ≥ 0.90  → fire (Phase B)
#   - TTR ≤ 30s  AND  winner_ask ≥ 0.85  → fire (Phase C)
#   - TTR ≤ 10s  AND  winner_ask ≥ 0.80  → fire (Phase D)
#
# Direction: side with higher ask is "winner"; sell the OTHER side at its bid.
#
# Requires:
#   - Books present
#   - Books not stale (≤30s)
#   - Loser bid > 0 (need *something* to sell at)
#
# May 3 modeling on actual depth_log: 76 fires of 78 markets, 70 correct,
# 6 catastrophes (8% catastrophe rate when fired). Net P&L: −$4.40 vs
# actual day's −$6.60 (Δ +$2.20/day vs PROD-as-it-ran-today).
# ───────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════
# v6.3.0: BSS — Both-Sides See-Saw entry evaluator + entry helper
# ═══════════════════════════════════════════════════════════════════
# Mirror of _bs_evaluate_verification_late. Same skeleton, opposite
# direction:
#   verification_late:  arm on winner_ask ≥ 0.70, sustain, fire SELL_LOSER
#   bss_entry:          arm on cheap_ask ≤ 0.45, sustain, fire BUY_FIRST_LEG
#
# State lives on MultiDurationMarket.bss_* (mirroring how VL state lives
# on BothSidesPosition.vl_*). Both halves of the strategy use the same
# logger (_bs_log_trade_event) and the same bs_trades CSV.
#
# Two-stage entry:
#   stage 1 (WATCH → WAITING_2ND): one side dips below T_FIRST and stays
#     for SUSTAIN_FIRST_S seconds → "buy" first leg at current ask
#   stage 2 (WAITING_2ND → BOTH | ABORT): other side dips below
#     T_SECOND_STRICT (within RELAX_AT_S) or T_SECOND_RELAXED (after
#     RELAX_AT_S) and stays for SUSTAIN_SECOND_S seconds → "buy" second
#     leg, create a real BothSidesPosition with both legs filled at the
#     captured BSS prices (so the bot's existing resolution flow handles
#     it normally)
#   abort (WAITING_2ND → ABORT): if elapsed > ABORT_AT_S without second
#     leg confirming → "sell" first leg at current bid; log; done
#
# DRY-only by design (cfg.mode != 'dry' refused at fire site).

def _ppmp_bail(state: BotState, mdm: MultiDurationMarket, now: float,
               yes_ask: float, no_ask: float, yes_bid: float, no_bid: float,
               sold_at: float, maker: bool, reason: str) -> None:
    """v6.3.14 PPMP: managed bail of an unpaired leg-1. Sets ABORT state and
    logs a FEE-ACCURATE BSS_ABORT_DRY row. maker=True → sold-up (0 taker fee,
    rebate-eligible); maker=False → taker dump (crypto taker fee charged).

    Fees are modeled here even though the rest of this build's DRY P&L is
    fee-free, per the requirement that a bail is a real sell of a held leg
    (taker fee on entry + on the taker exit). Controlled by
    BS_USE_POLYMARKET_FEE_FORMULA / BS_POLYMARKET_TAKER_FEE_RATE.
    """
    mdm.bss_abort_sold_at = sold_at
    mdm.bss_abort_ts = now
    mdm.bss_abort_reason = reason
    mdm.bss_state = "ABORT"
    first_price = mdm.bss_first_price or 0.0
    size = state.config.position_size_usdc
    qty = size / first_price if first_price > 0 else 0.0
    # leg-1 was bought as a taker (took the ask) → entry-side fee always applies
    entry_fee = _ppmp_taker_fee(qty, first_price)
    # exit: maker sell-up pays 0; taker dump (hard_stop / timeout) pays the fee
    exit_fee = 0.0 if maker else _ppmp_taker_fee(qty, sold_at)
    gross = (sold_at - first_price) * qty
    net = gross - entry_fee - exit_fee
    if state.bs_trades_logger is not None:
        try:
            market = mdm.market
            note = (f"src=ppmp_bail,reason={reason},maker={int(maker)},"
                    f"first_paid={first_price:.4f},sold_at={sold_at:.4f},"
                    f"gross={gross:+.4f},entry_fee={entry_fee:.4f},"
                    f"exit_fee={exit_fee:.4f},net_pnl={net:+.4f},"
                    f"fee_model={'on' if _PPMP_USE_FEE else 'off'}")
            row = [
                int(time.time() * 1000), "BSS_ABORT_DRY",
                market.condition_id, market.slug, market.market_url,
                f"{market.end_ts:.0f}", mdm.bss_first_side or "",
                (market.yes_token_id if mdm.bss_first_side == "YES"
                 else market.no_token_id),
                f"{first_price:.4f}",
                f"{(yes_bid if mdm.bss_first_side == 'YES' else no_bid):.4f}",
                f"{size:.4f}", f"{qty:.4f}", f"{sold_at:.4f}", f"{now:.0f}",
                f"{net:+.4f}", f"{(entry_fee + exit_fee):.4f}",
                state.config.mode, note,
            ]
            state.bs_trades_logger.log(row)
        except Exception as e:
            print(f"[ppmp_bail] log error slug={mdm.market.slug}: "
                  f"{type(e).__name__}: {e}", flush=True)
    print(f"[bss_entry] PPMP_BAIL[{reason}] "
          f"market={mdm.market.condition_id[:10]}… "
          f"first={mdm.bss_first_side}@{first_price:.3f} sold@{sold_at:.3f} "
          f"maker={maker} net={net:+.4f}", flush=True)


def _bs_evaluate_bss_entry(state: BotState, mdm: MultiDurationMarket,
                            now: float) -> None:
    """Run one tick of the BSS state machine for one market. Mutates
    mdm.bss_* directly. Mirrors the structural pattern of
    _bs_evaluate_verification_late but for ENTRY rather than SELL.
    Only invoked when _BS_STRATEGY == 'bss_entry'.

    States:
      WATCH         → looking for first-leg sustain trigger
      WAITING_2ND   → first leg "filled"; looking for second-leg sustain
      BOTH          → both legs filled (BothSidesPosition created); done
                       handing off to existing resolution flow
      ABORT         → second never confirmed; first leg sold at bid; done
      RESOLVED      → terminal (only used internally for ABORT path)
    """
    market = mdm.market

    # ── v6.3.9 DIAG ─────────────────────────────────────────────────────
    # Rate-limited (once per 30s per market+reason) tracing of why the
    # evaluator bails out silently. Logged at INFO so it shows in Railway.
    cid_short = market.condition_id[:10]
    diag_key_base = f"_BSS_DIAG_{market.condition_id}"
    last_diag_ts = getattr(mdm, "_bss_diag_last_ts", 0.0)
    diag_due = (now - last_diag_ts) >= 30.0

    def _diag(reason: str, **fields):
        # Only emit one diag per market per 30s; resets last_diag_ts
        if not diag_due:
            return
        mdm._bss_diag_last_ts = now
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[bss_diag] v6.3.9 cid={cid_short}… reason={reason} "
              f"state={mdm.bss_state} {kv}", flush=True)

    # ── end DIAG header ─────────────────────────────────────────────────

    # Once we've handed off to BOTH or finalized ABORT, nothing to do
    if mdm.bss_state in ("BOTH", "ABORT", "RESOLVED"):
        _diag("terminal_state")
        return

    # Idempotency guard: if a position already exists for this market
    # (e.g. created by BSS earlier or by a stale entry path), don't
    # double-enter.
    if market.condition_id in state.both_sides_positions:
        _diag("idempotency_guard_position_exists")
        if mdm.bss_state == "WAITING_2ND":
            mdm.bss_state = "BOTH"
        return

    # Book freshness check (v6.3.8: always sample, gate fires on staleness)
    yb = state.poly_books.get(market.yes_token_id)
    nb = state.poly_books.get(market.no_token_id)
    if yb is None or nb is None:
        _diag("book_missing",
              yes_token=market.yes_token_id[:10] + "…",
              no_token=market.no_token_id[:10] + "…",
              yb_present=(yb is not None),
              nb_present=(nb is not None),
              total_books_tracked=len(state.poly_books))
        return
    yes_ask = float(yb.ask)
    no_ask = float(nb.ask)
    yes_bid = float(yb.bid)
    no_bid = float(nb.bid)
    if yes_ask <= 0 or no_ask <= 0:
        _diag("ask_invalid",
              yes_ask=f"{yes_ask:.4f}", no_ask=f"{no_ask:.4f}",
              yes_bid=f"{yes_bid:.4f}", no_bid=f"{no_bid:.4f}")
        return

    # Always log a "healthy tick" diag on the 30s cadence so we can confirm
    # the evaluator IS being called for this market with valid books.
    book_age = max(now - yb.last_update_ts, now - nb.last_update_ts)
    n_samples = len(mdm.bss_price_samples)
    last_sample_age = (now - mdm.bss_last_sample_ts) if mdm.bss_last_sample_ts else None
    _diag("healthy_tick",
          yes_ask=f"{yes_ask:.3f}", no_ask=f"{no_ask:.3f}",
          book_age=f"{book_age:.1f}",
          n_samples=n_samples,
          last_sample_age_s=f"{last_sample_age:.1f}" if last_sample_age is not None else "never")

    # v6.3.3: sample price history at ~1Hz for dashboard rendering
    # v6.3.8: ALWAYS append, even if books are stale. Pre-market tokens can
    # go silent for minutes when MM quotes don't change. Previously the
    # 30s freshness gate caused us to silently stop sampling AND stop
    # evaluating fires for quiet pre-market tokens. Now we always sample
    # the last-seen prices (so dashboard reflects truth) and only gate
    # FIRES on freshness below.
    if now - mdm.bss_last_sample_ts >= 1.0:
        mdm.bss_price_samples.append((now, yes_ask, no_ask))
        mdm.bss_last_sample_ts = now
        # Cap at 1800 samples (~30min at 1Hz)
        if len(mdm.bss_price_samples) > 1800:
            mdm.bss_price_samples = mdm.bss_price_samples[-1800:]

    # v6.3.8: freshness gate moved to AFTER sample. Bumped 30s → 120s
    # because pre-market price_change events are naturally sparse — books
    # can be quiet for >30s without anything being wrong.
    book_age = max(now - yb.last_update_ts, now - nb.last_update_ts)
    if book_age > 120.0:
        return

    # ─── Resolution gate ───
    # If market has ended and we're still in WAITING_2ND, force-abort
    if now >= market.end_ts:
        if mdm.bss_state == "WAITING_2ND":
            sold = yes_bid if mdm.bss_first_side == "YES" else no_bid
            mdm.bss_abort_sold_at = sold
            mdm.bss_abort_ts = now
            mdm.bss_state = "ABORT"
            _bs_log_bss_abort_event(state, mdm, now,
                                     yes_ask, no_ask, yes_bid, no_bid,
                                     reason="market_ended_in_WAITING_2ND")
            mdm.bss_state = "RESOLVED"
        return

    # v6.3.2: Phase determination — pre-market vs live window.
    # The 5m market exists for ~30 min before window open. T=0 of the
    # live window is at (end_ts - duration_s) = end_ts - 300 for 5m.
    window_open_ts = market.end_ts - mdm.duration_s
    in_pre_market = now < window_open_ts
    # Threshold for first leg: looser in pre-market
    t_first = _BS_BSS_T_FIRST_PRE if in_pre_market else _BS_BSS_T_FIRST
    sustain_first_s = (_BS_BSS_SUSTAIN_FIRST_PRE_S if in_pre_market
                        else _BS_BSS_SUSTAIN_FIRST_S)

    # ── WATCH: first-leg sustain detection ──
    if mdm.bss_state == "WATCH":
        # ── v6.3.17 PPMP fallback: no cheap leg-1, take the flat pair ──
        # If we're within FALLBACK_LEAD_S of the live window opening and still
        # have no leg-1 (the market never dipped to T_FIRST_PRE), buy BOTH
        # sides at the current ask so we don't miss the market. Becomes a flat
        # ~1.00 pair held to expiry on the existing exit stack.
        if (_BS_BSS_FALLBACK_BOTH_ENABLED and in_pre_market
                and (window_open_ts - now) <= _BS_BSS_FALLBACK_LEAD_S):
            sum_ask = yes_ask + no_ask
            if (yes_ask > 0.0 and no_ask > 0.0
                    and sum_ask <= _BS_BSS_FALLBACK_MAX_SUM):
                # leg-1 = the cheaper side (cosmetic), leg-2 = the other; both
                # filled now at their current asks.
                if yes_ask <= no_ask:
                    mdm.bss_first_side = "YES"
                    mdm.bss_first_price = yes_ask
                    mdm.bss_second_price = no_ask
                else:
                    mdm.bss_first_side = "NO"
                    mdm.bss_first_price = no_ask
                    mdm.bss_second_price = yes_ask
                mdm.bss_first_fill_ts = now
                mdm.bss_second_fill_ts = now
                mdm.bss_first_filled_in_pre = True
                mdm.bss_second_phase = "fallback"
                print(f"[bss_entry] FALLBACK_BOTH market={market.condition_id[:10]}… "
                      f"yes={yes_ask:.3f} no={no_ask:.3f} sum={sum_ask:.4f} "
                      f"({_BS_BSS_FALLBACK_LEAD_S:.0f}s pre-open, no cheap leg-1 "
                      f"→ flat pair)", flush=True)
                _create_bss_position_and_log(state, mdm, now, yes_ask, no_ask,
                                              yes_bid, no_bid, 0.0, sum_ask, 0.0)
                return
            # book too wide / blown out — skip fallback, stay WATCH
        # v6.3.2: track separate pre-market and live-market streaks so
        # transitioning between phases doesn't carry over a stale streak
        if in_pre_market:
            yes_st_field = 'bss_yes_below_pre_start_ts'
            no_st_field  = 'bss_no_below_pre_start_ts'
        else:
            yes_st_field = 'bss_yes_below_first_start_ts'
            no_st_field  = 'bss_no_below_first_start_ts'
        # YES streak
        if yes_ask < t_first:
            if getattr(mdm, yes_st_field) is None:
                setattr(mdm, yes_st_field, now)
        else:
            setattr(mdm, yes_st_field, None)
        # NO streak
        if no_ask < t_first:
            if getattr(mdm, no_st_field) is None:
                setattr(mdm, no_st_field, now)
        else:
            setattr(mdm, no_st_field, None)
        # Sustain check (longer streak wins on tie — deterministic)
        yes_sus_s = ((now - getattr(mdm, yes_st_field))
                     if getattr(mdm, yes_st_field) else 0.0)
        no_sus_s = ((now - getattr(mdm, no_st_field))
                    if getattr(mdm, no_st_field) else 0.0)
        fire_side = None
        fire_price = 0.0
        sus_s_at_fire = 0.0
        if yes_sus_s >= sustain_first_s and yes_sus_s >= no_sus_s:
            fire_side = "YES"; fire_price = yes_ask; sus_s_at_fire = yes_sus_s
        elif no_sus_s >= sustain_first_s:
            fire_side = "NO"; fire_price = no_ask; sus_s_at_fire = no_sus_s
        if fire_side:
            # v6.3.18 PPMP: real-dip filter. A 1¢ pre-market book quotes one
            # side at 0.495 forever — that's not a discount. Require the
            # candidate side's current ask to be at least REAL_DIP_DROP below
            # its own max within the last REAL_DIP_LOOKBACK_S seconds, so we
            # only fire on actual price action, not the resting book.
            if _BS_BSS_REAL_DIP_DROP > 0.0:
                _side_idx = 1 if fire_side == "YES" else 2
                _rmax = _bss_recent_max_ask(mdm.bss_price_samples, _side_idx,
                                              now, _BS_BSS_REAL_DIP_LOOKBACK_S)
                if _rmax is None or (_rmax - fire_price) < _BS_BSS_REAL_DIP_DROP:
                    # Structural quote, not a real dip — reset streak and skip
                    setattr(mdm,
                            yes_st_field if fire_side == "YES" else no_st_field,
                            None)
                    return
            # v6.3.16 PPMP: leg-1 is a PRE-MARKET entry only. If the live
            # window has opened and we somehow still tripped a first-leg
            # trigger, do NOT open it — there's no time left to complete the
            # pair, so it would just force an immediate bail. Reset the streak
            # and skip. (Reversible via BS_BSS_LEG1_PREMARKET_ONLY=false.)
            if _BS_BSS_LEG1_PREMARKET_ONLY and not in_pre_market:
                setattr(mdm, yes_st_field if fire_side == "YES" else no_st_field, None)
                return
            # v6.3.6: BTC-velocity filter — LIVE PHASE ONLY. Pre-market prices
            # are flat noise that don't react to BTC moves second-by-second,
            # so filtering on BTC velocity during pre-market is meaningless
            # and was blocking a large fraction of would-be fires for no
            # benefit. Filter still applies during live window where it
            # legitimately defends against buying mid-trend.
            if _BS_BSS_BTC_VEL_FILTER_PCT > 0.0 and not in_pre_market:
                btc_v_pct = _btc_velocity_pct(state, now,
                                                _BS_BSS_BTC_VEL_LOOKBACK_S)
                if btc_v_pct is not None:
                    fired_with_btc = (
                        (fire_side == "YES" and btc_v_pct >  _BS_BSS_BTC_VEL_FILTER_PCT) or
                        (fire_side == "NO"  and btc_v_pct < -_BS_BSS_BTC_VEL_FILTER_PCT)
                    )
                    if fired_with_btc:
                        # Reset streak so we don't spam-block on the next tick
                        setattr(mdm, yes_st_field if fire_side == "YES" else no_st_field, None)
                        print(f"[bss_entry] FIRST_LEG_BLOCKED "
                              f"market={market.condition_id[:10]}… "
                              f"side={fire_side} ask={fire_price:.3f} "
                              f"btc_v_30s={btc_v_pct:+.4f}% "
                              f"phase={'PRE' if in_pre_market else 'LIVE'} "
                              f"(moving with side; skip)", flush=True)
                        return
            mdm.bss_first_side = fire_side
            mdm.bss_first_price = fire_price
            mdm.bss_first_fill_ts = now
            mdm.bss_first_filled_in_pre = in_pre_market
            mdm.bss_state = "WAITING_2ND"
            _bs_log_bss_first_leg_event(state, mdm, now,
                                         yes_ask, no_ask, yes_bid, no_bid,
                                         sus_s=sus_s_at_fire)
            phase_tag = "PRE" if in_pre_market else "LIVE"
            ttr_to_open = window_open_ts - now
            print(f"[bss_entry] FIRST_LEG[{phase_tag}] market={market.condition_id[:10]}… "
                  f"side={fire_side} @{fire_price:.3f} "
                  f"sustain={sus_s_at_fire:.1f}s "
                  f"TTR={market.end_ts - now:.0f}s "
                  f"window_open_in={ttr_to_open:.0f}s "
                  f"slug={market.slug[:30]}",
                  flush=True)
        return

    # ── WAITING_2ND: second-leg sustain detection + abort gate ──
    if mdm.bss_state == "WAITING_2ND":
        # v6.3.2: Determine the timer reference for abort/relax tiers.
        # If first leg fired in pre-market, the abort/relax timer starts
        # at WINDOW OPEN (T=0), not at first-leg fill (which could be
        # 25min+ ago). If first leg fired in live window, timer starts
        # at first-leg fill (existing v6.3.0 behavior).
        if mdm.bss_first_filled_in_pre:
            timer_start_ts = max(mdm.bss_first_fill_ts or now, window_open_ts)
        else:
            timer_start_ts = mdm.bss_first_fill_ts or now
        elapsed_s = now - timer_start_ts

        # v6.3.14 PPMP: the old abort-at-ABORT_AT_S (270s) gate is replaced by
        # the managed bail in the live-window block below (first-30s sell-up +
        # 0.46 bid hard-cashout). No timeout-abort here.

        # ── Pre-market second-leg detection ──
        # Pre-market uses a single threshold (T_SECOND_PRE), no relax tier,
        # no abort. Just wait for a sustained dip.
        other_ask = no_ask if mdm.bss_first_side == "YES" else yes_ask
        if in_pre_market:
            if other_ask < _BS_BSS_T_SECOND_PRE:
                if mdm.bss_other_below_pre_start_ts is None:
                    mdm.bss_other_below_pre_start_ts = now
            else:
                mdm.bss_other_below_pre_start_ts = None
            sus_s_pre = ((now - mdm.bss_other_below_pre_start_ts)
                          if mdm.bss_other_below_pre_start_ts else 0.0)
            if sus_s_pre >= _BS_BSS_SUSTAIN_SECOND_PRE_S and other_ask < _BS_BSS_T_SECOND_PRE:
                mdm.bss_second_price = other_ask
                mdm.bss_second_fill_ts = now
                mdm.bss_second_phase = "pre"
                _create_bss_position_and_log(state, mdm, now,
                                              yes_ask, no_ask,
                                              yes_bid, no_bid,
                                              sus_s_pre, _BS_BSS_T_SECOND_PRE,
                                              elapsed_s)
            return

        # ── v6.3.14 PPMP: live-window — complete cheap, else managed bail ──
        # Reached only when the live window is open and leg-2 hasn't filled.
        # We try to complete the pair (floor / open-grace ≤0.51) AND, in
        # parallel, run a managed bail of the unpaired leg-1: sell-up over the
        # first BAIL_WINDOW_S, hard cashout if the bid drops below the floor,
        # take the bid at timeout. Completion always wins if it fires first.
        live_elapsed = now - window_open_ts
        first_bid = yes_bid if mdm.bss_first_side == "YES" else no_bid
        first_ask = yes_ask if mdm.bss_first_side == "YES" else no_ask
        if mdm.bss_bail_started_ts is None:
            # First live tick: arm the managed bail. "Post at the current ask"
            # = our resting sell-up offer for leg 1.
            mdm.bss_bail_started_ts = now
            mdm.bss_bail_sell_target = first_ask

        # PPMP priority: COMPLETE a cheap pair first (a held pair at sum<1.00
        # always beats a losing bail); only if completion isn't available do we
        # run the bail (hard-stop / sold-up / timeout). Matches the spec:
        # "complete at 0.50/0.51 if you can, else sell."
        # (1) COMPLETE THE PAIR if leg-2 is cheap. Floor (deep dip) fires
        # immediately; otherwise accept up to the open-grace ask on a short
        # sustain.
        if other_ask <= _BS_BSS_T_SECOND_FLOOR:
            mdm.bss_second_price = other_ask
            mdm.bss_second_fill_ts = now
            mdm.bss_second_phase = "floor"
            print(f"[bss_entry] SECOND_LEG_FLOOR market={market.condition_id[:10]}… "
                  f"other_ask={other_ask:.3f} ≤ floor={_BS_BSS_T_SECOND_FLOOR:.2f}",
                  flush=True)
            _create_bss_position_and_log(state, mdm, now, yes_ask, no_ask,
                                          yes_bid, no_bid, 0.0,
                                          _BS_BSS_T_SECOND_FLOOR, elapsed_s)
            return
        if other_ask <= _BS_BSS_T_SECOND_OPEN:
            if mdm.bss_other_below_strict_start_ts is None:
                mdm.bss_other_below_strict_start_ts = now
            open_sus = now - mdm.bss_other_below_strict_start_ts
            if open_sus >= _BS_BSS_SUSTAIN_SECOND_S:
                mdm.bss_second_price = other_ask
                mdm.bss_second_fill_ts = now
                mdm.bss_second_phase = "open"
                print(f"[bss_entry] SECOND_LEG_OPEN market={market.condition_id[:10]}… "
                      f"other_ask={other_ask:.3f} ≤ open={_BS_BSS_T_SECOND_OPEN:.2f} "
                      f"sustain={open_sus:.1f}s", flush=True)
                _create_bss_position_and_log(state, mdm, now, yes_ask, no_ask,
                                              yes_bid, no_bid, open_sus,
                                              _BS_BSS_T_SECOND_OPEN, elapsed_s)
                return
        else:
            mdm.bss_other_below_strict_start_ts = None

        # (2) HARD STOP — can't complete a pair this tick; if leg-1's bid has
        # dropped below the cashout floor, dump it now (taker).
        if first_bid < _BS_BSS_BAIL_HARD_STOP:
            _ppmp_bail(state, mdm, now, yes_ask, no_ask, yes_bid, no_bid,
                       sold_at=first_bid, maker=False, reason="hard_stop")
            return

        # (3) SOLD-UP — resting sell offer lifted (bid rose to our ask). Maker
        # fill at target (0 fee, rebate-eligible).
        if (mdm.bss_bail_sell_target is not None
                and first_bid >= mdm.bss_bail_sell_target):
            _ppmp_bail(state, mdm, now, yes_ask, no_ask, yes_bid, no_bid,
                       sold_at=mdm.bss_bail_sell_target, maker=True,
                       reason="sold_up")
            return

        # (4) TIMEOUT — bail window elapsed without a pair or sell-up. Take the
        # current bid (taker).
        if live_elapsed >= _BS_BSS_BAIL_WINDOW_S:
            _ppmp_bail(state, mdm, now, yes_ask, no_ask, yes_bid, no_bid,
                       sold_at=first_bid, maker=False, reason="timeout")
            return
        return


def _create_bss_position_and_log(state: BotState, mdm: MultiDurationMarket,
                                   now: float, yes_ask: float, no_ask: float,
                                   yes_bid: float, no_bid: float,
                                   sus_s: float, threshold: float,
                                   elapsed_s: float) -> None:
    """v6.3.2: factor out the BothSidesPosition creation + logging that
    follows a confirmed second-leg sustain. Keeps the WAITING_2ND state
    machine readable. Same logic as before, just extracted."""
    market = mdm.market
    second_side = "NO" if mdm.bss_first_side == "YES" else "YES"
    yes_price = (mdm.bss_first_price if mdm.bss_first_side == "YES"
                 else mdm.bss_second_price)
    no_price = (mdm.bss_first_price if mdm.bss_first_side == "NO"
                else mdm.bss_second_price)
    yes_fill_ts = (mdm.bss_first_fill_ts if mdm.bss_first_side == "YES"
                   else mdm.bss_second_fill_ts)
    no_fill_ts = (mdm.bss_first_fill_ts if mdm.bss_first_side == "NO"
                  else mdm.bss_second_fill_ts)
    pos = _bs_place_bss_entry(state, mdm,
                               yes_price=yes_price, no_price=no_price,
                               yes_bid=yes_bid, no_bid=no_bid,
                               yes_fill_ts=yes_fill_ts,
                               no_fill_ts=no_fill_ts)
    if pos is not None:
        mdm.bss_state = "BOTH"
        _bs_log_bss_second_leg_event(state, mdm, pos, now,
                                      yes_ask, no_ask, yes_bid, no_bid,
                                      sus_s=sus_s, threshold=threshold)
        print(f"[bss_entry] SECOND_LEG[{mdm.bss_second_phase}] "
              f"market={market.condition_id[:10]}… "
              f"side={second_side} @{mdm.bss_second_price:.3f} "
              f"phase={mdm.bss_second_phase} "
              f"sustain={sus_s:.1f}s "
              f"elapsed={elapsed_s:.0f}s "
              f"sum_ask={(mdm.bss_first_price + mdm.bss_second_price):.4f}",
              flush=True)


def _bs_place_bss_entry(state: BotState, mdm: MultiDurationMarket,
                         yes_price: float, no_price: float,
                         yes_bid: float, no_bid: float,
                         yes_fill_ts: float, no_fill_ts: float
                         ) -> Optional[BothSidesPosition]:
    """v6.3.0: BSS variant of _bs_place_entry. Creates a real
    BothSidesPosition using prices captured at DIFFERENT timestamps
    (first leg early, second leg later) — unlike _bs_place_entry which
    fires both legs at the same instant. Hands off to the existing
    resolution flow by inserting the position into
    state.both_sides_positions.

    DRY-only by design — same gate as _bs_place_entry.
    """
    cfg = state.config
    market = mdm.market
    size_usdc = cfg.position_size_usdc

    if cfg.mode != "dry":
        print(f"[bss_entry][BLOCKED] LIVE BSS not implemented in v6.3.0 — "
              f"refusing entry on market_id={market.condition_id[:10]}",
              flush=True)
        return None

    yes_qty = size_usdc / yes_price if yes_price > 0 else 0.0
    no_qty = size_usdc / no_price if no_price > 0 else 0.0

    yes_leg = BothSidesLeg(
        side="YES", token_id=market.yes_token_id,
        entry_ask=yes_price, entry_bid=yes_bid,
        size_usdc=size_usdc, qty_shares=yes_qty,
        entry_ts=yes_fill_ts,
        peak_bid=yes_bid, peak_bid_ts=yes_fill_ts,
    )
    no_leg = BothSidesLeg(
        side="NO", token_id=market.no_token_id,
        entry_ask=no_price, entry_bid=no_bid,
        size_usdc=size_usdc, qty_shares=no_qty,
        entry_ts=no_fill_ts,
        peak_bid=no_bid, peak_bid_ts=no_fill_ts,
    )
    sum_ask = yes_price + no_price
    pos = BothSidesPosition(
        market_id=market.condition_id,
        market_url=market.market_url,
        market_question=market.question,
        slug=market.slug,
        duration_s=mdm.duration_s,
        end_ts=market.end_ts,
        entry_ts=max(yes_fill_ts, no_fill_ts),
        sum_ask_at_entry=sum_ask,
        yes_leg=yes_leg,
        no_leg=no_leg,
    )
    state.both_sides_positions[market.condition_id] = pos
    state.bs_entered_market_ids.add(market.condition_id)
    state.bs_total_entered += 1

    # Log both legs through the standard logger so bs_trades CSV picks
    # them up. Tag with bss_entry so downstream parser can split.
    _bs_log_trade_event(state, "ENTRY_YES_DRY", pos, yes_leg,
                         note=f"src=bss_entry,first_side={mdm.bss_first_side}")
    _bs_log_trade_event(state, "ENTRY_NO_DRY", pos, no_leg,
                         note=f"src=bss_entry,first_side={mdm.bss_first_side}")
    return pos


def _bs_log_bss_first_leg_event(state: BotState, mdm: MultiDurationMarket,
                                  now: float, yes_ask: float, no_ask: float,
                                  yes_bid: float, no_bid: float,
                                  sus_s: float) -> None:
    """v6.3.0: log a BSS_FIRST_LEG_DRY event to bs_trades. No
    BothSidesPosition exists yet (only first leg fired), so we
    construct the row directly via state.bs_trades_logger.log()
    using the same schema as _bs_log_trade_event."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        ttr_s = market.end_ts - now
        note = (f"src=bss_entry,sustain={sus_s:.1f}s,ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f},"
                f"yes_bid={yes_bid:.4f},no_bid={no_bid:.4f}")
        size = state.config.position_size_usdc
        qty = size / mdm.bss_first_price if mdm.bss_first_price else 0.0
        row = [
            int(time.time() * 1000),
            "BSS_FIRST_LEG_DRY",
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            mdm.bss_first_side or "",
            (market.yes_token_id if mdm.bss_first_side == "YES"
             else market.no_token_id),
            f"{mdm.bss_first_price:.4f}",
            f"{(yes_bid if mdm.bss_first_side == 'YES' else no_bid):.4f}",
            f"{size:.4f}",
            f"{qty:.4f}",
            "0.0000", "",
            "0.0000",
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error first_leg slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


def _bs_log_bss_second_leg_event(state: BotState, mdm: MultiDurationMarket,
                                   pos: BothSidesPosition, now: float,
                                   yes_ask: float, no_ask: float,
                                   yes_bid: float, no_bid: float,
                                   sus_s: float, threshold: float) -> None:
    """v6.3.0: log a BSS_SECOND_LEG_DRY event. At this point a real
    BothSidesPosition exists, so we use the standard logger."""
    second_side = "NO" if mdm.bss_first_side == "YES" else "YES"
    second_leg = pos.no_leg if second_side == "NO" else pos.yes_leg
    elapsed_s = now - (mdm.bss_first_fill_ts or now)
    note = (f"src=bss_entry,phase={mdm.bss_second_phase},"
            f"sustain={sus_s:.1f}s,threshold={threshold:.4f},"
            f"elapsed={elapsed_s:.1f}s,"
            f"first_paid={mdm.bss_first_price:.4f},"
            f"second_paid={mdm.bss_second_price:.4f},"
            f"sum_ask={(mdm.bss_first_price + mdm.bss_second_price):.4f}")
    _bs_log_trade_event(state, "BSS_SECOND_LEG_DRY", pos, second_leg, note=note)


def _bs_log_bss_abort_event(state: BotState, mdm: MultiDurationMarket,
                              now: float, yes_ask: float, no_ask: float,
                              yes_bid: float, no_bid: float,
                              reason: str) -> None:
    """v6.3.0: log a BSS_ABORT_DRY event. No BothSidesPosition exists
    (we never made it to BOTH), so write the row directly."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        first_price = mdm.bss_first_price or 0.0
        sold_at = mdm.bss_abort_sold_at or 0.0
        per_share_pnl = sold_at - first_price
        pnl_usdc = (state.config.position_size_usdc * (sold_at / first_price - 1.0)
                    if first_price > 0 else 0.0)
        elapsed_s = now - (mdm.bss_first_fill_ts or now)
        note = (f"src=bss_entry,reason={reason},"
                f"elapsed={elapsed_s:.1f}s,"
                f"first_paid={first_price:.4f},sold_at={sold_at:.4f},"
                f"per_share_pnl={per_share_pnl:+.4f},"
                f"pnl_usdc={pnl_usdc:+.4f},"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f}")
        size = state.config.position_size_usdc
        qty = size / first_price if first_price > 0 else 0.0
        row = [
            int(time.time() * 1000),
            "BSS_ABORT_DRY",
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            mdm.bss_first_side or "",
            (market.yes_token_id if mdm.bss_first_side == "YES"
             else market.no_token_id),
            f"{first_price:.4f}",
            f"{(yes_bid if mdm.bss_first_side == 'YES' else no_bid):.4f}",
            f"{size:.4f}",
            f"{qty:.4f}",
            f"{sold_at:.4f}",
            f"{now:.0f}",
            f"{pnl_usdc:+.4f}",
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error abort slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


# ───────────────────────────────────────────────────────────────────────
def _bs_evaluate_verification_late(
        state: BotState, pos: BothSidesPosition,
        now: float) -> Tuple[bool, str, str, float, float]:
    """Returns (should_fire, reason, loser_side, loser_bid, winner_ask).
    Only invoked when _BS_STRATEGY == 'verification_late'.

    v6.2.4 ARM/FREEZE state machine (whipsaw detection):
      - On every tick where TTR ≤ 60s, update pos.vl_* state.
      - ARM the moment winner_ask ≥ _BS_VL_ARM_THRESHOLD (default 0.70).
        Captures vl_armed_side, starts vl_peak_winner_ask tracking.
      - Once armed, FREEZE permanently if:
          (a) the winning side flips (different side now has higher ask), OR
          (b) winner_ask drops more than _BS_VL_DROP_TOLERANCE (0.03) below
              the peak observed since arming.
      - Once frozen → never fire on this market for the rest of its life.
      - If TTR ≤ 60s passes without arming (winner_ask never ≥ 0.70) → also
        treated as "not safe to fire" — the verification rules are gated
        behind "armed AND not frozen".

    All state transitions are recorded:
      - sell_loser_status carries the live status (armed/frozen/etc.)
      - On freeze: vl_freeze_reason + vl_freeze_ts captured (visible in CSV
        downstream via diag and in dashboard via sell_loser_status).
    """

    ttr = pos.end_ts - now
    if ttr < 0.0 or ttr > 60.0:
        return False, "ttr_outside_60s", "", 0.0, 0.0

    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        return False, "no_book", "", 0.0, 0.0

    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        return False, f"book_stale:{book_age_max:.0f}s", "", 0.0, 0.0

    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    yes_bid = float(yes_book.bid)
    no_bid = float(no_book.bid)

    # Identify winner = side with HIGHER ask (closer to $1)
    if yes_ask >= no_ask and yes_ask > 0:
        winner_side = "YES"
        winner_ask = yes_ask
        loser_side = "NO"
        loser_bid = no_bid
    elif no_ask > 0:
        winner_side = "NO"
        winner_ask = no_ask
        loser_side = "YES"
        loser_bid = yes_bid
    else:
        return False, "both_asks_zero", "", 0.0, 0.0

    # ────────────────────────────────────────────────────────────────────
    # v6.2.4: ARM / FREEZE state machine
    # ────────────────────────────────────────────────────────────────────
    if pos.vl_frozen:
        return False, (f"vl_frozen:{pos.vl_freeze_reason}"), \
               loser_side, loser_bid, winner_ask

    # ARM logic
    if not pos.vl_armed:
        if winner_ask >= _BS_VL_ARM_THRESHOLD:
            pos.vl_armed = True
            pos.vl_armed_side = winner_side
            pos.vl_peak_winner_ask = winner_ask
            pos.vl_armed_ts = now              # v6.2.5: arm timestamp for vl_armed_for_s diag
            pos.vl_peak_update_count = 1       # v6.2.5: count this initial peak set as update #1
            print(f"[vl_arm] market={pos.market_id[:10]}… armed_side={winner_side} "
                  f"winner_ask={winner_ask:.3f} ttr={ttr:.0f}s", flush=True)
        else:
            # Not yet armed and winner_ask too low — wait for next tick
            return False, (f"vl_unarmed:winner_ask={winner_ask:.3f}<"
                           f"{_BS_VL_ARM_THRESHOLD:.2f},ttr={ttr:.0f}s"), \
                   loser_side, loser_bid, winner_ask

    # Already armed — FREEZE checks
    # Check (a): side flip — was winning, now isn't
    if pos.vl_armed_side != winner_side:
        pos.vl_frozen = True
        pos.vl_freeze_reason = (f"side_flipped:was={pos.vl_armed_side},"
                                f"now={winner_side},winner_ask={winner_ask:.3f}")
        pos.vl_freeze_ts = now
        print(f"[vl_freeze] market={pos.market_id[:10]}… "
              f"reason=side_flip {pos.vl_armed_side}→{winner_side} "
              f"peak={pos.vl_peak_winner_ask:.3f} now={winner_ask:.3f} "
              f"ttr={ttr:.0f}s", flush=True)
        return False, (f"vl_frozen:{pos.vl_freeze_reason}"), \
               loser_side, loser_bid, winner_ask

    # Update peak (only if armed side is still winning, which we just checked)
    if winner_ask > pos.vl_peak_winner_ask:
        pos.vl_peak_winner_ask = winner_ask
        pos.vl_peak_update_count += 1   # v6.2.5: 1Hz-leak diagnostic counter

    # Check (b): drop > tolerance below peak.
    # Add a tiny epsilon (1e-6) to guard against float-arithmetic edge cases
    # like (0.85 - 0.82) yielding 0.0300000...4 instead of exactly 0.03.
    drop = pos.vl_peak_winner_ask - winner_ask
    if drop > _BS_VL_DROP_TOLERANCE + 1e-6:
        pos.vl_frozen = True
        pos.vl_freeze_reason = (f"drop:peak={pos.vl_peak_winner_ask:.3f},"
                                f"now={winner_ask:.3f},drop={drop:.3f}>"
                                f"{_BS_VL_DROP_TOLERANCE:.3f}")
        pos.vl_freeze_ts = now
        print(f"[vl_freeze] market={pos.market_id[:10]}… "
              f"reason=drop peak={pos.vl_peak_winner_ask:.3f} "
              f"now={winner_ask:.3f} drop={drop:.3f}>"
              f"{_BS_VL_DROP_TOLERANCE:.3f} ttr={ttr:.0f}s", flush=True)
        return False, (f"vl_frozen:{pos.vl_freeze_reason}"), \
               loser_side, loser_bid, winner_ask

    # Armed AND not frozen — proceed to phase B/C/D evaluation
    # ────────────────────────────────────────────────────────────────────

    # Tiered: tightest TTR window first (lowest threshold)
    threshold_used = None
    phase = None
    if ttr <= 10.0 and winner_ask >= 0.80:
        threshold_used = 0.80; phase = "D"
    elif ttr <= 30.0 and winner_ask >= 0.85:
        threshold_used = 0.85; phase = "C"
    elif ttr <= 60.0 and winner_ask >= 0.90:
        threshold_used = 0.90; phase = "B"

    if threshold_used is None:
        return False, (f"vl_armed_no_phase:winner_ask={winner_ask:.3f},"
                       f"ttr={ttr:.0f}s,peak={pos.vl_peak_winner_ask:.3f}"), \
               loser_side, loser_bid, winner_ask

    if loser_bid <= 0.0:
        return False, f"loser_bid_zero:{loser_bid:.3f}", \
               loser_side, loser_bid, winner_ask

    reason = (f"verification_late:phase={phase},"
              f"winner_ask={winner_ask:.3f},thr={threshold_used:.2f},"
              f"ttr={ttr:.0f}s,peak={pos.vl_peak_winner_ask:.3f}")
    return True, reason, loser_side, loser_bid, winner_ask


def _bs_close_leg(leg: BothSidesLeg, close_price: float, close_ts: float,
                   reason: str) -> None:
    """Close one leg in place. Computes pnl_usdc using the buy-side fee
    structure shared with the v5.7.0 _close_with_pnl path: pnl = qty *
    close_price - size_usdc. (Fees not modeled in DRY — same as v5.8.1.)
    """
    leg.closed = True
    leg.close_reason = reason
    leg.close_price = close_price
    leg.close_ts = close_ts
    leg.pnl_usdc = leg.qty_shares * close_price - leg.size_usdc


# ─────────────────────────────────────────────────────────────────────
# v6.1.2: resolution cascade helpers — determine winner from any source.
# Returns Optional[bool]: True = YES won, False = NO won, None = unknown.
# Used by _bs_settle_position when WS book is cleared at end_ts+2s.
# ─────────────────────────────────────────────────────────────────────

def _resolve_btc_winner_via_chainlink_for_market(
        end_ts: float, duration_s: int) -> Optional[bool]:
    """v6.1.2: read Chainlink relay BTC price at start_ts and end_ts; return
    True if YES won (price went up over the candle), False if NO won, None
    if the relay doesn't have data within tolerance.

    Reuses the existing chainlink_stream_log infrastructure (already streaming
    via boot()). Tolerance 60s — the relay publishes every 5-10s typically,
    so this should hit on the first try unless the stream had a gap.
    """
    if not _CHAINLINK_AVAILABLE or chainlink_stream_log is None:
        return None
    try:
        symbol = chainlink_stream_log.get_symbol_for_coin("BTC")
        if symbol is None:
            return None
        start_ts = end_ts - duration_s
        start_pt = chainlink_stream_log.get_price_at(symbol, start_ts, tolerance_s=60.0)
        end_pt = chainlink_stream_log.get_price_at(symbol, end_ts, tolerance_s=60.0)
        if start_pt is None or end_pt is None:
            return None
        start_price = start_pt["value"]
        end_price = end_pt["value"]
        if start_price <= 0 or end_price <= 0:
            return None
        # Edge case: BTC moved literally 0.00 cents across the candle.
        # Polymarket's resolution rules treat this as a tie (both sides 0.5
        # in outcomes) but in practice this is essentially never observed
        # at sub-second precision. We return None to defer to Gamma which
        # has the on-chain answer for tie-resolution.
        if end_price == start_price:
            return None
        return end_price > start_price
    except Exception as e:
        print(f"[bs_settle_chainlink] error: {type(e).__name__}: {e}", flush=True)
        return None


def _resolve_btc_winner_via_gamma(
        market_id: str, yes_token_id: str,
        no_token_id: str) -> Optional[bool]:
    """v6.1.2: query Polymarket Gamma API for the on-chain market resolution.
    Returns True/False once Polymarket has marked the market closed AND has
    valid outcomePrices. None until then (Polymarket usually settles within
    60-120s of end_ts).

    Throttling is the caller's responsibility (the network call is ~1s).
    """
    md = _fetch_market_resolution(market_id)
    if md is None:
        return None
    closed = bool(md.get("closed", False))
    if not closed:
        return None
    out_raw = md.get("outcomePrices")
    try:
        prices = json.loads(out_raw) if isinstance(out_raw, str) else out_raw
    except Exception:
        return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    try:
        yes_payout = float(prices[0])
        no_payout = float(prices[1])
    except Exception:
        return None
    # Map outcome prices to YES/NO via outcome labels (v5.8.1 _settle_position
    # uses the same logic). outcomes[0]='Up'/'Yes' → prices[0] is YES payout.
    outcomes_raw = md.get("outcomes")
    try:
        outcomes = (json.loads(outcomes_raw) if isinstance(outcomes_raw, str)
                    else outcomes_raw)
    except Exception:
        outcomes = None
    if isinstance(outcomes, list) and len(outcomes) == 2:
        o0 = (outcomes[0] or "").strip().lower()
        if o0 in ("up", "yes"):
            return yes_payout >= 0.5
        else:
            return no_payout < 0.5
    # Fallback: assume index 0 is YES.
    return yes_payout >= 0.5


def _resolve_btc_winner_via_binance_for_market(
        state: BotState, end_ts: float, duration_s: int,
        tolerance_s: float = 30.0) -> Optional[bool]:
    """v6.1.6: read state.binance_prices deque for BTC at start_ts and end_ts.
    Returns True if YES won (price went up), False if NO won, None if not
    enough data within tolerance.

    Binance trades stream at ~1Hz+ continuously, so unlike the Chainlink
    relay (which only publishes on 0.5% deviation or hourly heartbeat),
    this nearly always has fresh samples within a few seconds of any
    target timestamp. This is the workhorse resolution source post-v6.1.6.

    The deque holds (ts_seconds, price) tuples, where ts is epoch seconds
    (float). Snapshot the deque to avoid race with the Binance WS thread.
    """
    if not state.binance_prices:
        return None
    snapshot = list(state.binance_prices)
    start_ts = end_ts - duration_s

    def closest(target_ts: float) -> Optional[float]:
        best_price = None
        best_dt = float('inf')
        for ts, price in snapshot:
            dt = abs(ts - target_ts)
            if dt < best_dt and dt <= tolerance_s:
                best_dt = dt
                best_price = price
        return best_price

    start_price = closest(start_ts)
    end_price = closest(end_ts)
    if start_price is None or end_price is None:
        return None
    if start_price <= 0 or end_price <= 0:
        return None
    # Tie: same price at both endpoints — defer to a slower oracle for
    # tie-resolution semantics rather than guessing.
    if end_price == start_price:
        return None
    return end_price > start_price


def _is_book_chaotic(yes_ask: float, yes_bid: float,
                       no_ask: float, no_bid: float,
                       tolerance: float = 0.05) -> bool:
    """v6.1.6: detect a chaotic / broken Polymarket book snapshot.

    A healthy binary market book has yes_ask + no_ask ≈ 1.00 (with a small
    spread). When the book is in transition (final seconds, market clearing,
    massive order cancellations), sum_ask can deviate wildly — observed
    values include 0.30, 0.01, 1.25, 1.49 in production logs.

    These chaotic snapshots are unreliable as resolution signals. Returns
    True if the book deviates from the sum_ask = 1.0 invariant by more
    than `tolerance` (default ±5¢).

    Edge case: if both asks are 0 (book completely cleared by Polymarket
    post-resolution), sum_ask=0 — that's chaotic too. Even more chaotic
    when one side is 0 and the other has a value (e.g., sum_ask=0.30).
    """
    sum_ask = yes_ask + no_ask
    return abs(sum_ask - 1.0) > tolerance


def _bs_resolve_via_cascade(state: BotState, pos: BothSidesPosition,
                              now: float) -> Tuple[Optional[bool], str]:
    """v6.1.2: try each resolution source in priority order.
    Returns (yes_won, source) or (None, "none") if all sources failed.

    Priority:
      1. cached WS book (populated 1-3s before end_ts in both_sides_tick)
      2. live WS book (often cleared at end_ts+2s — usually empty)
      3. Chainlink relay (most reliable secondary source)
      4. Polymarket Gamma API (throttled to 30s/market)

    Sources 3 and 4 are the difference between v6.1.2 and earlier — they
    eliminate the "both_zero VOID" branch entirely for BTC up/down markets.

    v6.1.3: order changed — Chainlink moved to position #1. Polymarket
    resolves these markets using Chainlink price feed; cache/live book
    can disagree on photo-finish markets (BTC moved <0.005%) where the
    book signal is essentially 50/50 noise. Chainlink is authoritative.
    Chainlink lookup is in-memory deque scan (<1ms), no perf cost.

    v6.1.6: Production diagnosis (May 2 afternoon, 7 trades) found that
    Chainlink fell through 100% of the time for stable BTC over 5m
    windows (relay publishes on 0.5% deviation OR hourly heartbeat —
    rarely triggered in 5m), and the bot fell back to cache. When the
    cache happened to capture chaotic final-second book whipsaw (sum_ask
    diverging from 1.0 — observed 0.30, 1.49, 1.39 in production), it
    mislabeled the winner. One such mislabel cost -$1.88 (NO should have
    won per Binance, but cached book showed YES at end_ts-3s during
    chaos). Two new defenses:
      A. Binance source — continuous (~1Hz+) sub-second-density BTC
         prices from state.binance_prices, inserted between Chainlink
         and cache. Almost always returns a valid result.
      B. Chaotic cache detection — skip cache/live when the snapshot
         shows sum_ask deviating from 1.0 by > ±0.05 (book is broken).
    """
    # --- Source 1: Chainlink relay (AUTHORITATIVE when available) ---
    # v6.1.3: moved to position #1. Polymarket itself uses Chainlink to
    # resolve these markets, so Chainlink is the single source of truth
    # WHEN it has data. v6.1.6 production data shows it almost never does
    # for 5m BTC windows (no deviation trigger), but if we ever do get
    # a fresh sample at both endpoints, prefer it.
    cl = _resolve_btc_winner_via_chainlink_for_market(pos.end_ts, pos.duration_s)
    if cl is not None:
        return cl, "chainlink"

    # --- Source 2: Binance trade stream (NEW v6.1.6 — continuous data) ---
    # state.binance_prices is a deque populated by the Binance WS thread
    # at trade rate (~1Hz+). Unlike Chainlink, this nearly always has fresh
    # samples within a few seconds of any target timestamp. This is the
    # workhorse resolution source post-v6.1.6.
    bn = _resolve_btc_winner_via_binance_for_market(state, pos.end_ts, pos.duration_s)
    if bn is not None:
        return bn, "binance"

    # --- Source 3: cached WS book (v6.1.2 cache, with v6.1.6 chaos check) ---
    # Only trust the cache if the book is in a healthy state (sum_ask near 1.0).
    # If sum_ask deviates significantly (e.g., 0.30 or 1.49), the book is
    # transitioning / broken — fall through.
    if pos.last_book_ts > 0.0:
        if not _is_book_chaotic(pos.last_yes_ask, pos.last_yes_bid,
                                  pos.last_no_ask, pos.last_no_bid):
            yes_signal = max(pos.last_yes_ask, pos.last_yes_bid)
            no_signal = max(pos.last_no_ask, pos.last_no_bid)
            if yes_signal > 0.0 or no_signal > 0.0:
                return (yes_signal >= no_signal), f"cached@{now - pos.last_book_ts:.1f}s"

    # --- Source 4: live WS book (with v6.1.6 chaos check) ---
    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is not None and no_book is not None:
        ya = float(yes_book.ask); yb = float(yes_book.bid)
        na = float(no_book.ask); nb = float(no_book.bid)
        if not _is_book_chaotic(ya, yb, na, nb):
            yes_signal = max(ya, yb)
            no_signal = max(na, nb)
            if yes_signal > 0.0 or no_signal > 0.0:
                return (yes_signal >= no_signal), "live"

    # --- Source 5: Polymarket Gamma API (throttled, last resort) ---
    if (now - pos.last_gamma_fetch_ts) >= 30.0:
        pos.last_gamma_fetch_ts = now
        gm = _resolve_btc_winner_via_gamma(
            pos.market_id, pos.yes_leg.token_id, pos.no_leg.token_id)
        if gm is not None:
            return gm, "gamma"

    return None, "none"


def _bs_collect_btc_samples(state: BotState, pos: BothSidesPosition,
                              n_samples: int = 30) -> Tuple[Optional[float], List[float]]:
    """v6.1.9: collect BTC trajectory for the trade lifetime, for the
    dashboard sparkline. Snapshots state.binance_prices (deque maxlen=12000,
    ~100min on btcusdt@trade) and downsamples to n_samples evenly-spaced
    points across [pos.entry_ts, close_ts]. Returns (strike, samples) where
    strike is BTC at pos.entry_ts. On insufficient data returns (None, []).
    """
    close_ts = max(pos.yes_leg.close_ts, pos.no_leg.close_ts)
    if close_ts <= pos.entry_ts or not state.binance_prices:
        return None, []
    snapshot = list(state.binance_prices)
    strike = None
    for ts, p in snapshot:
        if ts >= pos.entry_ts:
            strike = p
            break
    if strike is None:
        return None, []
    span = close_ts - pos.entry_ts
    samples: List[float] = []
    snap_idx = 0
    for i in range(n_samples):
        target = pos.entry_ts + span * (i / max(1, n_samples - 1))
        while (snap_idx + 1 < len(snapshot)
               and abs(snapshot[snap_idx + 1][0] - target) < abs(snapshot[snap_idx][0] - target)):
            snap_idx += 1
        samples.append(round(snapshot[snap_idx][1], 2))
    return round(strike, 2), samples


def _bs_record_trade_history(state: BotState, pos: BothSidesPosition,
                              source: str) -> None:
    """v6.1.2: append the resolved both-sides position to bs_trade_history
    for the dashboard. Trims list to last 100 entries to bound memory.

    Outcome derivation:
      - 'WIN' if total_pnl > 0
      - 'LOSS' if total_pnl < 0
      - 'EVEN' if total_pnl == 0
    Sell-loser flag is derived from leg close_reason ('sell_loser' on
    either leg → True). No 'VOID' category — v6.1.2 removes it.

    v6.1.8: derive market_winner from close prices. In Polymarket binary
    BTC markets there is ALWAYS a winner — one side closes at $1.00
    (or whatever the resolution oracle produced) and the other at $0.00.
    "EVEN" outcome at the bot level just means the entry asymmetry
    happened to net to ~$0 (e.g. NO bought at 0.50, won at $1.00, and
    YES bought at 0.51, lost — net = $0.00 to the cent). The market still
    had a real winner, and the dashboard now surfaces that explicitly.
    """
    total_pnl = pos.yes_leg.pnl_usdc + pos.no_leg.pnl_usdc
    had_sell_loser = (pos.yes_leg.close_reason == "sell_loser"
                      or pos.no_leg.close_reason == "sell_loser")
    if total_pnl > 0.0001:
        outcome = "WIN"
    elif total_pnl < -0.0001:
        outcome = "LOSS"
    else:
        outcome = "EVEN"
    # v6.1.8: derive the side that won the underlying market from close
    # prices. Whichever leg closed at the higher price is the bot's
    # recorded winner. "" when the close prices tie (e.g. both at 0,
    # extremely rare — would only occur if both legs were sold-as-loser
    # via independent mechanisms or all resolution sources failed).
    if pos.yes_leg.close_price > pos.no_leg.close_price:
        market_winner = "YES"
    elif pos.no_leg.close_price > pos.yes_leg.close_price:
        market_winner = "NO"
    else:
        market_winner = ""
    entry = {
        "market_id": pos.market_id,
        "market_url": pos.market_url,
        "slug": pos.slug,
        "outcome": outcome,
        "market_winner": market_winner,
        "had_sell_loser": had_sell_loser,
        "yes_entry_ask": round(pos.yes_leg.entry_ask, 4),
        "yes_close_price": round(pos.yes_leg.close_price, 4),
        "yes_pnl": round(pos.yes_leg.pnl_usdc, 4),
        "yes_close_reason": pos.yes_leg.close_reason,
        # v6.1.4: peak bid + when (relative to entry, in seconds)
        "yes_peak_bid": round(pos.yes_leg.peak_bid, 4),
        "yes_peak_bid_at_s": (round(pos.yes_leg.peak_bid_ts - pos.yes_leg.entry_ts, 1)
                                if pos.yes_leg.peak_bid_ts > 0 else 0.0),
        "no_entry_ask": round(pos.no_leg.entry_ask, 4),
        "no_close_price": round(pos.no_leg.close_price, 4),
        "no_pnl": round(pos.no_leg.pnl_usdc, 4),
        "no_close_reason": pos.no_leg.close_reason,
        # v6.1.4: peak bid + when (relative to entry, in seconds)
        "no_peak_bid": round(pos.no_leg.peak_bid, 4),
        "no_peak_bid_at_s": (round(pos.no_leg.peak_bid_ts - pos.no_leg.entry_ts, 1)
                               if pos.no_leg.peak_bid_ts > 0 else 0.0),
        "total_pnl": round(total_pnl, 4),
        "sum_ask_at_entry": round(pos.sum_ask_at_entry, 4),
        "entry_ts": pos.entry_ts,
        "close_ts": max(pos.yes_leg.close_ts, pos.no_leg.close_ts),
        "resolution_source": source,
        "pending_duration_s": (
            round(max(pos.yes_leg.close_ts, pos.no_leg.close_ts) - pos.pending_since, 1)
            if pos.pending_since > 0 else 0.0),
        # v6.2.4: verification_late freeze diagnostics. Always present in record
        # (not gated on strategy mode) so historical CSVs are uniform.
        # Useful for downstream analysis: which markets armed, which froze and why.
        "vl_armed": pos.vl_armed,
        "vl_armed_side": pos.vl_armed_side,
        "vl_peak_winner_ask": round(pos.vl_peak_winner_ask, 4),
        "vl_frozen": pos.vl_frozen,
        "vl_freeze_reason": pos.vl_freeze_reason,
        "vl_freeze_ts": (round(pos.vl_freeze_ts - pos.entry_ts, 1)
                         if pos.vl_freeze_ts > 0 else 0.0),
    }
    # v6.1.9: BTC trajectory for dashboard sparkline (None/[] on insufficient data)
    btc_strike, btc_samples = _bs_collect_btc_samples(state, pos)
    entry["btc_strike"] = btc_strike
    entry["btc_samples"] = btc_samples
    state.bs_trade_history.append(entry)
    if len(state.bs_trade_history) > 100:
        state.bs_trade_history = state.bs_trade_history[-100:]


def _bs_settle_position(state: BotState, pos: BothSidesPosition,
                          now: float) -> None:
    """v6.1.2: settle a both-sides position using the resolution cascade.

    Tries 4 sources in priority order (cache → live → chainlink → gamma).
    If ALL return None, the position is marked PENDING and stays in
    state.both_sides_positions for retry on the next tick.

    There is NO VOID branch. BTC up/down binary markets always resolve
    on Polymarket (Chainlink price comparison) — if our bot can't read
    the result, that's a bot-side data gap, not a market void. We keep
    retrying forever; positions pending >= 600s flag as STUCK on the
    dashboard for user investigation.

    v6.1.1 P&L accounting preserved: only NEWLY closed legs are added
    to bs_pnl_today_usdc (legs sold by sell-loser already added at
    sell time).
    """
    yes_was_closed = pos.yes_leg.closed
    no_was_closed = pos.no_leg.closed

    yes_won, source = _bs_resolve_via_cascade(state, pos, now)

    if yes_won is None:
        # All sources failed. Mark/keep pending; retry next tick. Do NOT
        # delete the position. Do NOT void. Do NOT log $0 P&L.
        if pos.pending_since == 0.0:
            pos.pending_since = now
            print(
                f"[bs_settle] market={pos.market_id[:10]}… PENDING "
                f"(cache={'yes' if pos.last_book_ts > 0 else 'no'}, "
                f"live=cleared, chainlink=miss, gamma=miss); "
                f"will retry until a source returns",
                flush=True,
            )
        pos.pending_attempts += 1
        # Throttled progress logging: 30s, 2min, 10min, then every 30min.
        elapsed = now - pos.pending_since
        log_now = False
        if pos.last_pending_log_ts == 0.0:
            # First retry attempt, no logging yet
            pass
        if elapsed >= 30 and pos.last_pending_log_ts < pos.pending_since + 30:
            log_now = True
        elif elapsed >= 120 and pos.last_pending_log_ts < pos.pending_since + 120:
            log_now = True
        elif elapsed >= 600 and pos.last_pending_log_ts < pos.pending_since + 600:
            log_now = True
        elif elapsed >= 1800 and (now - pos.last_pending_log_ts) >= 1800:
            log_now = True
        if log_now:
            tag = "STUCK" if elapsed >= 600 else "PENDING"
            print(
                f"[bs_settle] market={pos.market_id[:10]}… {tag} "
                f"elapsed={elapsed:.0f}s attempts={pos.pending_attempts}",
                flush=True,
            )
            pos.last_pending_log_ts = now
        return

    # We have a winner. Settle both legs (skip already-closed ones).
    if not pos.yes_leg.closed:
        if yes_won:
            _bs_close_leg(pos.yes_leg, 1.0, now, "resolved_win")
            _bs_log_trade_event(state, "RESOLVE_WIN", pos, pos.yes_leg,
                                 note=f"source={source}")
        else:
            _bs_close_leg(pos.yes_leg, 0.0, now, "resolved_loss")
            _bs_log_trade_event(state, "RESOLVE_LOSS", pos, pos.yes_leg,
                                 note=f"source={source}")
    if not pos.no_leg.closed:
        if not yes_won:
            _bs_close_leg(pos.no_leg, 1.0, now, "resolved_win")
            _bs_log_trade_event(state, "RESOLVE_WIN", pos, pos.no_leg,
                                 note=f"source={source}")
        else:
            _bs_close_leg(pos.no_leg, 0.0, now, "resolved_loss")
            _bs_log_trade_event(state, "RESOLVE_LOSS", pos, pos.no_leg,
                                 note=f"source={source}")

    # v6.1.1: only add NEWLY-closed legs to running counter.
    new_pnl = 0.0
    if not yes_was_closed:
        new_pnl += pos.yes_leg.pnl_usdc
    if not no_was_closed:
        new_pnl += pos.no_leg.pnl_usdc
    state.bs_pnl_today_usdc += new_pnl
    state.bs_total_resolved += 1

    pos_pnl_total = pos.yes_leg.pnl_usdc + pos.no_leg.pnl_usdc

    pending_tag = ""
    if pos.pending_since > 0:
        pending_tag = f" [resolved-from-pending after {now - pos.pending_since:.0f}s]"

    print(
        f"[bs_settle] market={pos.market_id[:10]}… "
        f"YES_pnl={pos.yes_leg.pnl_usdc:+.4f} NO_pnl={pos.no_leg.pnl_usdc:+.4f} "
        f"TOTAL={pos_pnl_total:+.4f} "
        f"(new_pnl_added={new_pnl:+.4f}; already_counted="
        f"{'YES' if yes_was_closed else ''}"
        f"{'+NO' if (yes_was_closed and no_was_closed) else ('NO' if no_was_closed else '')}"
        f"{'none' if not (yes_was_closed or no_was_closed) else ''}) "
        f"source={source}{pending_tag}",
        flush=True,
    )

    # v6.1.2: append to dashboard rolling history before deleting.
    _bs_record_trade_history(state, pos, source)
    del state.both_sides_positions[pos.market_id]


def both_sides_tick(state: BotState) -> None:
    """Called from main_loop. No-op when STRATEGY_MODE=lag_signal.
    Otherwise: (a) attempt to enter both-sides on every 5m market in
    the lead-time window we haven't entered before, and (b) evaluate
    sell-loser preconditions on every open both-sides position.

    v6.3.0: When _BS_STRATEGY == 'bss_entry', the entry path is REPLACED
    by the BSS state-machine evaluator (which lazily creates positions
    only when both legs sustain), and the sell-loser pass is suppressed
    (BSS holds both legs to resolution; existing resolution flow handles
    the payout).
    """
    if not _BS_ACTIVE:
        return
    now = time.time()

    # v6.3.2: BSS_ENTRY MODE — evaluation now runs in dedicated fast
    # thread (bss_fast_tick_thread, 20Hz). main_loop's 1Hz pass for
    # BSS would just duplicate work. Suppress entry + VL passes here.
    if _BS_STRATEGY == "bss_entry":
        return  # BSS handled by fast thread; nothing else to do

    # ── ENTRY PASS ──
    # Iterate over a snapshot so the discovery thread can mutate the dict
    for mdm in list(state.bs_5m_in_window.values()):
        try:
            should_enter, reason, yes_ask, no_ask, sum_ask = \
                _bs_should_enter(state, mdm, now)
            if not should_enter:
                # Most reasons aren't worth logging every tick (would spam).
                # Only log on first encounter of a new market_id.
                continue
            _bs_place_entry(state, mdm, yes_ask, no_ask, sum_ask)
        except Exception as e:
            print(f"[bs_entry] error on market={mdm.market.condition_id[:10]}: "
                  f"{type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

    # ── SELL-LOSER PASS ──
    for mid in list(state.both_sides_positions.keys()):
        pos = state.both_sides_positions.get(mid)
        if pos is None:
            continue
        # v6.1.2: update last-known live book cache. Polymarket clears WS
        # books within 1-2s of end_ts, so this snapshot is what settle will
        # use to determine the winner. We only update before end_ts.
        if now < pos.end_ts:
            yb = state.poly_books.get(pos.yes_leg.token_id)
            nb = state.poly_books.get(pos.no_leg.token_id)
            if yb is not None and nb is not None:
                # Skip stale ticks (don't overwrite a fresh cache with a
                # stale book that just happens to still be in the dict).
                book_age = max(now - yb.last_update_ts, now - nb.last_update_ts)
                if book_age <= 30.0:
                    pos.last_yes_ask = float(yb.ask)
                    pos.last_yes_bid = float(yb.bid)
                    pos.last_no_ask = float(nb.ask)
                    pos.last_no_bid = float(nb.bid)
                    pos.last_book_ts = now
                    # v6.1.4: track peak bid per leg for diagnostic. Only
                    # update peak for legs that are still open — once a leg
                    # is closed (sell_loser fired), its peak is locked in.
                    # Bid is what we'd sell at, so this captures the highest
                    # exit price each side reached during its lifetime.
                    yes_bid_now = float(yb.bid)
                    no_bid_now = float(nb.bid)
                    if not pos.yes_leg.closed and yes_bid_now > pos.yes_leg.peak_bid:
                        pos.yes_leg.peak_bid = yes_bid_now
                        pos.yes_leg.peak_bid_ts = now
                    if not pos.no_leg.closed and no_bid_now > pos.no_leg.peak_bid:
                        pos.no_leg.peak_bid = no_bid_now
                        pos.no_leg.peak_bid_ts = now
                    # SPARKLINE: take a (t_rel, yes_bid, no_bid) sample at
                    # most once per ~3s. Capped at 120 entries (~6min of
                    # coverage, more than enough for a 5min market).
                    if (now - pos.last_live_sample_ts) >= 3.0:
                        t_rel = now - pos.entry_ts
                        pos.live_bid_history.append(
                            (round(t_rel, 1), yes_bid_now, no_bid_now))
                        if len(pos.live_bid_history) > 120:
                            pos.live_bid_history.pop(0)
                        pos.last_live_sample_ts = now
        try:
            # v6.2.2: BS_STRATEGY selects which sell-loser logic is active.
            # In "verification_late" mode, ALL v6.2.1 paths are bypassed and
            # only the pure BTC-tiered Phase B/C/D logic fires.
            if _BS_STRATEGY == "verification_late":
                vl_fire, vl_reason, vl_loser_side, vl_loser_bid, vl_winner_ask = \
                    _bs_evaluate_verification_late(state, pos, now)
                pos.sell_loser_status = vl_reason
                if not vl_fire:
                    continue
                if pos.yes_leg.closed or pos.no_leg.closed:
                    continue  # idempotency guard
                should_sell = True
                reason = vl_reason
                loser_side = vl_loser_side
                loser_bid = vl_loser_bid
                winner_ask = vl_winner_ask
                fire_source = "verification_late"
                pos.identified_loser_side = loser_side
                # Skip to fire path (preserves diag/logging structure below)
                diag = _bs_compute_sell_loser_diagnostics(state, pos, now)
                diag = f"src={fire_source},{diag}"
                if loser_side == "YES" and not pos.yes_leg.closed:
                    _bs_close_leg(pos.yes_leg, loser_bid, now, "sell_loser")
                    state.bs_total_sold_loser += 1
                    state.bs_pnl_today_usdc += pos.yes_leg.pnl_usdc
                    # SPARKLINE: stamp sold-side metadata for dashboard marker
                    pos.sold_at_ts = now
                    pos.sold_side = "YES"
                    pos.sold_price = float(loser_bid)
                    pos.sold_ttr_s = float(pos.end_ts - now)
                    pos.sold_reason = fire_source
                    _bs_log_trade_event(state, "SELL_LOSER_DRY", pos, pos.yes_leg,
                        note=f"winner_ask={winner_ask:.3f},loser_bid={loser_bid:.3f},{diag}")
                    print(f"[bs_sell] market={pos.market_id[:10]}… loser=YES "
                          f"sold@{loser_bid:.3f} pnl={pos.yes_leg.pnl_usdc:+.4f} "
                          f"src={fire_source} TTR={pos.end_ts - now:.0f}s", flush=True)
                elif loser_side == "NO" and not pos.no_leg.closed:
                    _bs_close_leg(pos.no_leg, loser_bid, now, "sell_loser")
                    state.bs_total_sold_loser += 1
                    state.bs_pnl_today_usdc += pos.no_leg.pnl_usdc
                    # SPARKLINE: stamp sold-side metadata for dashboard marker
                    pos.sold_at_ts = now
                    pos.sold_side = "NO"
                    pos.sold_price = float(loser_bid)
                    pos.sold_ttr_s = float(pos.end_ts - now)
                    pos.sold_reason = fire_source
                    _bs_log_trade_event(state, "SELL_LOSER_DRY", pos, pos.no_leg,
                        note=f"winner_ask={winner_ask:.3f},loser_bid={loser_bid:.3f},{diag}")
                    print(f"[bs_sell] market={pos.market_id[:10]}… loser=NO "
                          f"sold@{loser_bid:.3f} pnl={pos.no_leg.pnl_usdc:+.4f} "
                          f"src={fire_source} TTR={pos.end_ts - now:.0f}s", flush=True)
                continue  # done with this market

            # Default v621 strategy below — full v6.2.1 stack
            should_sell, reason, loser_side, loser_bid, winner_ask = \
                _bs_evaluate_sell_loser(state, pos, now)
            pos.sell_loser_status = reason
            fire_source = "prod"
            # v6.2.0: BTC late-fallback. If PROD didn't fire AND neither leg
            # is closed yet, check the BTC-fundamentals fallback. Catches
            # held-both markets with sharp final-minute BTC moves that the
            # book never reflected at the 0.93 threshold.
            if (not _BS_TIER_ENABLED
                    and not should_sell
                    and not pos.yes_leg.closed
                    and not pos.no_leg.closed):
                btc_fire, btc_reason, btc_loser_side, btc_loser_bid, btc_winner_ask = \
                    _bs_evaluate_btc_late_fallback(state, pos, now)
                if btc_fire:
                    should_sell = True
                    reason = btc_reason
                    loser_side = btc_loser_side
                    loser_bid = btc_loser_bid
                    winner_ask = btc_winner_ask
                    fire_source = "btc_late"
                    pos.sell_loser_status = reason
                    pos.identified_loser_side = loser_side
            # v6.2.1: Late-conviction override. If neither PROD nor BTC late-
            # fallback fired AND neither leg is closed, check the late-
            # conviction path: TTR≤5s + winner_ask≥0.98 + |BTC Δ|≥$10. This
            # bypasses the standard $30 BTC guard at very late TTR with
            # overwhelming book conviction. Captures held-both markets where
            # main guard is too conservative.
            if (not _BS_TIER_ENABLED
                    and not should_sell
                    and not pos.yes_leg.closed
                    and not pos.no_leg.closed):
                lc_fire, lc_reason, lc_loser_side, lc_loser_bid, lc_winner_ask = \
                    _bs_evaluate_late_conviction(state, pos, now)
                if lc_fire:
                    should_sell = True
                    reason = lc_reason
                    loser_side = lc_loser_side
                    loser_bid = lc_loser_bid
                    winner_ask = lc_winner_ask
                    fire_source = "late_conv"
                    pos.sell_loser_status = reason
                    pos.identified_loser_side = loser_side
            if not should_sell:
                continue
            # IDEMPOTENCY GUARD: once either leg is sold (sell-loser already
            # fired earlier in this position's lifetime), do NOT fire again.
            # The strategy is: sell ONE loser, keep ONE winner to collect $1
            # at settlement. Firing twice closes the kept winner at a bad
            # price and breaks the strategy. Bug observed 2026-06-02 on
            # market 0xb01264ce: NO sold @ 0.090 TTR=75s, then YES sold @
            # 0.020 TTR=3s, both legs exited at bad prices, net -$1.78.
            if pos.yes_leg.closed or pos.no_leg.closed:
                continue
            # Fire — close the loser leg at its current bid
            # v6.1.7: compute richer diagnostics ONCE per fire, used in both
            # YES-loser and NO-loser branches below.
            diag = _bs_compute_sell_loser_diagnostics(state, pos, now)
            diag = f"src={fire_source},{diag}"
            if loser_side == "YES" and not pos.yes_leg.closed:
                _bs_close_leg(pos.yes_leg, loser_bid, now, "sell_loser")
                state.bs_total_sold_loser += 1
                state.bs_pnl_today_usdc += pos.yes_leg.pnl_usdc
                # SPARKLINE: stamp sold-side metadata for dashboard marker
                pos.sold_at_ts = now
                pos.sold_side = "YES"
                pos.sold_price = float(loser_bid)
                pos.sold_ttr_s = float(pos.end_ts - now)
                pos.sold_reason = fire_source
                _bs_log_trade_event(
                    state, "SELL_LOSER_DRY", pos, pos.yes_leg,
                    note=f"winner_ask={winner_ask:.3f},loser_bid={loser_bid:.3f},{diag}",
                )
                print(
                    f"[bs_sell] market={pos.market_id[:10]}… loser=YES "
                    f"sold@{loser_bid:.3f} pnl={pos.yes_leg.pnl_usdc:+.4f} "
                    f"winner_ask={winner_ask:.3f} TTR={pos.end_ts - now:.0f}s",
                    flush=True,
                )
            elif loser_side == "NO" and not pos.no_leg.closed:
                _bs_close_leg(pos.no_leg, loser_bid, now, "sell_loser")
                state.bs_total_sold_loser += 1
                state.bs_pnl_today_usdc += pos.no_leg.pnl_usdc
                # SPARKLINE: stamp sold-side metadata for dashboard marker
                pos.sold_at_ts = now
                pos.sold_side = "NO"
                pos.sold_price = float(loser_bid)
                pos.sold_ttr_s = float(pos.end_ts - now)
                pos.sold_reason = fire_source
                _bs_log_trade_event(
                    state, "SELL_LOSER_DRY", pos, pos.no_leg,
                    note=f"winner_ask={winner_ask:.3f},loser_bid={loser_bid:.3f},{diag}",
                )
                print(
                    f"[bs_sell] market={pos.market_id[:10]}… loser=NO "
                    f"sold@{loser_bid:.3f} pnl={pos.no_leg.pnl_usdc:+.4f} "
                    f"winner_ask={winner_ask:.3f} TTR={pos.end_ts - now:.0f}s",
                    flush=True,
                )
        except Exception as e:
            print(f"[bs_sell] error on market={mid[:10]}: "
                  f"{type(e).__name__}: {e}", flush=True)
            traceback.print_exc()


def _bs_resolution_tick(state: BotState) -> None:
    """Called from main_loop. Settles any both-sides positions whose
    end_ts has passed. Runs only when v6.1.0 is active. Independent from
    the v5.8.1 resolution_thread (which handles single-leg positions)."""
    if not _BS_ACTIVE:
        return
    now = time.time()
    for mid in list(state.both_sides_positions.keys()):
        pos = state.both_sides_positions.get(mid)
        if pos is None:
            continue
        # Settle a couple of seconds AFTER end_ts to give the final book
        # tick a chance to arrive (Chainlink lag + WS jitter).
        if now < pos.end_ts + 2.0:
            continue
        try:
            _bs_settle_position(state, pos, now)
        except Exception as e:
            print(f"[bs_settle] error on market={mid[:10]}: "
                  f"{type(e).__name__}: {e}", flush=True)
            traceback.print_exc()


def pre_market_books_log_tick(state: BotState) -> None:
    """Called from main_loop. Writes a row to pre_market_books_<date>.csv
    for every market in bs_5m_in_window / bs_15m_in_window /
    bs_60m_in_window whose last log was >= LOG_SAMPLE_INTERVAL_S ago.

    Schema:
      ts_ms, duration_label, market_id, slug, end_ts, ttr_s,
      yes_ask, yes_bid, yes_ask_size, yes_bid_size,
      no_ask, no_bid, no_ask_size, no_bid_size,
      sum_ask, sum_bid, btc_price_now, mode, has_position

    Logging is enabled in BOTH lag_signal AND both_sides_btc modes when
    LOG_TO_DISK=true — the only difference is which durations have
    candidate markets to log against. In lag_signal mode the v6.1.0
    discovery thread is idle so the dicts stay empty and this tick is
    effectively a no-op.
    """
    if state.pre_market_books_logger is None:
        return
    if not state.pre_market_books_logger.enabled:
        return
    now = time.time()

    # Latest BTC price for cross-reference (snapshot from binance buffer)
    prices = list(state.binance_prices)
    btc_price_now = prices[-1][1] if prices else 0.0

    for duration_dict in (state.bs_5m_in_window,
                          state.bs_15m_in_window,
                          state.bs_60m_in_window):
        for mdm in duration_dict.values():
            try:
                if (now - mdm.last_logged_ts) < _LOG_SAMPLE_INTERVAL_S:
                    continue
                market = mdm.market
                yes_book = state.poly_books.get(market.yes_token_id)
                no_book = state.poly_books.get(market.no_token_id)
                yes_ask = float(yes_book.ask) if yes_book else 0.0
                yes_bid = float(yes_book.bid) if yes_book else 0.0
                yes_ask_sz = float(yes_book.ask_size) if yes_book else 0.0
                yes_bid_sz = float(yes_book.bid_size) if yes_book else 0.0
                no_ask = float(no_book.ask) if no_book else 0.0
                no_bid = float(no_book.bid) if no_book else 0.0
                no_ask_sz = float(no_book.ask_size) if no_book else 0.0
                no_bid_sz = float(no_book.bid_size) if no_book else 0.0
                # Skip rows where both books are missing — no useful data
                if yes_book is None and no_book is None:
                    mdm.last_logged_ts = now
                    continue
                ttr = market.end_ts - now
                has_position = market.condition_id in state.both_sides_positions
                row = [
                    int(now * 1000),
                    mdm.duration_label,
                    market.condition_id,
                    market.slug,
                    market.market_url,
                    f"{market.end_ts:.0f}",
                    f"{ttr:.1f}",
                    f"{yes_ask:.4f}", f"{yes_bid:.4f}",
                    f"{yes_ask_sz:.2f}", f"{yes_bid_sz:.2f}",
                    f"{no_ask:.4f}", f"{no_bid:.4f}",
                    f"{no_ask_sz:.2f}", f"{no_bid_sz:.2f}",
                    f"{(yes_ask + no_ask):.4f}",
                    f"{(yes_bid + no_bid):.4f}",
                    f"{btc_price_now:.2f}",
                    state.config.mode,
                    "1" if has_position else "0",
                ]
                state.pre_market_books_logger.log(row)
                mdm.last_logged_ts = now
            except Exception as e:
                print(f"[pre_market_books_log] error on "
                      f"{mdm.duration_label}/{mdm.market.condition_id[:10]}: "
                      f"{type(e).__name__}: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# RESOLUTION POLLING + EXIT
# ═══════════════════════════════════════════════════════════════════

GAMMA_MARKET_BY_ID_URL = "https://gamma-api.polymarket.com/markets"


def _fetch_market_resolution(condition_id: str) -> Optional[Dict[str, Any]]:
    import requests
    headers = {
        "User-Agent": "polybot-simple-v1/0.6 (+https://polymarket.com)",
        "Accept": "application/json",
    }
    attempts = [
        {"condition_ids": condition_id, "closed": "true"},
        {"condition_ids": condition_id, "closed": "true", "archived": "true"},
        {"condition_ids": condition_id},
    ]
    for params in attempts:
        try:
            r = requests.get(GAMMA_MARKET_BY_ID_URL, params=params, headers=headers, timeout=8)
        except Exception as e:
            _record_clob_status(0)  # v6.1.3: 0 = network exception
            print(f"[resolution] fetch error for {condition_id[:10]} params={params}: {e}", flush=True)
            continue
        _record_clob_status(r.status_code)  # v6.1.3
        if r.status_code != 200:
            print(f"[resolution] HTTP {r.status_code} for {condition_id[:10]} params={params}", flush=True)
            continue
        try:
            data = r.json()
        except Exception:
            continue
        items = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
        if items:
            return items[0]
    print(f"[resolution] market {condition_id[:10]} not found in any query shape", flush=True)
    return None


def _resolve_via_chainlink(state: BotState, position: Position) -> Optional[bool]:
    if not _CHAINLINK_AVAILABLE or chainlink_stream_log is None:
        return None

    symbol = chainlink_stream_log.get_symbol_for_coin(position.coin)
    if symbol is None:
        print(f"[resolution] chainlink: no relay symbol for coin={position.coin!r}", flush=True)
        return None

    end_ts = position.resolution_ts
    start_ts = end_ts - 300

    start_pt = chainlink_stream_log.get_price_at(symbol, start_ts, tolerance_s=60.0)
    end_pt = chainlink_stream_log.get_price_at(symbol, end_ts, tolerance_s=60.0)

    if start_pt is None or end_pt is None:
        miss = []
        if start_pt is None: miss.append("start")
        if end_pt is None: miss.append("end")
        print(f"[resolution] chainlink: {symbol} no price at {','.join(miss)} "
              f"(start_ts={int(start_ts)} end_ts={int(end_ts)}) — falling back",
              flush=True)
        return None

    start_price = start_pt["value"]
    end_price = end_pt["value"]
    if start_price <= 0 or end_price <= 0:
        return None

    yes_won = end_price > start_price
    print(f"[resolution] chainlink: {symbol} "
          f"start=${start_price:.2f}(age={start_pt['age_s']:.0f}s) "
          f"end=${end_price:.2f}(age={end_pt['age_s']:.0f}s) "
          f"→ YES_WON={yes_won}",
          flush=True)
    return yes_won


def _resolve_via_binance(state: BotState, position: Position) -> Optional[bool]:
    end_ts = position.resolution_ts
    start_ts = end_ts - 300

    # v5.5.24-fix: snapshot deque before iterating
    prices = list(state.binance_prices)
    if not prices:
        return None

    def closest_price(target_ts):
        best = None
        best_diff = float("inf")
        for ts, price in prices:
            d = abs(ts - target_ts)
            if d < best_diff:
                best_diff = d
                best = (ts, price)
        return best, best_diff

    start_pt, start_diff = closest_price(start_ts)
    end_pt, end_diff = closest_price(end_ts)

    if start_pt is None or end_pt is None:
        return None
    if start_diff > 30 or end_diff > 30:
        print(f"[resolution] binance fallback: anchor too far off "
              f"(start_diff={start_diff:.1f}s end_diff={end_diff:.1f}s), inconclusive", flush=True)
        return None

    start_price = start_pt[1]
    end_price = end_pt[1]
    yes_won = end_price > start_price
    print(f"[resolution] binance fallback: start=${start_price:.2f}@{start_pt[0]:.0f} "
          f"end=${end_price:.2f}@{end_pt[0]:.0f} → YES_WON={yes_won}", flush=True)
    return yes_won


def resolution_thread(state: BotState) -> None:
    cfg = state.config
    print(f"[resolution] thread started, poll_interval={cfg.resolution_poll_s}s", flush=True)
    last_status_log = 0.0
    poll_count = 0

    while not state.kill_flag:
        try:
            pos = state.open_position
            now = time.time()

            if pos is None:
                if now - last_status_log > 300:
                    print(f"[resolution] idle, no open position (poll #{poll_count})", flush=True)
                    last_status_log = now
            else:
                time_past_resolution = now - pos.resolution_ts

                # v5.5.30: HARD TIMEOUT FIRST.
                # Run the timeout check BEFORE any Gamma / Chainlink / Binance
                # fetch. If those external calls raise (Polymarket down, network
                # flake), the prior code structure caught the exception and the
                # timeout block at the bottom never executed → position could
                # stay stuck for hours. This was observed 2026-04-28 09:51 UTC
                # when Polymarket went down: open position from 09:51:17 was
                # still un-VOIDed 90+ min later despite the 1800s timeout.
                if time_past_resolution > 1800:
                    print(f"[resolution] HARD TIMEOUT {time_past_resolution:.0f}s past "
                          f"resolution; force-voiding {pos.trade_id}", flush=True)
                    try:
                        _close_with_pnl(state, pos, exit_price=pos.entry_price,
                                        win=None, void=True)
                    except Exception as e:
                        print(f"[resolution] hard-timeout close failed: {e}", flush=True)
                        traceback.print_exc()
                elif time_past_resolution >= -1:
                    poll_count += 1
                    print(f"[resolution] checking trade_id={pos.trade_id} "
                          f"market={pos.market_id[:10]} "
                          f"past_resolution={time_past_resolution:.0f}s (poll #{poll_count})",
                          flush=True)
                    md = _fetch_market_resolution(pos.market_id)
                    settled = False
                    if md is not None:
                        before = state.open_position
                        _settle_position(state, pos, md)
                        settled = state.open_position is None and before is not None

                    if not settled and state.open_position is not None and time_past_resolution > 60:
                        yes_won = _resolve_via_chainlink(state, pos)
                        source = "chainlink"

                        if yes_won is None:
                            print(f"[resolution] gamma+chainlink failed after "
                                  f"{time_past_resolution:.0f}s; trying binance fallback",
                                  flush=True)
                            yes_won = _resolve_via_binance(state, pos)
                            source = "binance"

                        if yes_won is not None:
                            bet_was_up = pos.direction.upper() == "UP"
                            win = (bet_was_up and yes_won) or ((not bet_was_up) and (not yes_won))
                            payout = 1.0 if win else 0.0
                            qty = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0.0
                            pnl = qty * payout - pos.size_usdc
                            print(f"[resolution] settled via {source}: yes_won={yes_won}, "
                                  f"bet={pos.direction}, win={win}, pnl={pnl:+.4f}",
                                  flush=True)
                            _close_with_pnl(state, pos, exit_price=payout, win=win, void=False, pnl=pnl)
        except Exception as e:
            print(f"[resolution] crash: {e}", flush=True)
            traceback.print_exc()

        slept = 0.0
        while slept < cfg.resolution_poll_s and not state.kill_flag:
            time.sleep(0.5)
            slept += 0.5


def _settle_position(state: BotState, position: Position, market_data: dict) -> None:
    closed = bool(market_data.get("closed", False))
    if not closed:
        return

    out_raw = market_data.get("outcomePrices")
    try:
        prices = json.loads(out_raw) if isinstance(out_raw, str) else out_raw
    except Exception:
        prices = None
    if not isinstance(prices, list) or len(prices) != 2:
        print(f"[resolution] {position.trade_id} closed but outcomes={out_raw!r} — treating as VOID", flush=True)
        _close_with_pnl(state, position, exit_price=position.entry_price, win=None, void=True)
        return

    try:
        yes_payout = float(prices[0])
        no_payout = float(prices[1])
    except Exception:
        print(f"[resolution] {position.trade_id} bad outcome prices: {prices}", flush=True)
        _close_with_pnl(state, position, exit_price=position.entry_price, win=None, void=True)
        return

    market = state.btc_5m_market
    if market is None or position.market_id != market.condition_id:
        token_ids = market_data.get("clobTokenIds")
        outcomes = market_data.get("outcomes")
        try:
            token_ids = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        except Exception:
            token_ids = []
            outcomes = []

        yes_id = no_id = None
        if isinstance(token_ids, list) and isinstance(outcomes, list) and len(token_ids) == 2:
            o0 = (outcomes[0] or "").strip().lower()
            if o0 in ("up", "yes"):
                yes_id, no_id = token_ids[0], token_ids[1]
            else:
                yes_id, no_id = token_ids[1], token_ids[0]

        if position.token_id == yes_id:
            payout = yes_payout
        elif position.token_id == no_id:
            payout = no_payout
        else:
            print(f"[resolution] {position.trade_id} token not in resolved market — VOID", flush=True)
            _close_with_pnl(state, position, exit_price=position.entry_price, win=None, void=True)
            return
    else:
        if position.token_id == market.yes_token_id:
            payout = yes_payout
        else:
            payout = no_payout

    qty = position.size_usdc / position.entry_price if position.entry_price > 0 else 0.0
    proceeds = qty * payout
    pnl = proceeds - position.size_usdc
    win = payout >= 0.5

    _close_with_pnl(state, position, exit_price=payout, win=win, void=False, pnl=pnl)


# ═══════════════════════════════════════════════════════════════════
# v5.7.0: TAKE-PROFIT EARLY EXIT
# ═══════════════════════════════════════════════════════════════════

def take_profit_tick(state: BotState) -> None:
    """v5.7.0+v5.8.0: check whether the open position should be closed early
    via take-profit (TP) or stop-loss (SL). Called once per main_loop tick.

    No-op in any of these cases (all preserve existing behavior):
      - both _TP_THRESHOLD <= 0 AND _STOP_LOSS_THRESHOLD <= 0  (both off)
      - state.open_position is None     (nothing to exit)
      - state.mode != "dry"             (LIVE early-exit not implemented)
      - book is missing or has bid<=0   (no quotes — reset both counters)
      - book stale (age > 5s)           (don't trust bid; reset counters)

    Take-profit semantics: bid >= entry+TP_THRESHOLD for >= TP_PERSIST_S
    seconds → exit at current bid.
    Stop-loss semantics: bid <= STOP_LOSS_THRESHOLD (ABSOLUTE FLOOR) for
    >= STOP_LOSS_PERSIST_S seconds → exit at current bid.

    If both conditions are met simultaneously (mathematically possible only
    if TP threshold is below SL threshold which would be nonsensical and
    is rejected by env range guards), TP is checked first and short-circuits.

    Recorded in trades_logger via _close_with_pnl with `,tp_exit:<thresh>`
    or `,sl_exit:<thresh>` appended to existing notes — no schema change.
    """
    if _TP_THRESHOLD <= 0 and _STOP_LOSS_THRESHOLD <= 0 and _SL_LATE_MODE == "":
        return  # all three exit features disabled — fast path
    # v6.1.0: in both_sides_btc mode, the v5.8.1 single-leg TP/SL exit path
    # is not used — both-sides positions are managed by both_sides_tick
    # (entry + sell-loser) and _bs_resolution_tick (settle). We return
    # early to ensure no v5.8.1 logic ever touches a v6.1.0 position
    # (which it couldn't anyway — open_position is always None in v6.1.0
    # since place_entry is gated upstream — but defense-in-depth).
    if _BS_ACTIVE:
        return
    pos = state.open_position
    if pos is None:
        return
    if state.mode != "dry":
        # LIVE TP/SL/SL_LATE would require sell-order placement (signed FAK
        # against the held token's bid); not implemented in v5.8.1.
        return

    book = state.poly_books.get(pos.token_id)
    if book is None or book.bid <= 0:
        pos.tp_consecutive_ticks = 0
        pos.sl_consecutive_ticks = 0
        pos.sl_late_consecutive_ticks = 0
        return

    now = time.time()
    age = now - book.last_update_ts if book.last_update_ts else float("inf")
    if age > 5.0:
        pos.tp_consecutive_ticks = 0
        pos.sl_consecutive_ticks = 0
        pos.sl_late_consecutive_ticks = 0
        return

    # Track peak (for TRAILING_DROP hook; harmless when disabled).
    if book.bid > pos.peak_mark:
        pos.peak_mark = book.bid

    # ─── Take-profit check ───────────────────────────────────────
    if _TP_THRESHOLD > 0:
        target = min(pos.entry_price + _TP_THRESHOLD, 0.99)
        if book.bid >= target:
            pos.tp_consecutive_ticks += 1
        else:
            pos.tp_consecutive_ticks = 0

        persist_ticks = int(_TP_PERSIST_S)
        if pos.tp_consecutive_ticks >= persist_ticks:
            exit_price = book.bid
            qty = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0.0
            pnl = qty * exit_price - pos.size_usdc
            win = pnl > 0
            held_s = now - pos.entry_ts
            print(
                f"[trade] TAKE-PROFIT trade_id={pos.trade_id} {pos.direction} "
                f"entry={pos.entry_price:.3f} exit={exit_price:.3f} "
                f"pnl={pnl:+.4f} held={held_s:.0f}s "
                f"ticks_at_target={pos.tp_consecutive_ticks} "
                f"peak_mark={pos.peak_mark:.3f}",
                flush=True,
            )
            _close_with_pnl(
                state, pos,
                exit_price=exit_price,
                win=win,
                void=False,
                pnl=pnl,
                extra_notes=f",tp_exit:{_TP_THRESHOLD:.2f}",
            )
            return  # exited; don't also check SL

    # ─── Stop-loss check (v5.8.0) ────────────────────────────────
    # IMPORTANT: SL is gated by minimum entry price (_SL_MIN_ENTRY, default
    # $0.30). Backtest showed an unconditional absolute-floor SL@$0.10
    # destroys $114 of TP profit by cutting low-entry trades during normal
    # volatility (every trade's bid touches near-zero at some point). The
    # entry-price gate fires SL only when the trade entered above the floor
    # — i.e., when reaching the floor represents a genuine catastrophic
    # adverse move, not just normal noise on a low-priced lottery ticket.
    if _STOP_LOSS_THRESHOLD > 0 and pos.entry_price >= _SL_MIN_ENTRY:
        # Absolute floor: trigger when bid drops to or below the threshold.
        if book.bid <= _STOP_LOSS_THRESHOLD:
            pos.sl_consecutive_ticks += 1
        else:
            pos.sl_consecutive_ticks = 0

        sl_persist_ticks = int(_SL_PERSIST_S)
        if pos.sl_consecutive_ticks >= sl_persist_ticks:
            exit_price = book.bid
            qty = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0.0
            pnl = qty * exit_price - pos.size_usdc
            win = pnl > 0  # SL exits are usually losses but tag honestly
            held_s = now - pos.entry_ts
            print(
                f"[trade] STOP-LOSS trade_id={pos.trade_id} {pos.direction} "
                f"entry={pos.entry_price:.3f} exit={exit_price:.3f} "
                f"pnl={pnl:+.4f} held={held_s:.0f}s "
                f"ticks_at_floor={pos.sl_consecutive_ticks} "
                f"peak_mark={pos.peak_mark:.3f}",
                flush=True,
            )
            _close_with_pnl(
                state, pos,
                exit_price=exit_price,
                win=win,
                void=False,
                pnl=pnl,
                extra_notes=f",sl_exit:{_STOP_LOSS_THRESHOLD:.2f}",
            )
            return

    # ─── Late-stage SL check (v5.8.1) ────────────────────────────
    # Two modes: "pct" (bid <= entry × pct) or "abs" (bid <= floor).
    # Both require time_remaining_s <= window AND condition holds for
    # persist consecutive ticks. Distinct counter from sl_consecutive_ticks.
    if _SL_LATE_MODE in ("pct", "abs"):
        # Compute time remaining (resolution_ts is in epoch seconds)
        time_remaining_s = pos.resolution_ts - now
        in_window = time_remaining_s <= _SL_LATE_WINDOW_S and time_remaining_s > 0
        if not in_window:
            pos.sl_late_consecutive_ticks = 0
        else:
            if _SL_LATE_MODE == "pct":
                trigger_floor = pos.entry_price * _SL_LATE_PCT
                marker = f",sl_late_pct:{_SL_LATE_PCT:.2f}"
            else:  # "abs"
                trigger_floor = _SL_LATE_FLOOR
                marker = f",sl_late_abs:{_SL_LATE_FLOOR:.2f}"
            if book.bid <= trigger_floor:
                pos.sl_late_consecutive_ticks += 1
            else:
                pos.sl_late_consecutive_ticks = 0
            late_persist_ticks = int(_SL_LATE_PERSIST_S)
            if pos.sl_late_consecutive_ticks >= late_persist_ticks:
                exit_price = book.bid
                qty = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0.0
                pnl = qty * exit_price - pos.size_usdc
                win = pnl > 0
                held_s = now - pos.entry_ts
                print(
                    f"[trade] LATE-SL ({_SL_LATE_MODE}) trade_id={pos.trade_id} "
                    f"{pos.direction} entry={pos.entry_price:.3f} "
                    f"exit={exit_price:.3f} pnl={pnl:+.4f} held={held_s:.0f}s "
                    f"time_remaining={time_remaining_s:.0f}s "
                    f"trigger_floor={trigger_floor:.3f} "
                    f"ticks_at_floor={pos.sl_late_consecutive_ticks}",
                    flush=True,
                )
                _close_with_pnl(
                    state, pos,
                    exit_price=exit_price,
                    win=win,
                    void=False,
                    pnl=pnl,
                    extra_notes=marker,
                )
                return


def _close_with_pnl(state: BotState, position: Position, exit_price: float,
                    win: Optional[bool], void: bool, pnl: float = 0.0,
                    extra_notes: str = "") -> None:
    if not void:
        state.pnl_today_usdc += pnl
        if win is True:
            state.trades_won += 1
        elif win is False:
            state.trades_lost += 1

    # v5.7.0/v5.8.0/v5.8.1: classify exit and (where determinable) the market's outcome.
    #   exit_type: "TP"      if extra_notes contains the take-profit marker,
    #              "SL"      if it contains the entry-gated stop-loss marker,
    #              "SL_LATE" if it contains the late-stage stop-loss marker (v5.8.1),
    #              "RESOLUTION" otherwise (legacy hold-to-resolution exit).
    notes_str = extra_notes or ""
    is_tp_exit = "tp_exit" in notes_str
    is_sl_exit = "sl_exit" in notes_str
    is_sl_late_exit = "sl_late" in notes_str  # v5.8.1
    if is_tp_exit:
        exit_type = "TP"
    elif is_sl_late_exit:
        exit_type = "SL_LATE"
    elif is_sl_exit:
        exit_type = "SL"
    else:
        exit_type = "RESOLUTION"
    if void:
        resolution: Optional[str] = "VOID"
    elif is_tp_exit or is_sl_exit or is_sl_late_exit:
        resolution = None  # unknown — bot exited before market settled
    else:
        # Resolution exit: bot bet UP and won → market resolved UP, etc.
        if position.direction == "UP":
            resolution = "UP" if win else "DOWN"
        elif position.direction == "DOWN":
            resolution = "DOWN" if win else "UP"
        else:
            resolution = None

    # v5.8.0: register this market_id as already-exited so re-entry blocker
    # (in compute_strategy_decision) prevents the bot from buying back into
    # the same market_id this session. Done before logger writes, so the
    # state is consistent the moment _close_with_pnl returns.
    state.exited_market_ids.add(position.market_id)

    history_entry = {
        "trade_id": position.trade_id,
        "direction": position.direction,
        "entry_price": position.entry_price,
        "exit_price": exit_price,
        "size_usdc": position.size_usdc,
        "pnl_usdc": 0.0 if void else pnl,
        "entry_ts": position.entry_ts,
        "exit_ts": time.time(),
        "win": "VOID" if void else ("YES" if win else "NO"),
        "edge_at_entry": position.edge_at_entry,
        "delta_pct_at_entry": position.delta_pct_at_entry,
        "market_url": position.market_url,
        "exit_type": exit_type,        # v5.7.0
        "resolution": resolution,      # v5.7.0
    }
    state.trade_history.append(history_entry)
    if len(state.trade_history) > 100:
        state.trade_history = state.trade_history[-100:]

    event_label = "VOID" if void else ("WIN" if win else "LOSS")
    if state.trades_logger is not None:
        # v5.7.0: notes field carries extra_notes (e.g. ',tp_exit:0.15') so
        # downstream analysis can distinguish TP exits from resolution
        # exits without changing the CSV schema.
        state.trades_logger.log([
            int(time.time() * 1000),
            f"CLOSE_{event_label}",
            position.trade_id,
            position.direction,
            f"{position.delta_pct_at_entry:+.5f}",
            position.token_id,
            0.0, 0.0, 0.0, 0.0,
            f"{exit_price:.4f}",
            position.size_usdc,
            position.market_id,
            "",
            state.config.mode,
            f"pnl={pnl:+.4f}{extra_notes}",
        ])

    print(
        f"[trade] CLOSE {event_label} trade_id={position.trade_id} "
        f"{position.direction} entry={position.entry_price:.3f} exit={exit_price:.3f} "
        f"pnl={'VOID' if void else f'{pnl:+.4f}'}",
        flush=True,
    )

    state.open_position = None


# ═══════════════════════════════════════════════════════════════════
# v6.2.5: LOG RETENTION (disk-space management)
# ═══════════════════════════════════════════════════════════════════
# When LOG_RETENTION_DAYS > 0, deletes daily-rotated CSVs whose date in
# filename is older than the cutoff. Hard safety: never deletes files
# dated today or yesterday (UTC), even if user sets LOG_RETENTION_DAYS=1.
# Matches the regex used by list_log_files plus rotated variants
# <name>_<YYYY-MM-DD>.vN.csv produced by CsvLogger schema rotation.

_LOG_PURGE_FILENAME_RE = re.compile(
    r"^[a-z0-9_]+_(\d{4})-(\d{2})-(\d{2})(?:\.v\d+)?\.csv$"
)


def _purge_old_logs(log_dir: Path, retention_days: int) -> Tuple[int, int]:
    """One-shot purge pass. Returns (files_deleted, bytes_freed).
    Safe to call when log_dir doesn't exist (returns 0, 0). Refuses to
    delete files whose parsed date is today or yesterday UTC, regardless
    of retention_days, to avoid clobbering live data after a clock skew.
    """
    if retention_days <= 0:
        return 0, 0
    if not log_dir.exists() or not log_dir.is_dir():
        return 0, 0
    today = datetime.now(timezone.utc).date()
    cutoff_ordinal = today.toordinal() - retention_days
    # Floor — never touch files dated today or yesterday no matter what the
    # retention math says. Defensive against clock skew or a misconfigured
    # LOG_RETENTION_DAYS=0 → 1 swap during a hot deploy.
    safety_floor_ordinal = today.toordinal() - 1

    files_deleted = 0
    bytes_freed = 0
    try:
        entries = list(log_dir.iterdir())
    except OSError as e:
        print(f"[purge] cannot list {log_dir}: {e}", flush=True)
        return 0, 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            m = _LOG_PURGE_FILENAME_RE.match(entry.name)
            if m is None:
                continue
            try:
                yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                file_date = datetime(yr, mo, dy, tzinfo=timezone.utc).date()
            except (ValueError, OverflowError):
                continue
            file_ord = file_date.toordinal()
            if file_ord >= cutoff_ordinal:
                continue
            if file_ord >= safety_floor_ordinal:
                # Belt-and-braces: even if retention math says delete,
                # never touch yesterday/today.
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            try:
                entry.unlink()
                files_deleted += 1
                bytes_freed += size
                print(f"[purge] deleted {entry.name} ({size:,} bytes, "
                      f"date={file_date.isoformat()})", flush=True)
            except OSError as e:
                print(f"[purge] failed to delete {entry.name}: {e}", flush=True)
        except Exception as e:
            print(f"[purge] error processing {entry.name}: "
                  f"{type(e).__name__}: {e}", flush=True)
    return files_deleted, bytes_freed


def log_retention_thread(state: BotState) -> None:
    """Daemon thread: runs _purge_old_logs once on entry and every 24h
    thereafter. No-op when LOG_RETENTION_DAYS=0. Sleeps in 60s slices so
    kill_flag is honored within a minute of shutdown."""
    if _LOG_RETENTION_DAYS <= 0:
        print("[purge] LOG_RETENTION_DAYS=0 — retention thread idle",
              flush=True)
        return
    if not state.log_dir:
        print("[purge] no log_dir — retention thread idle", flush=True)
        return
    log_dir = Path(state.log_dir)
    print(f"[purge] thread started; retention={_LOG_RETENTION_DAYS} days; "
          f"log_dir={log_dir}", flush=True)
    while not state.kill_flag:
        try:
            files, bytes_freed = _purge_old_logs(log_dir, _LOG_RETENTION_DAYS)
            if files > 0:
                print(f"[purge] cycle done — {files} files, "
                      f"{bytes_freed:,} bytes freed", flush=True)
            else:
                print(f"[purge] cycle done — nothing to delete", flush=True)
        except Exception as e:
            print(f"[purge] crash: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        # Sleep ~24h in 60s slices for graceful shutdown
        slept = 0.0
        while slept < 86400.0 and not state.kill_flag:
            time.sleep(60.0)
            slept += 60.0


# ═══════════════════════════════════════════════════════════════════
# BOOT
# ═══════════════════════════════════════════════════════════════════

def boot() -> BotState:
    print("[boot] Loading config...", flush=True)
    cfg = load_config()
    print(f"[boot] mode={cfg.mode} data_dir={cfg.data_dir} log_to_disk={cfg.log_to_disk}", flush=True)
    if cfg.validation_mode:
        print("[boot] *** VALIDATION_MODE active — gates loosened, DRY only ***", flush=True)
        print(f"[boot]    delta>={cfg.delta_threshold_pct}%  band=[{cfg.entry_price_min},{cfg.entry_price_max}]  "
              f"edge>={cfg.edge_min}  spread<={cfg.spread_max}  ws_age<={cfg.ws_freshness_s}s", flush=True)

    if _SIGNAL_INVERT:
        print("[boot] *** SIGNAL_INVERT ACTIVE — entry direction will be flipped UP↔DOWN ***", flush=True)
        print("[boot]    set SIGNAL_INVERT=false to disable without redeploy", flush=True)

    print("[boot] Verifying data_dir writable...", flush=True)
    verify_data_dir_writable(cfg.data_dir)
    print("[boot] data_dir: OK", flush=True)

    print("[boot] Initializing CLOB client...", flush=True)
    clob = init_clob_client(cfg)
    print("[boot] CLOB client: OK", flush=True)

    if _BS_TIER_ENABLED:
        print(f"[boot][v6.5.11] *** TIER LADDER ACTIVE *** "
              f"T0(any TTR,≥{_BS_TIER_T0_WINNER:.2f},strict) "
              f"T1(≤{_BS_TIER_T1_TTR:.0f}s,≥{_BS_TIER_T1_WINNER:.2f}) "
              f"T2(≤{_BS_TIER_T2_TTR:.0f}s,≥{_BS_TIER_T2_WINNER:.2f}) "
              f"T3(≤{_BS_TIER_T3_TTR:.0f}s,≥{_BS_TIER_T3_WINNER:.2f}) "
              f"persist={_BS_TIER_PERSIST_S:.0f}s "
              f"swing[{_BS_TIER_SWING_WINDOW_S:.0f}s,"
              f"Δ{_BS_TIER_SWING_DRAWDOWN:.2f}/↑{_BS_TIER_SWING_BOUNCE:.2f}] "
              f"dip[{_BS_TIER_DIP_WINDOW_S:.0f}s,≥{_BS_TIER_DIP_FLOOR:.2f}] "
              f"(BTC fallbacks gated OFF)", flush=True)
    else:
        print("[boot][v6.5.11] tier ladder DISABLED — legacy v621 stack active "
              f"(sell≥{_BS_SELL_THRESH:.2f}, btc_late=${_BS_BTC_LATE_THRESHOLD_USD:.0f})",
              flush=True)

    state = BotState(config=cfg, boot_ts=time.time(), clob_client=clob)

    # v6.2.5: bot-isolated log subdir. When BOT_NAME is set, each bot writes
    # to its own subfolder so two bots sharing infra don't trample each
    # other's daily CSV files. Empty BOT_NAME → legacy {data_dir}/logs path.
    if _BOT_NAME:
        log_dir = Path(cfg.data_dir) / "logs" / _BOT_NAME
        print(f"[boot][v6.2.5] BOT_NAME={_BOT_NAME!r} → log_dir={log_dir}",
              flush=True)
    else:
        log_dir = Path(cfg.data_dir) / "logs"
    state.log_dir = str(log_dir)
    state.binance_logger = CsvLogger(
        log_dir, "binance_prices",
        ["ts_ms", "price", "qty"],
    )
    state.signal_logger = CsvLogger(
        log_dir, "signal_log",
        ["ts_ms", "uptime_s", "binance_price", "binance_age_s", "binance_samples",
         "lookback_s", "live_delta_pct", "signal_status",
         "yes_bid", "yes_ask", "yes_age_s",
         "no_bid", "no_ask", "no_age_s",
         "market_question", "market_ends_in_s",
         "market_open_btc", "delta_from_start_pct",  # v5.5.31: new columns
         "validation_ok", "validation_reason"],
    )
    state.trades_logger = CsvLogger(
        log_dir, "trades",
        ["ts_ms", "event", "trade_id", "direction", "delta_pct",
         "token_id", "ask", "bid", "spread", "edge",
         "fill_price", "size_usdc", "market_id", "market_question", "mode", "notes"],
    )

    # v5.6.0: depth_log header. 51 columns.
    # Order: ts_ms, market_id, slug, yes_bid p1..s5 (10), yes_ask p1..s5 (10),
    #        no_bid p1..s5 (10), no_ask p1..s5 (10), aggregates (8).
    depth_header: List[str] = ["ts_ms", "market_id", "slug"]
    for side_label in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        for i in range(1, DEPTH_LEVELS + 1):
            depth_header.append(f"{side_label}_p{i}")
            depth_header.append(f"{side_label}_s{i}")
    depth_header += [
        "yes_bid_depth_5", "yes_ask_depth_5",
        "no_bid_depth_5", "no_ask_depth_5",
        "yes_imbalance_5", "no_imbalance_5",
        "yes_book_age_s", "no_book_age_s",
    ]
    state.depth_logger = CsvLogger(log_dir, "depth_log", depth_header)

    # v5.6.0: flow_log header. 25 columns.
    flow_header: List[str] = ["ts_ms", "market_id", "slug"]
    for side_label in ("yes", "no"):
        flow_header += [
            f"{side_label}_n_20s",
            f"{side_label}_buy_vol_20s",
            f"{side_label}_sell_vol_20s",
            f"{side_label}_net_20s",
            f"{side_label}_vwap_20s",
            f"{side_label}_n_120s",
            f"{side_label}_buy_vol_120s",
            f"{side_label}_sell_vol_120s",
            f"{side_label}_net_120s",
            f"{side_label}_vwap_120s",
            f"{side_label}_last_fill_ts_ms",
        ]
    state.flow_logger = CsvLogger(log_dir, "flow_log", flow_header)

    # v6.1.0: pre_market_books_<date>.csv — book snapshots for 5m/15m/60m
    # BTC markets. Logged in BOTH lag_signal and both_sides_btc modes
    # whenever LOG_TO_DISK=true; in lag_signal mode the discovery thread
    # is idle so the dicts stay empty and this CSV simply gets no rows.
    # 20 columns (v6.1.2 added market_url).
    pre_market_books_header = [
        "ts_ms", "duration_label", "market_id", "slug", "market_url", "end_ts", "ttr_s",
        "yes_ask", "yes_bid", "yes_ask_size", "yes_bid_size",
        "no_ask", "no_bid", "no_ask_size", "no_bid_size",
        "sum_ask", "sum_bid", "btc_price_now", "mode", "has_position",
    ]
    state.pre_market_books_logger = CsvLogger(
        log_dir, "pre_market_books", pre_market_books_header,
    )

    # v6.1.0: bs_trades_<date>.csv — both-sides entry/exit events.
    # Independent from trades_<date>.csv (which is v5.8.1 single-leg
    # lag-signal trades only). Schema rows are written by
    # _bs_log_trade_event(). 18 columns (v6.1.2 added market_url).
    bs_trades_header = [
        "ts_ms", "event", "market_id", "slug", "market_url", "end_ts",
        "side", "token_id",
        "entry_ask", "entry_bid",
        "size_usdc", "qty_shares",
        "close_price", "close_ts", "pnl_usdc",
        "sum_ask_at_entry", "mode", "notes",
    ]
    state.bs_trades_logger = CsvLogger(
        log_dir, "bs_trades", bs_trades_header,
    )

    if not cfg.log_to_disk:
        state.binance_logger.enabled = False
        state.signal_logger.enabled = False
        state.trades_logger.enabled = False
        state.depth_logger.enabled = False
        state.flow_logger.enabled = False
        state.pre_market_books_logger.enabled = False
        state.bs_trades_logger.enabled = False
        print("[boot] CSV logging DISABLED (LOG_TO_DISK=false)", flush=True)
    else:
        print(f"[boot] CSV logging enabled → {log_dir}", flush=True)
        print(f"[boot]   datasets: binance_prices, signal_log, trades, "
              f"depth_log, flow_log, pre_market_books, bs_trades", flush=True)

    _print_banner(state)
    return state


def _print_banner(state: BotState) -> None:
    cfg = state.config
    bar = "═" * 64
    mode_label = "DRY MODE: no real orders will be placed" if cfg.mode == "dry" \
        else "LIVE MODE: real orders WILL be placed"
    invert_label = "ON  (UP↔DOWN flipped)" if _SIGNAL_INVERT else "off (raw signal)"
    # v5.7.0/v5.8.0: take-profit and stop-loss status lines
    if _TP_THRESHOLD > 0:
        tp_label = f"+${_TP_THRESHOLD:.2f} for {_TP_PERSIST_S:.0f}s consecutive (DRY only)"
    else:
        tp_label = "off (TAKE_PROFIT_THRESHOLD=0)"
    if _STOP_LOSS_THRESHOLD > 0:
        sl_label = (f"floor ${_STOP_LOSS_THRESHOLD:.2f} for {_SL_PERSIST_S:.0f}s "
                    f"consecutive (entry ≥${_SL_MIN_ENTRY:.2f}, DRY only)")
    else:
        sl_label = "off (STOP_LOSS_THRESHOLD=0)"
    # v5.8.1: late-stage SL banner line
    if _SL_LATE_MODE == "pct":
        sl_late_label = (f"pct mode, ≤ entry × {_SL_LATE_PCT:.2f} in last "
                         f"{_SL_LATE_WINDOW_S:.0f}s, persist {_SL_LATE_PERSIST_S:.0f}s")
    elif _SL_LATE_MODE == "abs":
        sl_late_label = (f"abs mode, ≤ ${_SL_LATE_FLOOR:.2f} in last "
                         f"{_SL_LATE_WINDOW_S:.0f}s, persist {_SL_LATE_PERSIST_S:.0f}s")
    else:
        sl_late_label = "off (SL_LATE_MODE empty)"
    reentry_label = "blocked" if _BLOCK_REENTRY else "ALLOWED (BLOCK_REENTRY_AFTER_EXIT=false)"
    # v6.1.0: strategy mode + both-sides parameter banner lines
    if _BS_ACTIVE:
        bs_label = (
            f"both_sides_btc — 5m trade (DRY), 15m+60m log only (BTC-only)"
        )
    else:
        bs_label = "lag_signal (v5.8.1 path; STRATEGY_MODE=lag_signal)"
    bs_params_line = (
        f"lead=[{_BS_LEAD_MIN_S:.0f},{_BS_LEAD_MAX_S:.0f}]s  "
        f"sum_ask≤{_BS_SUM_ASK_MAX:.2f}  "
        f"sell_loser≥{_BS_SELL_THRESH:.2f} ttr_floor={_BS_SELL_TTR_FLOOR_S:.0f}s "
        f"persist={_BS_SELL_PERSIST_S:.0f}s min_loser_bid={_BS_SELL_MIN_BID:.2f}"
    )
    # v6.2.0: BTC guards line. v6.2.1: late-conviction override line.
    btc_late_label = (f"{_BS_BTC_LATE_THRESHOLD_USD:.0f}"
                      if _BS_BTC_LATE_THRESHOLD_USD < 999999.0 else "off")
    bs_btc_line = (
        f"min_btc_delta=${_BS_MIN_BTC_DELTA_USD:.0f}  "
        f"btc_late_thresh=${btc_late_label}@TTR≤60s"
    )
    lc_disabled = (_BS_LATE_CONV_TTR_S <= 0.0
                   or _BS_LATE_CONV_WINNER_THRESHOLD >= 1.0
                   or _BS_LATE_CONV_MIN_BTC_USD >= 999.0)
    if lc_disabled:
        bs_lc_line = "late_conv: off"
    else:
        bs_lc_line = (f"late_conv: winner≥{_BS_LATE_CONV_WINNER_THRESHOLD:.2f} "
                      f"+ TTR≤{_BS_LATE_CONV_TTR_S:.0f}s "
                      f"+ |btcΔ|≥${_BS_LATE_CONV_MIN_BTC_USD:.0f}")
    bs_log_line = (
        f"15m_prefix={_LOG_15M_PREFIX!r}  60m_prefix={_LOG_60M_PREFIX!r}  "
        f"window=[{_LOG_WINDOW_MIN_S:.0f},{_LOG_WINDOW_MAX_S:.0f}]s  "
        f"sample_every={_LOG_SAMPLE_INTERVAL_S:.0f}s"
    )
    lines = [
        bar,
        f"  POLYBOT SIMPLE — v{BOT_VERSION} "
        f"({'BOTH-SIDES + multi-duration logging' if _BS_ACTIVE else 'lag-signal (v5.8.1 path)'}, DRY only)",
        bar,
        f"  Mode:           {cfg.mode.upper()}    ({mode_label})",
        f"  Strategy:       {bs_label}",
        f"  v6.1.0 params:  {bs_params_line}",
        f"  v6.2.0 guards:  {bs_btc_line}",
        f"  v6.2.1 override: {bs_lc_line}",
        f"  v6.2.3 strategy: {_BS_STRATEGY}"
        + ("  ★ THE MONEY LOOSER ★" if _BS_STRATEGY == "verification_late" else ""),
        (f"  v6.2.4 vl_freeze: arm≥{_BS_VL_ARM_THRESHOLD:.2f}  "
         f"drop_tol={_BS_VL_DROP_TOLERANCE:.2f}  "
         f"(freeze=permanent hold-both)"
         if _BS_STRATEGY == "verification_late" else
         f"  v6.2.4 vl_freeze: inert (only active when strategy=verification_late)"),
        f"  v6.2.5 BOT_NAME: {_BOT_NAME!r}" + (
            "  (log subdir + download prefix active)" if _BOT_NAME
            else "  (legacy non-isolated paths)"),
        f"  v6.2.5 skip_end_minutes: " + (
            ",".join(f"{m:02d}" for m in sorted(_SKIP_END_MINUTES))
            if _SKIP_END_MINUTES else "(none — no end-minute filter)"),
        f"  v6.2.5 log_retention: " + (
            f"{_LOG_RETENTION_DAYS} days (purges daily; today + yesterday "
            f"protected)" if _LOG_RETENTION_DAYS > 0 else "off"),
        f"  v6.3.0 bss_entry: " + (
            f"ACTIVE  T_first={_BS_BSS_T_FIRST:.2f} "
            f"sustain={_BS_BSS_SUSTAIN_FIRST_S:.0f}s, "
            f"T_2nd={_BS_BSS_T_SECOND_STRICT:.2f}/{_BS_BSS_T_SECOND_RELAXED:.2f} "
            f"sustain={_BS_BSS_SUSTAIN_SECOND_S:.0f}s, "
            f"relax@{_BS_BSS_RELAX_AT_S:.0f}s abort@{_BS_BSS_ABORT_AT_S:.0f}s  "
            f"★ BOTH-SIDES SEE-SAW ★"
            if _BS_STRATEGY == "bss_entry" else
            "inert (only active when BS_STRATEGY=bss_entry)"),
        f"  v6.3.1 btc_vel_filter: " + (
            f"ON  threshold={_BS_BSS_BTC_VEL_FILTER_PCT:.4f}% "
            f"lookback={_BS_BSS_BTC_VEL_LOOKBACK_S:.0f}s "
            f"(LIVE only, v6.3.6: skipped in pre-market)"
            if (_BS_STRATEGY == "bss_entry" and _BS_BSS_BTC_VEL_FILTER_PCT > 0)
            else "OFF (set BS_BSS_BTC_VEL_FILTER_PCT>0 to enable)"),
        f"  v6.3.2 bss_pre_market: " + (
            f"T_first_pre={_BS_BSS_T_FIRST_PRE:.2f} "
            f"T_2nd_pre={_BS_BSS_T_SECOND_PRE:.2f} "
            f"sustain={_BS_BSS_SUSTAIN_FIRST_PRE_S:.0f}s/{_BS_BSS_SUSTAIN_SECOND_PRE_S:.0f}s "
            f"(no abort during pre-market; switches to live at T=0)"
            if _BS_STRATEGY == "bss_entry"
            else "inert (only active when BS_STRATEGY=bss_entry)"),
        f"  v6.3.2 bss_fast_tick: " + (
            f"{1.0/_BS_BSS_TICK_INTERVAL_S:.0f} Hz "
            f"({_BS_BSS_TICK_INTERVAL_S*1000:.0f}ms tick) "
            f"— dedicated thread, main_loop unaffected"
            if _BS_STRATEGY == "bss_entry"
            else "inert (only active when BS_STRATEGY=bss_entry)"),
        f"  v6.3.3 patience+chart: " + (
            f"relax@{_BS_BSS_RELAX_AT_S:.0f}s abort@{_BS_BSS_ABORT_AT_S:.0f}s "
            f"(was 30/45) · sim shows +$0.19/fire vs −$0.007 · "
            f"dashboard: design-3 chart for active trade"
            if _BS_STRATEGY == "bss_entry"
            else "inert (only active when BS_STRATEGY=bss_entry)"),
        f"  v6.3.7 patient_2nd: " + (
            f"floor={_BS_BSS_T_SECOND_FLOOR:.2f} (fire-immediately) · "
            f"patient_drop={_BS_BSS_OPP_VEL_PATIENT_DROP:.4f} over "
            f"{_BS_BSS_OPP_VEL_LOOKBACK_S:.0f}s "
            f"(wait at strict if still falling)"
            if (_BS_STRATEGY == "bss_entry" and _BS_BSS_OPP_VEL_PATIENT_DROP > 0)
            else "OFF (set BS_BSS_OPP_VEL_PATIENT_DROP>0 to enable)"),
        f"  v6.3.8 sampling:   ALWAYS (was: skipped on stale books) · "
        f"fire-staleness threshold 30s→120s (pre-market quiet tokens are normal)",
        f"  v6.3.9 DIAGNOSTIC: per-market evaluator trace every 30s "
        f"(grep '[bss_diag]' in logs to find why fires aren't happening)",
        f"  v6.3.11 gamma_poll: REST polling every 2.5s on gamma-api/markets "
        f"(replaces v6.3.10 WS refresh; proven scalper3 pattern that went LIVE)",
        f"  v6.3.12 watch_through_live: BSS watch list keeps markets through "
        f"pre-market AND live phases until end_ts (was: dropped at TTR<600s, "
        f"5min BEFORE live window opens — caused zero live-phase fires)",
        f"  v6.1.0 logging: {bs_log_line}",
        bar,
        f"  Signal invert:  {invert_label}",
        f"  Take-profit:    {tp_label}",
        f"  Stop-loss:      {sl_label}",
        f"  Late-SL:        {sl_late_label}",
        f"  Re-entry:       {reentry_label}",
        f"  Proxy wallet:   {cfg.proxy_wallet}",
        f"  Sig type:       {cfg.force_signature_type} (Polymarket native)",
        f"  Position size:  ${cfg.position_size_usdc:.2f} USDC",
        f"  Daily loss cap: ${cfg.daily_loss_limit_usdc:.2f} USDC (enforced LIVE only)",
        f"  Entry band:     {cfg.entry_price_min:.2f} – {cfg.entry_price_max:.2f}  (lag-signal path only)",
        f"  Edge min:       {cfg.edge_min:.2f}    Spread max: {cfg.spread_max:.2f}",
        f"  Signal:         |Δ| ≥ {cfg.delta_threshold_pct:.2f}% over {cfg.lookback_s}s",
        f"  WS freshness:   ≤ {cfg.ws_freshness_s}s    Binance tol: {cfg.binance_tolerance_pct:.2f}%",
        f"  Data dir:       {cfg.data_dir}",
        f"  HTTP port:      {cfg.port}",
        bar,
        f"  {'BOTH-SIDES path active' if _BS_ACTIVE else 'lag-signal path active'}. "
        f"{'VALIDATION_MODE active' if cfg.validation_mode else 'production gates'}.",
        "  Dashboard at public URL. CSV files under <data_dir>/logs/.",
        "  Set KILL=true on Railway to halt without a redeploy.",
        bar,
    ]
    for line in lines:
        print(line, flush=True)
    # v5.7.0/v5.8.0/v5.8.1: explicit warnings if features set with LIVE mode
    # (TP/SL/SL_LATE skipped silently in LIVE — no sell-order placement implemented).
    if (_TP_THRESHOLD > 0 or _STOP_LOSS_THRESHOLD > 0 or _SL_LATE_MODE != "") \
            and cfg.mode == "live":
        print("  *** WARNING: TP / SL / Late-SL set but mode=live — early-exit "
              "paths are DRY only in v5.8.1. All early exits will be SKIPPED ***",
              flush=True)
    if _TRAILING_DROP > 0:
        print(f"  *** NOTE: TRAILING_DROP={_TRAILING_DROP} set, but trailing-stop "
              f"is a RESERVED HOOK in v5.8.0 (not implemented). Ignored. ***",
              flush=True)
    # v6.1.0: hard refusal of LIVE both-sides
    if _BS_ACTIVE and cfg.mode == "live":
        print("  *** WARNING: STRATEGY_MODE=both_sides_btc with MODE=live — "
              "v6.1.0 ships DRY-only for both-sides. _bs_place_entry will refuse "
              "LIVE entries. Reserved for v6.2.x after DRY EV validates. ***",
              flush=True)


# ═══════════════════════════════════════════════════════════════════
# THREAD STARTUP
# ═══════════════════════════════════════════════════════════════════

def start_feed_threads(state: BotState) -> None:
    threads = [
        ("binance_ws", binance_ws_thread),
        ("market_disc", market_discovery_thread),
        ("poly_ws", poly_ws_thread),
        ("http", http_server_thread),
        ("resolution", resolution_thread),
    ]
    # v6.1.0: add both-sides discovery thread when STRATEGY_MODE=both_sides_btc.
    # The thread itself short-circuits on _BS_ACTIVE check, but we only add it
    # when active to keep the thread list visibly minimal in lag_signal mode.
    if _BS_ACTIVE:
        threads.append(("bs_disc", both_sides_discovery_thread))
    # v6.3.2: BSS fast-tick thread (20Hz default). Only spawned when
    # bss_entry strategy is active.
    if _BS_ACTIVE and _BS_STRATEGY == "bss_entry":
        threads.append(("bss_fast", bss_fast_tick_thread))
    # v6.3.11: Gamma REST polling thread. Replaces v6.3.10's ws_refresh hack.
    # Polls Polymarket Gamma /markets endpoint every 2.5s for each BSS-watched
    # market and updates state.poly_books with fresh prices. This is the
    # proven pattern from scalper3 (April 2026) which went live successfully —
    # Polymarket WS drops on Railway and silently degrades for non-active
    # markets, so REST is the reliable path.
    if _BS_ACTIVE and _BS_STRATEGY == "bss_entry":
        threads.append(("bss_gamma_poll", bss_gamma_poll_thread))
    # v6.2.5: log-retention purger thread (no-op when LOG_RETENTION_DAYS=0).
    # Always added — the thread itself early-exits if retention is disabled.
    if _LOG_RETENTION_DAYS > 0:
        threads.append(("log_retention", log_retention_thread))
    for name, target in threads:
        t = threading.Thread(target=target, args=(state,), name=name, daemon=True)
        t.start()
        print(f"[boot] started thread: {name}", flush=True)

    if _CHAINLINK_AVAILABLE and chainlink_stream_log is not None:
        try:
            chainlink_stream_log.start(add_log_fn=lambda msg, lvl="info": print(msg, flush=True))
            print(f"[boot] started chainlink_stream_log (Polymarket RTDS relay)", flush=True)
        except Exception as e:
            print(f"[boot] chainlink_stream_log start failed: {type(e).__name__}: {e}", flush=True)
    else:
        print(f"[boot] chainlink_stream_log not available — Binance-only resolution", flush=True)

    def _kill_check():
        return state.kill_flag

    for logger_name, logger in (("csv_binance", state.binance_logger),
                                 ("csv_signal", state.signal_logger),
                                 ("csv_trades", state.trades_logger),
                                 ("csv_depth", state.depth_logger),    # v5.6.0
                                 ("csv_flow", state.flow_logger),      # v5.6.0
                                 # v6.1.0:
                                 ("csv_pre_market_books", state.pre_market_books_logger),
                                 ("csv_bs_trades", state.bs_trades_logger)):
        if logger is None:
            continue
        t = threading.Thread(
            target=logger.writer_loop, args=(_kill_check,),
            name=logger_name, daemon=True,
        )
        t.start()
        print(f"[boot] started thread: {logger_name} (enabled={logger.enabled})", flush=True)


# ═══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════

def _format_heartbeat(state: BotState) -> str:
    now = time.time()

    # v5.5.24-fix: snapshot for safe iteration
    prices_snapshot = list(state.binance_prices)

    if state.binance_last_msg_ts > 0:
        age_s = now - state.binance_last_msg_ts
        latest_price = prices_snapshot[-1][1] if prices_snapshot else 0.0
        if age_s < 5 and state.binance_ws_connected:
            binance = f"binance=OK({age_s*1000:.0f}ms,${latest_price:,.0f})"
        else:
            binance = f"binance=STALE({age_s:.0f}s)"
    else:
        binance = "binance=DOWN"

    if state.poly_last_msg_ts > 0:
        age_s = now - state.poly_last_msg_ts
        if age_s < 10 and state.poly_ws_connected:
            poly = f"poly_ws=OK({age_s*1000:.0f}ms)"
        else:
            poly = f"poly_ws=STALE({age_s:.0f}s)"
    elif state.poly_ws_connected:
        poly = "poly_ws=CONN(no_msgs)"
    else:
        poly = "poly_ws=DOWN"

    if state.btc_5m_market:
        time_left = state.btc_5m_market.end_ts - now
        q = state.btc_5m_market.question
        if len(q) > 50:
            q = q[:47] + "…"
        market = f"market='{q}' ends_in={time_left:.0f}s"
    else:
        market = "market=NONE"

    books = f"books={len(state.poly_books)}"
    pos = "OPEN" if state.open_position else "NONE"

    if state.live_delta_pct is None:
        sig = f"signal=NONE({state.signal_status_msg})"
    elif state.last_validation_ok is True:
        sig = f"signal=VALID({state.live_delta_pct:+.2f}%)"
    elif state.last_validation_ok is False:
        sig = f"signal=SKIP({state.last_validation_reason})"
    else:
        sig = f"signal=NONE({state.live_delta_pct:+.2f}%<thr)"

    invert_tag = " INVERT" if _SIGNAL_INVERT else ""

    # v6.1.0: when both_sides_btc is active, the "position=" / "trades=" /
    # "pnl=" fields are dominated by both-sides activity, not the v5.8.1
    # single-leg counters. Add a parallel summary line.
    if _BS_ACTIVE:
        bs_open = len(state.both_sides_positions)
        d5 = len(state.bs_5m_in_window)
        d15 = len(state.bs_15m_in_window)
        d60 = len(state.bs_60m_in_window)
        # v6.1.2: count pending positions (none in v6.1.1 — VOID was used)
        bs_pending = sum(1 for p in state.both_sides_positions.values()
                          if p.pending_since > 0)
        bs_summary = (
            f" bs_open={bs_open} disc=[5m:{d5},15m:{d15},60m:{d60}] "
            f"bs_entered={state.bs_total_entered} "
            f"bs_sold={state.bs_total_sold_loser} "
            f"bs_resolved={state.bs_total_resolved} "
            f"bs_pending={bs_pending} "
            f"bs_pnl=${state.bs_pnl_today_usdc:+.2f}"
        )
    else:
        bs_summary = ""

    return (f"[heartbeat] uptime={state.uptime_s:.0f}s mode={state.mode}{invert_tag} "
            f"position={pos} {binance} {poly} {market} {books} {sig} "
            f"trades={state.trades_today}({state.trades_won}W/{state.trades_lost}L) "
            f"pnl=${state.pnl_today_usdc:+.2f} skips={state.skips_today}{bs_summary}")


# ═══════════════════════════════════════════════════════════════════
# v6.3.2: BSS FAST TICK THREAD
# ═══════════════════════════════════════════════════════════════════
# Runs the BSS evaluator at high frequency (default 20Hz / 50ms ticks)
# in its own thread. Main loop stays at 1Hz for everything else.
#
# Only spawned when _BS_STRATEGY == 'bss_entry'. Otherwise inert.
#
# Thread safety:
#   - state.bs_5m_in_window: iterated as list(.values()) snapshot
#   - state.poly_books: read-only here, GIL-safe dict access
#   - state.both_sides_positions: writes are dict assignment (GIL-safe);
#       BSS only writes a fresh entry, never overwrites
#   - mdm.bss_*: written only here. No concurrent writer.
#   - state.bs_trades_logger: queued logger, thread-safe.

def _fetch_market_live_prices(condition_id: str) -> Optional[Dict[str, Any]]:
    """v6.3.11: fetch CURRENT yes/no prices from Polymarket Gamma for a live
    (open) market. Single HTTP request (no closed/archived filter).

    Returns the market dict from Gamma containing outcomePrices and outcomes,
    or None on any failure. Designed to be called frequently (every 2-3s)
    so failures must be silent and cheap.
    """
    import requests
    headers = {
        "User-Agent": "polybot-simple-v1/0.6 (+https://polymarket.com)",
        "Accept": "application/json",
    }
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_ids": condition_id},
            headers=headers, timeout=4,
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    items = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
    if not items:
        return None
    return items[0]


def _gamma_refresh_one_market(state: BotState,
                                mdm: MultiDurationMarket) -> bool:
    """v6.3.11: refresh poly_books for one market using Gamma data.
    Returns True if books were updated, False otherwise. Silent on failure."""
    md = _fetch_market_live_prices(mdm.market.condition_id)
    if md is None:
        return False
    out_raw = md.get("outcomePrices")
    try:
        prices = json.loads(out_raw) if isinstance(out_raw, str) else out_raw
    except Exception:
        return False
    if not isinstance(prices, list) or len(prices) != 2:
        return False
    try:
        # Gamma outcomePrices order matches outcomes order, which is
        # ['Up','Down'] (== ['Yes','No']) for BTC Up/Down markets.
        # The bot's convention: yes_token_id corresponds to 'Up', no_token_id
        # to 'Down', mirroring outcomes[0]/outcomes[1].
        yes_p = float(prices[0])
        no_p = float(prices[1])
    except Exception:
        return False
    if yes_p <= 0 or no_p <= 0 or yes_p >= 1 or no_p >= 1:
        return False
    now = time.time()
    yb = state.poly_books.get(mdm.market.yes_token_id)
    nb = state.poly_books.get(mdm.market.no_token_id)
    # Update existing books if present. Gamma gives mid/last-trade only —
    # we set bid==ask==price, which matches the scalper3 pattern that
    # went live successfully. Fine for threshold-based entry decisions.
    if yb is not None:
        yb.bid = yes_p
        yb.ask = yes_p
        yb.last_update_ts = now
    else:
        # Book doesn't exist yet (token not in WS subscription) — create
        # minimal PolyBook so the BSS evaluator can read prices.
        state.poly_books[mdm.market.yes_token_id] = PolyBook(
            token_id=mdm.market.yes_token_id,
            bid=yes_p, ask=yes_p, bid_size=0.0, ask_size=0.0,
            last_update_ts=now, bid_levels=[], ask_levels=[],
            last_book_snapshot_ts=now,
        )
    if nb is not None:
        nb.bid = no_p
        nb.ask = no_p
        nb.last_update_ts = now
    else:
        state.poly_books[mdm.market.no_token_id] = PolyBook(
            token_id=mdm.market.no_token_id,
            bid=no_p, ask=no_p, bid_size=0.0, ask_size=0.0,
            last_update_ts=now, bid_levels=[], ask_levels=[],
            last_book_snapshot_ts=now,
        )
    return True


def bss_gamma_poll_thread(state: BotState) -> None:
    """v6.3.11: poll Polymarket Gamma REST API for fresh prices on every
    BSS-watched market. Replaces the v6.3.10 ws_refresh hack.

    Background: on May 7 we discovered that Polymarket's WS subscription
    delivers initial book snapshots but only sends ongoing price_change
    events to the most-recently active market. BSS-watched pre-market
    5-min and 15-min markets go silent forever after the initial snapshot.

    The proven workaround (scalper3, April 2026, went live successfully)
    is to poll Gamma's REST /markets endpoint, which always returns the
    latest outcomePrices regardless of WS state. Each market costs one
    HTTP request (~200-500ms). At 3 markets every 2.5s, total load is
    well under 2 RPS to Gamma — orders of magnitude below rate limit.

    DRY trade-off: Gamma returns last-trade-price, not top-of-book bid/ask.
    In thin markets this can lag by a few seconds. Acceptable for sustain-
    based entry triggers (4s sustain) which already smooth over short
    spikes. Future v6.3.12 may add CLOB REST get_order_book for
    bid/ask precision when LIVE mode arrives.
    """
    POLL_INTERVAL_S = 2.5
    print(f"[bss_gamma_poll] v6.3.11 starting "
          f"(interval={POLL_INTERVAL_S:.1f}s, source=gamma-api/markets)",
          flush=True)
    cycle_count = 0
    err_count = 0
    last_log_ts = 0.0
    while not state.kill_flag:
        loop_start = time.time()
        try:
            # Snapshot the watch list (avoid mutation during iteration)
            mdms = list(state.bs_5m_in_window.values())
            updated = 0
            for mdm in mdms:
                try:
                    if _gamma_refresh_one_market(state, mdm):
                        updated += 1
                except Exception as e:
                    err_count += 1
                    if err_count <= 3 or err_count % 50 == 0:
                        print(f"[bss_gamma_poll] error refreshing "
                              f"{mdm.market.condition_id[:10]}…: "
                              f"{type(e).__name__}: {e}", flush=True)
            cycle_count += 1
            now = time.time()
            # Heartbeat every 30s so we can confirm the thread is alive
            if now - last_log_ts >= 30.0:
                print(f"[bss_gamma_poll] cycle={cycle_count} "
                      f"watched={len(mdms)} updated_last_cycle={updated} "
                      f"errs_total={err_count}", flush=True)
                last_log_ts = now
        except Exception as e:
            print(f"[bss_gamma_poll] outer error: "
                  f"{type(e).__name__}: {e}", flush=True)
        # Sleep remainder of interval (account for time spent polling)
        elapsed = time.time() - loop_start
        sleep_s = max(0.1, POLL_INTERVAL_S - elapsed)
        slept = 0.0
        while slept < sleep_s and not state.kill_flag:
            time.sleep(min(0.5, sleep_s - slept))
            slept += 0.5


def bss_fast_tick_thread(state: BotState) -> None:
    """v6.3.2: dedicated BSS evaluator loop. ~20Hz default."""
    interval_s = _BS_BSS_TICK_INTERVAL_S
    print(f"[bss_fast] starting at {1.0/interval_s:.0f} Hz "
          f"(interval={interval_s*1000:.0f}ms)", flush=True)
    last_err_ts = 0.0
    err_count = 0
    while not state.kill_flag:
        now = time.time()
        try:
            for mdm in list(state.bs_5m_in_window.values()):
                try:
                    _bs_evaluate_bss_entry(state, mdm, now)
                except Exception as e:
                    # Suppress per-market errors but count them — don't
                    # let a bad market kill the loop
                    err_count += 1
                    if now - last_err_ts > 5.0:  # rate-limit error log
                        print(f"[bss_fast] eval error market="
                              f"{mdm.market.condition_id[:10]}: "
                              f"{type(e).__name__}: {e} "
                              f"(suppressed errors since last: {err_count})",
                              flush=True)
                        last_err_ts = now
                        err_count = 0
        except Exception as e:
            # Outer try catches things like dict-mutation-during-iteration
            print(f"[bss_fast] OUTER error: {type(e).__name__}: {e}",
                  flush=True)
            traceback.print_exc()
        time.sleep(interval_s)
    print("[bss_fast] thread exiting (kill_flag set)", flush=True)


def main_loop(state: BotState) -> None:
    print("[main_loop] Entering heartbeat loop.", flush=True)
    last_heartbeat = 0.0
    while not state.kill_flag:
        now = time.time()

        try:
            signal_tick(state)
        except Exception as e:
            print(f"[signal] tick error: {e}", flush=True)
            traceback.print_exc()

        # v5.7.0: take-profit check (no-op when TP env vars unset or no position).
        # Wrapped independently so a TP fault never breaks signal/depth/flow.
        try:
            take_profit_tick(state)
        except Exception as e:
            print(f"[take_profit] tick error: {e}", flush=True)
            traceback.print_exc()

        # v6.1.0: both-sides entry + sell-loser tick. No-op when
        # STRATEGY_MODE=lag_signal (default). Wrapped independently so
        # a both-sides fault can never break the v5.8.1 path.
        try:
            both_sides_tick(state)
        except Exception as e:
            print(f"[both_sides] tick error: {e}", flush=True)
            traceback.print_exc()

        # v6.1.0: both-sides resolution settle. No-op when v6.1.0 inactive
        # OR no positions are at/past end_ts.
        try:
            _bs_resolution_tick(state)
        except Exception as e:
            print(f"[bs_resolution] tick error: {e}", flush=True)
            traceback.print_exc()

        # v6.1.0: pre-market books logger. Writes to CSV only when
        # discovery has populated bs_*_in_window dicts (i.e. when
        # v6.1.0 is active). No-op otherwise.
        try:
            pre_market_books_log_tick(state)
        except Exception as e:
            print(f"[pre_market_books] tick error: {e}", flush=True)
            traceback.print_exc()

        # v5.6.0: depth + flow logging ticks. Both wrapped independently so
        # a transient logger fault never breaks the trading-relevant path.
        try:
            _log_depth_tick(state)
        except Exception as e:
            print(f"[depth] tick error: {e}", flush=True)
            traceback.print_exc()

        try:
            _log_flow_tick(state)
        except Exception as e:
            print(f"[flow] tick error: {e}", flush=True)
            traceback.print_exc()

        if now - last_heartbeat >= 30.0:
            print(_format_heartbeat(state), flush=True)
            last_heartbeat = now
        if os.environ.get("KILL", "").strip().lower() == "true":
            print("[main_loop] KILL=true detected. Exiting.", flush=True)
            state.kill_flag = True
            break
        time.sleep(1.0)


# ═══════════════════════════════════════════════════════════════════
# SIGNAL HANDLING
# ═══════════════════════════════════════════════════════════════════

def install_signal_handlers(state: BotState) -> None:
    def _handler(signum, frame):
        print(f"\n[signal] received {signum}, shutting down gracefully...", flush=True)
        state.kill_flag = True
        try:
            if state.poly_ws_handle:
                state.poly_ws_handle.close()
        except Exception:
            pass

    signal_module.signal(signal_module.SIGTERM, _handler)
    signal_module.signal(signal_module.SIGINT, _handler)


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main() -> int:
    try:
        state = boot()
    except RuntimeError as e:
        print(f"\n[boot] FATAL: {e}\n", flush=True, file=sys.stderr)
        return 1

    install_signal_handlers(state)
    start_feed_threads(state)

    try:
        main_loop(state)
    except Exception as e:
        print(f"\n[main_loop] CRASH: {e}\n", flush=True, file=sys.stderr)
        traceback.print_exc()
        return 2

    print("[main] Clean shutdown.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
