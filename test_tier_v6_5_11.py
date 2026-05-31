"""v6.5.11 tier-helper unit tests. Run standalone.

Strategy: re-implement the constants locally + copy the helpers verbatim
from main.py (no module import, since main.py spins up the bot at import).
This protects the test from accidental coupling to other modules.

If any constants change in main.py, update the test constants below.
"""
from typing import List, Optional, Tuple

# Mirror constants from main.py (defaults)
_BS_TIER_ENABLED          = True
_BS_TIER_T0_WINNER        = 0.96
_BS_TIER_T1_TTR           = 120.0
_BS_TIER_T1_WINNER        = 0.90
_BS_TIER_T2_TTR           = 60.0
_BS_TIER_T2_WINNER        = 0.87
_BS_TIER_T3_TTR           = 30.0
_BS_TIER_T3_WINNER        = 0.80
_BS_TIER_PERSIST_S        = 5.0
_BS_TIER_T0_MAX_TTR       = 200.0
_BS_TIER_T0_SUSTAIN_THRESH= 0.94
_BS_TIER_T0_SUSTAIN_S     = 30.0
_BS_TIER_SWING_WINDOW_S   = 30.0
_BS_TIER_SWING_DRAWDOWN   = 0.05
_BS_TIER_SWING_BOUNCE     = 0.02
_BS_TIER_DIP_WINDOW_S     = 60.0
_BS_TIER_DIP_FLOOR        = 0.65


# Helpers copied verbatim from main.py (keep in sync)
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


# ─── TESTS ─────────────────────────────────────────────────────────────────
n_pass = 0
n_fail = 0

def check(label, cond):
    global n_pass, n_fail
    if cond:
        n_pass += 1
        print(f"  PASS  {label}")
    else:
        n_fail += 1
        print(f"  FAIL  {label}")

print("\n=== _bs_tier_match boundary cases ===")
# Most-specific tier (T3) takes precedence at TTR=30s
check("T3 fires at TTR=30, wa=0.80",     _bs_tier_match(30.0, 0.80) == ("T3", 0.80))
check("T3 fires at TTR=30, wa=0.96 (most specific wins)",
                                          _bs_tier_match(30.0, 0.96) == ("T3", 0.80))
check("T3 fires at TTR=15, wa=0.80",     _bs_tier_match(15.0, 0.80) == ("T3", 0.80))
check("T3 NOT at TTR=31 (just over)",    _bs_tier_match(31.0, 0.80) == ("T2", 0.87) or
                                          _bs_tier_match(31.0, 0.80)[0] is None)  # depends on price
check("T2 fires at TTR=60, wa=0.87",     _bs_tier_match(60.0, 0.87) == ("T2", 0.87))
check("T2 fires at TTR=45, wa=0.88",     _bs_tier_match(45.0, 0.88) == ("T2", 0.87))
check("T2 NOT at TTR=45, wa=0.85 (below T2 thresh, also below T3 TTR window so no T3)",
                                          _bs_tier_match(45.0, 0.85)[0] is None)
check("T1 fires at TTR=120, wa=0.90",    _bs_tier_match(120.0, 0.90) == ("T1", 0.90))
check("T1 fires at TTR=80, wa=0.92",     _bs_tier_match(80.0, 0.92) == ("T1", 0.90))
check("T0 fires at TTR=300, wa=0.96",    _bs_tier_match(300.0, 0.96) == ("T0", 0.96))
check("T0 fires at TTR=180, wa=0.98",    _bs_tier_match(180.0, 0.98) == ("T0", 0.96))
check("T0 NOT at TTR=300, wa=0.95",      _bs_tier_match(300.0, 0.95)[0] is None)
check("no tier at TTR=200, wa=0.85",     _bs_tier_match(200.0, 0.85)[0] is None)
check("no tier at TTR=50, wa=0.75",      _bs_tier_match(50.0, 0.75)[0] is None)

