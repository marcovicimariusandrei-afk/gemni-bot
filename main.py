"""
main.py — polybot_skuld_v1 (v6.5.6 "Skuld" — LIVE orphan-sell: real CLOB FAK orders for orphan-sell + take-profit rules).
v6.5.1 — applied 2026-05-09.

═══════════════════════════════════════════════════════════════════════
v6.5.1 "SKULD" — price_change ladder tracking + simulator no-liquidity
═══════════════════════════════════════════════════════════════════════

22-hour DRY audit on v6.5.0 found 24 of 484 leg fills (5%) recorded at
prices that did not exist in the book. Root cause traced to a two-bug
interaction in the WS handler + DRY simulator. v6.5.1 fixes both.

BUG 1 (WS price_change handler, line ~2050 in v6.5.0):
  Previous code accepted ANY SELL price_change with `price < book.ask`
  as the new best ask, INCLUDING cancellation events (size=0). This
  poisoned `book.ask` whenever a stale ask level got removed at a price
  far below the current top — the bot's view stuck on the cancelled
  price until the next full book snapshot reset it (could be 5-30s).

  Fix: maintain `book.ask_levels` and `book.bid_levels` as the full
  live ladder. On every price_change:
    - size > 0  → set/replace level at that price
    - size = 0  → remove level at that price
  Then recompute best bid/ask from the ladder. Cancellations cannot
  poison `book.ask` because removing a level just deletes it; the new
  best ask is recomputed from what's actually in the ladder.

  Side benefit: depth_log columns yes_ask_p1, ask_levels[0..4], etc.
  are now ALWAYS current. Previous behavior only refreshed levels on
  full `book` snapshot events (every 5-30s); now they update on every
  price_change tick. Dashboard depth display becomes reliable.

BUG 2 (DRY simulator, _bss_simulate_dry_fill in v6.5.0):
  When `book.ask_size <= 0`, the simulator fell into the
  "we have no size data — assume it fits" branch and recorded a
  successful fill at decision_ask. Combined with bug 1's poisoned
  ask, this manifested as fictional fills on prices with zero
  liquidity behind them.

  Fix: when ask_size <= 0, return outcome="no_liquidity" and let
  the placement code log a BSS_LEG_FOK_FAIL_DRY. Mirrors the LIVE
  semantics where a FAK at a phantom price would be rejected by the
  CLOB. Also added "no_liquidity" to the failure-classification set
  in _bss_place_leg1 and _bss_place_leg2.

EXPECTED IMPACT:
  - Stale fires (24 in 22h on v6.5.0) → 0 in v6.5.1
  - Daily DRY P&L: ~$51/day (with fictional fills inflating wins)
                 → ~$44/day (honest, fictional fills replaced by
                   FOK fails that don't take a position)
  - Win rate on flagged markets: 91.3% (fictional) → resolved
    normally per real outcome distributions
  - depth_log staleness (book_age_s > 5s on 8.9% of rows in v6.5.0)
    → eliminated on price_change-driven updates

EVERYTHING ELSE FROM v6.5.0 CARRIED FORWARD UNCHANGED:
  - Per-leg placement architecture (no abort, ORPHAN_END at end_ts)
  - State machine: WATCH → WAITING_2ND → BOTH (or → ORPHAN_END)
  - Position sizing $1/leg, 2% taker fee model
  - LIVE_BSS_ENABLED gate, BS_BOOK_WALK_ENABLED, all thresholds
  - Resolution cascade (chainlink → binance → cache → gamma)
  - Logging schema (bs_trades columns unchanged)

═══════════════════════════════════════════════════════════════════════

Originally based on v5.8.1 (2026-04-29):

NEW IN v5.8.1: late-stage stop-loss (LATE-SL).

═══════════════════════════════════════════════════════════════════════
v6.5.0 "SKULD" rev 2 — per-leg placement, no abort, real DRY numbers
═══════════════════════════════════════════════════════════════════════

v6.4.0 was broken at an architectural level: both legs committed at
second-leg-decision time using a fictional first-leg price from minutes
ago. The v6.4.0 "DRY realism simulator" then attempted to validate
that fictional price against current book and FOK-failed itself ~5,400
times in 5 hours (May 8 morning data). v6.5.0 fixes the architecture.

CORE BEHAVIOR (changes from v6.4.0):
  - Per-leg placement: each leg is placed at its OWN decision moment.
    Leg 1 fires when first sustain completes. Leg 2 fires when second
    sustain completes. No deferred fictional fills.
  - DRY simulation modeled on proven April 13 LIVE pattern: book-walk
    if top-of-book size insufficient, taker fee applied. NO latency
    sleep, NO fake FOK-fail-on-drift. FAK semantics (partial fills OK).
  - Abort REMOVED entirely. There is no abort. Single-leg positions
    held to resolution like every other position. New ORPHAN_END
    event logged at window close for downstream analysis only.
Originally based on v5.8.1 (2026-04-29):

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
import urllib.parse
import uuid
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
BOT_VERSION = "6.5.11"


# ──────────────────────────────────────────────────────────────────────────
# v6.5.11 — TIERED EXIT LADDER
# ──────────────────────────────────────────────────────────────────────────
# Replaces the single-tier _bs_evaluate_sell_loser (legacy kept available).
# Four tiers gated by (TTR, winner_ask):
#   T0 (any TTR, ≥0.96):  needs TTR ≤ T0_MAX_TTR + sustained ≥ SUSTAIN_THRESH
#                         for SUSTAIN_S + AND-guard (no_swing AND no_dip)
#   T1 (TTR ≤ 120s, ≥0.90): standard OR-guard (no_swing OR no_dip)
#   T2 (TTR ≤ 60s,  ≥0.87): standard OR-guard
#   T3 (TTR ≤ 30s,  ≥0.80): standard OR-guard
# All tiers require winner_ask sustained ≥ tier_threshold for TIER_PERSIST_S.
#
# Guards (purely price-based per design):
#   no_swing = no V-shape (≥SWING_DRAWDOWN drop then ≥SWING_BOUNCE recovery)
#              in last SWING_WINDOW_S seconds on the winner side
#   no_dip   = winner_ask never below DIP_FLOOR in last DIP_WINDOW_S seconds
#
# Backtest on TPS signal_log (~221 markets, see session 2026-05-31):
#   fire rate ~76%, accuracy ~92%, catastrophes ~6 per 100 markets.
# No BTC fundamentals (pure-numbers design per operator request).
#
# All env-controllable to allow tuning without redeploy. Set BS_TIER_ENABLED=
# false to fall back to the legacy single-tier evaluator + BTC-late fallbacks.
def _tier_env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _tier_env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return max(lo, min(hi, v))
    except Exception:
        return default

_BS_TIER_ENABLED          = _tier_env_bool("BS_TIER_ENABLED", True)
_BS_TIER_T0_WINNER        = _tier_env_float("BS_TIER_T0_WINNER",        0.96, 0.50, 0.99)
_BS_TIER_T1_TTR           = _tier_env_float("BS_TIER_T1_TTR",          120.0,  1.0, 300.0)
_BS_TIER_T1_WINNER        = _tier_env_float("BS_TIER_T1_WINNER",        0.90, 0.50, 0.99)
_BS_TIER_T2_TTR           = _tier_env_float("BS_TIER_T2_TTR",           60.0,  1.0, 300.0)
_BS_TIER_T2_WINNER        = _tier_env_float("BS_TIER_T2_WINNER",        0.87, 0.50, 0.99)
_BS_TIER_T3_TTR           = _tier_env_float("BS_TIER_T3_TTR",           30.0,  1.0, 300.0)
_BS_TIER_T3_WINNER        = _tier_env_float("BS_TIER_T3_WINNER",        0.80, 0.50, 0.99)
_BS_TIER_PERSIST_S        = _tier_env_float("BS_TIER_PERSIST_S",         5.0,  0.0,  60.0)
_BS_TIER_T0_MAX_TTR       = _tier_env_float("BS_TIER_T0_MAX_TTR",      200.0, 30.0, 400.0)
_BS_TIER_T0_SUSTAIN_THRESH= _tier_env_float("BS_TIER_T0_SUSTAIN_THRESH", 0.94, 0.50, 0.99)
_BS_TIER_T0_SUSTAIN_S     = _tier_env_float("BS_TIER_T0_SUSTAIN_S",     30.0,  0.0, 120.0)
_BS_TIER_SWING_WINDOW_S   = _tier_env_float("BS_TIER_SWING_WINDOW_S",   30.0,  5.0, 120.0)
_BS_TIER_SWING_DRAWDOWN   = _tier_env_float("BS_TIER_SWING_DRAWDOWN",    0.05, 0.01,  0.50)
_BS_TIER_SWING_BOUNCE     = _tier_env_float("BS_TIER_SWING_BOUNCE",      0.02, 0.01,  0.50)
_BS_TIER_DIP_WINDOW_S     = _tier_env_float("BS_TIER_DIP_WINDOW_S",     60.0,  5.0, 300.0)
_BS_TIER_DIP_FLOOR        = _tier_env_float("BS_TIER_DIP_FLOOR",         0.65, 0.30,  0.95)
# History retention: trim ask_history older than this many seconds.
_BS_TIER_HISTORY_MAX_S = max(_BS_TIER_DIP_WINDOW_S,
                              _BS_TIER_SWING_WINDOW_S,
                              _BS_TIER_T0_SUSTAIN_S) + 5.0


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

    strategy_mode = _s("STRATEGY_MODE", "both_sides_btc",
                       ("lag_signal", "both_sides_btc"))

    # Lead-time window: default 1200-1800 = 20-30 min before resolution.
    bs_lead_min = _f("BS_LEAD_TIME_MIN_S", 1200.0, 60.0, 3600.0)
    bs_lead_max = _f("BS_LEAD_TIME_MAX_S", 1800.0, 60.0, 3600.0)
    if bs_lead_min >= bs_lead_max:
        print(f"[boot][v6.1.0] warning: BS_LEAD_TIME_MIN_S ({bs_lead_min}) "
              f">= BS_LEAD_TIME_MAX_S ({bs_lead_max}); resetting to 1200/1800",
              flush=True)
        bs_lead_min, bs_lead_max = 1200.0, 1800.0

    bs_sum_ask_max = _f("BS_SUM_ASK_MAX", 1.03, 1.00, 1.20)
    bs_sell_thresh = _f("BS_SELL_LOSER_THRESHOLD", 0.93, 0.50, 0.99)
    bs_sell_ttr_floor = _f("BS_SELL_LOSER_TTR_FLOOR_S", 120.0, 0.0, 300.0)
    bs_sell_persist = _f("BS_SELL_LOSER_PERSIST_S", 5.0, 0.0, 60.0)
    bs_sell_min_bid = _f("BS_SELL_LOSER_MIN_LOSER_BID", 0.05, 0.0, 0.50)
    # v6.2.0: BTC-confirmation guard. PROD's existing book-based sell-loser fire
    # additionally requires |btc_now - btc_strike| ≥ this many USD. Set to 0 to
    # disable (= v6.1.x behavior). Default 30 — derived from May 3 catastrophe
    # analysis where all 5 catastrophes fired with |delta| ≤ $30.
    # v6.5.11: default lowered from 30.0 → 0.0 (disabled). The tiered exit
    # ladder is pure-numbers by design. Set to 30 via Railway env to restore
    # the legacy BTC guard.
    bs_min_btc_delta = _f("BS_MIN_BTC_DELTA_USD", 0.0, 0.0, 500.0)
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
    # v6.5.8: patience drop threshold for LEG1 (same side as entry).
    # If leg1 side is still falling faster than this per lookback window,
    # hold one tick — we'll get a better fill. 0 = disabled.
    bs_bss_leg1_patient_drop = _f("BS_BSS_LEG1_PATIENT_DROP", 0.005, 0.0, 0.5)
    # v6.5.11: max allowed bounce above the running low seen during the
    # leg1 sustain streak. If fire_price > streak_low + this → wait.
    # Prevents buying at $0.33 when the streak low was $0.30.
    # 0 = disabled (fire on any bounce, legacy behaviour).
    bs_bss_leg1_max_bounce = _f("BS_BSS_LEG1_MAX_BOUNCE", 0.02, 0.0, 0.5)

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
            bs_bss_leg1_patient_drop,
            bs_bss_leg1_max_bounce,
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
 _BS_BSS_LEG1_PATIENT_DROP,
 _BS_BSS_LEG1_MAX_BOUNCE,
 _BS_BSS_T_FIRST_PRE, _BS_BSS_T_SECOND_PRE,
 _BS_BSS_SUSTAIN_FIRST_PRE_S, _BS_BSS_SUSTAIN_SECOND_PRE_S,
 _BS_BSS_TICK_INTERVAL_S,
 _LOG_15M_PREFIX, _LOG_60M_PREFIX,
 _LOG_WINDOW_MIN_S, _LOG_WINDOW_MAX_S, _LOG_SAMPLE_INTERVAL_S
 ) = _read_v610_env()

_BS_ACTIVE = (_STRATEGY_MODE == "both_sides_btc")


def _bs_default_runtime_active() -> bool:
    """Default paired both-sides runtime (BS_STRATEGY != bss_entry).
    Paired YES+NO entry + sell-loser. BSS threads unreachable.
    """
    return _BS_ACTIVE and _BS_STRATEGY != "bss_entry"


def _bs_bss_runtime_active() -> bool:
    """Legacy bss_entry runtime. Only True when BS_STRATEGY == 'bss_entry'.
    Enables BSS threads, WAITING_2ND, ORPHAN_END, orphan machinery.
    """
    return _BS_ACTIVE and _BS_STRATEGY == "bss_entry"


if _bs_default_runtime_active():
    print("[boot] default both-sides runtime = paired entry + sell-loser; BSS threads disabled", flush=True)
elif _bs_bss_runtime_active():
    print("[boot] legacy bss_entry runtime active; dedicated BSS threads enabled", flush=True)


def _set_trading_paused(state: "BotState", paused: bool) -> None:
    state.trading_paused = bool(paused)


def _trading_pause_reason(state: "BotState") -> str:
    return "paused_by_operator" if getattr(state, "trading_paused", False) else ""


def _entry_blocked_by_pause(state: "BotState", tag: str = "entry") -> bool:
    if not getattr(state, "trading_paused", False):
        return False
    now = time.time()
    last = getattr(state, "_pause_log_last_ts", 0.0)
    if now - last >= 15.0:
        print(f"[pause] trading paused — skipping new entry ({tag})", flush=True)
        state._pause_log_last_ts = now
    return True


# ═══════════════════════════════════════════════════════════════════
# v6.5.0 SKULD: LIVE gating + DRY book-walk simulation
# ═══════════════════════════════════════════════════════════════════
# LIVE_BSS_ENABLED: secondary gate beyond MODE=live. Both must be true
# for the bot to place real CLOB orders. Defaults false — flipping
# MODE=live alone is intentionally insufficient.
def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

_LIVE_BSS_ENABLED = _bool_env("LIVE_BSS_ENABLED", False)

# v6.5.0: DRY simulation modeled on proven April 13 LIVE pattern.
# When desired qty > top-of-book ask size, walk to next book level.
# This is the only realistic friction at $1 sizing on these markets.
# Default ON (true). When OFF, DRY fills 100% of qty at top-of-book ask
# regardless of size (legacy v6.3.x behavior).
_BS_BOOK_WALK_ENABLED = _bool_env("BS_BOOK_WALK_ENABLED", True)

# Taker fee — Polymarket crypto-market formula (v6.5.5):
#   fee = shares × rate × price × (1 - price)
# where rate=0.07 for crypto category (BTC up/down). Symmetric peak at
# p=0.50 ($1.75 on a $50 trade), falls toward extremes.
# Source: https://docs.polymarket.com/trading/fees (verified May 18 2026).
#
# Pre-v6.5.5 the bot used flat 2% which under-counted fees by ~120% for
# our typical entry prices (mean leg-1 fill ~$0.34 → real fee $0.046,
# bot was charging $0.020). The 62.4h audit showed bot underreporting
# ~$22 in fees, flipping the strategy from "near break-even" to "slightly
# negative" once accounted for.
#
# BS_POLYMARKET_TAKER_FEE_RATE = the rate constant in the formula (0.07
# for crypto markets, 0.0 for geopolitical fee-free markets). Override
# via env if Polymarket changes the rate or to disable fees in sim.
# BS_TAKER_FEE_PCT (legacy) = retained for backward compat only — used
# as the flat-fee fallback when BS_USE_POLYMARKET_FEE_FORMULA=false.
_BS_POLYMARKET_TAKER_FEE_RATE = float(
    os.environ.get("BS_POLYMARKET_TAKER_FEE_RATE", "0.07") or "0.07"
)
_BS_USE_POLYMARKET_FEE_FORMULA = (os.environ.get(
    "BS_USE_POLYMARKET_FEE_FORMULA", "true") or "true").lower() in ("1","true","yes")
_BS_TAKER_FEE_PCT = float(os.environ.get("BS_TAKER_FEE_PCT", "0.02") or "0.02")


def _polymarket_taker_fee(shares: float, price: float) -> float:
    """v6.5.5: compute taker fee for a trade of `shares` at `price`.

    Returns fee in USDC. Uses Polymarket crypto formula by default:
        fee = shares × rate × price × (1 - price)

    Falls back to flat % when BS_USE_POLYMARKET_FEE_FORMULA=false (for
    backward-compat sim or testing). Guards against degenerate prices.

    For a $1-sized trade at price p:
        shares = 1/p, fee = (1/p) × rate × p × (1-p) = rate × (1-p)
    so the per-$1 fee is rate × (1-p). At p=0.30, rate=0.07 → fee = $0.049.
    """
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    if _BS_USE_POLYMARKET_FEE_FORMULA:
        return shares * _BS_POLYMARKET_TAKER_FEE_RATE * price * (1.0 - price)
    # Legacy fallback: flat % of trade value (= shares × price)
    return shares * price * _BS_TAKER_FEE_PCT

# Health log cadence
_BS_HEALTH_LOG_INTERVAL_S = float(os.environ.get("BS_HEALTH_LOG_INTERVAL_S", "10.0") or "10.0")


# ═══════════════════════════════════════════════════════════════════
# v6.5.2 SKULD: leg-1 entry filter (TTR floor)
# ═══════════════════════════════════════════════════════════════════
# BS_BSS_MIN_TTR_AT_LEG1_S: skip leg-1 firing when time-to-resolution
# (TTR) is below this floor. Council recommendation after 4-day data
# analysis: entries with TTR<240s have ~28-50% orphan rate vs ~9-10%
# at TTR>=240s. Default 240.0 = leg-1 only fires in the first 60s of
# the 5-minute window.
#
# Settings:
#   0.0   → filter off (revert to v6.5.1 behavior)
#   180.0 → relaxed (first 120s window, more entries, more orphans)
#   210.0 → moderate (first 90s window)
#   240.0 → council default (first 60s window)
#   270.0 → aggressive (first 30s window, fewer entries, low orphan rate)
#
# Tunable via Railway env var without code change.
_BS_BSS_MIN_TTR_AT_LEG1_S = float(
    os.environ.get("BS_BSS_MIN_TTR_AT_LEG1_S", "240.0") or "240.0"
)


# ═══════════════════════════════════════════════════════════════════
# v6.5.3.1 SKULD: shadow emergency-sell tick cadence
# ═══════════════════════════════════════════════════════════════════
# BS_BSS_SHADOW_TICK_INTERVAL_S: how often to emit BSS_HOLD_SHADOW_DRY
# events during the WAITING_2ND hold. Default 5.0 = one event per 5
# seconds per held leg. Used to record what an emergency-sell rule
# WOULD have done at each moment of the hold, so we can post-hoc
# design and calibrate the actual rule before flipping it live.
#
# Set to 0 to disable shadow logging entirely (reverts to v6.5.3
# behavior with no extra logging during hold).
#
# Volume estimate: ~250 holds/day × ~50 ticks per hold @ 5s cadence
# ≈ 12,500 rows/day extra in bs_trades. CSV size negligible.
_BS_BSS_SHADOW_TICK_INTERVAL_S = float(
    os.environ.get("BS_BSS_SHADOW_TICK_INTERVAL_S", "3.0") or "3.0"
)


# ═══════════════════════════════════════════════════════════════════
# v6.5.4 SKULD: orphan-sell rule (positive-exit)
# ═══════════════════════════════════════════════════════════════════
# When enabled, during the WAITING_2ND hold, the bot evaluates an exit
# rule on every shadow tick (default 3s cadence). If conditions hold for
# a configurable number of consecutive ticks, leg-1 is sold at the
# current bid, locking in a small profit and avoiding the orphan loss.
#
# Rule (default thresholds, derived from 49.5h of hold-shadow data):
#   sell_pnl_now >= BS_BSS_ORPHAN_SELL_MIN_PNL           ($0.00)
#   AND hold_elapsed_s >= BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S  (90.0s)
#   AND bin_adverse_since_leg1_bps >= BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS  (0.0)
#   for BS_BSS_ORPHAN_SELL_PERSIST_TICKS consecutive shadow ticks (2)
#
# In-sample (49.5h): saves 85% of orphans (186/220) at the cost of 29%
# of paireds being exited early (87/301). Net effect: +$233 vs baseline
# (+$113/day extrapolated). Default DISABLED for safe rollout — flip
# BS_BSS_ORPHAN_SELL_ENABLED=true to activate.
_BS_BSS_ORPHAN_SELL_ENABLED = (os.environ.get(
    "BS_BSS_ORPHAN_SELL_ENABLED", "false") or "false").lower() in ("1", "true", "yes")
_BS_BSS_ORPHAN_SELL_MIN_PNL = float(
    os.environ.get("BS_BSS_ORPHAN_SELL_MIN_PNL", "0.0") or "0.0"
)
_BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S = float(
    os.environ.get("BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S", "90.0") or "90.0"
)
_BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS = float(
    os.environ.get("BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS", "0.0") or "0.0"
)
_BS_BSS_ORPHAN_SELL_PERSIST_TICKS = int(float(
    os.environ.get("BS_BSS_ORPHAN_SELL_PERSIST_TICKS", "2") or "2"
))


# ═══════════════════════════════════════════════════════════════════
# v6.5.5 SKULD: orphan take-profit (TP) rule
# ═══════════════════════════════════════════════════════════════════
# Complementary to the orphan-sell defensive exit (which fires at
# break-even when BTC is adverse). The TP rule fires OPPORTUNISTICALLY
# when leg-1 bid has recovered substantially above entry — locking in a
# real profit before potential reversal.
#
# Trigger:
#   leg1_bid_now / leg1_entry_ask >= BS_BSS_ORPHAN_TP_RATIO    (default 1.75)
#   for BS_BSS_ORPHAN_TP_PERSIST_TICKS consecutive shadow ticks (default 1)
#
# Defaults derived from 62.4h audit (May 16-19, with correct Polymarket
# fees and bid-book-walk slippage modeling):
#   ratio=1.50, persist=1 → +$47/day, fires on 54% of paireds (aggressive)
#   ratio=1.75, persist=1 → +$43/day, fires on 25% of paireds (balanced) ← default
#   ratio=2.00, persist=1 → +$27/day, fires on 9% of paireds (very selective)
#
# This is a SEPARATE trigger from orphan-sell. Either rule can fire,
# whichever fires first wins. TP doesn't require BTC-adverse or elapsed-
# time conditions — when bid spikes, take the gain.
#
# Default DISABLED for safe rollout. Flip BS_BSS_ORPHAN_TP_ENABLED=true
# after verifying v6.5.5 boot + dashboard + fee fix on Railway.
_BS_BSS_ORPHAN_TP_ENABLED = (os.environ.get(
    "BS_BSS_ORPHAN_TP_ENABLED", "false") or "false").lower() in ("1","true","yes")
_BS_BSS_ORPHAN_TP_RATIO = float(
    os.environ.get("BS_BSS_ORPHAN_TP_RATIO", "1.75") or "1.75"
)
_BS_BSS_ORPHAN_TP_PERSIST_TICKS = int(float(
    os.environ.get("BS_BSS_ORPHAN_TP_PERSIST_TICKS", "1") or "1"
))


# ═══════════════════════════════════════════════════════════════════
# v6.5.5.2 SKULD: band-based sustain (replaces tick-counting persist)
# ═══════════════════════════════════════════════════════════════════
# The v6.5.4 persist mechanism counted CONSECUTIVE shadow ticks where
# all conditions held. May 19 data showed the bid wobbles 5-10¢ between
# consecutive ticks 42% of the time — far too volatile for a "must be
# identical 2 ticks in a row" rule. Most legitimate profit windows were
# missed because conditions briefly dipped negative between ticks.
#
# v6.5.5.2 replaces tick-counting with timestamp-based band tracking:
#
#   - On each tick where conditions ARE met:
#       * If no qualifying run is in progress, start one (set first_ts)
#       * Update last_ts to now
#   - On each tick where conditions are NOT met:
#       * If the gap since last qualifying tick > GRACE_S: reset both
#       * Otherwise: tolerate the wobble, leave timestamps alone
#   - Fire when: conditions met NOW AND (now - first_ts) >= SUSTAIN_S
#
# This handles natural price wobble inside a "good enough" band, while
# still requiring the conditions to actually be sustained across time.
#
# SUSTAIN_S defaults match the time equivalent of old PERSIST_TICKS at
# the 3s tick cadence (orphan-sell: 6s ≈ 2 ticks, TP: 3s ≈ 1 tick).
# GRACE_S is the wobble tolerance — a brief failure within GRACE_S of
# the last qualifying tick does NOT reset the run.
_BS_BSS_ORPHAN_SELL_SUSTAIN_S = float(
    os.environ.get("BS_BSS_ORPHAN_SELL_SUSTAIN_S", "6.0") or "6.0"
)
_BS_BSS_ORPHAN_SELL_GRACE_S = float(
    os.environ.get("BS_BSS_ORPHAN_SELL_GRACE_S", "3.0") or "3.0"
)
_BS_BSS_ORPHAN_TP_SUSTAIN_S = float(
    os.environ.get("BS_BSS_ORPHAN_TP_SUSTAIN_S", "3.0") or "3.0"
)
_BS_BSS_ORPHAN_TP_GRACE_S = float(
    os.environ.get("BS_BSS_ORPHAN_TP_GRACE_S", "1.0") or "1.0"
)


# ═══════════════════════════════════════════════════════════════════
# v6.5.7 SKULD: reverse-sniper cashout (Rule C)
# ═══════════════════════════════════════════════════════════════════
# When the other side reaches conviction (winner_ask >= threshold),
# sell the losing orphan leg at cashout bid to recover partial value
# rather than holding to a near-certain -$1 loss at resolution.
#
# Data (May 21, 46 orphans): winner crosses 0.70 avg 30s into market,
# sell bid avg $0.272 → risk-adjusted recovery $28.17 vs -$46.92 loss.
# Firing at 0.70 recovers 60% of orphan losses. No TTR cap needed —
# the market dynamics (loser already below T_SECOND_FLOOR) confirm
# leg2 is dead the moment winner hits 0.70.
#
# Cashout always fills on the sell side — buyers exist for cheap
# lottery tickets. No minimum bid floor required.
#
# Env vars:
#   BS_BSS_ORPHAN_RS_ENABLED          (default false)
#   BS_BSS_ORPHAN_RS_WINNER_THRESHOLD (default 0.70)
#   BS_BSS_ORPHAN_RS_SUSTAIN_S        (default 6.0)
#   BS_BSS_ORPHAN_RS_GRACE_S          (default 1.5)
_BS_BSS_ORPHAN_RS_ENABLED = (os.environ.get(
    "BS_BSS_ORPHAN_RS_ENABLED", "false") or "false").lower() in ("1", "true", "yes")
_BS_BSS_ORPHAN_RS_WINNER_THRESHOLD = float(
    os.environ.get("BS_BSS_ORPHAN_RS_WINNER_THRESHOLD", "0.70") or "0.70"
)
_BS_BSS_ORPHAN_RS_SUSTAIN_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_SUSTAIN_S", "6.0") or "6.0"
)
_BS_BSS_ORPHAN_RS_GRACE_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_GRACE_S", "1.5") or "1.5"
)
# Only fire RS in the last N seconds — gives position time to recover/pair first
_BS_BSS_ORPHAN_RS_TTR_MAX_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TTR_MAX_S", "60.0") or "60.0"
)
# v6.5.9: minimum hold time before RS can fire. Data shows 84% of early cheap
# entries (loser@0.29-0.34) recover to PE-profitable within 60-120s — firing RS
# in the first 90s destroys those recoveries. Same floor as PE min_elapsed.
_BS_BSS_ORPHAN_RS_MIN_ELAPSED_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_MIN_ELAPSED_S", "90.0") or "90.0"
)
# v6.5.10: TIERED adaptive RS — built from 820-market depth×BTC analysis.
# Below winner=0.90: 20-40% natural recovery → never sell (hold for PE/leg2).
# winner>=0.90 + TTR<120s: 82-93% full loss → sell to recover loser bid.
# winner>=0.95 at any TTR: 96-100% full loss → sell immediately.
# BTC guard: falling BTC cuts full-loss rate from 82% → 43% → suppress RS.
#
# Tier 1: fire when winner >= this, regardless of TTR (no recovery possible)
_BS_BSS_ORPHAN_RS_TIER1_WIN = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TIER1_WIN", "0.95") or "0.95"
)
# Tier 2: fire when winner >= this AND TTR <= tier2_ttr_s
_BS_BSS_ORPHAN_RS_TIER2_WIN = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TIER2_WIN", "0.92") or "0.92"
)
_BS_BSS_ORPHAN_RS_TIER2_TTR_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TIER2_TTR_S", "120.0") or "120.0"
)
# Tier 3: fire when winner >= this AND TTR <= tier3_ttr_s
_BS_BSS_ORPHAN_RS_TIER3_WIN = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TIER3_WIN", "0.90") or "0.90"
)
_BS_BSS_ORPHAN_RS_TIER3_TTR_S = float(
    os.environ.get("BS_BSS_ORPHAN_RS_TIER3_TTR_S", "120.0") or "120.0"
)
# BTC guard: suppress RS when BTC has fallen > X USD in the last 60s.
# Falling BTC on a YES-up orphan signals potential reversal — hold instead.
# Set to 0 to disable the guard.
_BS_BSS_ORPHAN_RS_BTC_GUARD_USD = float(
    os.environ.get("BS_BSS_ORPHAN_RS_BTC_GUARD_USD", "5.0") or "5.0"
)
# v6.5.9: when TTR > this threshold, require sell_pnl > BS_BSS_PE_HIGH_BAR_PNL
# before PE fires. Data shows 79% of early PE fires (TTR>120s) would have
# paired or recovered to better P&L if given more time.
_BS_BSS_PE_HIGH_BAR_TTR_S = float(
    os.environ.get("BS_BSS_PE_HIGH_BAR_TTR_S", "120.0") or "120.0"
)
_BS_BSS_PE_HIGH_BAR_PNL = float(
    os.environ.get("BS_BSS_PE_HIGH_BAR_PNL", "0.15") or "0.15"
)


# ═══════════════════════════════════════════════════════════════════
# v6.5.3 SKULD: Tier 1 logging instrumentation
# ═══════════════════════════════════════════════════════════════════
# Per-market ring buffer of recent tick state, used to compute pre-entry
# features at BSS_FIRST_LEG fire time AND at BSS_CANDIDATE detection time.
#
# Each tick captures: (ts_ms, yes_ask, no_ask, yes_bid, no_bid,
#                       yes_ask_depth5, no_ask_depth5).
# Indexed by market.condition_id. Appended by _v653_buf_append on every
# evaluation tick (called from _bs_evaluate_bss_entry). Read at fire time
# by _v653_compute_features. Cleared by _v653_buf_clear when market
# reaches a terminal state.
#
# Buffer size 2400 entries = ~480s at 5Hz (sample cadence is ~1Hz to
# multi-Hz depending on tick activity). Easily covers the 120s lookback
# we need. Memory: ~64 bytes/entry × 2400 × ~30 active markets ≈ 5 MB.
#
# Thread safety: deque.append is atomic in CPython; the lock guards only
# dict-level insert/remove. No lock needed for read; snapshot via list().
import collections as _v653_collections

_V653_BUF_MAXLEN = 2400
_v653_buf: Dict[str, "_v653_collections.deque"] = {}
_v653_buf_lock = threading.Lock()


def _v653_buf_append(market_id: str, ts_ms: int,
                      yes_ask: float, no_ask: float,
                      yes_bid: float, no_bid: float,
                      yes_ask_depth5: float, no_ask_depth5: float) -> None:
    """Append a tick of state to the per-market ring buffer. Lazy-creates
    the deque on first call for this market. Never raises."""
    try:
        buf = _v653_buf.get(market_id)
        if buf is None:
            with _v653_buf_lock:
                buf = _v653_buf.get(market_id)
                if buf is None:
                    buf = _v653_collections.deque(maxlen=_V653_BUF_MAXLEN)
                    _v653_buf[market_id] = buf
        buf.append((ts_ms, float(yes_ask), float(no_ask),
                    float(yes_bid), float(no_bid),
                    float(yes_ask_depth5), float(no_ask_depth5)))
    except Exception:
        pass  # never crash the eval loop on logging


def _v653_buf_clear(market_id: str) -> None:
    """Remove buffer for a market (called when market reaches terminal state).
    Idempotent — safe to call multiple times. Never raises."""
    try:
        with _v653_buf_lock:
            _v653_buf.pop(market_id, None)
    except Exception:
        pass


def _v653_ask_depth_5(book) -> float:
    """Sum of top-5 ask-side level sizes. Returns 0.0 if no ladder."""
    try:
        levels = getattr(book, "ask_levels", None) or []
        return float(sum(sz for (_p, sz) in levels[:5]))
    except Exception:
        return 0.0


def _v653_compute_features(market_id: str, fire_ts_ms: int,
                            leg1_side: str, leg2_side: str,
                            now_unix: float,
                            yes_ask_depth5_now: float,
                            no_ask_depth5_now: float,
                            yes_book_age_s: float,
                            no_book_age_s: float,
                            binance_last_tick_ts_ms: Optional[int],
                            binance_prices_snapshot: List[Tuple[float, float]],
                            leg2_token_id: Optional[str] = None,
                            poly_trades: Optional[Dict[str, Any]] = None,
                            decision_lat_ms: Optional[int] = None) -> dict:
    """Compute v6.5.3 feature set at fire/candidate time.
    Returns dict suitable for JSON encoding into notes.extra_json.
    Never raises; on error returns at least {'v': '6.5.3', 'err': '...'}.

    Council-agreed Tier 1 features:
    - leg2 ask microstructure (n_changes, distinct, min/max/net) on 30/60/120s
    - leg2 ask depth delta (resting supply trend) on 30/120s
    - leg1 bid trajectory (min/max/at_fire/falling) on 30s + 5s
    - leg2 trade count (l2_n_120s) — orthogonal flow-side measure
    - Latency: bin_age_ms, l1/l2_book_age_ms, decision_lat_ms
    - Regime: hod_utc, Binance ret/vol on 5m/15m/60m windows
    """
    out: dict = {"v": "6.5.3"}
    try:
        # leg2_idx: index in buf tuple for leg2 ask
        # (0=ts, 1=yes_ask, 2=no_ask, 3=yes_bid, 4=no_bid, 5=yes_d5, 6=no_d5)
        leg2_ask_idx = 1 if leg2_side == "YES" else 2
        leg1_bid_idx = 3 if leg1_side == "YES" else 4
        leg2_depth_idx = 5 if leg2_side == "YES" else 6

        leg1_book_age = yes_book_age_s if leg1_side == "YES" else no_book_age_s
        leg2_book_age = yes_book_age_s if leg2_side == "YES" else no_book_age_s

        # Snapshot buffer
        buf = _v653_buf.get(market_id)
        if buf is not None and len(buf) >= 3:
            snap = list(buf)

            def in_window(ts_window_ms: int) -> list:
                lo = fire_ts_ms - ts_window_ms
                return [r for r in snap if lo <= r[0] <= fire_ts_ms]

            # leg2 ask microstructure: change count, distinct, min/max, net
            for w_s in (30, 60, 120):
                sub = in_window(w_s * 1000)
                if len(sub) >= 3:
                    asks = [r[leg2_ask_idx] for r in sub]
                    nch = sum(1 for i in range(1, len(asks))
                              if asks[i] != asks[i-1])
                    out[f"l2_nch_{w_s}s"] = nch
                    out[f"l2_nd_{w_s}s"] = len(set(asks))
                    out[f"l2_min_{w_s}s"] = round(min(asks), 4)
                    out[f"l2_max_{w_s}s"] = round(max(asks), 4)
                    if w_s <= 60:
                        out[f"l2_net_{w_s}s"] = round(asks[-1] - asks[0], 4)

            # leg2 depth delta (resting supply trend)
            for w_s in (30, 120):
                sub = in_window(w_s * 1000)
                if len(sub) >= 3:
                    d0 = sub[0][leg2_depth_idx]
                    d1 = sub[-1][leg2_depth_idx]
                    out[f"l2_dd_{w_s}s"] = round(d1 - d0, 1)

            out["l2_d5_now"] = round(
                yes_ask_depth5_now if leg2_side == "YES" else no_ask_depth5_now,
                1)

            # leg1 bid trajectory (entry quality)
            sub30 = in_window(30 * 1000)
            if len(sub30) >= 3:
                bids = [r[leg1_bid_idx] for r in sub30]
                out["l1_bid_min"] = round(min(bids), 4)
                out["l1_bid_max"] = round(max(bids), 4)
                out["l1_bid_atf"] = round(bids[-1], 4)
                sub5 = in_window(5 * 1000)
                if len(sub5) >= 3:
                    bids5 = [r[leg1_bid_idx] for r in sub5]
                    out["l1_bid_fall"] = int(bids5[-1] < bids5[0])

        # Latency telemetry
        if binance_last_tick_ts_ms is not None:
            out["bin_age_ms"] = int(fire_ts_ms - binance_last_tick_ts_ms)
        out["l2_book_age_ms"] = int(leg2_book_age * 1000)
        out["l1_book_age_ms"] = int(leg1_book_age * 1000)
        if decision_lat_ms is not None:
            out["decision_lat_ms"] = int(decision_lat_ms)

        # Hour of day UTC
        try:
            out["hod_utc"] = datetime.utcfromtimestamp(now_unix).hour
        except Exception:
            pass

        # leg2 trade count (orthogonal-ish flow measure; bot doesn't have
        # bid/ask classified trades on Polymarket but raw count + volume
        # are correlated with leg2 "activity intensity"). Read from the
        # bot's per-token trade deque if available.
        if leg2_token_id and poly_trades is not None:
            try:
                deque_l2 = poly_trades.get(leg2_token_id)
                if deque_l2:
                    cutoff_120s = now_unix - 120.0
                    cutoff_20s = now_unix - 20.0
                    snap = list(deque_l2)
                    n_120 = 0
                    n_20 = 0
                    vol_120 = 0.0
                    for trade in snap:
                        # (ts, price, size, side)
                        if len(trade) < 3:
                            continue
                        ts = trade[0]
                        if ts >= cutoff_120s:
                            n_120 += 1
                            try:
                                vol_120 += float(trade[1]) * float(trade[2])
                            except Exception:
                                pass
                            if ts >= cutoff_20s:
                                n_20 += 1
                    out["l2_n_120s"] = n_120
                    out["l2_n_20s"] = n_20
                    out["l2_vol_120s"] = round(vol_120, 2)
            except Exception:
                pass

        # Binance regime: realized vol + signed return on 5m / 15m / 60m
        # windows. Vol is per-tick log-return std × 10000 (bps). Ret is
        # net return × 10000 (bps).
        if binance_prices_snapshot and len(binance_prices_snapshot) >= 10:
            import math as _math
            for w_label, w_s in (("5m", 300.0), ("15m", 900.0), ("60m", 3600.0)):
                cutoff = now_unix - w_s
                recent = [(t, p) for t, p in binance_prices_snapshot
                          if t >= cutoff and p > 0]
                if len(recent) < 5:
                    continue
                prices = [p for _, p in recent]
                ret_bps = (prices[-1] / prices[0] - 1.0) * 10000.0
                out[f"bin_ret_{w_label}_bps"] = round(ret_bps, 1)
                log_rets = []
                for i in range(len(prices) - 1):
                    if prices[i] > 0 and prices[i+1] > 0:
                        log_rets.append(_math.log(prices[i+1] / prices[i]))
                if log_rets:
                    avg = sum(log_rets) / len(log_rets)
                    var = sum((r - avg) ** 2 for r in log_rets) / len(log_rets)
                    vol_bps = _math.sqrt(var) * 10000.0
                    out[f"bin_vol_{w_label}_bps"] = round(vol_bps, 1)
    except Exception as e:
        out["err"] = f"{type(e).__name__}"
    return out


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


def _safe_log_file_path(state: "BotState", name: str) -> Optional[Path]:
    try:
        raw = (name or "").strip()
        if not raw or "/" in raw or "\\" in raw:
            return None
        if not raw.lower().endswith(".csv"):
            return None
        if not state.log_dir:
            return None
        allowed = {item.get("filename", "") for item in list_log_files(Path(state.log_dir))}
        if raw not in allowed:
            return None
        p = (Path(state.log_dir) / raw).resolve()
        base = Path(state.log_dir).resolve()
        try:
            p.relative_to(base)
        except Exception:
            return None
        if not p.exists() or not p.is_file():
            return None
        return p
    except Exception:
        return None


def _manual_purge_old_logs(state: "BotState") -> Dict[str, Any]:
    if _LOG_RETENTION_DAYS <= 0 or not state.log_dir:
        return {"ok": True, "files_deleted": 0, "bytes_freed": 0}
    log_dir = Path(state.log_dir)
    before: Dict[str, int] = {}
    try:
        for p in log_dir.glob("*.csv"):
            try:
                before[str(p)] = p.stat().st_size
            except Exception:
                pass
    except Exception:
        before = {}
    _purge_old_logs(log_dir, _LOG_RETENTION_DAYS)
    after_keys: set = set()
    try:
        for p in log_dir.glob("*.csv"):
            after_keys.add(str(p))
    except Exception:
        pass
    files_deleted = sum(1 for k in before if k not in after_keys)
    bytes_freed = sum(sz for k, sz in before.items() if k not in after_keys)
    return {"ok": True, "files_deleted": files_deleted, "bytes_freed": int(bytes_freed)}


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
    # v6.5.11: tiered exit ladder state. ask_history is a deque-like list of
    # (ts, yes_ask, no_ask) tuples used by the swing/dip/sustain checks. We
    # trim it to ~60s on every update. fire_tier records which tier label
    # ("T0"/"T1"/"T2"/"T3") fired at sell time, used in the SELL_LOSER_DRY
    # event note for offline analysis. tier_last_eval_status is a debug
    # string surfaced on /api/status for live tuning.
    tier_ask_history: List[Tuple[float, float, float]] = field(default_factory=list)
    fire_tier: str = ""
    tier_last_eval_status: str = "preconditions_pending"


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
    # v6.5.0: states are WATCH → WAITING_2ND (semantic: HALF, leg 1 actually
    # held) → BOTH (PAIRED) → RESOLVED. ABORT is NEVER entered. Window-end
    # transitions HALF directly to ORPHAN_END logging then RESOLVED.
    bss_state: str = "WATCH"
    bss_yes_below_first_start_ts: Optional[float] = None
    bss_no_below_first_start_ts: Optional[float] = None
    bss_yes_leg1_low: Optional[float] = None   # v6.5.11: running min YES ask during WATCH streak
    bss_no_leg1_low:  Optional[float] = None   # v6.5.11: running min NO ask during WATCH streak
    bss_first_side: Optional[str] = None
    bss_first_price: Optional[float] = None         # decision-time ask (legacy field — kept)
    bss_first_fill_ts: Optional[float] = None
    # v6.5.0: actual fill state for leg 1. Distinguished from decision price
    # so DRY simulation honesty + LIVE response prices are tracked correctly.
    bss_leg1_actual_ask: Optional[float] = None      # the price the leg actually filled at
    bss_leg1_qty: Optional[float] = None             # qty actually obtained (post book-walk)
    bss_leg1_fee: Optional[float] = None             # taker fee charged on leg 1
    bss_leg1_size_usdc: Optional[float] = None       # USDC committed on leg 1 (post book-walk)
    bss_leg1_orphan_end_logged: bool = False         # has BSS_ORPHAN_END been written?
    bss_other_below_strict_start_ts: Optional[float] = None
    bss_other_below_relax_start_ts: Optional[float] = None
    bss_second_price: Optional[float] = None
    bss_second_fill_ts: Optional[float] = None
    bss_second_phase: Optional[str] = None     # 'strict' | 'relaxed'
    # v6.5.0: leg 2 actual fill state
    bss_leg2_actual_ask: Optional[float] = None
    bss_leg2_qty: Optional[float] = None
    bss_leg2_fee: Optional[float] = None
    bss_leg2_size_usdc: Optional[float] = None
    # v6.5.0: deprecated, kept for compat — never written
    bss_abort_sold_at: Optional[float] = None
    bss_abort_ts: Optional[float] = None

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

    # v6.5.3.1: shadow-tick cadence tracker. Updated each time a
    # BSS_HOLD_SHADOW_DRY event is emitted during the WAITING_2ND hold.
    # Used to throttle emission to ~3s cadence (configurable). Reset to
    # 0.0 on terminal state transitions implicitly via mdm replacement.
    bss_last_shadow_ts: float = 0.0

    # v6.5.3.1: per-hold bookkeeping for shadow logging. All initialized
    # lazily on the FIRST shadow tick of a hold (detected via
    # bss_hold_id is None). Reset automatically when the bot creates a
    # new MultiDurationMarket for the next market cycle.
    bss_hold_id: Optional[str] = None
    bss_hold_tick_idx: int = 0
    bss_hold_bin_price_atleg1: Optional[float] = None    # BTC price when leg1 filled (first shadow)
    bss_hold_leg2_ask_atleg1: Optional[float] = None     # leg2 ask at first shadow (≈ leg1 fill)
    bss_hold_pnl_peak: Optional[float] = None            # peak sell-pnl seen across hold
    bss_hold_pnl_peak_ts: Optional[float] = None         # when that peak was hit
    bss_hold_pnl_was_positive: bool = False              # was sell-pnl ≥ 0 at any point?
    bss_hold_leg1_bid_max: Optional[float] = None        # peak leg1 bid across hold
    bss_hold_leg1_bid_min: Optional[float] = None        # trough leg1 bid across hold
    bss_hold_leg2_ask_max: Optional[float] = None        # peak leg2 ask across hold
    bss_hold_leg2_ask_min: Optional[float] = None        # trough leg2 ask across hold
    bss_hold_l2_visits_below_055: int = 0                # count of dips below 0.55 (proximity to strict)
    bss_hold_l2_visits_below_062: int = 0                # count of dips below 0.62 (proximity to relaxed)
    bss_hold_l2_prev_above_055: bool = True              # for visit-edge detection
    bss_hold_l2_prev_above_062: bool = True

    # v6.5.4: orphan-sell rule (positive-exit). When all three rule
    # conditions hold across N consecutive shadow ticks, leg-1 is sold
    # at the current bid. The counter increments each shadow tick when
    # conditions are met, resets to 0 when they aren't.
    bss_orphan_sell_consecutive_ticks: int = 0
    bss_orphan_sold_at: Optional[float] = None       # leg-1 bid at sell moment
    bss_orphan_sold_ts: Optional[float] = None       # when the sell fired
    bss_orphan_sold_pnl: Optional[float] = None      # realized sell pnl (incl. fees)

    # v6.5.5: take-profit (TP) rule. Independent counter — TP can fire
    # before, after, or instead of orphan-sell. Reset to 0 when ratio
    # condition lapses. `bss_orphan_sold_reason` records which rule
    # actually fired ('positive_exit' or 'take_profit') for telemetry.
    bss_orphan_tp_consecutive_ticks: int = 0
    bss_orphan_sold_reason: Optional[str] = None     # 'positive_exit' | 'take_profit'

    # v6.5.5.2: band-based sustain timestamps (replace consecutive_ticks
    # counters). first_qual_ts is when the current qualifying run began;
    # last_qual_ts is the most recent tick conditions were met. A brief
    # failure (within GRACE_S of last_qual_ts) tolerates wobble without
    # resetting; sustained failure (>GRACE_S) clears the run. Fire when
    # conditions are met NOW AND (now - first_qual_ts) >= SUSTAIN_S.
    bss_orphan_sell_first_qual_ts: Optional[float] = None
    bss_orphan_sell_last_qual_ts: Optional[float] = None
    bss_orphan_tp_first_qual_ts: Optional[float] = None
    bss_orphan_tp_last_qual_ts: Optional[float] = None
    # v6.5.7: reverse-sniper cashout (Rule C) sustain timestamps
    bss_orphan_rs_first_qual_ts: Optional[float] = None
    bss_orphan_rs_last_qual_ts: Optional[float] = None


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
    trading_paused: bool = False  # pauses new entries only; exits/management unaffected

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
    # v6.4.0: hard-disabled at init time, kept for compat.
    pre_market_books_logger: Optional[Any] = None
    # New CSV logger for bs_trades_<date>.csv (both-sides entry/exit events).
    bs_trades_logger: Optional[Any] = None
    # v6.4.0 SKULD: new loggers
    resolution_audit_logger: Optional[Any] = None
    health_logger: Optional[Any] = None
    last_health_log_ts: float = 0.0

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
SLUG_LOOKAHEAD_5M = 4   # covers TTR up to 1200s + active 300s = window
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
                # v6.5.1: maintain the full ladder. Each price_change is a
                # per-level delta on the order book:
                #   size > 0  → set/replace level at that price
                #   size = 0  → remove level at that price
                # After applying all changes, recompute best bid/ask from
                # the live ladder. This fixes the v6.5.0 bug where SELL
                # cancellations (size=0) at stale low prices poisoned
                # `book.ask` with a price that wasn't in the book.
                book = state.poly_books.get(asset_id)
                if book:
                    changes = event.get("changes") or event.get("price_changes") or []
                    # Local copies so readers see consistent ladders during
                    # the multi-step update — final assignment is atomic.
                    new_bid_levels: List[Tuple[float, float]] = list(book.bid_levels)
                    new_ask_levels: List[Tuple[float, float]] = list(book.ask_levels)
                    for ch in changes:
                        try:
                            side = (ch.get("side") or "").upper()
                            price = float(ch.get("price"))
                            size = float(ch.get("size"))
                        except Exception:
                            continue
                        if price <= 0:
                            continue
                        if side == "BUY":
                            # Remove any existing level at this price
                            new_bid_levels = [(p, s) for (p, s) in new_bid_levels
                                              if p != price]
                            # Add new level if size > 0 (size=0 means cancellation)
                            if size > 0:
                                new_bid_levels.append((price, size))
                        elif side == "SELL":
                            new_ask_levels = [(p, s) for (p, s) in new_ask_levels
                                              if p != price]
                            if size > 0:
                                new_ask_levels.append((price, size))
                    # Sort + truncate to DEPTH_LEVELS for storage parity with
                    # `book` snapshot path. Bids: highest first. Asks: lowest first.
                    new_bid_levels.sort(key=lambda x: x[0], reverse=True)
                    new_bid_levels = new_bid_levels[:DEPTH_LEVELS]
                    new_ask_levels.sort(key=lambda x: x[0])
                    new_ask_levels = new_ask_levels[:DEPTH_LEVELS]
                    # Atomic swap-in
                    book.bid_levels = new_bid_levels
                    book.ask_levels = new_ask_levels
                    # Recompute best from updated ladder. Empty ladder → 0.0,
                    # which the BSS evaluator's `if yes_ask <= 0` gate already
                    # treats as invalid (returns early without firing).
                    if new_bid_levels:
                        book.bid = new_bid_levels[0][0]
                        book.bid_size = new_bid_levels[0][1]
                    else:
                        book.bid = 0.0
                        book.bid_size = 0.0
                    if new_ask_levels:
                        book.ask = new_ask_levels[0][0]
                        book.ask_size = new_ask_levels[0][1]
                    else:
                        book.ask = 0.0
                        book.ask_size = 0.0
                    book.last_update_ts = now
                    # v6.5.1: bump last_book_snapshot_ts on price_change too.
                    # The ladder now reflects current state continuously, not
                    # just at full-snapshot intervals — depth_log book_age_s
                    # becomes meaningful as a freshness signal again.
                    book.last_book_snapshot_ts = now
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
# v6.5.3.2 SKULD: Speranța dashboard image (base64-embedded). Substituted
# into DASHBOARD_HTML at serve time via simple placeholder replacement.
# Source: AI-generated illustration "Speranța" (Romanian: "Hope") —
# 480×320 JPEG quality 78, ~28KB raw / ~37KB base64.
# ═══════════════════════════════════════════════════════════════════
_SPERANTA_DATA_URI = (
    "data:image/jpeg;base64,"
    "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"
)


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
/* v6.5.3.2: Skuld hero header — Speranța ship as background with dark glass overlay */
header.skuld-hero{position:relative;padding:24px 24px 20px;min-height:130px;border-bottom:1px solid var(--border);overflow:hidden;background-image:url({{SPERANTA_BG}});background-size:cover;background-position:center 55%;}
header.skuld-hero::before{content:"";position:absolute;inset:0;background:rgba(0,0,0,0.35);pointer-events:none;}
header.skuld-hero > *{position:relative;z-index:2;}
header.skuld-hero h1{color:#fff;text-shadow:0 1px 8px rgba(0,0,0,0.7);font-size:22px;}
header.skuld-hero .badge-version{background:rgba(240,192,128,0.18);color:#f0c080;border:1px solid rgba(240,192,128,0.4);text-shadow:0 1px 4px rgba(0,0,0,0.5);}
header.skuld-hero .badge-dry{background:rgba(122,165,210,0.22);color:#bcdcff;border:1px solid rgba(122,165,210,0.5);text-shadow:0 1px 4px rgba(0,0,0,0.5);}
header.skuld-hero .badge-live{background:rgba(248,81,73,0.22);color:#ffb0a8;border:1px solid rgba(248,81,73,0.55);text-shadow:0 1px 4px rgba(0,0,0,0.5);}
header.skuld-hero .uptime{color:#d8e0f0;text-shadow:0 1px 4px rgba(0,0,0,0.6);}
header.skuld-hero .skuld-tagline{display:block;width:100%;margin-top:4px;font-family:Georgia,"Times New Roman",serif;font-style:italic;font-size:14px;color:#f0c080;text-shadow:0 1px 6px rgba(0,0,0,0.7);}
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
<header class="skuld-hero"><h1 id="header-title">polybot simple v1</h1><span id="version-badge" class="badge badge-version">v—</span><span id="mode-badge" class="badge badge-dry">DRY</span><span id="strategy-badge" class="badge badge-bs" style="display:none">BOTH-SIDES</span><span id="variant-badge" class="badge badge-bs" style="display:none">v621</span><span id="tradingPausedBadge" style="background:#14532d;color:#fff;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700;margin-left:6px;">TRADING ACTIVE</span><button id="pauseTradingBtn" style="margin-left:6px;padding:3px 10px;border-radius:5px;border:1px solid var(--muted);background:rgba(255,255,255,.07);color:var(--text);cursor:pointer;font-size:11px;font-weight:600;">Pause trading</button><button id="resumeTradingBtn" style="display:none;margin-left:6px;padding:3px 10px;border-radius:5px;border:1px solid #f8c849;background:rgba(248,200,73,.12);color:#f8c849;cursor:pointer;font-size:11px;font-weight:600;">Resume trading</button><button id="purgeOldLogsBtn" style="margin-left:6px;padding:3px 10px;border-radius:5px;border:1px solid var(--muted);background:rgba(255,255,255,.07);color:var(--muted);cursor:pointer;font-size:11px;">Purge old logs</button><span class="uptime" id="uptime">uptime —</span><span class="skuld-tagline">Toate Pânzele Sus</span></header>
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
<div class="logs-title"><span>CSV logs</span><span id="logs-meta" style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0"></span></div>
<div class="logs-list" id="logs-list"><div class="trades-empty">loading…</div></div>
</div>
<div class="footer" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
<span>Polling /api/status every 1s · <a href="/api/status" target="_blank" style="color:var(--blue)">view JSON</a> · <a href="/api/datasets" target="_blank" style="color:var(--blue)">view datasets JSON</a></span>
<span style="display:flex;align-items:center;gap:10px;">
<span id="vol-usage" style="color:var(--muted);font-size:11px"></span>
<button id="wipe-btn" onclick="wipeVolume()" style="background:#c0392b;color:#fff;border:none;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-family:inherit;">Wipe old CSVs</button>
<span id="wipe-status" style="font-size:11px;color:var(--muted)"></span>
</span>
</div>
</main>
<script>
const $ = id => document.getElementById(id);
function fmtUptime(s){if(s==null)return '—';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=Math.floor(s%60);if(h)return `${h}h ${m}m ${sec}s`;if(m)return `${m}m ${sec}s`;return `${sec}s`;}
function fmtBytes(b){if(b==null)return '—';if(b<1024)return b+' B';if(b<1048576)return (b/1024).toFixed(1)+' KB';if(b<1073741824)return (b/1048576).toFixed(1)+' MB';return (b/1073741824).toFixed(2)+' GB';}
async function tickDatasets(){
try{
const r=await fetch('/api/datasets',{cache:'no-store'});
if(!r.ok)throw 0;
const d=await r.json();
const files=d.files||[];
const stats=d.writer_stats||{};
const list=$('logs-list');
if(!files.length){list.innerHTML='<div class="trades-empty">no log files yet</div>';$('logs-meta').textContent='';updateVolUsage(files);return;}
let totalRows=0,totalBytes=0;
const rowsHtml=files.map(f=>{
const wstats=stats[f.dataset]||{};
const rows=wstats.rows_written;
if(rows!=null)totalRows+=rows;
if(f.size_bytes)totalBytes+=f.size_bytes;
return `<div class="log-row">`
+`<span class="log-dataset">${f.dataset}</span>`
+`<span class="log-date">${f.date}</span>`
+`<span class="log-size">${fmtBytes(f.size_bytes)}</span>`
+`<span class="log-rows">${rows!=null?rows.toLocaleString()+' rows':'—'}</span>`
+`<a class="log-dl" href="/api/logs/download?name=${encodeURIComponent(f.filename)}" target="_blank" rel="noopener noreferrer">download</a>`
+`<button onclick="deleteLogFile(${JSON.stringify(f.filename)})" style="margin-left:6px;padding:2px 8px;border-radius:4px;border:1px solid #c0392b;background:rgba(192,57,43,.12);color:#e74c3c;cursor:pointer;font-size:10px;">delete</button>`
+`</div>`;
}).join('');
list.innerHTML=rowsHtml;
$('logs-meta').textContent=files.length+' file'+(files.length===1?'':'s')+(totalRows?' · '+totalRows.toLocaleString()+' rows total':'');
// Update volume usage display
const today=new Date().toISOString().slice(0,10);
const oldFiles=files.filter(f=>f.date&&f.date<today);
const oldBytes=oldFiles.reduce((s,f)=>s+(f.size_bytes||0),0);
const volEl=$('vol-usage');
if(volEl)volEl.textContent=fmtBytes(totalBytes)+' total'+(oldBytes>0?' · '+fmtBytes(oldBytes)+' old':'');
}catch(e){$('logs-list').innerHTML='<div class="trades-empty">error loading logs list</div>';}
}
async function wipeVolume(){
const btn=$('wipe-btn');const st=$('wipe-status');
// Preview first
const prev=await fetch('/api/cleanup',{cache:'no-store'});
const prevData=await prev.json();
const toDelete=prevData.would_delete||[];
if(!toDelete.length){st.textContent='Nothing to wipe (no old files)';st.style.color='var(--muted)';return;}
if(!confirm('Delete '+toDelete.length+' old CSV file(s)?\n\n'+toDelete.join('\n'))){st.textContent='Cancelled';return;}
btn.disabled=true;st.textContent='Wiping…';st.style.color='var(--muted)';
try{
const r=await fetch('/api/cleanup?confirm=true',{cache:'no-store'});
const d=await r.json();
if(d.deleted&&d.deleted.length){
  st.textContent='Deleted '+d.deleted.length+' file(s)';st.style.color='#2ecc71';
}else if(d.errors&&d.errors.length){
  st.textContent='Error: '+d.errors[0];st.style.color='#e74c3c';
}else{
  st.textContent='Nothing deleted';st.style.color='var(--muted)';
}
}catch(e){st.textContent='Request failed';st.style.color='#e74c3c';}
btn.disabled=false;
setTimeout(()=>{st.textContent='';},8000);
tickDatasets();
}
async function tick(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw 0;const s=await r.json();render(s);}catch(e){$('uptime').textContent='connection lost';}}
async function refreshStatus(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)return;render(await r.json());}catch(e){}}
async function refreshLogs(){try{await tickDatasets();}catch(e){}}
async function postJson(url){const r=await fetch(url,{method:'POST'});return await r.json();}
async function deleteLogFile(name){
  if(!window.confirm('Delete '+name+'?'))return;
  await fetch('/api/logs/delete?name='+encodeURIComponent(name),{method:'POST'});
  await refreshLogs();
}
(function(){
  const pb=document.getElementById('pauseTradingBtn');
  if(pb)pb.onclick=async()=>{await postJson('/api/pause_trading');await refreshStatus();};
  const rb=document.getElementById('resumeTradingBtn');
  if(rb)rb.onclick=async()=>{await postJson('/api/resume_trading');await refreshStatus();};
  const pg=document.getElementById('purgeOldLogsBtn');
  if(pg)pg.onclick=async()=>{const r=await postJson('/api/logs/purge_old');alert('Purged: '+(r.files_deleted||0)+' files, '+(r.bytes_freed||0)+' bytes');await refreshLogs();};
})();
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
(function(){
  const paused=!!s.trading_paused;
  const badge=document.getElementById('tradingPausedBadge');
  const pb=document.getElementById('pauseTradingBtn');
  const rb=document.getElementById('resumeTradingBtn');
  if(badge){badge.textContent=paused?'TRADING PAUSED':'TRADING ACTIVE';badge.style.background=paused?'#7f1d1d':'#14532d';}
  if(pb)pb.style.display=paused?'none':'';
  if(rb)rb.style.display=paused?'':'none';
})();
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
// v6.5.5: last 15 trades (was 10). Now includes ORPHAN_SOLD positions
// from the orphan-sell (positive-exit) and take-profit rules — these
// are leg-1-only closes that didn't reach resolution.
//   Schema per row (single line):
//     [pill] [sold|resolved · winner_badge]   [sparkline 100x24]   [+$X.XX BTC]   [pnl]
//   Color rule (sparkline + tinted bg):
//     outcome === 'ORPHAN_SOLD' (TP)        → cyan (opportunistic gain)
//     outcome === 'ORPHAN_SOLD' (POS_EXIT)  → orange (defensive exit)
//     outcome === 'LOSS'                    → yellow (attention)
//     market_winner === 'YES' (good outcome)→ green
//     market_winner === 'NO'  (good outcome)→ red
//     else                                  → muted gray
const hist=((s.bs_state&&s.bs_state.trade_history)||[]).slice(-15).reverse();
if(!hist.length){
  list.innerHTML='<div class="trades-empty" style="color:var(--muted);font-family:var(--mono);font-size:12px;padding:8px 0;">no resolved trades yet</div>';
  return;
}
const nowSec=Date.now()/1000;
list.innerHTML=hist.map(tr=>{
  // v6.5.5: ORPHAN_SOLD has a different rendering path — no sparkline,
  // shows entry → sold prices and sell reason instead.
  if(tr.outcome==='ORPHAN_SOLD'){
    const reason=tr.sell_reason||'positive_exit';
    const isTP=(reason==='take_profit');
    const lineCol=isTP?'#5cb8d9':'#d99340';  // cyan TP, orange positive-exit
    const pillCls=isTP?'up':'';
    const pillTxt=isTP?'TP':'P-EXIT';
    const reasonLabel=isTP
      ? `1.75× hit · ${(tr.tp_ratio||1.75).toFixed(2)}×`
      : `bid recovered · adv ${(tr.bin_adverse_bps||0).toFixed(0)}bps`;
    const pmLink=tr.market_url
      ? `<a href="${tr.market_url}" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:none;font-size:11px;margin-left:6px;" title="Open on Polymarket">↗</a>`
      : '';
    const pnl=tr.sell_pnl||tr.total_pnl||0;
    const pnlCls=pnl>0.0001?'up':pnl<-0.0001?'down':'';
    const pnlStr=(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
    const ageSec=tr.close_ts?(nowSec-tr.close_ts):null;
    const entryAsk=(tr.leg1_entry_ask||0).toFixed(3);
    const sellPx=(tr.sell_price||0).toFixed(3);
    const side=tr.leg1_side||'?';
    const hold=tr.hold_elapsed_s?` · ${tr.hold_elapsed_s.toFixed(0)}s held`:'';
    const priceDisplay=`<span style="color:var(--muted);font-size:10px;">${side} ${entryAsk}→${sellPx}${hold}</span>`;
    return `<div class="trade-row" style="display:grid;grid-template-columns:55px 130px 100px 80px 90px 24px;gap:10px;align-items:center;padding:6px 10px;background:transparent;border:none;border-bottom:0.5px solid var(--border-soft);border-radius:0;font-family:var(--mono);font-size:11px;">`
      +`<span><span class="trade-res ${pillCls}" style="background:${lineCol}22;color:${lineCol};border:1px solid ${lineCol}44;">${pillTxt}</span></span>`
      +`<span style="color:var(--muted);font-size:10px;">orphan-sold · ${reason==='take_profit'?'TP':'pos-exit'}</span>`
      +priceDisplay
      +`<span style="color:var(--muted);font-size:10px;text-align:right;">${reasonLabel}</span>`
      +`<span class="${pnlCls}" style="text-align:right;font-weight:600;">${pnlStr} <span style="color:var(--muted);font-weight:400;">· ${fmtAgeShort(ageSec)}</span></span>`
      +`<span style="text-align:center;">${pmLink}</span>`
      +`</div>`;
  }
  // Standard paired-trade rendering (preserved from v6.1.9)
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
      ${p.bss_state==='WAITING_2ND' && (p.orphan_sell_enabled || p.orphan_tp_enabled)?(()=>{
        // v6.5.5.3: in-flight orphan-sell indicator
        const pnlNow = p.orphan_sell_pnl_now||0;
        const tpRatio = p.orphan_tp_ratio_now||0;
        const tpThr = p.orphan_tp_ratio_thr||1.75;
        // Positive-exit run progress
        const peRun = p.orphan_sell_run_s||0;
        const peSustain = p.orphan_sell_sustain_s||6;
        const pePct = Math.min(100, Math.round(peRun/peSustain*100));
        // TP run progress
        const tpRun = p.orphan_tp_run_s||0;
        const tpSustain = p.orphan_tp_sustain_s||3;
        const tpPct = Math.min(100, Math.round(tpRun/tpSustain*100));
        // Status text + color
        const peArmed = peRun > 0;
        const tpArmed = tpRun > 0;
        const willFire = (peArmed && peRun >= peSustain) || (tpArmed && tpRun >= tpSustain);
        const statusColor = willFire ? 'var(--red)' : (peArmed || tpArmed ? 'var(--yellow)' : 'var(--muted)');
        const statusText = willFire ? '⚡ SELL ARMED' : (peArmed || tpArmed ? '◐ approaching sell' : 'monitoring');
        const bidStr = (p.leg1_top_bid||0).toFixed(4);
        const entryStr = (p.leg1_entry_ask||0).toFixed(4);
        return `<div style="margin-top:6px;padding:8px;background:rgba(255,255,255,0.03);border-radius:4px;border-left:2px solid ${statusColor};font-size:11px;">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <span style="color:${statusColor};font-weight:600;">${statusText}</span>
            <span style="color:var(--muted);">bid ${bidStr} · entry ${entryStr} · would-sell <b style="color:${pnlNow>=0?'var(--green)':'var(--red)'};">${pnlNow>=0?'+':''}$${pnlNow.toFixed(4)}</b></span>
          </div>
          ${p.orphan_sell_enabled?`<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px;">
            <span style="min-width:80px;color:var(--muted);">positive-exit</span>
            <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;">
              <div style="width:${pePct}%;height:100%;background:${peRun>=peSustain?'var(--red)':'var(--yellow)'};transition:width 0.3s;"></div>
            </div>
            <span style="min-width:60px;text-align:right;color:var(--muted);">${peRun.toFixed(1)}/${peSustain.toFixed(0)}s</span>
          </div>`:''}
          ${p.orphan_tp_enabled?`<div style="display:flex;align-items:center;gap:8px;">
            <span style="min-width:80px;color:var(--muted);">take-profit</span>
            <div style="flex:1;height:6px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;">
              <div style="width:${tpPct}%;height:100%;background:${tpRun>=tpSustain?'var(--red)':'var(--cyan,#3b82f6)'};transition:width 0.3s;"></div>
            </div>
            <span style="min-width:60px;text-align:right;color:var(--muted);">ratio ${tpRatio.toFixed(2)}/${tpThr.toFixed(2)} · ${tpRun.toFixed(1)}/${tpSustain.toFixed(0)}s</span>
          </div>`:''}
        </div>`;
      })():''}
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
  // ACTIVE TRADE (Design 3 chart) — first item of watching
  if(watching.length){
    const active=watching[0];
    cards.push(active.chart_active?renderActiveChart(active):`<div class="bs-pos" style="border-left:3px solid var(--muted);padding:8px 12px;font-size:12px;color:var(--muted);">${escapeHtml((active.market_id||'').slice(0,12))}… · ${escapeHtml(active.bss_state)} (no chart data yet)</div>`);
    // OTHER WATCHING — compact rows. v6.3.4: skip pre-market WATCH
    // items (clutter, not actionable). Keep WAITING_2ND + live-WATCH.
    // is_pre_market: ttr_s > 300 (window hasn't opened yet)
    if(watching.length>1){
      const others=watching.slice(1);
      const visible=others.filter(p=>{
        const isPreWatch = p.bss_state==='WATCH' && p.ttr_s>300;
        return !isPreWatch;
      });
      const hiddenPre=others.length-visible.length;
      const labelParts=[];
      if(visible.length) labelParts.push(`${visible.length} other watching`);
      if(hiddenPre) labelParts.push(`+${hiddenPre} pre-market hidden`);
      if(labelParts.length){
        cards.push(`<div style="font-size:10px;color:var(--muted);margin-top:8px;text-transform:uppercase;letter-spacing:0.5px;">${labelParts.join(' · ')}</div>`);
      }
      visible.forEach(p=>cards.push(renderCompact(p)));
    }
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
  if(!cards.length){list.innerHTML='<div class="bs-empty">BSS active · no markets in window yet</div>';return;}
  list.innerHTML=cards.join('');
  return;
}
if(!positions.length){list.innerHTML='<div class="bs-empty">no open both-sides positions</div>';return;}
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
    +`</div></div>`;
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
                "t_first": _BS_BSS_T_FIRST,
                "sustain_first_s": _BS_BSS_SUSTAIN_FIRST_S,
            }
            if mdm.bss_state == "WAITING_2ND":
                elapsed = now - (mdm.bss_first_fill_ts or now)
                # v6.3.5: pre-market-aware phase determination. If we're
                # v6.4.0: live-window only — no pre-market branch
                window_open_ts = mdm.market.end_ts - mdm.duration_s
                in_pre_market_now = False  # always false in v6.4.0
                other_ask = na if mdm.bss_first_side == "YES" else ya
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
                    "pre_market_remaining_s": None,
                })

                # ── v6.5.5.3 IN-FLIGHT ORPHAN-SELL INDICATOR ────────
                # Surfaces both rule states so the dashboard can show
                # "this orphan is about to be sold" before it fires.
                # Computes the same fields the orphan-sell evaluator
                # uses, without ever firing (read-only snapshot).
                leg1_side = mdm.bss_first_side
                leg1_book = ((yb if leg1_side == "YES" else nb)
                              if leg1_side else None)
                leg1_top_bid = (float(leg1_book.bid)
                                if leg1_book is not None else 0.0)
                leg1_qty = mdm.bss_leg1_qty or 0.0
                leg1_size = mdm.bss_leg1_size_usdc or 1.0
                leg1_fee_paid = mdm.bss_leg1_fee or 0.0
                leg1_entry_ask = mdm.bss_leg1_actual_ask or 0.0

                # Current would-sell pnl (cashout convention)
                if leg1_top_bid and leg1_qty:
                    _sell_fee = _polymarket_taker_fee(
                        leg1_qty, leg1_top_bid)
                    sell_pnl_now = (leg1_qty * leg1_top_bid - _sell_fee
                                     - leg1_size - leg1_fee_paid)
                else:
                    sell_pnl_now = 0.0

                # Current TP ratio (vs entry ask)
                tp_ratio_now = ((leg1_top_bid / leg1_entry_ask)
                                 if leg1_entry_ask > 0 else 0.0)

                # In-flight band-sustain progress (timestamps live on mdm,
                # updated by the orphan-sell evaluator each shadow tick)
                pe_first = mdm.bss_orphan_sell_first_qual_ts
                tp_first = mdm.bss_orphan_tp_first_qual_ts
                pe_run = (now - pe_first) if pe_first else 0.0
                tp_run = (now - tp_first) if tp_first else 0.0

                entry.update({
                    "orphan_sell_enabled": _BS_BSS_ORPHAN_SELL_ENABLED,
                    "orphan_sell_pnl_now": round(sell_pnl_now, 4),
                    "orphan_sell_run_s": round(pe_run, 1),
                    "orphan_sell_sustain_s": _BS_BSS_ORPHAN_SELL_SUSTAIN_S,
                    "orphan_sell_min_pnl": _BS_BSS_ORPHAN_SELL_MIN_PNL,
                    "orphan_sell_min_elapsed_s": _BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S,
                    "orphan_tp_enabled": _BS_BSS_ORPHAN_TP_ENABLED,
                    "orphan_tp_ratio_now": round(tp_ratio_now, 3),
                    "orphan_tp_ratio_thr": _BS_BSS_ORPHAN_TP_RATIO,
                    "orphan_tp_run_s": round(tp_run, 1),
                    "orphan_tp_sustain_s": _BS_BSS_ORPHAN_TP_SUSTAIN_S,
                    "leg1_top_bid": round(leg1_top_bid, 4) if leg1_top_bid else 0.0,
                    "leg1_entry_ask": round(leg1_entry_ask, 4),
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
                bss_watching[0]["first_filled_in_pre"] = False  # v6.4.0: always False

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
        # v6.2.2: strategy variant
        "bs_strategy": _BS_STRATEGY,
        "trading_paused": bool(getattr(state, "trading_paused", False)),
        "bot_name": _BOT_NAME,
        "log_retention_days": _LOG_RETENTION_DAYS,
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


CSV_EXPORT_FILES = [
    ("planned_entries.csv", "planned_entries.csv"),
    ("executed_trades.csv", "executed_trades.csv"),
    ("open_positions.csv", "open_positions.csv"),
    ("exits.csv", "exits.csv"),
]

def _ensure_validation_csvs(state):
    try:
        os.makedirs(state.log_dir, exist_ok=True)
        headers = {
            "planned_entries.csv": "ts,market_slug,question,yes_ask,no_ask,sum_ask,eligible,entry_window_s,polymarket_url\n",
            "executed_trades.csv": "ts,market_slug,question,action,side,price,size,reason,polymarket_url\n",
            "open_positions.csv": "ts,market_slug,question,yes_entry,no_entry,size_usdc,status,polymarket_url\n",
            "exits.csv": "ts,market_slug,question,side_sold,exit_price,reason,realized_pnl,polymarket_url\n",
        }
        for fname, header in headers.items():
            fpath = os.path.join(state.log_dir, fname)
            if not os.path.exists(fpath):
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(header)
    except Exception as e:
        print(f"[csv] init failed: {type(e).__name__}: {e}", flush=True)


def http_server_thread(state: BotState) -> None:
    cfg = state.config

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                # v6.5.3.2: substitute Speranța placeholder with embedded data URI
                body = DASHBOARD_HTML.replace(
                    "{{SPERANTA_BG}}", _SPERANTA_DATA_URI
                ).encode("utf-8")
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

            # v6.5.4: human-triggered CSV cleanup. Deletes CSV files older
            # than the current UTC day from state.log_dir. Requires explicit
            # ?confirm=true to prevent accidental triggers. Today's actively-
            # written files are NEVER deleted regardless of confirm.
            if path == "/api/cleanup" or path == "/api/cleanup/":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                confirm_ok = "confirm=true" in qs
                today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                deleted: List[str] = []
                preview: List[str] = []
                errors: List[str] = []
                if not state.log_dir:
                    body = json.dumps({
                        "ok": False, "error": "log_dir not configured",
                    }).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    log_path = Path(state.log_dir)
                    for f in sorted(log_path.glob("*.csv")):
                        # File name must include a date stamp YYYY-MM-DD.
                        m = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", f.name)
                        if not m:
                            continue
                        file_date = m.group(1)
                        if file_date >= today_utc:
                            continue  # never touch today's active files
                        if confirm_ok:
                            try:
                                f.unlink()
                                deleted.append(f.name)
                            except Exception as e:
                                errors.append(f"{f.name}: {type(e).__name__}: {e}")
                        else:
                            preview.append(f.name)
                except Exception as e:
                    errors.append(f"scan: {type(e).__name__}: {e}")
                payload = {
                    "ok": len(errors) == 0,
                    "confirm_required": not confirm_ok,
                    "today_utc": today_utc,
                    "deleted": deleted,
                    "would_delete": preview,
                    "errors": errors,
                    "hint": ("call /api/cleanup?confirm=true to actually delete"
                             if not confirm_ok else None),
                }
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
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

            if path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return

            # GET /api/logs/download?name=filename.csv
            if path == "/api/logs/download":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                lp = _safe_log_file_path(state, name) if state.log_dir else None
                if lp is None:
                    body = json.dumps({"ok": False, "error": "file_not_found"}).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                data = lp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{lp.name}"')
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found\n")

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed_url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed_url.query)
            p = parsed_url.path

            if p == "/api/pause_trading":
                _set_trading_paused(state, True)
                self._send_json({"ok": True, "trading_paused": True})
                print("[dashboard] trading PAUSED — new entries blocked", flush=True)
                return

            if p == "/api/resume_trading":
                _set_trading_paused(state, False)
                self._send_json({"ok": True, "trading_paused": False})
                print("[dashboard] trading RESUMED — new entries allowed", flush=True)
                return

            if p == "/api/logs/delete":
                name = (qs.get("name") or [""])[0]
                lp = _safe_log_file_path(state, name) if state.log_dir else None
                if lp is None:
                    self._send_json({"ok": False, "error": "file_not_found"}, status=404)
                    return
                try:
                    lp.unlink()
                    self._send_json({"ok": True, "deleted": lp.name})
                except Exception as e:
                    self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
                return

            if p == "/api/logs/purge_old":
                if not state.log_dir:
                    self._send_json({"ok": False, "error": "log_dir not configured"}, status=400)
                    return
                self._send_json(_manual_purge_old_logs(state))
                return

            self.send_response(404)
            self.end_headers()

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
                if _entry_blocked_by_pause(state, "lag_signal"):
                    return
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
    if _entry_blocked_by_pause(state, "both_sides"):
        return (False, _trading_pause_reason(state), 0.0, 0.0, 0.0)
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


def _same_side_drop(mdm, fire_side: str, now: float,
                     lookback_s: float) -> Optional[float]:
    """v6.5.8: returns the absolute price drop of the SAME side as fire_side
    over the last `lookback_s` seconds. Used by patient first-leg logic to
    detect if the leg1 side is still actively falling — if so, wait for a
    better fill rather than firing immediately at sustain completion.

    Args:
      mdm        : MultiDurationMarket — has bss_price_samples (1Hz ring buffer)
      fire_side  : "YES" or "NO" (the side we're about to buy as leg1)
      now        : current ts
      lookback_s : how far back to look

    Returns:
      Absolute drop = price_old - price_new
      Positive = price has FALLEN (still sliding → wait for better entry)
      Negative = price has RISEN (stabilised / bounced → safe to fire)
      None if insufficient samples
    """
    samples = list(mdm.bss_price_samples)
    if len(samples) < 3:
        return None
    target_old_ts = now - lookback_s
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
    # Same side as leg1 — check if it's still falling
    if fire_side == "YES":
        old_p, new_p = best_old_yes, best_new_yes
    else:
        old_p, new_p = best_old_no, best_new_no
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


# ──────────────────────────────────────────────────────────────────────────
# v6.5.11 — Tiered exit ladder: helpers + evaluator
# ──────────────────────────────────────────────────────────────────────────
# See the constants block near BOT_VERSION for design rationale.
# Operator-chosen Option E: pure-numbers only, ~6 cats/100 trades floor.
# Helpers walk pos.tier_ask_history (a list of (ts, yes_ask, no_ask) tuples)
# which is updated and trimmed at the top of _bs_evaluate_sell_loser_tiered.

def _bs_tier_match(ttr: float, winner_ask: float) -> Tuple[Optional[str], Optional[float]]:
    """Return (tier_label, tier_threshold) for the most-specific tier
    whose window contains TTR and whose price bar is met. None if no match.
    Most-specific = lowest TTR window (T3 > T2 > T1). T0 is the any-time
    override that fires only if no narrower tier matched.
    """
    if ttr <= _BS_TIER_T3_TTR and winner_ask >= _BS_TIER_T3_WINNER:
        return "T3", _BS_TIER_T3_WINNER
    if ttr <= _BS_TIER_T2_TTR and winner_ask >= _BS_TIER_T2_WINNER:
        return "T2", _BS_TIER_T2_WINNER
    if ttr <= _BS_TIER_T1_TTR and winner_ask >= _BS_TIER_T1_WINNER:
        return "T1", _BS_TIER_T1_WINNER
    if winner_ask >= _BS_TIER_T0_WINNER:
        return "T0", _BS_TIER_T0_WINNER
    return None, None


def _bs_tier_detect_swing(history: List[Tuple[float, float, float]],
                          winner_side: str, now: float) -> bool:
    """Detect a V-shape swing on the winner side over the last
    BS_TIER_SWING_WINDOW_S seconds.

    A swing is a peak → trough → recovery pattern where:
      drawdown (peak - trough) >= BS_TIER_SWING_DRAWDOWN
      bounce   (recovery - trough) >= BS_TIER_SWING_BOUNCE

    Returns True if a swing is detected (guard should consider winner unstable).
    """
    cutoff = now - _BS_TIER_SWING_WINDOW_S
    window = [t for t in history if t[0] >= cutoff]
    if len(window) < 3:
        return False
    asks = [w[1] if winner_side == "YES" else w[2] for w in window]
    # Find the peak (highest point in the window)
    peak_idx = max(range(len(asks)), key=lambda i: asks[i])
    if peak_idx >= len(asks) - 1:
        return False  # peak is at the end → no after-peak data
    after_peak = asks[peak_idx:]
    # Find the trough after the peak
    trough_off = min(range(len(after_peak)), key=lambda i: after_peak[i])
    if trough_off >= len(after_peak) - 1:
        return False  # trough is at the end → no recovery data
    # Recovery is the max after the trough
    recovery = max(after_peak[trough_off:])
    drawdown = asks[peak_idx] - after_peak[trough_off]
    bounce = recovery - after_peak[trough_off]
    return drawdown >= _BS_TIER_SWING_DRAWDOWN and bounce >= _BS_TIER_SWING_BOUNCE


def _bs_tier_no_dip(history: List[Tuple[float, float, float]],
                    winner_side: str, now: float) -> bool:
    """Return True if winner_ask never dipped below BS_TIER_DIP_FLOOR
    in the last BS_TIER_DIP_WINDOW_S seconds.

    Conservative-True on empty history: no observed dip = no dip. Callers
    use this in a disjunction with no_swing for T1/T2/T3 (so "True from
    no data" doesn't bypass safety — it just lets no_swing decide).
    """
    cutoff = now - _BS_TIER_DIP_WINDOW_S
    window = [t for t in history if t[0] >= cutoff]
    if not window:
        return True
    asks = [w[1] if winner_side == "YES" else w[2] for w in window]
    return min(asks) >= _BS_TIER_DIP_FLOOR


def _bs_tier_sustained_above(history: List[Tuple[float, float, float]],
                              winner_side: str, threshold: float,
                              sustain_s: float, now: float) -> bool:
    """Return True if winner_ask has been >= `threshold` for at least
    `sustain_s` continuous seconds leading up to `now`.

    Walks the history (sorted by ts) backwards; finds the most recent tick
    where ask was < threshold. If that's more than sustain_s ago, we've
    been above for the required time. If we never saw a below-threshold
    tick in our history, we need at least sustain_s of total history to
    qualify.
    """
    if sustain_s <= 0:
        return True
    asks_ts = sorted(
        ((t[0], t[1] if winner_side == "YES" else t[2]) for t in history),
        key=lambda x: x[0],
    )
    if not asks_ts:
        return False
    # Walk back from the most recent tick
    for ts, ask in reversed(asks_ts):
        if ask < threshold:
            return (now - ts) >= sustain_s
    # Never below threshold in our history → need enough total span
    return (now - asks_ts[0][0]) >= sustain_s


def _bs_evaluate_sell_loser_tiered(state: BotState, pos: BothSidesPosition,
                                    now: float) -> Tuple[bool, str, str, float, float]:
    """v6.5.11 tiered exit evaluator (pure-numbers, no BTC fundamentals).

    Returns (should_sell, reason, loser_side, loser_bid, winner_ask) with
    the same shape as _bs_evaluate_sell_loser_legacy so callers don't
    change. `reason` encodes the fire tier on success
    (e.g. "fire_T2_LADDER") and a diagnostic string on rejection.

    Tier ladder (winner_ask thresholds widen as TTR shrinks — lower
    reversal risk as we approach resolution):
      T0  any TTR + ≥0.96 + extra guards (TTR≤200s, sustained ≥0.94/30s, AND-guard)
      T1  TTR ≤ 120s + ≥0.90
      T2  TTR ≤ 60s  + ≥0.87
      T3  TTR ≤ 30s  + ≥0.80
    All tiers require winner_ask sustained at tier_threshold for
    BS_TIER_PERSIST_S seconds (default 5s). T1/T2/T3 standard guard is
    no_swing OR no_dip; T0 requires AND of both plus the sustain check.
    """
    # 1. Book + freshness
    yes_book = state.poly_books.get(pos.yes_leg.token_id)
    no_book = state.poly_books.get(pos.no_leg.token_id)
    if yes_book is None or no_book is None:
        pos.tier_last_eval_status = "no_book"
        return False, "no_book", "", 0.0, 0.0
    book_age_max = max(now - yes_book.last_update_ts,
                       now - no_book.last_update_ts)
    if book_age_max > 30.0:
        pos.tier_last_eval_status = f"book_stale:{book_age_max:.0f}s"
        return False, f"book_stale:{book_age_max:.0f}s", "", 0.0, 0.0

    yes_ask = float(yes_book.ask)
    no_ask = float(no_book.ask)
    yes_bid = float(yes_book.bid)
    no_bid = float(no_book.bid)

    # 2. Locked-spread reject (carry over from v6.5.5.2 — anti-ghost guard
    # for stale frozen orderbook snapshots that occasionally show ask==bid).
    if yes_ask <= 0 or no_ask <= 0:
        pos.tier_last_eval_status = "asks_zero"
        return False, "asks_zero", "", 0.0, 0.0
    if yes_ask == yes_bid or no_ask == no_bid:
        pos.tier_last_eval_status = "locked_spread"
        return False, "locked_spread", "", 0.0, 0.0

    # 3. Update ask history and trim
    pos.tier_ask_history.append((now, yes_ask, no_ask))
    cutoff_history = now - _BS_TIER_HISTORY_MAX_S
    pos.tier_ask_history = [t for t in pos.tier_ask_history if t[0] >= cutoff_history]

    # 4. Identify winner side (higher ask = closer to $1)
    if yes_ask >= no_ask:
        winner_side, winner_ask = "YES", yes_ask
        loser_side, loser_bid = "NO", no_bid
    else:
        winner_side, winner_ask = "NO", no_ask
        loser_side, loser_bid = "YES", yes_bid

    # 5. Tier match
    ttr = pos.end_ts - now
    tier, tier_threshold = _bs_tier_match(ttr, winner_ask)
    if tier is None:
        pos.tier_last_eval_status = f"no_tier:ttr={ttr:.0f},wa={winner_ask:.3f}"
        return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    # 6. Persistence at tier threshold
    if _BS_TIER_PERSIST_S > 0 and not _bs_tier_sustained_above(
            pos.tier_ask_history, winner_side, tier_threshold,
            _BS_TIER_PERSIST_S, now):
        pos.tier_last_eval_status = f"{tier}_not_persisted_{_BS_TIER_PERSIST_S:.0f}s"
        return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    # 7. Guards
    no_swing = not _bs_tier_detect_swing(pos.tier_ask_history, winner_side, now)
    no_dip = _bs_tier_no_dip(pos.tier_ask_history, winner_side, now)

    if tier == "T0":
        # Strict guard: TTR ≤ T0_MAX_TTR + sustained ≥ T0_SUSTAIN_THRESH for T0_SUSTAIN_S + AND-guard
        if ttr > _BS_TIER_T0_MAX_TTR:
            pos.tier_last_eval_status = (
                f"T0_ttr_too_high:{ttr:.0f}>{_BS_TIER_T0_MAX_TTR:.0f}")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
        if not _bs_tier_sustained_above(pos.tier_ask_history, winner_side,
                                          _BS_TIER_T0_SUSTAIN_THRESH,
                                          _BS_TIER_T0_SUSTAIN_S, now):
            pos.tier_last_eval_status = (
                f"T0_not_sustained_at_{_BS_TIER_T0_SUSTAIN_THRESH:.2f}_for_"
                f"{_BS_TIER_T0_SUSTAIN_S:.0f}s")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
        if not (no_swing and no_dip):
            pos.tier_last_eval_status = (
                f"T0_AND_guard_fail:no_swing={no_swing},no_dip={no_dip}")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask
    else:
        # T1/T2/T3 OR-guard
        if not (no_swing or no_dip):
            pos.tier_last_eval_status = (
                f"{tier}_OR_guard_fail:no_swing={no_swing},no_dip={no_dip}")
            return False, pos.tier_last_eval_status, loser_side, loser_bid, winner_ask

    # 8. All guards pass — fire
    pos.identified_loser_side = loser_side
    pos.fire_tier = tier
    pos.tier_last_eval_status = f"fire_{tier}_LADDER"
    return True, f"fire_{tier}_LADDER", loser_side, loser_bid, winner_ask


def _bs_evaluate_sell_loser(state: BotState, pos: BothSidesPosition,
                             now: float) -> Tuple[bool, str, str, float, float]:
    """v6.5.11 dispatcher. Routes to tiered (default) or legacy based on
    the BS_TIER_ENABLED env flag. Existing main-loop callers don't change."""
    if _BS_TIER_ENABLED:
        return _bs_evaluate_sell_loser_tiered(state, pos, now)
    return _bs_evaluate_sell_loser_legacy(state, pos, now)


def _bs_evaluate_sell_loser_legacy(state: BotState, pos: BothSidesPosition,
                              now: float) -> Tuple[bool, str, str, float, float]:
    """LEGACY v6.5.10 single-tier evaluator. Kept callable when
    BS_TIER_ENABLED=false. The new tiered evaluator (v6.5.11) is the
    default; see _bs_evaluate_sell_loser_tiered below.

    Check the four sell-loser preconditions. Returns:
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
    if mdm.bss_state in ("BOTH", "ABORT", "RESOLVED", "ORPHAN_END", "ORPHAN_SOLD", "ORPHAN_SOLD_PARTIAL"):
        _diag("terminal_state")
        return

    # Idempotency guard: if a position already exists for this market
    # (e.g. created by BSS earlier or by a stale entry path), don't
    # double-enter.
    if market.condition_id in state.both_sides_positions:
        _diag("idempotency_guard_position_exists")
        if mdm.bss_state == "WAITING_2ND":
            mdm.bss_state = "BOTH"
            _v653_buf_clear(market.condition_id)  # v6.5.3
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

    # v6.5.3: Tier 1 logging — append tick to per-market ring buffer.
    # Used by _v653_compute_features at fire/candidate time. Never raises.
    _v653_buf_append(
        market_id=market.condition_id,
        ts_ms=int(now * 1000),
        yes_ask=yes_ask, no_ask=no_ask,
        yes_bid=yes_bid, no_bid=no_bid,
        yes_ask_depth5=_v653_ask_depth_5(yb),
        no_ask_depth5=_v653_ask_depth_5(nb),
    )

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
    # v6.5.0: when market ends with leg 1 still held (WAITING_2ND/HALF),
    # transition to ORPHAN_END. The held leg becomes a single-leg position
    # held to CTF resolution. No sell, no fake P&L.
    #
    # v6.5.5.3: this MUST run before any other gate. When a market ends,
    # the orderbook naturally locks (trading stops; book freezes at the
    # resolution price). The locked-spread reject below would otherwise
    # catch this and return early — leaving orphans silently abandoned,
    # never settled, never logged as losses. v6.5.5.2 had this bug:
    # 14/46 orphans on May 20 were dropped without settlement, hiding
    # $14 of real losses behind a fake +$10.53 P&L.
    if now >= market.end_ts:
        # v6.5.6: ORPHAN_SOLD_PARTIAL means a LIVE FAK partially filled
        # the orphan-sell — some shares were sold (P&L already booked),
        # some remain in the wallet awaiting natural CTF resolution.
        # _bss_handle_window_end_orphan uses mdm.bss_leg1_qty which was
        # reduced to the remaining shares at partial-fill time.
        if mdm.bss_state in ("WAITING_2ND", "ORPHAN_SOLD_PARTIAL"):
            _bss_handle_window_end_orphan(state, mdm, now,
                                            yes_ask, no_ask, yes_bid, no_bid)
        return

    # ── v6.5.5.2 LOCKED-SPREAD REJECT (defensive on entry) ────────────
    # Same stale-book detection used for orphan-sell. Real Polymarket
    # orderbooks always have at least 1¢ spread; zero spread means our
    # in-memory book is on a phantom snapshot from a missed websocket
    # update. Skip entry decisions on such ticks — they would fire on
    # prices that don't exist in the real market. The next valid update
    # will recover the book and the evaluator will resume normally.
    #
    # v6.5.5.3: moved AFTER resolution gate. See above for rationale.
    if _bs_is_book_locked(yes_ask, no_ask, yes_bid, no_bid):
        return

    # v6.4.0 SKULD: live-window-only. Skip everything before window opens.
    # The 5m market exists for ~30 min before window open. T=0 of the
    # live window is at (end_ts - duration_s) = end_ts - 300 for 5m.
    # Pre-market activity (watching, detecting, logging) is removed — bot
    # only acts once trading is live. This eliminates the pre-trade
    # orphan path that would otherwise dominate the abort population.
    window_open_ts = market.end_ts - mdm.duration_s
    if now < window_open_ts:
        return  # pre-market: don't watch, don't fire, don't log

    t_first = _BS_BSS_T_FIRST
    sustain_first_s = _BS_BSS_SUSTAIN_FIRST_S

    # ── WATCH: first-leg sustain detection (live-window only) ──
    if mdm.bss_state == "WATCH":
        # ── v6.5.2: leg-1 entry filter ──
        # Skip late-window entries. 4-day data analysis showed entries
        # with TTR<240s have ~28-50% orphan rate vs ~9-10% at TTR>=240s
        # (winsorized). Tunable via BS_BSS_MIN_TTR_AT_LEG1_S env var.
        if _BS_BSS_MIN_TTR_AT_LEG1_S > 0:
            ttr_s = market.end_ts - now
            if ttr_s < _BS_BSS_MIN_TTR_AT_LEG1_S:
                # Clear any streaks that may have started — they're
                # invalidated by the gate. Safe to reset because fire
                # timing is symmetric and a fresh streak post-gate is
                # impossible anyway (TTR can only decrease).
                mdm.bss_yes_below_first_start_ts = None
                mdm.bss_no_below_first_start_ts = None
                _diag("v6.5.2_ttr_gate", ttr_s=f"{ttr_s:.0f}",
                      floor_s=f"{_BS_BSS_MIN_TTR_AT_LEG1_S:.0f}")
                return
        yes_st_field = 'bss_yes_below_first_start_ts'
        no_st_field  = 'bss_no_below_first_start_ts'
        # YES streak
        if yes_ask < t_first:
            if getattr(mdm, yes_st_field) is None:
                setattr(mdm, yes_st_field, now)
                mdm.bss_yes_leg1_low = yes_ask  # v6.5.11: init low at streak start
                # v6.5.3: log candidate detection (streak start). Selection-
                # bias fix: gives analysis the "could have fired" population
                # vs only the "did fire" rows.
                _bs_log_bss_candidate_event(state, mdm, now,
                                             side="YES", ask_price=yes_ask,
                                             yes_ask=yes_ask, no_ask=no_ask,
                                             yes_bid=yes_bid, no_bid=no_bid)
            else:
                # v6.5.11: update running low throughout streak
                if mdm.bss_yes_leg1_low is None or yes_ask < mdm.bss_yes_leg1_low:
                    mdm.bss_yes_leg1_low = yes_ask
        else:
            setattr(mdm, yes_st_field, None)
            mdm.bss_yes_leg1_low = None  # v6.5.11: reset low when streak breaks
        # NO streak
        if no_ask < t_first:
            if getattr(mdm, no_st_field) is None:
                setattr(mdm, no_st_field, now)
                mdm.bss_no_leg1_low = no_ask  # v6.5.11: init low at streak start
                # v6.5.3: log candidate detection (streak start).
                _bs_log_bss_candidate_event(state, mdm, now,
                                             side="NO", ask_price=no_ask,
                                             yes_ask=yes_ask, no_ask=no_ask,
                                             yes_bid=yes_bid, no_bid=no_bid)
            else:
                # v6.5.11: update running low throughout streak
                if mdm.bss_no_leg1_low is None or no_ask < mdm.bss_no_leg1_low:
                    mdm.bss_no_leg1_low = no_ask
        else:
            setattr(mdm, no_st_field, None)
            mdm.bss_no_leg1_low = None  # v6.5.11: reset low when streak breaks
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
            # v6.3.6: BTC-velocity filter to defend against buying mid-trend.
            if _BS_BSS_BTC_VEL_FILTER_PCT > 0.0:
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
                              f"(moving with side; skip)", flush=True)
                        return
            # v6.5.8: LEG1 PATIENCE CHECK.
            # If the leg1 side is still actively falling, hold one tick —
            # we'll get a better entry price (more shares, better wins,
            # smaller orphan RS loss). Uses same ring-buffer logic as leg2.
            # Data: 43% of paired trades saw leg1 price drop further after
            # entry (avg 11.4¢). Better entry at 0.33 vs 0.40 nearly halves
            # orphan RS loss and adds ~$0.18/win on paired trades.
            if _BS_BSS_LEG1_PATIENT_DROP > 0:
                same_drop = _same_side_drop(mdm, fire_side, now,
                                             _BS_BSS_OPP_VEL_LOOKBACK_S)
                if (same_drop is not None
                        and same_drop >= _BS_BSS_LEG1_PATIENT_DROP):
                    print(f"[bss_entry] FIRST_LEG_PATIENT "
                          f"market={market.condition_id[:10]}… "
                          f"side={fire_side} ask={fire_price:.3f} "
                          f"drop_{int(_BS_BSS_OPP_VEL_LOOKBACK_S)}s={same_drop:+.4f} "
                          f"(still falling; wait for better entry)", flush=True)
                    return
            # v6.5.11: LEG1 MAX_BOUNCE CHECK.
            # Don't fire if current price has bounced more than
            # BS_BSS_LEG1_MAX_BOUNCE above the running low seen during this
            # sustain streak. Waits for price to re-dip close to the bottom
            # rather than buying mid-bounce.
            if _BS_BSS_LEG1_MAX_BOUNCE > 0:
                _low_field = "bss_yes_leg1_low" if fire_side == "YES" else "bss_no_leg1_low"
                _leg1_low = getattr(mdm, _low_field, None)
                if _leg1_low is not None and fire_price > _leg1_low + _BS_BSS_LEG1_MAX_BOUNCE:
                    print(f"[bss_entry] FIRST_LEG_BOUNCE_WAIT "
                          f"market={market.condition_id[:10]}… "
                          f"side={fire_side} ask={fire_price:.3f} "
                          f"low={_leg1_low:.3f} "
                          f"bounce={fire_price - _leg1_low:.3f}>{_BS_BSS_LEG1_MAX_BOUNCE:.3f} "
                          f"(bounced too far from low; waiting for re-dip)", flush=True)
                    return
            mdm.bss_first_side = fire_side
            mdm.bss_first_price = fire_price
            mdm.bss_first_fill_ts = now
            _bs_log_bss_first_leg_event(state, mdm, now,
                                         yes_ask, no_ask, yes_bid, no_bid,
                                         sus_s=sus_s_at_fire)
            if _entry_blocked_by_pause(state, "bss_leg1"):
                return
            _bss_place_leg1(state, mdm, now,
                             fire_side=fire_side,
                             decision_ask=fire_price,
                             yes_ask=yes_ask, no_ask=no_ask,
                             yes_bid=yes_bid, no_bid=no_bid,
                             sus_s=sus_s_at_fire)
        return

    # ── WAITING_2ND: second-leg sustain detection (v6.5.0: no abort gate) ──
    if mdm.bss_state == "WAITING_2ND":
        # v6.5.0: leg 2 attempts continue until window end_ts. The
        # BS_BSS_ABORT_AT_S env var is now a soft-diagnostic timer used
        # only for elapsed_s display in dashboard/logs — it does NOT
        # cause any state transition. The window-end transition to
        # ORPHAN_END is handled by the resolution gate at top of function.
        timer_start_ts = mdm.bss_first_fill_ts or now
        elapsed_s = now - timer_start_ts

        other_ask = no_ask if mdm.bss_first_side == "YES" else yes_ask

        # v6.5.3.1: shadow emergency-sell tick. PURE LOGGING — no actual
        # sell. Records what each candidate emergency-sell rule WOULD
        # have done at this tick. Throttled to ~5s by env var; set
        # BS_BSS_SHADOW_TICK_INTERVAL_S=0 to disable.
        if _BS_BSS_SHADOW_TICK_INTERVAL_S > 0:
            if (now - mdm.bss_last_shadow_ts) >= _BS_BSS_SHADOW_TICK_INTERVAL_S:
                _bs_log_bss_hold_shadow_event(state, mdm, now,
                                                yes_ask, no_ask,
                                                yes_bid, no_bid)
                mdm.bss_last_shadow_ts = now
                # v6.5.4: orphan-sell rule evaluation runs on the same
                # cadence as the shadow tick. No-op when
                # BS_BSS_ORPHAN_SELL_ENABLED=false (default).
                _bs_evaluate_orphan_sell(state, mdm, now,
                                          yes_ask, no_ask,
                                          yes_bid, no_bid)
                if mdm.bss_state == "ORPHAN_SOLD":
                    # Rule fired — position closed, exit eval cycle for
                    # this market. The terminal-state guard at the top
                    # of the next eval will skip future ticks naturally.
                    return

        # ── Live-window second-leg detection (v6.3.7: patient + floor) ──
        # Maintain sustain timers for both strict (0.50) and relaxed (0.62) tiers.
        if other_ask < _BS_BSS_T_SECOND_STRICT:
            if mdm.bss_other_below_strict_start_ts is None:
                mdm.bss_other_below_strict_start_ts = now
        else:
            mdm.bss_other_below_strict_start_ts = None
        if other_ask < _BS_BSS_T_SECOND_RELAXED:
            if mdm.bss_other_below_relax_start_ts is None:
                mdm.bss_other_below_relax_start_ts = now
        else:
            mdm.bss_other_below_relax_start_ts = None

        # v6.3.7: FLOOR BACKSTOP. If opposite side has crashed to FLOOR or
        # below (default 0.40), fire IMMEDIATELY — don't risk a bounce above
        # the strict threshold. This is the "ideal scenario": both legs cheap.
        if other_ask <= _BS_BSS_T_SECOND_FLOOR:
            print(f"[bss_entry] SECOND_LEG_FLOOR market={market.condition_id[:10]}… "
                  f"other_ask={other_ask:.3f} ≤ floor={_BS_BSS_T_SECOND_FLOOR:.2f} "
                  f"(deep-dip; firing immediately)", flush=True)
            _bss_place_leg2(state, mdm, now,
                             decision_ask=other_ask,
                             threshold=_BS_BSS_T_SECOND_FLOOR,
                             sus_s=0.0,
                             yes_ask=yes_ask, no_ask=no_ask,
                             yes_bid=yes_bid, no_bid=no_bid,
                             elapsed_s=elapsed_s,
                             phase_label="floor")
            return

        in_strict = elapsed_s <= _BS_BSS_RELAX_AT_S
        cur_thr = (_BS_BSS_T_SECOND_STRICT if in_strict
                   else _BS_BSS_T_SECOND_RELAXED)
        if in_strict:
            sus_s = ((now - mdm.bss_other_below_strict_start_ts)
                     if mdm.bss_other_below_strict_start_ts else 0.0)
        else:
            sus_s = ((now - mdm.bss_other_below_relax_start_ts)
                     if mdm.bss_other_below_relax_start_ts else 0.0)
        if sus_s >= _BS_BSS_SUSTAIN_SECOND_S and other_ask < cur_thr:
            # v6.3.7: PATIENCE CHECK (strict tier only, not relaxed).
            # If opposite is still actively falling, hold one tick — we'll
            # likely get a better fill. If price is flat or rising, fire now.
            if (in_strict and _BS_BSS_OPP_VEL_PATIENT_DROP > 0):
                opp_drop = _opposite_side_drop(mdm, mdm.bss_first_side, now,
                                                _BS_BSS_OPP_VEL_LOOKBACK_S)
                if (opp_drop is not None
                    and opp_drop >= _BS_BSS_OPP_VEL_PATIENT_DROP):
                    # Still falling fast — wait one more tick for a better fill.
                    print(f"[bss_entry] SECOND_LEG_PATIENT "
                          f"market={market.condition_id[:10]}… "
                          f"other_ask={other_ask:.3f} "
                          f"drop_{int(_BS_BSS_OPP_VEL_LOOKBACK_S)}s={opp_drop:+.4f} "
                          f"(still falling; wait)", flush=True)
                    return
            _bss_place_leg2(state, mdm, now,
                             decision_ask=other_ask,
                             threshold=cur_thr,
                             sus_s=sus_s,
                             yes_ask=yes_ask, no_ask=no_ask,
                             yes_bid=yes_bid, no_bid=no_bid,
                             elapsed_s=elapsed_s,
                             phase_label=("strict" if in_strict else "relaxed"))
        return


def _bss_simulate_dry_fill(state: BotState, token_id: str,
                            decision_ask: float, size_usdc: float
                            ) -> Tuple[float, float, float, str]:
    """v6.5.1: simulate a DRY fill modeled on the proven April 13 LIVE pattern.
    
    Returns (fill_ask, fill_qty, fee, fok_outcome).
    - fill_ask: weighted-average price actually paid per share
    - fill_qty: shares obtained (may be < intended_qty if book is thin)
    - fee: taker fee charged on actual filled size
    - fok_outcome: "filled_top" / "filled_walked" / "partial" / "no_book"
                   / "no_liquidity" (v6.5.1)
    
    Logic:
      - Read book from state.poly_books
      - If ask_size <= 0: NO LIQUIDITY → fail (was: assume fills, v6.5.0 bug)
      - If desired_qty <= ask_size_p1: fill at decision_ask (filled_top)
      - Else if BS_BOOK_WALK_ENABLED: walk down ladder using ask_levels
      - Else: partial fill at top-of-book only (partial)
      - Apply taker fee on the USDC actually committed
    
    v6.5.1 changes:
      1. Removed "ask_size <= 0 → assume fills" branch — that was Bug 2
         from the v6.5.0 stale-fire investigation. When the price_change
         handler poisoned book.ask with a cancelled level (size=0),
         the simulator silently rewarded that with a fictional fill.
         Now returns "no_liquidity" so the caller logs FOK fail.
      2. Book-walk now uses the actual ask_levels ladder when present,
         instead of the +$0.01 approximation. The price_change handler
         (v6.5.1 Fix B) keeps the ladder current on every WS tick, so
         walking it is reliable. Falls back to +$0.01 only when the
         ladder is empty/short.
    """
    intended_qty = size_usdc / decision_ask if decision_ask > 0 else 0.0
    book = state.poly_books.get(token_id)
    if book is None:
        # No book — can't simulate a fill at all
        return 0.0, 0.0, 0.0, "no_book"
    
    ask_size = float(getattr(book, 'ask_size', 0.0) or 0.0)
    
    # v6.5.1 Fix C: no liquidity = no fill. Don't fictionally fill at
    # decision_ask. In LIVE this is what an FAK at a phantom price gets:
    # rejected. Match that semantics in DRY so the caller logs FOK fail
    # and the data reflects what would happen with real money.
    if ask_size <= 0:
        return 0.0, 0.0, 0.0, "no_liquidity"
    
    if intended_qty <= ask_size:
        # Fits in top-of-book
        fill_ask = decision_ask
        fill_qty = intended_qty
        usdc_committed = size_usdc
        fee = _polymarket_taker_fee(fill_qty, fill_ask)  # v6.5.5: curved Polymarket formula
        return fill_ask, fill_qty, fee, "filled_top"
    
    # Top of book has insufficient size. Decide: walk or partial.
    if not _BS_BOOK_WALK_ENABLED:
        # Partial fill at top-of-book size only
        fill_ask = decision_ask
        fill_qty = ask_size
        usdc_committed = ask_size * decision_ask
        fee = _polymarket_taker_fee(fill_qty, fill_ask)  # v6.5.5
        return fill_ask, fill_qty, fee, "partial"
    
    # v6.5.1: walk the actual ask_levels ladder. Levels are sorted
    # ascending by price (best ask first). Accumulate fills level by level
    # until intended_qty is satisfied or ladder is exhausted.
    ask_levels = list(getattr(book, 'ask_levels', []) or [])
    if len(ask_levels) >= 2:
        qty_filled = 0.0
        usdc_committed = 0.0
        for level_price, level_size in ask_levels:
            if level_size <= 0 or level_price <= 0:
                continue
            if qty_filled >= intended_qty:
                break
            qty_remaining = intended_qty - qty_filled
            take = min(qty_remaining, level_size)
            qty_filled += take
            usdc_committed += take * level_price
        if qty_filled <= 0:
            return 0.0, 0.0, 0.0, "no_liquidity"
        fill_ask = usdc_committed / qty_filled
        fee = _polymarket_taker_fee(qty_filled, fill_ask)  # v6.5.5: curved formula on VWAP
        if qty_filled < intended_qty - 1e-9:
            return fill_ask, qty_filled, fee, "partial"
        return fill_ask, qty_filled, fee, "filled_walked"
    
    # Ladder is too short to walk reliably — fall back to +$0.01 estimate
    # (pessimistic: real LIVE may walk further on truly thin books).
    qty_remaining = intended_qty - ask_size
    level1_usdc = ask_size * decision_ask
    level2_ask = decision_ask + 0.01
    level2_usdc = qty_remaining * level2_ask
    fill_qty = intended_qty
    usdc_committed = level1_usdc + level2_usdc
    fill_ask = usdc_committed / fill_qty if fill_qty > 0 else decision_ask
    fee = _polymarket_taker_fee(fill_qty, fill_ask)  # v6.5.5
    return fill_ask, fill_qty, fee, "filled_walked"


def _bss_simulate_dry_sell(state: "BotState", token_id: str,
                            sell_qty: float, top_bid: float
                            ) -> Tuple[float, float, float, str]:
    """v6.5.5: simulate a DRY sell with realistic slippage.

    Mirror of `_bss_simulate_dry_fill` but for the EXIT side. Walks the
    bid book DOWNWARD (best bid first, then next-best, etc) until either
    we've sold all `sell_qty` shares or the book runs out.

    Returns (avg_sell_price, qty_sold, sell_fee, outcome):
      - avg_sell_price: VWAP across walked bid levels
      - qty_sold: shares actually sold (may be < sell_qty on thin books)
      - sell_fee: Polymarket taker fee on actual filled size
      - outcome: "filled_top" / "filled_walked" / "partial" / "no_book"
                 / "no_liquidity"

    Why this matters: the bot used to assume sells fill at `top_bid`
    fully. In reality, top_bid might have only 5 shares of size while
    we're selling 10. The remainder fills at next-best bid which is
    lower. Result: bot over-reported sell proceeds. v6.5.5 walks the
    bid ladder to give a realistic picture in DRY.

    Bid book invariant: bid_levels is sorted DESCENDING by price (best
    bid first). top_bid is a hint — function uses bid_levels[0] as
    ground truth.
    """
    book = state.poly_books.get(token_id)
    if book is None:
        return 0.0, 0.0, 0.0, "no_book"

    bid_size = float(getattr(book, 'bid_size', 0.0) or 0.0)
    if bid_size <= 0:
        return 0.0, 0.0, 0.0, "no_liquidity"

    # Walk the bid ladder if we need more than top-of-book size
    bid_levels = list(getattr(book, 'bid_levels', []) or [])
    if not bid_levels:
        # Fallback: assume top-of-book only
        if sell_qty <= bid_size:
            avg_price = top_bid
            qty_sold = sell_qty
            fee = _polymarket_taker_fee(qty_sold, avg_price)
            return avg_price, qty_sold, fee, "filled_top"
        # Partial
        avg_price = top_bid
        qty_sold = bid_size
        fee = _polymarket_taker_fee(qty_sold, avg_price)
        return avg_price, qty_sold, fee, "partial"

    # Walk top-down — best bid first
    qty_filled = 0.0
    proceeds = 0.0
    for level_price, level_size in bid_levels:
        if level_size <= 0 or level_price <= 0:
            continue
        if qty_filled >= sell_qty:
            break
        qty_remaining = sell_qty - qty_filled
        take = min(qty_remaining, level_size)
        qty_filled += take
        proceeds += take * level_price

    if qty_filled <= 0:
        return 0.0, 0.0, 0.0, "no_liquidity"
    avg_price = proceeds / qty_filled
    fee = _polymarket_taker_fee(qty_filled, avg_price)
    if qty_filled < sell_qty - 1e-9:
        return avg_price, qty_filled, fee, "partial"
    if abs(avg_price - bid_levels[0][0]) < 1e-9:
        return avg_price, qty_filled, fee, "filled_top"
    return avg_price, qty_filled, fee, "filled_walked"


def _bss_place_live_fak(state: BotState, token_id: str,
                         decision_ask: float, size_usdc: float
                         ) -> Tuple[Optional[float], Optional[float],
                                    Optional[float], str]:
    """v6.5.0: place a real FAK order on Polymarket CLOB.
    
    Returns (fill_ask, fill_qty, fee, outcome).
    - fill_ask: actual fill price from CLOB response (None if rejected)
    - fill_qty: actual shares filled from CLOB response
    - fee: taker fee on filled size
    - outcome: "filled_live" / "rejected" / "error" / "no_client"
    
    Pattern proven April 13 first live trade. py-clob-client OrderArgs
    with size=qty_shares (not USDC), side=BUY, OrderType.FAK.
    """
    if state.clob_client is None:
        print("[bss_entry][LIVE] clob_client not initialized", flush=True)
        return None, None, None, "no_client"
    
    intended_qty = size_usdc / decision_ask if decision_ask > 0 else 0.0
    
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except ImportError as e:
        print(f"[bss_entry][LIVE] py-clob-client import failed: {e}", flush=True)
        return None, None, None, "error"
    
    try:
        order_args = OrderArgs(
            price=decision_ask,
            size=intended_qty,
            side=BUY,
            token_id=token_id,
        )
        signed = state.clob_client.create_order(order_args)
        resp = state.clob_client.post_order(signed, OrderType.FAK)
    except Exception as e:
        print(f"[bss_entry][LIVE] order error: {type(e).__name__}: {e}",
              flush=True)
        return None, None, None, "error"
    
    success = bool(resp and resp.get("success", False))
    if not success:
        err = (resp or {}).get("errorMsg") or (resp or {}).get("error") or "rejected"
        print(f"[bss_entry][LIVE] FAK rejected: {err} resp={resp}", flush=True)
        return None, None, None, "rejected"
    
    fill_ask = float(resp.get("price", decision_ask))
    fill_qty = float(resp.get("size_matched", intended_qty))
    usdc_committed = fill_qty * fill_ask
    fee = _polymarket_taker_fee(fill_qty, fill_ask)  # v6.5.5: Polymarket charges this exact fee server-side; we mirror locally for bookkeeping
    return fill_ask, fill_qty, fee, "filled_live"


def _bss_place_live_sell(state: BotState, token_id: str,
                           decision_price: float, qty_shares: float
                           ) -> Tuple[Optional[float], Optional[float],
                                      Optional[float], str]:
    """v6.5.6: place a real FAK SELL order on Polymarket CLOB.

    Mirror of _bss_place_live_fak but for selling held shares. Used by
    the orphan-sell rule (and TP rule) when running in LIVE mode to
    actually exit a half-paired position.

    Args:
      state: BotState (provides clob_client)
      token_id: the side-token we're selling (leg1's token_id, not the
                opposite)
      decision_price: the limit price we'd like (typically leg1_top_bid).
                       FAK will match against any bid >= this price.
      qty_shares: number of shares to sell (typically leg1_qty)

    Returns:
      (fill_price, fill_qty, fee, outcome) where outcome is one of:
        - "filled_live"   — full requested qty matched
        - "partial_live"  — some matched, less than full qty
        - "rejected"      — FAK found no match at or above decision_price
        - "error"         — exception talking to CLOB
        - "no_client"     — clob_client not initialized

    Design notes:
      - FAK only, no GTC fallback. GTC would sit on the orderbook and
        async-fill at any later price/quantity, breaking our synchronous
        state machine. Past LIVE evidence (April 2026 scalper3) shows
        GTC fallback also fails on thin books — small upside, large
        complexity. If FAK rejects, the position stays HALF and the
        band-sustain run continues; the rule will retry next tick.
      - Returns the ACTUAL fill price from the CLOB response, not the
        decision_price. Caller MUST use this for P&L computation —
        the actual fill price can be ≥ decision_price (favorable, very
        rare) or could differ if the orderbook moved during submission.
    """
    if state.clob_client is None:
        print("[bss_orphan_sell][LIVE] clob_client not initialized", flush=True)
        return None, None, None, "no_client"

    if qty_shares <= 0 or decision_price <= 0:
        print(f"[bss_orphan_sell][LIVE] invalid args: "
              f"qty={qty_shares} price={decision_price}", flush=True)
        return None, None, None, "error"

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
    except ImportError as e:
        print(f"[bss_orphan_sell][LIVE] py-clob-client import failed: {e}",
              flush=True)
        return None, None, None, "error"

    try:
        order_args = OrderArgs(
            price=decision_price,
            size=qty_shares,
            side=SELL,
            token_id=token_id,
        )
        signed = state.clob_client.create_order(order_args)
        resp = state.clob_client.post_order(signed, OrderType.FAK)
    except Exception as e:
        print(f"[bss_orphan_sell][LIVE] order error: "
              f"{type(e).__name__}: {e}", flush=True)
        return None, None, None, "error"

    success = bool(resp and resp.get("success", False))
    if not success:
        err = (resp or {}).get("errorMsg") or (resp or {}).get("error") or "rejected"
        print(f"[bss_orphan_sell][LIVE] FAK rejected: {err} resp={resp}",
              flush=True)
        return None, None, None, "rejected"

    fill_price = float(resp.get("price", decision_price))
    fill_qty = float(resp.get("size_matched", 0.0))
    if fill_qty <= 0:
        # Server returned success but matched zero — treat as rejection.
        print(f"[bss_orphan_sell][LIVE] zero fill on success response: "
              f"resp={resp}", flush=True)
        return None, None, None, "rejected"

    fee = _polymarket_taker_fee(fill_qty, fill_price)
    # Detect partial fill (≥99% match counts as full; floating-point slack)
    is_partial = fill_qty < qty_shares * 0.99
    outcome = "partial_live" if is_partial else "filled_live"
    print(f"[bss_orphan_sell][LIVE] FAK {outcome}: "
          f"requested {qty_shares:.4f}@{decision_price:.4f}, "
          f"filled {fill_qty:.4f}@{fill_price:.4f}, "
          f"fee={fee:.4f}", flush=True)
    return fill_price, fill_qty, fee, outcome


def _bss_place_leg1(state: BotState, mdm: MultiDurationMarket, now: float,
                     fire_side: str, decision_ask: float,
                     yes_ask: float, no_ask: float,
                     yes_bid: float, no_bid: float,
                     sus_s: float) -> bool:
    """v6.5.0: place leg 1 immediately at first-leg sustain detection.
    
    Updates mdm state on success: bss_state → 'WAITING_2ND',
    bss_leg1_actual_ask, bss_leg1_qty, bss_leg1_fee, bss_leg1_size_usdc set.
    On failure: stays WATCH, logs leg fok fail event.
    
    Returns True on successful fill, False on FOK fail / rejection.
    """
    cfg = state.config
    market = mdm.market
    size_usdc = cfg.position_size_usdc
    token_id = (market.yes_token_id if fire_side == "YES"
                else market.no_token_id)
    
    is_live = (cfg.mode == "live")
    if is_live and not _LIVE_BSS_ENABLED:
        print(f"[bss_entry][BLOCKED] MODE=live but LIVE_BSS_ENABLED=false. "
              f"Refusing leg1 on market_id={market.condition_id[:10]}",
              flush=True)
        return False
    
    if is_live:
        fill_ask, fill_qty, fee, outcome = _bss_place_live_fak(
            state, token_id, decision_ask, size_usdc)
    else:
        fill_ask, fill_qty, fee, outcome = _bss_simulate_dry_fill(
            state, token_id, decision_ask, size_usdc)
    
    if outcome in ("rejected", "error", "no_book", "no_client", "no_liquidity") or not fill_qty:
        # Fill failed entirely. Log FOK fail, stay in WATCH.
        # Reset the streak so we don't re-fire on the very next tick.
        if fire_side == "YES":
            mdm.bss_yes_below_first_start_ts = None
        else:
            mdm.bss_no_below_first_start_ts = None
        _bs_log_bss_leg_fok_fail_event(state, mdm, now, leg_num=1,
                                         fire_side=fire_side,
                                         decision_ask=decision_ask,
                                         outcome=outcome,
                                         yes_ask=yes_ask, no_ask=no_ask,
                                         yes_bid=yes_bid, no_bid=no_bid)
        print(f"[bss_entry] LEG1_FOK_FAIL market={market.condition_id[:10]}… "
              f"side={fire_side} decision={decision_ask:.4f} "
              f"outcome={outcome} → stay WATCH", flush=True)
        return False
    
    # Fill OK. Record leg 1 state on MDM.
    actual_size_usdc = fill_qty * fill_ask
    mdm.bss_first_side = fire_side
    mdm.bss_first_price = decision_ask  # legacy field — decision price for compat
    mdm.bss_first_fill_ts = now
    mdm.bss_leg1_actual_ask = fill_ask
    mdm.bss_leg1_qty = fill_qty
    mdm.bss_leg1_fee = fee
    mdm.bss_leg1_size_usdc = actual_size_usdc
    mdm.bss_state = "WAITING_2ND"
    
    _bs_log_bss_leg_fill_event(state, mdm, now, leg_num=1,
                                 fire_side=fire_side,
                                 decision_ask=decision_ask,
                                 fill_ask=fill_ask, fill_qty=fill_qty,
                                 fee=fee, size_usdc=actual_size_usdc,
                                 outcome=outcome, sus_s=sus_s,
                                 is_live=is_live,
                                 yes_ask=yes_ask, no_ask=no_ask,
                                 yes_bid=yes_bid, no_bid=no_bid)
    print(f"[bss_entry] LEG1_FILL[{('LIVE' if is_live else 'DRY')}] "
          f"market={market.condition_id[:10]}… "
          f"side={fire_side} decision={decision_ask:.4f} "
          f"fill@{fill_ask:.4f} qty={fill_qty:.3f} "
          f"size=${actual_size_usdc:.3f} fee=${fee:.4f} "
          f"outcome={outcome} sustain={sus_s:.1f}s "
          f"TTR={market.end_ts - now:.0f}s "
          f"slug={market.slug[:30]}", flush=True)
    return True


def _bss_place_leg2(state: BotState, mdm: MultiDurationMarket, now: float,
                     decision_ask: float, threshold: float, sus_s: float,
                     yes_ask: float, no_ask: float,
                     yes_bid: float, no_bid: float,
                     elapsed_s: float, phase_label: str) -> bool:
    """v6.5.0: place leg 2 immediately at second-leg sustain detection.
    
    On success: builds BothSidesPosition with both legs (using mdm-stored
    leg 1 data + fresh leg 2 fill), inserts into state.both_sides_positions,
    sets bss_state='BOTH'.
    On failure: stays WAITING_2ND, logs leg fok fail event. Bot keeps watching
    for another sustain attempt within the window.
    
    Returns True on success.
    """
    cfg = state.config
    market = mdm.market
    size_usdc = cfg.position_size_usdc
    second_side = "NO" if mdm.bss_first_side == "YES" else "YES"
    token_id = (market.yes_token_id if second_side == "YES"
                else market.no_token_id)
    
    is_live = (cfg.mode == "live")
    if is_live and not _LIVE_BSS_ENABLED:
        print(f"[bss_entry][BLOCKED] MODE=live but LIVE_BSS_ENABLED=false. "
              f"Refusing leg2 on market_id={market.condition_id[:10]}",
              flush=True)
        return False
    
    if is_live:
        fill_ask, fill_qty, fee, outcome = _bss_place_live_fak(
            state, token_id, decision_ask, size_usdc)
    else:
        fill_ask, fill_qty, fee, outcome = _bss_simulate_dry_fill(
            state, token_id, decision_ask, size_usdc)
    
    if outcome in ("rejected", "error", "no_book", "no_client", "no_liquidity") or not fill_qty:
        # Leg 2 fill failed. Reset second-leg sustain timers so we wait for
        # a fresh dip rather than re-firing on the same sustained dip.
        mdm.bss_other_below_strict_start_ts = None
        mdm.bss_other_below_relax_start_ts = None
        _bs_log_bss_leg_fok_fail_event(state, mdm, now, leg_num=2,
                                         fire_side=second_side,
                                         decision_ask=decision_ask,
                                         outcome=outcome,
                                         yes_ask=yes_ask, no_ask=no_ask,
                                         yes_bid=yes_bid, no_bid=no_bid)
        print(f"[bss_entry] LEG2_FOK_FAIL market={market.condition_id[:10]}… "
              f"side={second_side} decision={decision_ask:.4f} "
              f"outcome={outcome} → stay HALF (will retry on fresh sustain)",
              flush=True)
        return False
    
    # Leg 2 filled. Record state and build BothSidesPosition.
    actual_size_usdc = fill_qty * fill_ask
    mdm.bss_second_price = decision_ask  # legacy
    mdm.bss_second_fill_ts = now
    mdm.bss_second_phase = phase_label
    mdm.bss_leg2_actual_ask = fill_ask
    mdm.bss_leg2_qty = fill_qty
    mdm.bss_leg2_fee = fee
    mdm.bss_leg2_size_usdc = actual_size_usdc
    
    # Build BothSidesPosition. yes_leg/no_leg use ACTUAL fill data.
    if mdm.bss_first_side == "YES":
        yes_actual_ask = mdm.bss_leg1_actual_ask
        yes_qty = mdm.bss_leg1_qty
        yes_size = mdm.bss_leg1_size_usdc
        yes_fill_ts = mdm.bss_first_fill_ts
        no_actual_ask = fill_ask
        no_qty = fill_qty
        no_size = actual_size_usdc
        no_fill_ts = now
    else:
        no_actual_ask = mdm.bss_leg1_actual_ask
        no_qty = mdm.bss_leg1_qty
        no_size = mdm.bss_leg1_size_usdc
        no_fill_ts = mdm.bss_first_fill_ts
        yes_actual_ask = fill_ask
        yes_qty = fill_qty
        yes_size = actual_size_usdc
        yes_fill_ts = now
    
    yes_leg = BothSidesLeg(
        side="YES", token_id=market.yes_token_id,
        entry_ask=yes_actual_ask, entry_bid=yes_bid,
        size_usdc=yes_size, qty_shares=yes_qty,
        entry_ts=yes_fill_ts,
        peak_bid=yes_bid, peak_bid_ts=yes_fill_ts,
    )
    no_leg = BothSidesLeg(
        side="NO", token_id=market.no_token_id,
        entry_ask=no_actual_ask, entry_bid=no_bid,
        size_usdc=no_size, qty_shares=no_qty,
        entry_ts=no_fill_ts,
        peak_bid=no_bid, peak_bid_ts=no_fill_ts,
    )
    sum_ask = yes_actual_ask + no_actual_ask
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
    mdm.bss_state = "BOTH"
    _v653_buf_clear(market.condition_id)  # v6.5.3
    
    # Log leg 2 fill (v6.5.5.2: surface floor/strict phase in CSV note)
    _bs_log_bss_leg_fill_event(state, mdm, now, leg_num=2,
                                 fire_side=second_side,
                                 decision_ask=decision_ask,
                                 fill_ask=fill_ask, fill_qty=fill_qty,
                                 fee=fee, size_usdc=actual_size_usdc,
                                 outcome=outcome, sus_s=sus_s,
                                 is_live=is_live,
                                 yes_ask=yes_ask, no_ask=no_ask,
                                 yes_bid=yes_bid, no_bid=no_bid,
                                 phase=phase_label)
    # Log the legacy ENTRY_YES_DRY/LIVE + ENTRY_NO_DRY/LIVE pair so existing
    # downstream analysis CSVs keep working.
    yes_event = "ENTRY_YES_LIVE" if is_live else "ENTRY_YES_DRY"
    no_event = "ENTRY_NO_LIVE" if is_live else "ENTRY_NO_DRY"
    _bs_log_trade_event(state, yes_event, pos, yes_leg,
                         note=f"src=bss_entry,first_side={mdm.bss_first_side},"
                              f"leg_n={'1' if mdm.bss_first_side=='YES' else '2'},"
                              f"fok={outcome},fee={_polymarket_taker_fee(yes_leg.qty_shares, yes_leg.entry_ask):.4f}")
    _bs_log_trade_event(state, no_event, pos, no_leg,
                         note=f"src=bss_entry,first_side={mdm.bss_first_side},"
                              f"leg_n={'1' if mdm.bss_first_side=='NO' else '2'},"
                              f"fok={outcome},fee={_polymarket_taker_fee(no_leg.qty_shares, no_leg.entry_ask):.4f}")
    print(f"[bss_entry] LEG2_FILL[{('LIVE' if is_live else 'DRY')}] "
          f"market={market.condition_id[:10]}… "
          f"side={second_side} decision={decision_ask:.4f} "
          f"fill@{fill_ask:.4f} qty={fill_qty:.3f} "
          f"phase={phase_label} sustain={sus_s:.1f}s "
          f"elapsed={elapsed_s:.0f}s sum_ask={sum_ask:.4f} "
          f"→ PAIRED (held to resolution)", flush=True)
    return True


def _bss_handle_window_end_orphan(state: BotState, mdm: MultiDurationMarket,
                                    now: float, yes_ask: float, no_ask: float,
                                    yes_bid: float, no_bid: float) -> None:
    """v6.5.0: when market end_ts is reached and we're still in WAITING_2ND
    (semantic: HALF), the held leg becomes an orphan. Build a BothSidesPosition
    with the held leg + a zero-size empty leg so the resolution flow handles
    payout naturally. CTF will pay $1/share to whoever holds the winning side.
    
    No sell, no flatten, no fake P&L. Just record the orphan event for
    downstream analysis and let resolution take over.
    """
    if mdm.bss_leg1_orphan_end_logged:
        return  # already handled
    market = mdm.market
    
    # Build BothSidesPosition: held leg with actual fill data, empty leg
    # with size=0, qty=0 so it contributes nothing to P&L at resolution.
    if mdm.bss_first_side == "YES":
        yes_leg = BothSidesLeg(
            side="YES", token_id=market.yes_token_id,
            entry_ask=mdm.bss_leg1_actual_ask or 0.0,
            entry_bid=yes_bid,
            size_usdc=mdm.bss_leg1_size_usdc or 0.0,
            qty_shares=mdm.bss_leg1_qty or 0.0,
            entry_ts=mdm.bss_first_fill_ts or now,
            peak_bid=yes_bid, peak_bid_ts=mdm.bss_first_fill_ts or now,
        )
        no_leg = BothSidesLeg(
            side="NO", token_id=market.no_token_id,
            entry_ask=0.0, entry_bid=no_bid,
            size_usdc=0.0, qty_shares=0.0,
            entry_ts=now,
            peak_bid=no_bid, peak_bid_ts=now,
        )
    else:
        no_leg = BothSidesLeg(
            side="NO", token_id=market.no_token_id,
            entry_ask=mdm.bss_leg1_actual_ask or 0.0,
            entry_bid=no_bid,
            size_usdc=mdm.bss_leg1_size_usdc or 0.0,
            qty_shares=mdm.bss_leg1_qty or 0.0,
            entry_ts=mdm.bss_first_fill_ts or now,
            peak_bid=no_bid, peak_bid_ts=mdm.bss_first_fill_ts or now,
        )
        yes_leg = BothSidesLeg(
            side="YES", token_id=market.yes_token_id,
            entry_ask=0.0, entry_bid=yes_bid,
            size_usdc=0.0, qty_shares=0.0,
            entry_ts=now,
            peak_bid=yes_bid, peak_bid_ts=now,
        )
    pos = BothSidesPosition(
        market_id=market.condition_id,
        market_url=market.market_url,
        market_question=market.question,
        slug=market.slug,
        duration_s=mdm.duration_s,
        end_ts=market.end_ts,
        entry_ts=mdm.bss_first_fill_ts or now,
        sum_ask_at_entry=mdm.bss_leg1_actual_ask or 0.0,
        yes_leg=yes_leg,
        no_leg=no_leg,
    )
    state.both_sides_positions[market.condition_id] = pos
    state.bs_entered_market_ids.add(market.condition_id)
    state.bs_total_entered += 1
    mdm.bss_state = "ORPHAN_END"
    mdm.bss_leg1_orphan_end_logged = True
    _v653_buf_clear(market.condition_id)  # v6.5.3
    
    _bs_log_bss_orphan_end_event(state, mdm, now,
                                   yes_ask=yes_ask, no_ask=no_ask,
                                   yes_bid=yes_bid, no_bid=no_bid)
    print(f"[bss_entry] ORPHAN_END market={market.condition_id[:10]}… "
          f"first={mdm.bss_first_side}@{mdm.bss_leg1_actual_ask:.4f} "
          f"qty={mdm.bss_leg1_qty:.3f} "
          f"second_never_came → held to resolution",
          flush=True)


def _bs_log_bss_first_leg_event(state: BotState, mdm: MultiDurationMarket,
                                  now: float, yes_ask: float, no_ask: float,
                                  yes_bid: float, no_bid: float,
                                  sus_s: float) -> None:
    """v6.3.0: log a BSS_FIRST_LEG_DRY event to bs_trades. Kept in v6.5.0
    for backward compat with downstream analysis. Logged BEFORE leg 1
    placement is attempted — so this row records the candidate detection
    even if the placement fails.

    v6.5.3: appends `extra_json={...}` to the `notes` field with Tier 1
    pre-entry features (leg2 microstructure, depth deltas, leg1 bid
    trajectory, latency, regime). Other fields unchanged for backward
    compat with downstream parsers."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        ttr_s = market.end_ts - now
        # v6.5.3: compute Tier 1 features
        leg1_side = mdm.bss_first_side or ""
        leg2_side = "NO" if leg1_side == "YES" else "YES"
        yb = state.poly_books.get(market.yes_token_id)
        nb = state.poly_books.get(market.no_token_id)
        yes_d5 = _v653_ask_depth_5(yb) if yb else 0.0
        no_d5 = _v653_ask_depth_5(nb) if nb else 0.0
        yes_age = (now - yb.last_update_ts) if yb else 999.0
        no_age = (now - nb.last_update_ts) if nb else 999.0
        bin_last_ts_ms = None
        bin_snap: List[Tuple[float, float]] = []
        try:
            if state.binance_prices:
                bin_snap = list(state.binance_prices)
                if bin_snap:
                    bin_last_ts_ms = int(bin_snap[-1][0] * 1000)
        except Exception:
            pass
        feats = _v653_compute_features(
            market_id=market.condition_id,
            fire_ts_ms=int(now * 1000),
            leg1_side=leg1_side, leg2_side=leg2_side,
            now_unix=now,
            yes_ask_depth5_now=yes_d5, no_ask_depth5_now=no_d5,
            yes_book_age_s=yes_age, no_book_age_s=no_age,
            binance_last_tick_ts_ms=bin_last_ts_ms,
            binance_prices_snapshot=bin_snap,
            leg2_token_id=(market.yes_token_id if leg2_side == "YES"
                           else market.no_token_id),
            poly_trades=state.poly_trades,
            decision_lat_ms=int((time.time() - now) * 1000),
        )
        extra_json = json.dumps(feats, separators=(',', ':'))
        note = (f"src=bss_entry,sustain={sus_s:.1f}s,ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f},"
                f"yes_bid={yes_bid:.4f},no_bid={no_bid:.4f},"
                f"extra_json={extra_json}")
        size = state.config.position_size_usdc
        first_price = mdm.bss_first_price or 0.0
        qty = size / first_price if first_price else 0.0
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
            f"{first_price:.4f}",
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


def _bs_log_bss_candidate_event(state: BotState, mdm: MultiDurationMarket,
                                  now: float, side: str, ask_price: float,
                                  yes_ask: float, no_ask: float,
                                  yes_bid: float, no_bid: float) -> None:
    """v6.5.3: log a BSS_CANDIDATE_DRY event when a side's ask first dips
    below T_FIRST (streak starts). This is the population from which
    BSS_FIRST_LEG fires are drawn — logging candidates lets analysis
    compare features at "could have fired" vs "did fire" time, fixing
    selection bias.

    Logged at most once per streak (caller ensures by only calling on
    the None→ts transition). Same schema as BSS_FIRST_LEG for parsers
    that expect uniform rows."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        leg2_side = "NO" if side == "YES" else "YES"
        ttr_s = market.end_ts - now
        yb = state.poly_books.get(market.yes_token_id)
        nb = state.poly_books.get(market.no_token_id)
        yes_d5 = _v653_ask_depth_5(yb) if yb else 0.0
        no_d5 = _v653_ask_depth_5(nb) if nb else 0.0
        yes_age = (now - yb.last_update_ts) if yb else 999.0
        no_age = (now - nb.last_update_ts) if nb else 999.0
        bin_last_ts_ms = None
        bin_snap: List[Tuple[float, float]] = []
        try:
            if state.binance_prices:
                bin_snap = list(state.binance_prices)
                if bin_snap:
                    bin_last_ts_ms = int(bin_snap[-1][0] * 1000)
        except Exception:
            pass
        feats = _v653_compute_features(
            market_id=market.condition_id,
            fire_ts_ms=int(now * 1000),
            leg1_side=side, leg2_side=leg2_side,
            now_unix=now,
            yes_ask_depth5_now=yes_d5, no_ask_depth5_now=no_d5,
            yes_book_age_s=yes_age, no_book_age_s=no_age,
            binance_last_tick_ts_ms=bin_last_ts_ms,
            binance_prices_snapshot=bin_snap,
            leg2_token_id=(market.yes_token_id if leg2_side == "YES"
                           else market.no_token_id),
            poly_trades=state.poly_trades,
            decision_lat_ms=int((time.time() - now) * 1000),
        )
        extra_json = json.dumps(feats, separators=(',', ':'))
        note = (f"src=bss_entry,kind=candidate,side={side},"
                f"ask={ask_price:.4f},ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f},"
                f"yes_bid={yes_bid:.4f},no_bid={no_bid:.4f},"
                f"extra_json={extra_json}")
        row = [
            int(time.time() * 1000),
            "BSS_CANDIDATE_DRY",
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            side,
            (market.yes_token_id if side == "YES" else market.no_token_id),
            f"{ask_price:.4f}",
            f"{(yes_bid if side == 'YES' else no_bid):.4f}",
            "0.0000",
            "0.0000",
            "0.0000", "",
            "0.0000",
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error candidate slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


def _bs_log_bss_hold_shadow_event(state: "BotState", mdm: "MultiDurationMarket",
                                    now: float,
                                    yes_ask: float, no_ask: float,
                                    yes_bid: float, no_bid: float) -> None:
    """v6.5.3.1: emit a BSS_HOLD_SHADOW_DRY event during the WAITING_2ND
    hold, capturing raw state for post-hoc emergency-sell rule design.

    PURE LOGGING — no actual sell happens. Throttled to
    _BS_BSS_SHADOW_TICK_INTERVAL_S cadence via mdm.bss_last_shadow_ts.
    Caller (eval loop) checks this before calling.

    Design philosophy: capture maximum-information raw state per tick.
    NO baked-in rule booleans — all rule evaluation happens post-hoc in
    pandas where it can be tuned freely. Each tick is a complete snapshot
    of the decision space: leg1 trajectory, leg2 trajectory, recovery
    dynamics, BTC directional context, latency, depth, and phase markers.

    First tick of a hold (bss_hold_id is None) initializes per-hold
    bookkeeping: hold_id (UUID), bin_price_atleg1, leg2_ask_atleg1, peaks/
    troughs, visit counters. Subsequent ticks update the running stats.
    """
    if state.bs_trades_logger is None:
        return
    if mdm.bss_state != "WAITING_2ND":
        return
    # Need leg1 fill state to compute sell pnl
    leg1_side = mdm.bss_first_side
    leg1_fill_ask = mdm.bss_leg1_actual_ask
    leg1_qty = mdm.bss_leg1_qty
    leg1_size = mdm.bss_leg1_size_usdc
    leg1_fee = mdm.bss_leg1_fee or 0.0
    if (leg1_side is None or leg1_fill_ask is None
            or leg1_qty is None or leg1_size is None):
        return  # incomplete state, skip

    try:
        market = mdm.market
        leg2_side = "NO" if leg1_side == "YES" else "YES"
        ttr_s = market.end_ts - now
        hold_elapsed_s = ((now - mdm.bss_first_fill_ts)
                          if mdm.bss_first_fill_ts else 0.0)

        # Current quotes
        leg1_bid_now = yes_bid if leg1_side == "YES" else no_bid
        leg2_ask_now = no_ask if leg1_side == "YES" else yes_ask

        # Hypothetical sell pnl right now (v6.5.5: Polymarket curved fee)
        sell_proceeds = leg1_qty * leg1_bid_now
        sell_fee = _polymarket_taker_fee(leg1_qty, leg1_bid_now)
        sell_pnl_now = sell_proceeds - sell_fee - leg1_size - leg1_fee
        recov_ratio = (leg1_bid_now / leg1_fill_ask
                       if leg1_fill_ask > 0 else 0.0)
        recov_abs_c = (leg1_bid_now - leg1_fill_ask) * 100.0  # cents

        # Current Binance price (latest tick)
        bin_price_now: Optional[float] = None
        bin_snap: List[Tuple[float, float]] = []
        try:
            if state.binance_prices:
                bin_snap = list(state.binance_prices)
                if bin_snap:
                    bin_price_now = bin_snap[-1][1]
        except Exception:
            pass

        # ── FIRST TICK INIT (lazy) ─────────────────────────────────
        is_first_tick = (mdm.bss_hold_id is None)
        if is_first_tick:
            mdm.bss_hold_id = uuid.uuid4().hex[:12]
            mdm.bss_hold_tick_idx = 0
            mdm.bss_hold_bin_price_atleg1 = bin_price_now
            mdm.bss_hold_leg2_ask_atleg1 = leg2_ask_now
            mdm.bss_hold_pnl_peak = sell_pnl_now
            mdm.bss_hold_pnl_peak_ts = now
            mdm.bss_hold_pnl_was_positive = (sell_pnl_now >= 0)
            mdm.bss_hold_leg1_bid_max = leg1_bid_now
            mdm.bss_hold_leg1_bid_min = leg1_bid_now
            mdm.bss_hold_leg2_ask_max = leg2_ask_now
            mdm.bss_hold_leg2_ask_min = leg2_ask_now
            mdm.bss_hold_l2_visits_below_055 = 0
            mdm.bss_hold_l2_visits_below_062 = 0
            mdm.bss_hold_l2_prev_above_055 = leg2_ask_now >= 0.55
            mdm.bss_hold_l2_prev_above_062 = leg2_ask_now >= 0.62
        else:
            mdm.bss_hold_tick_idx += 1

        # ── UPDATE RUNNING STATS ───────────────────────────────────
        # Pnl peak tracking
        if (mdm.bss_hold_pnl_peak is None
                or sell_pnl_now > mdm.bss_hold_pnl_peak):
            mdm.bss_hold_pnl_peak = sell_pnl_now
            mdm.bss_hold_pnl_peak_ts = now
        if sell_pnl_now >= 0 and not mdm.bss_hold_pnl_was_positive:
            mdm.bss_hold_pnl_was_positive = True
        # Leg1 bid extremes
        if (mdm.bss_hold_leg1_bid_max is None
                or leg1_bid_now > mdm.bss_hold_leg1_bid_max):
            mdm.bss_hold_leg1_bid_max = leg1_bid_now
        if (mdm.bss_hold_leg1_bid_min is None
                or leg1_bid_now < mdm.bss_hold_leg1_bid_min):
            mdm.bss_hold_leg1_bid_min = leg1_bid_now
        # Leg2 ask extremes
        if (mdm.bss_hold_leg2_ask_max is None
                or leg2_ask_now > mdm.bss_hold_leg2_ask_max):
            mdm.bss_hold_leg2_ask_max = leg2_ask_now
        if (mdm.bss_hold_leg2_ask_min is None
                or leg2_ask_now < mdm.bss_hold_leg2_ask_min):
            mdm.bss_hold_leg2_ask_min = leg2_ask_now
        # Visit-edge counters (count each downward crossing of threshold)
        if leg2_ask_now < 0.55 and mdm.bss_hold_l2_prev_above_055:
            mdm.bss_hold_l2_visits_below_055 += 1
        mdm.bss_hold_l2_prev_above_055 = (leg2_ask_now >= 0.55)
        if leg2_ask_now < 0.62 and mdm.bss_hold_l2_prev_above_062:
            mdm.bss_hold_l2_visits_below_062 += 1
        mdm.bss_hold_l2_prev_above_062 = (leg2_ask_now >= 0.62)

        # ── LEG1 BID + LEG2 ASK TRAJECTORY FROM v6.5.3 RING BUFFER ──
        # Ring buf tuple: (ts_ms, yes_ask, no_ask, yes_bid, no_bid,
        #                  yes_ask_d5, no_ask_d5)
        leg1_bid_idx = 3 if leg1_side == "YES" else 4
        leg2_ask_idx = 1 if leg2_side == "YES" else 2
        l1_d5_idx = 5 if leg1_side == "YES" else 6   # for asks of leg1 side (informational)
        l2_d5_idx = 5 if leg2_side == "YES" else 6

        l1_bid_drift_30s: Optional[float] = None
        l1_bid_drift_60s: Optional[float] = None
        l1_bid_drift_120s: Optional[float] = None
        l1_bid_nch_30s: Optional[int] = None
        l1_bid_falling_5s: Optional[int] = None
        l1_bid_climbing_5s: Optional[int] = None
        l2_ask_drift_30s: Optional[float] = None
        l2_ask_drift_60s: Optional[float] = None
        l2_ask_drift_120s: Optional[float] = None
        l2_ask_nch_30s: Optional[int] = None
        l2_ask_nch_60s: Optional[int] = None
        l1_bid_d5_now: Optional[float] = None
        l2_ask_d5_now: Optional[float] = None

        try:
            buf = _v653_buf.get(market.condition_id)
            if buf is not None and len(buf) >= 3:
                snap = list(buf)
                now_ms = int(now * 1000)
                # Most recent depth values (current)
                last = snap[-1]
                l1_bid_d5_now = float(last[l1_d5_idx])  # depth at leg1's ASK side (informational)
                l2_ask_d5_now = float(last[l2_d5_idx])
                for w_label, w_ms in (("30s", 30_000),
                                        ("60s", 60_000),
                                        ("120s", 120_000)):
                    sub = [r for r in snap if (now_ms - r[0]) <= w_ms]
                    if len(sub) >= 3:
                        l1_bids = [float(r[leg1_bid_idx]) for r in sub]
                        l2_asks = [float(r[leg2_ask_idx]) for r in sub]
                        nch_l1 = sum(1 for i in range(1, len(l1_bids))
                                     if l1_bids[i] != l1_bids[i-1])
                        nch_l2 = sum(1 for i in range(1, len(l2_asks))
                                     if l2_asks[i] != l2_asks[i-1])
                        if w_label == "30s":
                            l1_bid_drift_30s = leg1_bid_now - l1_bids[0]
                            l2_ask_drift_30s = leg2_ask_now - l2_asks[0]
                            l1_bid_nch_30s = nch_l1
                            l2_ask_nch_30s = nch_l2
                        elif w_label == "60s":
                            l1_bid_drift_60s = leg1_bid_now - l1_bids[0]
                            l2_ask_drift_60s = leg2_ask_now - l2_asks[0]
                            l2_ask_nch_60s = nch_l2
                        else:  # 120s
                            l1_bid_drift_120s = leg1_bid_now - l1_bids[0]
                            l2_ask_drift_120s = leg2_ask_now - l2_asks[0]
                # Falling/climbing detection on trailing 5s of leg1 bid
                sub5 = [r for r in snap if (now_ms - r[0]) <= 5_000]
                if len(sub5) >= 3:
                    l1_5 = [float(r[leg1_bid_idx]) for r in sub5]
                    l1_bid_falling_5s = int(l1_5[-1] < l1_5[0])
                    l1_bid_climbing_5s = int(l1_5[-1] > l1_5[0])
        except Exception:
            pass

        # ── BINANCE DIRECTIONAL CONTEXT ────────────────────────────
        bin_delta_since_leg1: Optional[float] = None
        bin_delta_since_leg1_bps: Optional[float] = None
        bin_adverse_since_leg1_bps: Optional[float] = None
        bin_ret_5s_bps: Optional[float] = None
        bin_ret_30s_bps: Optional[float] = None
        bin_ret_60s_bps: Optional[float] = None
        bin_ret_120s_bps: Optional[float] = None
        bin_vol_60s_bps: Optional[float] = None
        bin_age_ms: Optional[int] = None
        try:
            if bin_snap and len(bin_snap) >= 2:
                bin_age_ms = int((now - bin_snap[-1][0]) * 1000)
                if (mdm.bss_hold_bin_price_atleg1
                        and mdm.bss_hold_bin_price_atleg1 > 0):
                    bin_delta_since_leg1 = (
                        bin_price_now - mdm.bss_hold_bin_price_atleg1
                    ) if bin_price_now else 0.0
                    bin_delta_since_leg1_bps = (
                        (bin_price_now / mdm.bss_hold_bin_price_atleg1 - 1.0)
                        * 10000.0
                    ) if bin_price_now else 0.0
                    # Adverse: signed for leg1 direction
                    # leg1=YES means we bet UP — adverse = BTC down (negative ret → adverse positive)
                    # leg1=NO  means we bet DOWN — adverse = BTC up (positive ret → adverse positive)
                    if leg1_side == "YES":
                        bin_adverse_since_leg1_bps = -bin_delta_since_leg1_bps
                    else:
                        bin_adverse_since_leg1_bps = bin_delta_since_leg1_bps

                # Windowed returns
                for w_label, w_s in (("5s", 5.0), ("30s", 30.0),
                                       ("60s", 60.0), ("120s", 120.0)):
                    cutoff = now - w_s
                    recent = [(t, p) for t, p in bin_snap
                              if t >= cutoff and p > 0]
                    if len(recent) >= 2:
                        ret_bps = ((recent[-1][1] / recent[0][1]) - 1.0) * 10000.0
                        if w_label == "5s":
                            bin_ret_5s_bps = ret_bps
                        elif w_label == "30s":
                            bin_ret_30s_bps = ret_bps
                        elif w_label == "60s":
                            bin_ret_60s_bps = ret_bps
                        else:
                            bin_ret_120s_bps = ret_bps
                # 60s realized vol
                cutoff = now - 60.0
                recent = [(t, p) for t, p in bin_snap
                          if t >= cutoff and p > 0]
                if len(recent) >= 5:
                    import math as _math
                    prices = [p for _, p in recent]
                    log_rets = []
                    for i in range(len(prices) - 1):
                        if prices[i] > 0 and prices[i+1] > 0:
                            log_rets.append(_math.log(prices[i+1] / prices[i]))
                    if log_rets:
                        avg = sum(log_rets) / len(log_rets)
                        var = sum((r - avg) ** 2 for r in log_rets) / len(log_rets)
                        bin_vol_60s_bps = _math.sqrt(var) * 10000.0
        except Exception:
            pass

        # Polymarket-Binance lag indicator (raw data; modelling post-hoc)
        leg2_ask_move_since_leg1: Optional[float] = None
        if mdm.bss_hold_leg2_ask_atleg1 is not None:
            leg2_ask_move_since_leg1 = (
                leg2_ask_now - mdm.bss_hold_leg2_ask_atleg1
            )

        # ── LATENCY ────────────────────────────────────────────────
        yb = state.poly_books.get(market.yes_token_id)
        nb = state.poly_books.get(market.no_token_id)
        l1_book_age_ms: Optional[int] = None
        l2_book_age_ms: Optional[int] = None
        if leg1_side == "YES":
            l1_book_age_ms = int((now - yb.last_update_ts) * 1000) if yb else None
            l2_book_age_ms = int((now - nb.last_update_ts) * 1000) if nb else None
        else:
            l1_book_age_ms = int((now - nb.last_update_ts) * 1000) if nb else None
            l2_book_age_ms = int((now - yb.last_update_ts) * 1000) if yb else None
        decision_lat_ms = int((time.time() - now) * 1000)

        # ── PHASE MARKERS ──────────────────────────────────────────
        is_strict_window = int(hold_elapsed_s <= _BS_BSS_RELAX_AT_S)
        is_last_60s = int(ttr_s <= 60)
        is_last_30s = int(ttr_s <= 30)
        is_last_10s = int(ttr_s <= 10)

        # ── ASSEMBLE FEATURE DICT ──────────────────────────────────
        feats: Dict[str, Any] = {
            "v": "6.5.3.1",
            "hold_id": mdm.bss_hold_id,
            "tick_idx": mdm.bss_hold_tick_idx,
            "hold_elapsed_s": round(hold_elapsed_s, 1),
            "ttr_s": round(ttr_s, 1),
            "leg1_side": leg1_side,
            "leg2_side": leg2_side,
            "leg1_fill_p": round(leg1_fill_ask, 4),
            "leg1_qty": round(leg1_qty, 4),
            "leg1_size": round(leg1_size, 4),
            "leg1_bid_now": round(leg1_bid_now, 4),
            "sell_pnl_now": round(sell_pnl_now, 4),
            "recov_ratio": round(recov_ratio, 4),
            "recov_abs_c": round(recov_abs_c, 2),
            "leg2_ask_now": round(leg2_ask_now, 4),
            # Leg1 across-hold extremes
            "l1_bid_max_so_far": round(mdm.bss_hold_leg1_bid_max or 0.0, 4),
            "l1_bid_min_so_far": round(mdm.bss_hold_leg1_bid_min or 0.0, 4),
            # Leg2 across-hold extremes
            "l2_ask_max_so_far": round(mdm.bss_hold_leg2_ask_max or 0.0, 4),
            "l2_ask_min_so_far": round(mdm.bss_hold_leg2_ask_min or 0.0, 4),
            # Visit counters (proximity to firing thresholds)
            "l2_visits_below_055": mdm.bss_hold_l2_visits_below_055,
            "l2_visits_below_062": mdm.bss_hold_l2_visits_below_062,
            # Pnl recovery dynamics
            "pnl_peak_so_far": round(mdm.bss_hold_pnl_peak or 0.0, 4),
            "pnl_drop_from_peak": round(
                sell_pnl_now - (mdm.bss_hold_pnl_peak or sell_pnl_now), 4),
            "secs_since_pnl_peak": round(
                now - (mdm.bss_hold_pnl_peak_ts or now), 1),
            "pnl_was_pos": int(mdm.bss_hold_pnl_was_positive),
            # Phase markers
            "is_strict_window": is_strict_window,
            "is_last_60s": is_last_60s,
            "is_last_30s": is_last_30s,
            "is_last_10s": is_last_10s,
            # Latency
            "decision_lat_ms": decision_lat_ms,
        }
        # Optional fields (None-safe)
        if l1_bid_drift_30s is not None:
            feats["l1_bid_drift_30s"] = round(l1_bid_drift_30s, 4)
        if l1_bid_drift_60s is not None:
            feats["l1_bid_drift_60s"] = round(l1_bid_drift_60s, 4)
        if l1_bid_drift_120s is not None:
            feats["l1_bid_drift_120s"] = round(l1_bid_drift_120s, 4)
        if l1_bid_nch_30s is not None:
            feats["l1_bid_nch_30s"] = l1_bid_nch_30s
        if l1_bid_falling_5s is not None:
            feats["l1_bid_falling_5s"] = l1_bid_falling_5s
        if l1_bid_climbing_5s is not None:
            feats["l1_bid_climbing_5s"] = l1_bid_climbing_5s
        if l2_ask_drift_30s is not None:
            feats["l2_ask_drift_30s"] = round(l2_ask_drift_30s, 4)
        if l2_ask_drift_60s is not None:
            feats["l2_ask_drift_60s"] = round(l2_ask_drift_60s, 4)
        if l2_ask_drift_120s is not None:
            feats["l2_ask_drift_120s"] = round(l2_ask_drift_120s, 4)
        if l2_ask_nch_30s is not None:
            feats["l2_ask_nch_30s"] = l2_ask_nch_30s
        if l2_ask_nch_60s is not None:
            feats["l2_ask_nch_60s"] = l2_ask_nch_60s
        if l1_bid_d5_now is not None:
            feats["l1_bid_d5_now"] = round(l1_bid_d5_now, 1)
        if l2_ask_d5_now is not None:
            feats["l2_ask_d5_now"] = round(l2_ask_d5_now, 1)
        if bin_price_now is not None:
            feats["bin_price_now"] = round(bin_price_now, 1)
        if mdm.bss_hold_bin_price_atleg1 is not None:
            feats["bin_price_atleg1"] = round(mdm.bss_hold_bin_price_atleg1, 1)
        if bin_delta_since_leg1 is not None:
            feats["bin_delta_since_leg1"] = round(bin_delta_since_leg1, 1)
        if bin_delta_since_leg1_bps is not None:
            feats["bin_delta_since_leg1_bps"] = round(bin_delta_since_leg1_bps, 1)
        if bin_adverse_since_leg1_bps is not None:
            feats["bin_adverse_since_leg1_bps"] = round(bin_adverse_since_leg1_bps, 1)
        if bin_ret_5s_bps is not None:
            feats["bin_ret_5s_bps"] = round(bin_ret_5s_bps, 1)
        if bin_ret_30s_bps is not None:
            feats["bin_ret_30s_bps"] = round(bin_ret_30s_bps, 1)
        if bin_ret_60s_bps is not None:
            feats["bin_ret_60s_bps"] = round(bin_ret_60s_bps, 1)
        if bin_ret_120s_bps is not None:
            feats["bin_ret_120s_bps"] = round(bin_ret_120s_bps, 1)
        if bin_vol_60s_bps is not None:
            feats["bin_vol_60s_bps"] = round(bin_vol_60s_bps, 1)
        if leg2_ask_move_since_leg1 is not None:
            feats["leg2_ask_move_since_leg1"] = round(leg2_ask_move_since_leg1, 4)
        if bin_age_ms is not None:
            feats["bin_age_ms"] = bin_age_ms
        if l1_book_age_ms is not None:
            feats["l1_book_age_ms"] = l1_book_age_ms
        if l2_book_age_ms is not None:
            feats["l2_book_age_ms"] = l2_book_age_ms
        # Time-of-day context
        try:
            dt_utc = datetime.utcfromtimestamp(now)
            feats["hod_utc"] = dt_utc.hour
            feats["dow_utc"] = dt_utc.weekday()  # 0=Mon, 6=Sun
        except Exception:
            pass

        extra_json = json.dumps(feats, separators=(',', ':'))
        note = (f"src=bss_entry,kind=hold_shadow,"
                f"hold_id={mdm.bss_hold_id},"
                f"tick={mdm.bss_hold_tick_idx},"
                f"elapsed={hold_elapsed_s:.1f}s,ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f},"
                f"yes_bid={yes_bid:.4f},no_bid={no_bid:.4f},"
                f"extra_json={extra_json}")
        row = [
            int(time.time() * 1000),
            "BSS_HOLD_SHADOW_DRY",
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            leg1_side,
            (market.yes_token_id if leg1_side == "YES"
             else market.no_token_id),
            f"{leg1_fill_ask:.4f}",
            f"{leg1_bid_now:.4f}",
            f"{leg1_size:.4f}",
            f"{leg1_qty:.4f}",
            "0.0000", "",
            "0.0000",
            f"{sell_pnl_now:.4f}",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error hold_shadow slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# v6.5.4 SKULD: orphan-sell rule (positive-exit) + v6.5.5 TP
# ═══════════════════════════════════════════════════════════════════

def _bs_is_book_locked(yes_ask: Optional[float], no_ask: Optional[float],
                        yes_bid: Optional[float], no_bid: Optional[float]) -> bool:
    """v6.5.5.2: detect stale/ghost orderbook snapshot.

    Real Polymarket orderbooks always have at least 1¢ spread (the
    platform's tick size). When `yes_ask == yes_bid` (or `no_ask ==
    no_bid`), the bot's in-memory book has frozen on a phantom snapshot
    — typically caused by a websocket gap where price-level deltas were
    missed and the book never converged back to the real state.

    The signature of these phantoms is dead simple: `yes_ask = yes_bid =
    0.505` and `no_ask = no_bid = 0.495` (the "locked at 50/50" state),
    while the real depth book shows the market actually collapsed to
    0.99/0.01 or similar extreme.

    The May 19 audit found 23 of 24 phantom orphan-sell fires occurred
    on ticks with zero spread. Decisions made on these ticks would fire
    on prices that don't exist in the real market — booking fake DRY
    profit and (in LIVE) submitting sell orders that immediately fail.

    Returns True if the book appears stale and decisions should skip.
    """
    if (yes_ask is None or yes_bid is None
            or no_ask is None or no_bid is None):
        return True  # incomplete data treated as stale (defensive)
    if yes_ask <= 0 or no_ask <= 0:
        return True  # zero prices = collapsed/uninitialized book
    # A real Polymarket orderbook always has ask > bid (at least 1¢).
    return (yes_ask - yes_bid) <= 0 or (no_ask - no_bid) <= 0


def _bs_update_band_sustain(mdm: "MultiDurationMarket",
                             condition_met: bool, now: float,
                             first_attr: str, last_attr: str,
                             grace_s: float) -> Tuple[bool, float]:
    """v6.5.5.2: update band-based sustain timestamps for a rule.

    Replaces tick-counting persist mechanism with timestamp tracking
    that tolerates brief wobble (gaps shorter than `grace_s`).

    Logic:
      - If condition is met NOW:
          * Set first_attr if None (start of run)
          * Update last_attr to now
          * Return (True, run_duration_s)
      - If condition is NOT met:
          * If we have an active run AND gap since last > grace_s:
            reset both (sustained failure)
          * Otherwise: leave timestamps alone (brief wobble tolerated)
          * Return (False, 0.0)

    The caller checks the returned run_duration against its SUSTAIN_S
    threshold and fires only when conditions are met NOW.
    """
    if condition_met:
        if getattr(mdm, first_attr) is None:
            setattr(mdm, first_attr, now)
        setattr(mdm, last_attr, now)
        first_ts = getattr(mdm, first_attr)
        return True, now - first_ts
    else:
        last_ts = getattr(mdm, last_attr)
        if last_ts is not None and (now - last_ts) > grace_s:
            setattr(mdm, first_attr, None)
            setattr(mdm, last_attr, None)
        return False, 0.0


def _bs_evaluate_orphan_sell(state: "BotState", mdm: "MultiDurationMarket",
                              now: float,
                              yes_ask: float, no_ask: float,
                              yes_bid: float, no_bid: float) -> None:
    """v6.5.4/v6.5.5: evaluate orphan exit rules on this shadow tick.

    Two independent triggers can fire (whichever first):

    (A) POSITIVE-EXIT (v6.5.4 defensive — BTC adverse, breakeven):
        sell_pnl_now            >= _BS_BSS_ORPHAN_SELL_MIN_PNL
        hold_elapsed_s          >= _BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S
        bin_adverse_since_leg1  >= _BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS
        for N consecutive shadow ticks → sell leg-1, lock in profit.

    (B) TAKE-PROFIT (v6.5.5 opportunistic — bid up ratio×):
        leg1_bid_now / leg1_entry >= _BS_BSS_ORPHAN_TP_RATIO
        for M consecutive shadow ticks → sell leg-1, lock in gain.

    Both rules NO-OP when their respective ENABLED flags are false.

    v6.5.5.1 CASHOUT FIX: sell pnl uses `leg1_top_bid` directly (the
    cashout proxy) rather than walking the bid book. Matches the May 1
    "cashout style" agreement: "cashout always works — no liquidity
    concerns". Polymarket curved fee formula (rate × p × (1-p)) is
    applied as before. The book-walk simulator (`_bss_simulate_dry_sell`)
    remains in the codebase for future LIVE-realism work but no longer
    gates firing decisions.
    """
    if mdm.bss_state != "WAITING_2ND":
        return
    if not (_BS_BSS_ORPHAN_SELL_ENABLED or _BS_BSS_ORPHAN_TP_ENABLED):
        return

    # ── v6.5.5.2 LOCKED-SPREAD REJECT ────────────────────────────────
    # If the in-memory orderbook is in the "locked at 50/50" stale state
    # (yes_ask == yes_bid, no_ask == no_bid), our price view is a phantom
    # and any sell decision based on it would fire on a price that does
    # not exist in the real market. The May 19 audit found 23 of 24
    # phantom orphan-sell fires occurred on locked-spread ticks. Skip
    # the entire evaluation, including band-sustain updates. The book
    # will recover on the next valid websocket update.
    if _bs_is_book_locked(yes_ask, no_ask, yes_bid, no_bid):
        return

    leg1_side = mdm.bss_first_side
    leg1_qty = mdm.bss_leg1_qty
    leg1_size = mdm.bss_leg1_size_usdc
    leg1_fee = mdm.bss_leg1_fee or 0.0
    leg1_entry_ask = mdm.bss_leg1_actual_ask
    if (leg1_side is None or leg1_qty is None
            or leg1_size is None or leg1_entry_ask is None):
        return  # incomplete state

    # ── COMPUTE REALISTIC SELL VIA BID-WALK ───────────────────────────
    leg1_top_bid = yes_bid if leg1_side == "YES" else no_bid
    if leg1_top_bid is None or leg1_top_bid <= 0:
        # No bid available — cannot evaluate or fire. Clear band-sustain
        # state defensively (a missing bid is sustained failure).
        mdm.bss_orphan_sell_first_qual_ts = None
        mdm.bss_orphan_sell_last_qual_ts = None
        mdm.bss_orphan_tp_first_qual_ts = None
        mdm.bss_orphan_tp_last_qual_ts = None
        return

    market = mdm.market
    token_id = (market.yes_token_id if leg1_side == "YES"
                else market.no_token_id)

    # v6.5.5.1 CASHOUT FIX: Use leg1_top_bid directly as the cashout
    # proxy, matching the shadow logger and the cashout convention we
    # agreed in the May 1 design discussion ("cashout always works — no
    # liquidity concerns; price ≈ best bid").
    #
    # v6.5.5 used `_bss_simulate_dry_sell` to walk the bid book and gate
    # on `bid_size`/levels. That was wrong: it returned no_liquidity on
    # ghost-bid moments (visible price, zero depth) and blocked 75+
    # seconds of +$0.82 fires on the 0x59bd47e1 trade. The book-walk
    # simulator is RETAINED in the codebase for future LIVE realism work
    # (and a diagnostic) but is no longer used to gate firing decisions.
    #
    # Under the cashout convention:
    #   - Fill quantity = leg1_qty (full position, no partials)
    #   - Fill price    = leg1_top_bid (the cashout proxy)
    #   - Fill fee      = Polymarket curved formula on (qty, top_bid)
    #   - Outcome       = "cashout" (informational tag)
    sell_avg_p = leg1_top_bid
    qty_sold = leg1_qty
    sell_fee = _polymarket_taker_fee(qty_sold, sell_avg_p)
    sell_outcome = "cashout"

    # Realized sell pnl (Polymarket curved fee, cashout-style fill):
    #   pnl = qty * cashout_price - sell_fee - leg1_cost - leg1_entry_fee
    sell_proceeds = qty_sold * sell_avg_p
    sell_pnl_now = sell_proceeds - sell_fee - leg1_size - leg1_fee

    hold_elapsed_s = ((now - mdm.bss_first_fill_ts)
                       if mdm.bss_first_fill_ts else 0.0)

    # ── (A) POSITIVE-EXIT RULE ───────────────────────────────────────
    # Requires Binance adverse signal. Skip the whole branch if disabled.
    if _BS_BSS_ORPHAN_SELL_ENABLED:
        bin_adverse_bps: Optional[float] = None
        try:
            if (mdm.bss_hold_bin_price_atleg1
                    and mdm.bss_hold_bin_price_atleg1 > 0
                    and state.binance_prices):
                bin_snap = list(state.binance_prices)
                if bin_snap:
                    bin_price_now = bin_snap[-1][1]
                    if bin_price_now and bin_price_now > 0:
                        bin_delta_bps = (
                            (bin_price_now / mdm.bss_hold_bin_price_atleg1) - 1.0
                        ) * 10000.0
                        if leg1_side == "YES":
                            bin_adverse_bps = -bin_delta_bps
                        else:
                            bin_adverse_bps = bin_delta_bps
        except Exception:
            pass
        if bin_adverse_bps is None:
            # No Binance context → mark conditions not-met (band logic
            # will tolerate brief gaps but reset on sustained absence).
            _bs_update_band_sustain(
                mdm, condition_met=False, now=now,
                first_attr="bss_orphan_sell_first_qual_ts",
                last_attr="bss_orphan_sell_last_qual_ts",
                grace_s=_BS_BSS_ORPHAN_SELL_GRACE_S,
            )
        else:
            pe_conditions = (
                sell_pnl_now >= _BS_BSS_ORPHAN_SELL_MIN_PNL
                and hold_elapsed_s >= _BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S
                and bin_adverse_bps >= _BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS
                # v6.5.9: when significant TTR remains, require higher P&L bar.
                # Data: 79% of PE fires with TTR>120s would have paired or
                # recovered to better P&L if held. Require $0.15 min when
                # time remains to avoid leaving paired-trade value on the table.
                and (market.end_ts - now > _BS_BSS_PE_HIGH_BAR_TTR_S
                     and sell_pnl_now >= _BS_BSS_PE_HIGH_BAR_PNL
                     or market.end_ts - now <= _BS_BSS_PE_HIGH_BAR_TTR_S)
            )
            qualified_now, run_duration_s = _bs_update_band_sustain(
                mdm, condition_met=pe_conditions, now=now,
                first_attr="bss_orphan_sell_first_qual_ts",
                last_attr="bss_orphan_sell_last_qual_ts",
                grace_s=_BS_BSS_ORPHAN_SELL_GRACE_S,
            )
            # Fire only when conditions are met RIGHT NOW (fresh bid,
            # fresh pnl) AND the qualifying run has lasted SUSTAIN_S+.
            # The intermediate wobble (within GRACE_S) is tolerated by
            # the band-sustain helper.
            if (qualified_now
                    and run_duration_s >= _BS_BSS_ORPHAN_SELL_SUSTAIN_S):
                _bs_fire_orphan_sell(state, mdm, now,
                                      sell_avg_p, qty_sold, sell_fee,
                                      sell_pnl_now, hold_elapsed_s,
                                      bin_adverse_bps, sell_outcome,
                                      reason="positive_exit",
                                      yes_ask=yes_ask, no_ask=no_ask,
                                      yes_bid=yes_bid, no_bid=no_bid)
                return

    # ── (B) TAKE-PROFIT RULE ────────────────────────────────────────
    if _BS_BSS_ORPHAN_TP_ENABLED:
        ratio = leg1_top_bid / leg1_entry_ask if leg1_entry_ask > 0 else 0.0
        tp_condition = ratio >= _BS_BSS_ORPHAN_TP_RATIO
        qualified_now_tp, run_duration_s_tp = _bs_update_band_sustain(
            mdm, condition_met=tp_condition, now=now,
            first_attr="bss_orphan_tp_first_qual_ts",
            last_attr="bss_orphan_tp_last_qual_ts",
            grace_s=_BS_BSS_ORPHAN_TP_GRACE_S,
        )
        if (qualified_now_tp
                and run_duration_s_tp >= _BS_BSS_ORPHAN_TP_SUSTAIN_S):
            _bs_fire_orphan_sell(state, mdm, now,
                                  sell_avg_p, qty_sold, sell_fee,
                                  sell_pnl_now, hold_elapsed_s,
                                  None, sell_outcome,
                                  reason="take_profit",
                                  yes_ask=yes_ask, no_ask=no_ask,
                                  yes_bid=yes_bid, no_bid=no_bid,
                                  tp_ratio=ratio)
            return

    # ── (C) REVERSE-SNIPER CASHOUT (v6.5.7 / tiered v6.5.10) ────────
    # Tiered adaptive exit: only sell when depth+TTR data shows full-loss
    # probability is high enough to justify early exit over natural recovery.
    # 820-market analysis: below winner=0.90 recovery is 20-40% — never sell.
    # At winner>=0.90+TTR<120s: 82-93% full loss — sell to recover loser bid.
    # At winner>=0.95 any TTR: 96-100% full loss — sell immediately.
    # BTC guard: BTC falling >$5/60s cuts full-loss from 82%→43% → hold.
    if _BS_BSS_ORPHAN_RS_ENABLED:
        ttr_s_rs = market.end_ts - now
        winner_ask_now = no_ask if leg1_side == "YES" else yes_ask

        # BTC 60s delta — suppress RS if BTC moving in recovery direction
        _rs_btc_delta = 0.0
        try:
            if state.binance_prices and len(state.binance_prices) >= 2:
                _bp = list(state.binance_prices)
                _price_now = _bp[-1][1]
                _ts_now_ms = _bp[-1][0]
                _price_60s = next(
                    (p for ts, p in reversed(_bp)
                     if _ts_now_ms - ts >= 60_000), _bp[0][1]
                )
                _rs_btc_raw = _price_now - _price_60s
                # sign: positive = BTC rising, negative = BTC falling
                # For YES-up orphan: if leg1=NO and BTC is falling → good for NO
                # For NO-up orphan: if leg1=YES and BTC is rising → good for YES
                if leg1_side == "NO":   # we hold NO, winner=YES
                    _rs_btc_delta = -_rs_btc_raw  # falling BTC helps NO
                else:                   # we hold YES, winner=NO
                    _rs_btc_delta = _rs_btc_raw   # rising BTC helps YES
        except Exception:
            pass

        # BTC guard: if BTC is moving favourably for our position, hold
        _btc_recovery_signal = (
            _BS_BSS_ORPHAN_RS_BTC_GUARD_USD > 0
            and _rs_btc_delta > _BS_BSS_ORPHAN_RS_BTC_GUARD_USD
        )

        # Tiered condition: Tier1 (any TTR), Tier2, Tier3 (TTR-gated)
        rs_condition = (
            hold_elapsed_s >= _BS_BSS_ORPHAN_RS_MIN_ELAPSED_S
            and leg1_top_bid > 0
            and not _btc_recovery_signal
            and (
                winner_ask_now >= _BS_BSS_ORPHAN_RS_TIER1_WIN
                or (winner_ask_now >= _BS_BSS_ORPHAN_RS_TIER2_WIN
                    and ttr_s_rs <= _BS_BSS_ORPHAN_RS_TIER2_TTR_S)
                or (winner_ask_now >= _BS_BSS_ORPHAN_RS_TIER3_WIN
                    and ttr_s_rs <= _BS_BSS_ORPHAN_RS_TIER3_TTR_S)
            )
        )
        qualified_now_rs, run_duration_s_rs = _bs_update_band_sustain(
            mdm, condition_met=rs_condition, now=now,
            first_attr="bss_orphan_rs_first_qual_ts",
            last_attr="bss_orphan_rs_last_qual_ts",
            grace_s=_BS_BSS_ORPHAN_RS_GRACE_S,
        )
        if (qualified_now_rs
                and run_duration_s_rs >= _BS_BSS_ORPHAN_RS_SUSTAIN_S):
            _bs_fire_orphan_sell(state, mdm, now,
                                  sell_avg_p, qty_sold, sell_fee,
                                  sell_pnl_now, hold_elapsed_s,
                                  None, sell_outcome,
                                  reason="reverse_sniper",
                                  yes_ask=yes_ask, no_ask=no_ask,
                                  yes_bid=yes_bid, no_bid=no_bid,
                                  tp_ratio=winner_ask_now)
            return


def _bs_fire_orphan_sell(state: "BotState", mdm: "MultiDurationMarket",
                          now: float,
                          sell_avg_p: float, qty_sold: float,
                          sell_fee: float, sell_pnl_now: float,
                          hold_elapsed_s: float,
                          bin_adverse_bps: Optional[float],
                          sell_outcome: str,
                          reason: str,
                          yes_ask: float, no_ask: float,
                          yes_bid: float, no_bid: float,
                          tp_ratio: Optional[float] = None) -> None:
    """v6.5.5/v6.5.6: execute the orphan-sell action.

    v6.5.6: In LIVE mode, this now submits a REAL FAK sell order to the
    Polymarket CLOB via `_bss_place_live_sell`. Three branches:

      - filled_live  → actual full fill. Use ACTUAL fill price (not the
                       snapshot caller passed in) for P&L. Update state
                       to ORPHAN_SOLD, book P&L, log event.
      - partial_live → some shares matched, less than full. Book P&L for
                       sold portion proportionally, reduce mdm.bss_leg1_*
                       to reflect remaining shares, transition state to
                       ORPHAN_SOLD_PARTIAL. Remaining shares hold to
                       resolution (no further orphan-sell attempts).
      - rejected/error/no_client → don't change state, don't update P&L
                       counter. Log BSS_ORPHAN_SELL_LIVE_FAIL. Position
                       stays in WAITING_2ND. Band-sustain timestamps
                       remain valid; rule may fire again next tick.

    v6.5.4/v6.5.5: in DRY mode, no real order. Logs BSS_ORPHAN_SELL_DRY
    (positive-exit) or BSS_ORPHAN_TP_DRY (take-profit) event, updates
    dashboard P&L counter, records to trade history for dashboard
    last-15-trades display, transitions state to terminal.

    Caller passes (sell_avg_p, qty_sold, sell_fee, sell_pnl_now)
    computed from the cashout convention as the DESIRED sale. In LIVE
    these are inputs to the order; the actual fill may differ. In DRY
    these become the recorded values directly.
    """
    leg1_side = mdm.bss_first_side
    market = mdm.market
    leg1_entry_ask = mdm.bss_leg1_actual_ask or 0.0
    leg1_size = mdm.bss_leg1_size_usdc or 0.0
    leg1_qty = mdm.bss_leg1_qty or 0.0
    leg1_fee = mdm.bss_leg1_fee or 0.0
    token_id = (market.yes_token_id if leg1_side == "YES"
                else market.no_token_id)
    is_live = (state.config.mode == "live")

    # ── v6.5.6: LIVE BRANCH — submit actual FAK SELL order ───────────
    # In LIVE we attempt the real order BEFORE updating any state. If
    # the order fails or partially fills, the recorded P&L and state
    # must reflect what actually happened, not the requested cashout.
    actual_outcome = sell_outcome  # default: caller's "cashout" tag (DRY)
    if is_live:
        live_fill_p, live_fill_qty, live_fee, live_out = (
            _bss_place_live_sell(
                state, token_id,
                decision_price=sell_avg_p, qty_shares=qty_sold,
            )
        )
        if live_out in ("rejected", "error", "no_client"):
            # Order didn't go through. Don't change state, don't book
            # P&L, just log the failure so we have a record. The next
            # shadow tick will re-evaluate the rule; if conditions are
            # still met (and band-sustain still valid) it will retry.
            try:
                event_fail = ("BSS_ORPHAN_TP_LIVE_FAIL"
                              if reason == "take_profit"
                              else "BSS_ORPHAN_SELL_LIVE_FAIL")
                note_fail = (f"src=bss_entry,leg=1,side={leg1_side},"
                              f"reason={reason},attempted_price={sell_avg_p:.4f},"
                              f"attempted_qty={qty_sold:.4f},"
                              f"outcome={live_out},"
                              f"hold_elapsed={hold_elapsed_s:.1f}s,"
                              f"ttr={market.end_ts - now:.0f}s")
                if state.bs_trades_logger is not None:
                    row = [
                        int(now * 1000), event_fail,
                        market.condition_id, market.slug, market.market_url,
                        f"{market.end_ts:.0f}", leg1_side, token_id,
                        f"{leg1_entry_ask:.4f}", f"{sell_avg_p:.4f}",
                        f"{leg1_size:.4f}", f"{qty_sold:.4f}",
                        "0.0000", "", "0.0000", "0.0000",
                        state.config.mode, note_fail,
                    ]
                    state.bs_trades_logger.log(row)
            except Exception as e:
                print(f"[bss_orphan_sell] LIVE fail log error: "
                      f"{type(e).__name__}: {e}", flush=True)
            return  # Don't update state, don't book P&L.

        # filled_live or partial_live — recompute everything from ACTUAL
        # fill data (price and qty), not the caller's snapshot.
        sell_avg_p = live_fill_p
        qty_sold = live_fill_qty
        sell_fee = live_fee
        actual_outcome = live_out

        # P&L for the SOLD portion only (works for both full and partial).
        # For partial, the entry-side cost and fee must be proportional
        # to the fraction sold: cost_sold = leg1_size * (sold/leg1_qty),
        # entry_fee_sold = leg1_fee * (sold/leg1_qty).
        if leg1_qty > 0:
            sold_fraction = min(qty_sold / leg1_qty, 1.0)
        else:
            sold_fraction = 1.0
        cost_sold = leg1_size * sold_fraction
        entry_fee_sold = leg1_fee * sold_fraction
        sell_proceeds = qty_sold * sell_avg_p
        sell_pnl_now = sell_proceeds - sell_fee - cost_sold - entry_fee_sold

    # ── Stamp the sell on the mdm for downstream / dashboard visibility
    mdm.bss_orphan_sold_at = sell_avg_p
    mdm.bss_orphan_sold_ts = now
    mdm.bss_orphan_sold_pnl = sell_pnl_now
    mdm.bss_orphan_sold_reason = reason

    if actual_outcome == "partial_live":
        # Some shares remain unsold; they hold to natural market
        # resolution. Reduce mdm.bss_leg1_* so the resolution flow
        # settles only the remaining qty, not the original.
        remaining_qty = leg1_qty - qty_sold
        remaining_size = leg1_size * (remaining_qty / leg1_qty if leg1_qty > 0 else 0.0)
        remaining_fee = leg1_fee * (remaining_qty / leg1_qty if leg1_qty > 0 else 0.0)
        mdm.bss_leg1_qty = remaining_qty
        mdm.bss_leg1_size_usdc = remaining_size
        mdm.bss_leg1_fee = remaining_fee
        # New terminal-ish state. The entry evaluator's terminal-state
        # list and resolution gate both treat ORPHAN_SOLD_PARTIAL as a
        # held position awaiting natural settlement (see v6.5.6 entry
        # evaluator updates).
        mdm.bss_state = "ORPHAN_SOLD_PARTIAL"
    else:
        # Full fill (LIVE) or DRY simulated sell — terminal.
        mdm.bss_state = "ORPHAN_SOLD"

    # Update P&L counter (dashboard reflects this immediately)
    state.bs_pnl_today_usdc += sell_pnl_now
    state.bs_total_sold_loser += 1   # reuse existing counter for visibility

    # Log the event
    try:
        if reason == "take_profit":
            event = "BSS_ORPHAN_TP_LIVE" if is_live else "BSS_ORPHAN_TP_DRY"
        else:
            event = "BSS_ORPHAN_SELL_LIVE" if is_live else "BSS_ORPHAN_SELL_DRY"
        ttr_s = market.end_ts - now
        note = (f"src=bss_entry,leg=1,side={leg1_side},reason={reason},"
                f"entry_ask={leg1_entry_ask:.4f},"
                f"sold_at={sell_avg_p:.4f},"
                f"sell_outcome={actual_outcome},"
                f"qty_sold={qty_sold:.4f},"
                f"sell_fee={sell_fee:.4f},"
                f"sell_pnl={sell_pnl_now:+.4f},"
                f"hold_elapsed={hold_elapsed_s:.1f}s,"
                f"bin_adverse_bps={bin_adverse_bps if bin_adverse_bps is None else f'{bin_adverse_bps:+.1f}'},"
                f"tp_ratio={tp_ratio if tp_ratio is None else f'{tp_ratio:.3f}'},"
                f"ttr={ttr_s:.0f}s")
        if state.bs_trades_logger is not None:
            row = [
                int(now * 1000),
                event,
                market.condition_id,
                market.slug,
                market.market_url,
                f"{market.end_ts:.0f}",
                leg1_side,
                token_id,
                f"{leg1_entry_ask:.4f}",
                f"{sell_avg_p:.4f}",
                f"{leg1_size:.4f}",
                f"{qty_sold:.4f}",
                f"{sell_avg_p:.4f}",
                f"{now:.0f}",
                f"{sell_pnl_now:+.4f}",
                "0.0000",
                state.config.mode,
                note,
            ]
            state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_orphan_sell] log error slug={market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)

    # v6.5.5: record in bs_trade_history so dashboard "last 15" shows it
    try:
        _bs_record_orphan_sold_history(state, mdm, now,
                                        sell_avg_p, qty_sold,
                                        sell_pnl_now, hold_elapsed_s,
                                        bin_adverse_bps, reason, tp_ratio)
    except Exception as e:
        print(f"[bss_orphan_sell] history record error: "
              f"{type(e).__name__}: {e}", flush=True)

    # Clear ring buffer — position closed
    try:
        _v653_buf_clear(market.condition_id)
    except Exception:
        pass

    extra = (f"adv={bin_adverse_bps:+.1f}bps" if bin_adverse_bps is not None
             else f"ratio={tp_ratio:.3f}")
    print(f"[bss_orphan_sell] market={market.condition_id[:10]}… "
          f"reason={reason} side={leg1_side} "
          f"entry={leg1_entry_ask:.4f}→sold@{sell_avg_p:.4f} "
          f"({sell_outcome}) pnl=${sell_pnl_now:+.4f} "
          f"elapsed={hold_elapsed_s:.0f}s {extra} "
          f"ttr={(market.end_ts - now):.0f}s", flush=True)


def _bs_record_orphan_sold_history(state: "BotState", mdm: "MultiDurationMarket",
                                     now: float,
                                     sell_avg_p: float, qty_sold: float,
                                     sell_pnl: float, hold_elapsed_s: float,
                                     bin_adverse_bps: Optional[float],
                                     reason: str,
                                     tp_ratio: Optional[float]) -> None:
    """v6.5.5: append the orphan-sold position to bs_trade_history for
    the dashboard last-15-trades display. Schema is similar to paired
    trades but with `outcome='ORPHAN_SOLD'` and includes sell-side fields
    (sell_price, sell_ts_offset, sell_pnl, sell_reason, etc).
    """
    market = mdm.market
    leg1_side = mdm.bss_first_side or ""
    leg1_entry_ask = mdm.bss_leg1_actual_ask or 0.0
    leg1_size = mdm.bss_leg1_size_usdc or 0.0
    leg1_qty = mdm.bss_leg1_qty or 0.0
    entry_ts = mdm.bss_first_fill_ts or now

    entry = {
        "market_id": market.condition_id,
        "market_url": market.market_url,
        "slug": market.slug,
        "outcome": "ORPHAN_SOLD",
        "market_winner": "",   # we don't know yet — market still pending
        "had_sell_loser": False,
        # The leg we held (use yes_* fields if leg1=YES else no_*; the
        # OTHER side is empty since orphan never filled leg-2)
        "leg1_side": leg1_side,
        "leg1_entry_ask": round(leg1_entry_ask, 4),
        "leg1_qty": round(leg1_qty, 4),
        "leg1_size_usdc": round(leg1_size, 4),
        # Sell-specific fields (new in v6.5.5)
        "sell_price": round(sell_avg_p, 4),
        "sell_qty": round(qty_sold, 4),
        "sell_pnl": round(sell_pnl, 4),
        "sell_reason": reason,
        "hold_elapsed_s": round(hold_elapsed_s, 1),
        "bin_adverse_bps": (None if bin_adverse_bps is None
                             else round(bin_adverse_bps, 1)),
        "tp_ratio": (None if tp_ratio is None else round(tp_ratio, 3)),
        # Compat fields for dashboard renderer (paired-trade schema)
        "total_pnl": round(sell_pnl, 4),
        "sum_ask_at_entry": round(leg1_entry_ask, 4),
        "entry_ts": entry_ts,
        "close_ts": now,
        # Stub the yes_/no_ leg fields — dashboard branches on outcome
        "yes_entry_ask": (round(leg1_entry_ask, 4) if leg1_side == "YES"
                           else 0.0),
        "yes_close_price": (round(sell_avg_p, 4) if leg1_side == "YES"
                              else 0.0),
        "yes_pnl": (round(sell_pnl, 4) if leg1_side == "YES" else 0.0),
        "no_entry_ask": (round(leg1_entry_ask, 4) if leg1_side == "NO"
                          else 0.0),
        "no_close_price": (round(sell_avg_p, 4) if leg1_side == "NO"
                             else 0.0),
        "no_pnl": (round(sell_pnl, 4) if leg1_side == "NO" else 0.0),
    }
    state.bs_trade_history.append(entry)
    # Bound memory (mirror paired-history behavior)
    if len(state.bs_trade_history) > 100:
        state.bs_trade_history = state.bs_trade_history[-100:]


def _bs_log_bss_leg_fill_event(state: BotState, mdm: MultiDurationMarket,
                                  now: float, leg_num: int, fire_side: str,
                                  decision_ask: float, fill_ask: float,
                                  fill_qty: float, fee: float, size_usdc: float,
                                  outcome: str, sus_s: float, is_live: bool,
                                  yes_ask: float, no_ask: float,
                                  yes_bid: float, no_bid: float,
                                  phase: Optional[str] = None) -> None:
    """v6.5.0: log a per-leg fill event. event = BSS_LEG_FILL_DRY or _LIVE.
    Captures actual fill data (price, qty, fee) separately from decision price.
    Slippage = fill_ask - decision_ask (positive when book moved up).

    v6.5.5.2: optional `phase` parameter ("floor" | "strict" | "relaxed")
    surfaces second-leg firing path in the CSV note for analytics. Floor
    fires (sustain=0, deep-dip) and strict/relaxed (timed sustain) are
    logged identically otherwise; this distinction was previously visible
    only in stdout log lines, not in trade history.
    """
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        event = "BSS_LEG_FILL_LIVE" if is_live else "BSS_LEG_FILL_DRY"
        slippage = fill_ask - decision_ask
        ttr_s = market.end_ts - now
        phase_part = f",phase={phase}" if phase else ""
        note = (f"src=bss_entry,leg={leg_num}{phase_part},"
                f"decision_ask={decision_ask:.4f},"
                f"fill_ask={fill_ask:.4f},slippage={slippage:+.4f},"
                f"qty={fill_qty:.4f},fee={fee:.4f},"
                f"outcome={outcome},sustain={sus_s:.1f}s,ttr={ttr_s:.0f}s")
        token_id = (market.yes_token_id if fire_side == "YES"
                    else market.no_token_id)
        bid_at_fire = yes_bid if fire_side == "YES" else no_bid
        row = [
            int(now * 1000),
            event,
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            fire_side,
            token_id,
            f"{fill_ask:.4f}",
            f"{bid_at_fire:.4f}",
            f"{size_usdc:.4f}",
            f"{fill_qty:.4f}",
            "0.0000", "",
            f"{-fee:+.4f}",       # entry P&L = negative fee (cost paid)
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
        # v6.5.4: dashboard P&L fix. Until v6.5.3.x, the dashboard counter
        # state.bs_pnl_today_usdc was updated only on RESOLVE events (which
        # reflect gross win/loss without fees). The per-leg taker fee was
        # logged to CSV here (as pnl=-fee) but never accumulated. Result:
        # dashboard showed +$22.86 while real net was +$8.29 (a $14.57 gap
        # in the 49.5h audit, exactly matching 743 fills × $0.02 fee).
        # Add the fee here so the dashboard reflects true net.
        state.bs_pnl_today_usdc -= fee
    except Exception as e:
        print(f"[bss_log] error leg_fill slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


def _bs_log_bss_leg_fok_fail_event(state: BotState, mdm: MultiDurationMarket,
                                     now: float, leg_num: int, fire_side: str,
                                     decision_ask: float, outcome: str,
                                     yes_ask: float, no_ask: float,
                                     yes_bid: float, no_bid: float) -> None:
    """v6.5.0: log a per-leg FOK/fill-failure event. event = BSS_LEG_FOK_FAIL_DRY
    or _LIVE. Captures the failure reason (rejected, error, no_book, etc.).
    Logged ONCE per failure attempt. The state machine prevents retry-loop
    spam by resetting sustain timers — no new event will fire until the dip
    sustains afresh."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        is_live = (state.config.mode == "live")
        event = "BSS_LEG_FOK_FAIL_LIVE" if is_live else "BSS_LEG_FOK_FAIL_DRY"
        ttr_s = market.end_ts - now
        note = (f"src=bss_entry,leg={leg_num},"
                f"decision_ask={decision_ask:.4f},"
                f"outcome={outcome},ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f}")
        token_id = (market.yes_token_id if fire_side == "YES"
                    else market.no_token_id)
        size = state.config.position_size_usdc
        row = [
            int(now * 1000),
            event,
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            fire_side,
            token_id,
            f"{decision_ask:.4f}",
            "0.0000",
            f"{size:.4f}",
            "0.0000",
            "0.0000", "",
            "0.0000",
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error leg_fok_fail slug={mdm.market.slug}: "
              f"{type(e).__name__}: {e}", flush=True)


def _bs_log_bss_second_leg_event(state: BotState, mdm: MultiDurationMarket,
                                   pos: BothSidesPosition, now: float,
                                   yes_ask: float, no_ask: float,
                                   yes_bid: float, no_bid: float,
                                   sus_s: float, threshold: float) -> None:
    """v6.3.0: kept for backward compat. v6.5.0 now writes BSS_LEG_FILL_DRY/LIVE
    as the primary leg-2 event; this BSS_SECOND_LEG_DRY row is also written
    for downstream parsers that key on the old name."""
    if state.bs_trades_logger is None:
        return
    second_side = "NO" if mdm.bss_first_side == "YES" else "YES"
    second_leg = pos.no_leg if second_side == "NO" else pos.yes_leg
    elapsed_s = now - (mdm.bss_first_fill_ts or now)
    note = (f"src=bss_entry,phase={mdm.bss_second_phase},"
            f"sustain={sus_s:.1f}s,threshold={threshold:.4f},"
            f"elapsed={elapsed_s:.1f}s,"
            f"first_paid={mdm.bss_leg1_actual_ask or 0.0:.4f},"
            f"second_paid={mdm.bss_leg2_actual_ask or 0.0:.4f},"
            f"sum_ask={(mdm.bss_leg1_actual_ask or 0.0) + (mdm.bss_leg2_actual_ask or 0.0):.4f}")
    _bs_log_trade_event(state, "BSS_SECOND_LEG_DRY", pos, second_leg,
                         note=note)


def _bs_log_bss_orphan_end_event(state: BotState, mdm: MultiDurationMarket,
                                    now: float, yes_ask: float, no_ask: float,
                                    yes_bid: float, no_bid: float) -> None:
    """v6.5.0: log a BSS_ORPHAN_END event when window closes with leg 1
    held but no leg 2. No sell happens. Just records the transition for
    analysis — the actual P&L will be logged at RESOLVE time when the CTF
    pays out the held position."""
    if state.bs_trades_logger is None:
        return
    try:
        market = mdm.market
        first_ask = mdm.bss_leg1_actual_ask or 0.0
        first_qty = mdm.bss_leg1_qty or 0.0
        first_size = mdm.bss_leg1_size_usdc or 0.0
        first_fee = mdm.bss_leg1_fee or 0.0
        held_ago_s = now - (mdm.bss_first_fill_ts or now)
        ttr_s = market.end_ts - now
        note = (f"src=bss_entry,first_side={mdm.bss_first_side},"
                f"first_ask={first_ask:.4f},first_qty={first_qty:.4f},"
                f"first_size={first_size:.4f},first_fee={first_fee:.4f},"
                f"held_ago_s={held_ago_s:.0f},ttr={ttr_s:.0f}s,"
                f"yes_ask={yes_ask:.4f},no_ask={no_ask:.4f},"
                f"yes_bid={yes_bid:.4f},no_bid={no_bid:.4f}")
        token_id = (market.yes_token_id if mdm.bss_first_side == "YES"
                    else market.no_token_id)
        bid = yes_bid if mdm.bss_first_side == "YES" else no_bid
        row = [
            int(now * 1000),
            "BSS_ORPHAN_END",
            market.condition_id,
            market.slug,
            market.market_url,
            f"{market.end_ts:.0f}",
            mdm.bss_first_side or "",
            token_id,
            f"{first_ask:.4f}",
            f"{bid:.4f}",
            f"{first_size:.4f}",
            f"{first_qty:.4f}",
            "0.0000", "",
            "0.0000",       # P&L recorded at RESOLVE_WIN/RESOLVE_LOSS later
            "0.0000",
            state.config.mode,
            note,
        ]
        state.bs_trades_logger.log(row)
    except Exception as e:
        print(f"[bss_log] error orphan_end slug={mdm.market.slug}: "
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

    # --- Source 4: live WS book — REMOVED in v6.4.0 ---
    # Reading the WS book at end_ts to infer the winner from which side's
    # ask is higher was WRONG in 2 of 3 cases on May 7 (BTC moved $228+
    # opposite to the bot's claimed winner). The book at end_ts is in
    # mid-clearing transition and unreliable even with chaos detection.
    # Source ordering is now: chainlink → binance → cache (with chaos
    # check) → gamma. No "live" inference.

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

    # v6.4.0 SKULD: write resolution_audit_log row.
    # Independent BTC cross-check: compares the bot's claimed winner against
    # the BTC direction implied by binance_prices over the market window.
    # Disagreements are real signal, not necessarily bugs (knife-edge prices
    # can resolve either way per Polymarket's Chainlink oracle), but the
    # audit gives us evidence to diagnose source-bug regressions.
    try:
        if state.resolution_audit_logger is not None and state.resolution_audit_logger.enabled:
            start_ts = pos.end_ts - pos.duration_s
            btc_at_start = None
            btc_at_end = None
            if state.binance_prices:
                snapshot = list(state.binance_prices)
                def _closest_at(target_ts: float, tol_s: float = 30.0):
                    best = None; best_dt = float('inf')
                    for ts, p in snapshot:
                        dt = abs(ts - target_ts)
                        if dt < best_dt and dt <= tol_s:
                            best_dt = dt; best = p
                    return best
                btc_at_start = _closest_at(start_ts)
                btc_at_end = _closest_at(pos.end_ts)
            btc_delta_usd = None
            btc_delta_pct = None
            btc_implied = ""
            if btc_at_start is not None and btc_at_end is not None and btc_at_start > 0:
                btc_delta_usd = btc_at_end - btc_at_start
                btc_delta_pct = btc_delta_usd / btc_at_start * 100.0
                if btc_delta_usd > 0:
                    btc_implied = "YES"
                elif btc_delta_usd < 0:
                    btc_implied = "NO"
                else:
                    btc_implied = "TIE"
            claimed = "YES" if yes_won else "NO"
            agreement = ""
            if btc_implied in ("YES", "NO"):
                agreement = "AGREE" if btc_implied == claimed else "DISAGREE"
            elif btc_implied == "TIE":
                agreement = "TIE"
            else:
                agreement = "NO_DATA"
            state.resolution_audit_logger.log([
                int(now * 1000),
                pos.market_id,
                pos.slug,
                f"{pos.end_ts:.0f}",
                f"{pos.duration_s}",
                claimed,
                source,
                f"{btc_at_start:.4f}" if btc_at_start is not None else "",
                f"{btc_at_end:.4f}" if btc_at_end is not None else "",
                f"{btc_delta_usd:+.4f}" if btc_delta_usd is not None else "",
                f"{btc_delta_pct:+.6f}" if btc_delta_pct is not None else "",
                btc_implied,
                agreement,
                f"{pos_pnl_total:+.4f}",
            ])
    except Exception as e:
        print(f"[resolution_audit] error market={pos.market_id[:10]}: "
              f"{type(e).__name__}: {e}", flush=True)

    del state.both_sides_positions[pos.market_id]


def both_sides_tick(state: BotState) -> None:
    """Called from main_loop.

    DEFAULT (_bs_default_runtime_active()): paired YES+NO entry + sell-loser.
      Entry skipped when state.trading_paused=True. WAITING_2ND/ORPHAN_END
      unreachable under default env.

    LEGACY (_bs_bss_runtime_active()): handled by bss_fast_tick_thread +
      bss_gamma_poll_thread. Returns immediately.
    """
    if not _BS_ACTIVE:
        return
    if not _bs_default_runtime_active():
        return
    now = time.time()

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
                    _bs_log_trade_event(state, "SELL_LOSER_DRY", pos, pos.yes_leg,
                        note=f"winner_ask={winner_ask:.3f},loser_bid={loser_bid:.3f},{diag}")
                    print(f"[bs_sell] market={pos.market_id[:10]}… loser=YES "
                          f"sold@{loser_bid:.3f} pnl={pos.yes_leg.pnl_usdc:+.4f} "
                          f"src={fire_source} TTR={pos.end_ts - now:.0f}s", flush=True)
                elif loser_side == "NO" and not pos.no_leg.closed:
                    _bs_close_leg(pos.no_leg, loser_bid, now, "sell_loser")
                    state.bs_total_sold_loser += 1
                    state.bs_pnl_today_usdc += pos.no_leg.pnl_usdc
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
            # v6.5.11: skipped when BS_TIER_ENABLED — operator chose pure-numbers
            # design (Option E). The BTC late-fallback is directional and was
            # explicitly out of scope. Set BS_TIER_ENABLED=false to restore.
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
            # v6.5.11: skipped when BS_TIER_ENABLED — see btc_late guard above.
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
            # Fire — close the loser leg at its current bid
            # v6.1.7: compute richer diagnostics ONCE per fire, used in both
            # YES-loser and NO-loser branches below.
            diag = _bs_compute_sell_loser_diagnostics(state, pos, now)
            diag = f"src={fire_source},{diag}"
            if loser_side == "YES" and not pos.yes_leg.closed:
                _bs_close_leg(pos.yes_leg, loser_bid, now, "sell_loser")
                state.bs_total_sold_loser += 1
                state.bs_pnl_today_usdc += pos.yes_leg.pnl_usdc
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


def _health_log_tick(state: BotState) -> None:
    """v6.4.0 SKULD: writes a health snapshot row every
    BS_HEALTH_LOG_INTERVAL_S (default 10s). Captures WS+API health and
    bot-state counters so a postmortem can answer 'why did the bot stop
    trading at HH:MM' from the CSV alone.

    No-op when health_logger is None or disabled.
    """
    if state.health_logger is None or not state.health_logger.enabled:
        return
    now = time.time()
    if now - state.last_health_log_ts < _BS_HEALTH_LOG_INTERVAL_S:
        return
    state.last_health_log_ts = now
    try:
        binance_age = (now - state.binance_last_msg_ts) if state.binance_last_msg_ts else None
        poly_age = (now - state.poly_last_msg_ts) if state.poly_last_msg_ts else None
        # CLOB health rolling counters (best effort — fields may not exist)
        clob_calls = getattr(state, 'clob_calls_60s', 0)
        clob_2xx = getattr(state, 'clob_2xx_60s', 0)
        clob_4xx = getattr(state, 'clob_4xx_60s', 0)
        clob_5xx = getattr(state, 'clob_5xx_60s', 0)
        # BSS state counts
        bss_state_counts = {}
        bss_watching_n = 0
        try:
            for mdm in state.bs_5m_in_window.values():
                st = getattr(mdm, 'bss_state', None) or "NONE"
                bss_state_counts[st] = bss_state_counts.get(st, 0) + 1
                if st in ("WATCH", "WAITING_2ND"):
                    bss_watching_n += 1
        except Exception:
            pass
        state.health_logger.log([
            int(now * 1000),
            f"{state.uptime_s:.1f}",
            BOT_VERSION,
            state.config.mode,
            "true" if _LIVE_BSS_ENABLED else "false",
            f"{binance_age:.2f}" if binance_age is not None else "",
            len(state.binance_prices) if state.binance_prices is not None else 0,
            f"{poly_age:.2f}" if poly_age is not None else "",
            len(state.poly_books) if state.poly_books is not None else 0,
            clob_calls, clob_2xx, clob_4xx, clob_5xx,
            len(state.both_sides_positions) if state.both_sides_positions is not None else 0,
            state.bs_total_entered,
            state.bs_total_resolved,
            f"{state.bs_pnl_today_usdc:+.4f}",
            bss_watching_n,
            ";".join(f"{k}={v}" for k, v in sorted(bss_state_counts.items())),
        ])
    except Exception as e:
        print(f"[health_log] error: {type(e).__name__}: {e}", flush=True)


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
    # v6.4.0: DISABLED — pre-market observation removed. Logger object
    # is still constructed for compat but force-disabled below.
    pre_market_books_header = [
        "ts_ms", "duration_label", "market_id", "slug", "market_url", "end_ts", "ttr_s",
        "yes_ask", "yes_bid", "yes_ask_size", "yes_bid_size",
        "no_ask", "no_bid", "no_ask_size", "no_bid_size",
        "sum_ask", "sum_bid", "btc_price_now", "mode", "has_position",
    ]
    state.pre_market_books_logger = CsvLogger(
        log_dir, "pre_market_books", pre_market_books_header,
    )
    # v6.4.0: hard-disable. No pre-market logging in Skuld.
    state.pre_market_books_logger.enabled = False

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

    # v6.4.0 SKULD: resolution_audit_log_<date>.csv
    # Per-market BTC-cross-check at resolution. Compares bot's claimed
    # winner against actual BTC direction from the binance_prices feed.
    # Lets us spot resolution-source bugs (like the source=live issue
    # killed in v6.4.0) without needing offline analysis.
    resolution_audit_header = [
        "ts_ms", "market_id", "slug", "end_ts", "duration_s",
        "claimed_winner", "claimed_source",
        "btc_at_start", "btc_at_end", "btc_delta_usd", "btc_delta_pct",
        "btc_implied_winner", "agreement", "pnl_usdc",
    ]
    state.resolution_audit_logger = CsvLogger(
        log_dir, "resolution_audit", resolution_audit_header,
    )

    # v6.4.0 SKULD: health_log_<date>.csv
    # WS + API health snapshot every BS_HEALTH_LOG_INTERVAL_S (default 10s).
    # Lets us debug "why did the bot stop trading" with timestamped state.
    health_log_header = [
        "ts_ms", "uptime_s", "bot_version", "mode", "live_bss_enabled",
        "binance_ws_last_msg_age_s", "binance_prices_n",
        "poly_ws_last_msg_age_s", "poly_books_n",
        "clob_calls_60s", "clob_2xx_60s", "clob_4xx_60s", "clob_5xx_60s",
        "bs_open", "bs_total_entered", "bs_total_resolved", "pnl_today_usdc",
        "bss_watching_n", "bss_state_counts",
    ]
    state.health_logger = CsvLogger(
        log_dir, "health_log", health_log_header,
    )

    if not cfg.log_to_disk:
        state.binance_logger.enabled = False
        state.signal_logger.enabled = False
        state.trades_logger.enabled = False
        state.depth_logger.enabled = False
        state.flow_logger.enabled = False
        state.pre_market_books_logger.enabled = False
        state.bs_trades_logger.enabled = False
        state.resolution_audit_logger.enabled = False
        state.health_logger.enabled = False
        print("[boot] CSV logging DISABLED (LOG_TO_DISK=false)", flush=True)
    else:
        print(f"[boot] CSV logging enabled → {log_dir}", flush=True)
        print(f"[boot]   datasets: binance_prices, signal_log, trades, "
              f"depth_log, flow_log, bs_trades, resolution_audit, health_log",
              flush=True)
        print(f"[boot]   pre_market_books: DISABLED in v6.4.0 (no pre-market)",
              flush=True)

    # Called here: state.log_dir set, all loggers initialized, both BOT_NAME
    # and legacy branches covered.
    _ensure_validation_csvs(state)

    # v6.4.0 SKULD: config_snapshot_<boot_ts>.json
    # Dump every effective config value the bot is running with, once at boot.
    # Filename includes boot ts so multiple runs in the same day don't overwrite.
    try:
        boot_ts_ms = int(time.time() * 1000)
        snapshot = {
            "boot_ts_ms": boot_ts_ms,
            "boot_iso_utc": datetime.now(timezone.utc).isoformat(),
            "bot_version": BOT_VERSION,
            "mode": cfg.mode,
            "live_bss_enabled": _LIVE_BSS_ENABLED,
            "bot_name": _BOT_NAME,
            "data_dir": cfg.data_dir,
            "log_to_disk": cfg.log_to_disk,
            "validation_mode": cfg.validation_mode,
            "position_size_usdc": cfg.position_size_usdc,
            "daily_loss_limit_usdc": cfg.daily_loss_limit_usdc,
            "force_signature_type": cfg.force_signature_type,
            "proxy_wallet": cfg.proxy_wallet,
            "strategy": {
                "strategy_mode": _STRATEGY_MODE,
                "bs_strategy": _BS_STRATEGY,
                "bs_active": _BS_ACTIVE,
                "bs_lead_min_s": _BS_LEAD_MIN_S,
                "bs_lead_max_s": _BS_LEAD_MAX_S,
                "bs_sum_ask_max": _BS_SUM_ASK_MAX,
            },
            "bss_thresholds": {
                "t_first": _BS_BSS_T_FIRST,
                "sustain_first_s": _BS_BSS_SUSTAIN_FIRST_S,
                "t_second_strict": _BS_BSS_T_SECOND_STRICT,
                "t_second_relaxed": _BS_BSS_T_SECOND_RELAXED,
                "sustain_second_s": _BS_BSS_SUSTAIN_SECOND_S,
                "relax_at_s": _BS_BSS_RELAX_AT_S,
                "abort_at_s": _BS_BSS_ABORT_AT_S,
                "btc_vel_filter_pct": _BS_BSS_BTC_VEL_FILTER_PCT,
                "btc_vel_lookback_s": _BS_BSS_BTC_VEL_LOOKBACK_S,
                "tick_interval_s": _BS_BSS_TICK_INTERVAL_S,
            },
            "dry_simulation": {
                "_design": "v6.5.0 — book-walk + taker fee, no latency sleep, no FOK-fail-on-drift",
                "book_walk_enabled": _BS_BOOK_WALK_ENABLED,
                "taker_fee_pct": _BS_TAKER_FEE_PCT,
            },
            "v640_deleted_envs": {
                "BS_LIVE_SIM_ENABLED": os.environ.get("BS_LIVE_SIM_ENABLED", "<unset>"),
                "BS_LIVE_SIM_LATENCY_MS": os.environ.get("BS_LIVE_SIM_LATENCY_MS", "<unset>"),
                "BS_LIVE_SIM_FOK_TOLERANCE": os.environ.get("BS_LIVE_SIM_FOK_TOLERANCE", "<unset>"),
                "_note": "These env vars from v6.4.0 are IGNORED in v6.5.0. The realism design they belonged to was deleted.",
            },
            "v640_inert_pre_market_envs": {
                "BS_BSS_T_FIRST_PRE": _BS_BSS_T_FIRST_PRE,
                "BS_BSS_T_SECOND_PRE": _BS_BSS_T_SECOND_PRE,
                "BS_BSS_SUSTAIN_FIRST_PRE_S": _BS_BSS_SUSTAIN_FIRST_PRE_S,
                "BS_BSS_SUSTAIN_SECOND_PRE_S": _BS_BSS_SUSTAIN_SECOND_PRE_S,
                "_note": "These env vars are read but unused in v6.5.0 (pre-market path removed in v6.4.0).",
            },
            "infrastructure": {
                "log_retention_days": _LOG_RETENTION_DAYS,
                "skip_end_minutes": sorted(list(_SKIP_END_MINUTES)) if _SKIP_END_MINUTES else [],
                "data_dir": cfg.data_dir,
                "log_dir": str(log_dir),
            },
        }
        snapshot_path = log_dir / f"config_snapshot_{boot_ts_ms}.json"
        os.makedirs(log_dir, exist_ok=True)
        snapshot_path.write_text(json.dumps(snapshot, indent=2, default=str))
        print(f"[boot] config snapshot → {snapshot_path}", flush=True)
    except Exception as e:
        print(f"[boot] config snapshot failed: {type(e).__name__}: {e}",
              flush=True)


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
    if _BS_TIER_ENABLED:
        bs_params_line = (
            f"lead=[{_BS_LEAD_MIN_S:.0f},{_BS_LEAD_MAX_S:.0f}]s  "
            f"sum_ask≤{_BS_SUM_ASK_MAX:.2f}  "
            f"TIER LADDER (v6.5.11): "
            f"T0(any TTR,≥{_BS_TIER_T0_WINNER:.2f},strict) "
            f"T1(≤{_BS_TIER_T1_TTR:.0f}s,≥{_BS_TIER_T1_WINNER:.2f}) "
            f"T2(≤{_BS_TIER_T2_TTR:.0f}s,≥{_BS_TIER_T2_WINNER:.2f}) "
            f"T3(≤{_BS_TIER_T3_TTR:.0f}s,≥{_BS_TIER_T3_WINNER:.2f}) "
            f"persist={_BS_TIER_PERSIST_S:.0f}s  "
            f"swing[{_BS_TIER_SWING_WINDOW_S:.0f}s,Δ{_BS_TIER_SWING_DRAWDOWN:.2f}/↑{_BS_TIER_SWING_BOUNCE:.2f}]  "
            f"dip[{_BS_TIER_DIP_WINDOW_S:.0f}s,≥{_BS_TIER_DIP_FLOOR:.2f}]"
        )
    else:
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
    # v6.5.0 SKULD: LIVE BSS gating + DRY book-walk simulation status
    if _BS_ACTIVE:
        if cfg.mode == "live":
            if _LIVE_BSS_ENABLED:
                ttr_floor = _BS_BSS_MIN_TTR_AT_LEG1_S
                if ttr_floor > 0:
                    first_s = max(0, 300 - int(ttr_floor))
                    ttr_msg = (f" v6.5.2 entry filter: leg-1 only fires "
                               f"in first {first_s}s (TTR>={int(ttr_floor)}s).")
                else:
                    ttr_msg = " v6.5.2 entry filter: OFF."
                print("  *** LIVE MODE: BSS entries ENABLED. Real CLOB orders "
                      "will be placed at $1/leg via FAK (proven April 13 pattern). "
                      "Both MODE=live AND LIVE_BSS_ENABLED=true are set." +
                      ttr_msg + " ***",
                      flush=True)
            else:
                print("  *** LIVE MODE but LIVE_BSS_ENABLED=false → BSS entries "
                      "REFUSED. To enable real orders, also set "
                      "LIVE_BSS_ENABLED=true. ***",
                      flush=True)
        else:
            walk_status = ("ON" if _BS_BOOK_WALK_ENABLED else "OFF")
            ttr_floor = _BS_BSS_MIN_TTR_AT_LEG1_S
            if ttr_floor > 0:
                first_s = max(0, 300 - int(ttr_floor))
                ttr_status = (f"leg-1 only fires in first {first_s}s "
                              f"(TTR>={int(ttr_floor)}s)")
            else:
                ttr_status = "OFF (no TTR floor — v6.5.1 behavior)"
            shadow_status = (f"{_BS_BSS_SHADOW_TICK_INTERVAL_S:.0f}s cadence"
                             if _BS_BSS_SHADOW_TICK_INTERVAL_S > 0 else "OFF")
            orphan_sell_status = (
                f"ENABLED (pnl>=${_BS_BSS_ORPHAN_SELL_MIN_PNL:.2f}, "
                f"elapsed>={_BS_BSS_ORPHAN_SELL_MIN_ELAPSED_S:.0f}s, "
                f"adv>={_BS_BSS_ORPHAN_SELL_MIN_ADVERSE_BPS:.0f}bps, "
                f"persist={_BS_BSS_ORPHAN_SELL_PERSIST_TICKS} ticks)"
                if _BS_BSS_ORPHAN_SELL_ENABLED
                else "DISABLED (flip BS_BSS_ORPHAN_SELL_ENABLED=true to activate)"
            )
            tp_status = (
                f"ENABLED (ratio>={_BS_BSS_ORPHAN_TP_RATIO:.2f}, "
                f"persist={_BS_BSS_ORPHAN_TP_PERSIST_TICKS} tick)"
                if _BS_BSS_ORPHAN_TP_ENABLED
                else "DISABLED (flip BS_BSS_ORPHAN_TP_ENABLED=true to activate)"
            )
            fee_status = (
                f"Polymarket curved ({_BS_POLYMARKET_TAKER_FEE_RATE*100:.0f}%×p×(1-p))"
                if _BS_USE_POLYMARKET_FEE_FORMULA
                else f"legacy flat {_BS_TAKER_FEE_PCT*100:.1f}%"
            )
            rs_status = (
                f"ENABLED tiered v6.5.10: "
                f"T1≥{_BS_BSS_ORPHAN_RS_TIER1_WIN:.2f}(any TTR) | "
                f"T2≥{_BS_BSS_ORPHAN_RS_TIER2_WIN:.2f}+TTR≤{_BS_BSS_ORPHAN_RS_TIER2_TTR_S:.0f}s | "
                f"T3≥{_BS_BSS_ORPHAN_RS_TIER3_WIN:.2f}+TTR≤{_BS_BSS_ORPHAN_RS_TIER3_TTR_S:.0f}s | "
                f"hold≥{_BS_BSS_ORPHAN_RS_MIN_ELAPSED_S:.0f}s | "
                f"btc_guard=${_BS_BSS_ORPHAN_RS_BTC_GUARD_USD:.0f}"
                if _BS_BSS_ORPHAN_RS_ENABLED
                else "DISABLED (flip BS_BSS_ORPHAN_RS_ENABLED=true to activate)"
            )
            leg1_pat_status = (
                f"ON drop>={_BS_BSS_LEG1_PATIENT_DROP:.4f} "
                f"over {_BS_BSS_OPP_VEL_LOOKBACK_S:.0f}s"
                if _BS_BSS_LEG1_PATIENT_DROP > 0
                else "OFF (BS_BSS_LEG1_PATIENT_DROP=0)"
            )
            print(f"  *** DRY MODE v6.5.8 — per-leg placement, no abort, "
                  f"book-walk={walk_status} (taker_fee={fee_status}). "
                  f"v6.5.2 entry filter: {ttr_status}. "
                  f"v6.5.3 Tier-1 logging: ring buffer + extra_json + "
                  f"BSS_CANDIDATE_DRY (pre-entry feats: leg2 microstructure, "
                  f"depth-delta, leg1 bid trajectory, latency, regime). "
                  f"v6.5.3.1 hold-shadow: BSS_HOLD_SHADOW_DRY at "
                  f"{shadow_status} — raw state per tick. "
                  f"v6.5.3.2 dashboard: Speranța hero header — Toate Pânzele Sus. "
                  f"v6.5.4 orphan-sell (positive-exit): {orphan_sell_status}. "
                  f"v6.5.4 dashboard P&L: fees in counter. "
                  f"v6.5.4 cleanup: GET /api/cleanup?confirm=true. "
                  f"v6.5.5 take-profit (TP): {tp_status}. "
                  f"v6.5.5 fee formula: corrected to Polymarket curved. "
                  f"v6.5.5.1 CASHOUT FIX: orphan-sell uses leg1_bid_now (cashout), "
                  f"no longer gated on book-walk. "
                  f"v6.5.5.2 LOCKED-SPREAD REJECT: skip ticks where "
                  f"yes_ask==yes_bid or no_ask==no_bid (ghost snapshots). "
                  f"v6.5.5.2 BAND-SUSTAIN: orphan-sell uses time-based "
                  f"sustain ({_BS_BSS_ORPHAN_SELL_SUSTAIN_S:.0f}s with "
                  f"{_BS_BSS_ORPHAN_SELL_GRACE_S:.0f}s wobble grace), "
                  f"TP uses {_BS_BSS_ORPHAN_TP_SUSTAIN_S:.0f}s sustain / "
                  f"{_BS_BSS_ORPHAN_TP_GRACE_S:.0f}s grace. "
                  f"v6.5.5.2 phase visibility: floor/strict tagged in CSV. "
                  f"v6.5.5.3 HOTFIX: resolution gate runs BEFORE locked-"
                  f"spread reject (fixes v6.5.5.2 silent-drop of natural orphans). "
                  f"v6.5.5.3 dashboard in-flight indicator: WAITING_2ND positions "
                  f"show orphan-sell sustain progress + would-sell pnl. "
                  f"v6.5.6 LIVE SELL: orphan-sell now submits real FAK orders "
                  f"in LIVE mode via _bss_place_live_sell (no GTC fallback). "
                  f"Partial fills → ORPHAN_SOLD_PARTIAL (proportional P&L, "
                  f"remaining qty held to resolution). Rejected/error → "
                  f"BSS_ORPHAN_SELL_LIVE_FAIL logged, state stays WAITING_2ND, "
                  f"retry next tick. DRY behavior unchanged. "
                  f"v6.5.6.1 HOTFIX: fixed PolyBook-as-price crash in dashboard. "
                  f"v6.5.7 reverse-sniper cashout (Rule C): {rs_status}. "
                  f"Fires when winner_ask>={_BS_BSS_ORPHAN_RS_WINNER_THRESHOLD:.2f} "
                  f"AND ttr<={_BS_BSS_ORPHAN_RS_TTR_MAX_S:.0f}s; sells losing orphan "
                  f"leg at cashout bid (~$0.27 avg recovery vs full -$1.02 loss). "
                  f"reason=reverse_sniper in CSV. "
                  f"v6.5.8 leg1-patience: {leg1_pat_status}. "
                  f"Holds leg1 fire when same-side ask still falling fast — "
                  f"yields deeper entry (avg 11c better in 43pct of paired trades), "
                  f"more shares, better wins, smaller orphan RS losses. "
                  f"FIRST_LEG_PATIENT logged when held. "
                  f"v6.5.5 dashboard: last-15 trades with ORPHAN_SOLD render. ***",
                  flush=True)
        # Note v6.4.0 deleted env vars if user still has them set
        deleted_set = [v for v in (
            "BS_LIVE_SIM_ENABLED", "BS_LIVE_SIM_LATENCY_MS",
            "BS_LIVE_SIM_FOK_TOLERANCE") if os.environ.get(v, "")]
        if deleted_set:
            print(f"  *** NOTE: v6.4.0 env vars detected ({', '.join(deleted_set)}) — "
                  f"these are IGNORED in v6.5.0 (deleted from realism design). ***",
                  flush=True)
        # Note inert pre-market env vars if user set any
        pre_set = any(os.environ.get(v, "") for v in (
            "BS_BSS_T_FIRST_PRE", "BS_BSS_T_SECOND_PRE",
            "BS_BSS_SUSTAIN_FIRST_PRE_S", "BS_BSS_SUSTAIN_SECOND_PRE_S"))
        if pre_set:
            print("  *** NOTE: BS_BSS_*_PRE_* env vars detected — these are "
                  "INERT in v6.5.0 (pre-market path removed in v6.4.0). ***",
                  flush=True)
        # Note that BS_BSS_ABORT_AT_S is now soft-diagnostic only
        abort_set = os.environ.get("BS_BSS_ABORT_AT_S", "")
        if abort_set:
            print(f"  *** NOTE: BS_BSS_ABORT_AT_S={abort_set} is now SOFT-DIAGNOSTIC "
                  f"in v6.5.0. Leg 2 attempts continue until window end_ts. "
                  f"Single-leg positions hold to resolution (no abort, no fake sell). ***",
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
    # BSS fast-tick thread (20Hz). Only when _bs_bss_runtime_active().
    if _bs_bss_runtime_active():
        threads.append(("bss_fast", bss_fast_tick_thread))
    # BSS Gamma REST polling thread. Same gate.
    if _bs_bss_runtime_active():
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
        # v6.4.0 SKULD: pre_market_books_logger force-disabled at boot;
        # this call is a no-op early-return.
        try:
            pre_market_books_log_tick(state)
        except Exception as e:
            print(f"[pre_market_books] tick error: {e}", flush=True)
            traceback.print_exc()

        # v6.4.0 SKULD: health log tick (10s default cadence).
        try:
            _health_log_tick(state)
        except Exception as e:
            print(f"[health_log] tick error: {e}", flush=True)
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