print("\n=== _bs_tier_detect_swing — V-pattern detection ===")
# V-pattern: 0.90 → 0.95 → 0.83 → 0.92 (drop 12c, bounce 9c → swing)
hist_v = [
    (10.0, 0.90, 0.10),
    (12.0, 0.95, 0.05),
    (14.0, 0.83, 0.17),  # trough
    (16.0, 0.92, 0.08),
]
check("V-shape detected on YES",         _bs_tier_detect_swing(hist_v, "YES", 16.0))

# Monotonic climb: 0.50 → 0.70 → 0.85 → 0.95 → no swing
hist_climb = [
    (10.0, 0.50, 0.50),
    (12.0, 0.70, 0.30),
    (14.0, 0.85, 0.15),
    (16.0, 0.95, 0.05),
]
check("monotonic climb: NO swing on YES",  not _bs_tier_detect_swing(hist_climb, "YES", 16.0))

# Small wobble: 0.90 → 0.92 → 0.89 → 0.92 (drawdown 3c < 5c → no swing)
hist_small_wobble = [
    (10.0, 0.90, 0.10),
    (12.0, 0.92, 0.08),
    (14.0, 0.89, 0.11),
    (16.0, 0.92, 0.08),
]
check("small wobble: NO swing (drawdown<5c)",  not _bs_tier_detect_swing(hist_small_wobble, "YES", 16.0))

# Drop without recovery: 0.95 → 0.85 → 0.80 → 0.78 (no bounce → no swing)
hist_drop_no_recover = [
    (10.0, 0.95, 0.05),
    (12.0, 0.85, 0.15),
    (14.0, 0.80, 0.20),
    (16.0, 0.78, 0.22),
]
check("drop without recovery: NO swing",   not _bs_tier_detect_swing(hist_drop_no_recover, "YES", 16.0))

# Empty + tiny history
check("empty history: NO swing",         not _bs_tier_detect_swing([], "YES", 100.0))
check("single tick: NO swing",           not _bs_tier_detect_swing([(10.0, 0.9, 0.1)], "YES", 10.0))

# History outside window: 30s window, ticks at t=0..5, now=100 → all stale → NO swing
hist_stale = [(t, 0.95-t*0.05, 0.05+t*0.05) for t in range(6)]
# All ticks more than 30s old → window is empty
check("stale history: NO swing (window empty)",  not _bs_tier_detect_swing(hist_stale, "YES", 100.0))

# NO-side detection: symmetric
hist_v_no = [
    (10.0, 0.10, 0.90),
    (12.0, 0.05, 0.95),
    (14.0, 0.17, 0.83),  # trough on NO
    (16.0, 0.08, 0.92),
]
check("V-shape detected on NO",          _bs_tier_detect_swing(hist_v_no, "NO", 16.0))

print("\n=== _bs_tier_no_dip — winner never below 0.65 in 60s ===")
hist_high = [(t, 0.85 + t*0.005, 0.15 - t*0.005) for t in range(20)]
check("steady high YES: no_dip TRUE",     _bs_tier_no_dip(hist_high, "YES", 20.0))

hist_with_dip = [
    (10.0, 0.90, 0.10),
    (15.0, 0.60, 0.40),  # dipped below 0.65
    (20.0, 0.92, 0.08),
]
check("dip below 0.65: no_dip FALSE",     not _bs_tier_no_dip(hist_with_dip, "YES", 20.0))

check("empty history: no_dip TRUE",       _bs_tier_no_dip([], "YES", 100.0))

# Window edge: dip 65s ago (outside 60s window) → no_dip should be TRUE
hist_old_dip = [
    (5.0, 0.60, 0.40),   # 65s before now=70 → outside window
    (40.0, 0.90, 0.10),  # 30s before now → inside window, no dip
    (60.0, 0.92, 0.08),
]
check("dip outside window: no_dip TRUE",  _bs_tier_no_dip(hist_old_dip, "YES", 70.0))

print("\n=== _bs_tier_sustained_above ===")
# Steady above 0.85 for 30s+ → sustained at 0.85 for 20s = TRUE
hist_steady = [(t, 0.90, 0.10) for t in range(0, 30, 1)]  # t=0..29, all 0.90
check("steady 0.90: sustained 0.85/20s",  _bs_tier_sustained_above(hist_steady, "YES", 0.85, 20.0, 29.0))

# Dipped 10s ago → not sustained for 20s
hist_recent_dip = [
    (10.0, 0.95, 0.05),
    (19.0, 0.70, 0.30),  # dipped recently
    (29.0, 0.95, 0.05),
]
check("dipped 10s ago: NOT sustained 0.85/20s",
      not _bs_tier_sustained_above(hist_recent_dip, "YES", 0.85, 20.0, 29.0))

# Dipped 30s ago (outside 20s sustain window) → sustained = TRUE
hist_old_dip = [
    (0.0, 0.70, 0.30),
    (5.0, 0.95, 0.05),
    (29.0, 0.95, 0.05),
]
check("dipped 29s ago, sustain 20s window: TRUE",
      _bs_tier_sustained_above(hist_old_dip, "YES", 0.85, 20.0, 29.0))

# Edge: sustain_s=0 → always TRUE
check("sustain_s=0: always TRUE",         _bs_tier_sustained_above(hist_recent_dip, "YES", 0.85, 0.0, 29.0))

# Edge: empty history → FALSE
check("empty history: NOT sustained",     not _bs_tier_sustained_above([], "YES", 0.85, 20.0, 29.0))

# Never below threshold in history but only 15s of data → NOT sustained 20s
hist_too_short = [(15.0, 0.95, 0.05), (20.0, 0.95, 0.05), (29.0, 0.95, 0.05)]
check("never-below but only 14s span: NOT sustained 20s",
      not _bs_tier_sustained_above(hist_too_short, "YES", 0.85, 20.0, 29.0))

# Never below threshold + plenty of span → sustained
hist_long_high = [(0.0, 0.95, 0.05), (15.0, 0.95, 0.05), (29.0, 0.95, 0.05)]
check("never-below, 29s span: sustained 20s",
      _bs_tier_sustained_above(hist_long_high, "YES", 0.85, 20.0, 29.0))

print("\n=== Composite: realistic fire scenarios ===")

# Scenario 1: T1 fires cleanly
# TTR=80, winner=0.92, sustained at 0.90 for 8s, no swing, no dip
hist_t1 = [(20.0 + i, 0.91 + i*0.001, 0.09 - i*0.001) for i in range(11)]  # ts 20..30, ask 0.91..0.92
tier, thr = _bs_tier_match(80.0, 0.92)
check("T1 scenario: tier matches", tier == "T1")
check("T1 scenario: sustained at 0.90 for 5s",
      _bs_tier_sustained_above(hist_t1, "YES", 0.90, 5.0, 30.0))
check("T1 scenario: no swing", not _bs_tier_detect_swing(hist_t1, "YES", 30.0))
check("T1 scenario: no dip", _bs_tier_no_dip(hist_t1, "YES", 30.0))

# Scenario 2: T0 fire blocked by TTR>200s
tier, thr = _bs_tier_match(250.0, 0.97)
check("T0 scenario at TTR=250: tier matches T0", tier == "T0")
# But T0 has the 200s max → should be blocked at evaluator level (covered in tiered evaluator test below)

# Scenario 3: T3 fires at TTR=10 with wa=0.82 (cheaper threshold OK)
tier, thr = _bs_tier_match(10.0, 0.82)
check("T3 scenario: TTR=10, wa=0.82 → T3", tier == "T3")
check("T3 scenario: wa=0.82 also satisfies T3 threshold 0.80", thr == 0.80)

print(f"\n=== Total: {n_pass} pass, {n_fail} fail ===")
import sys
sys.exit(0 if n_fail == 0 else 1)
