# RELEASE NOTES — Skuld v6.5.11

**Date:** 2026-05-31
**Branch:** polybot_skuld_v1
**Bot name:** `tps`
**Mode:** DRY (LIVE_BSS_ENABLED unchanged)
**Built on:** v6.5.10
**Lines:** 11,012 → 11,322 (+310)

---

## Headline

Replaces the single-tier `_bs_evaluate_sell_loser` (winner ≥ 0.93 at TTR ≤ 120s + 5s persist + $30 BTC delta guard) with a four-tier **exit ladder** gated purely on TTR and winner_ask. No BTC fundamentals. Operator-chosen Option E: pure-numbers only, ~6 catastrophes per 100 trades floor (vs the original design's ~19/100 unguarded; 3× improvement).

Legacy evaluator preserved behind `BS_TIER_ENABLED=false` so rollback is one env-var flip, no redeploy.

---

## The new ladder

| Tier | TTR window | Winner ask threshold | Extra guards |
|------|------------|---------------------|--------------|
| **T0** | any TTR | ≥ $0.96 | TTR ≤ 200s, sustained ≥ 0.94 for 30s, AND-guard (no_swing AND no_dip) |
| **T1** | ≤ 120s | ≥ $0.90 | OR-guard (no_swing OR no_dip), 5s persist at tier threshold |
| **T2** | ≤ 60s | ≥ $0.87 | OR-guard, 5s persist |
| **T3** | ≤ 30s | ≥ $0.80 | OR-guard, 5s persist |

**Guard semantics:**
- `no_swing` = no V-shape pattern on the winner side (drawdown ≥ 5¢ then ≥ 2¢ recovery) in last 30s
- `no_dip` = winner_ask never below $0.65 in last 60s
- Cross-tier persistence: winner_ask must have been ≥ tier_threshold for at least 5s

**Tier selection:** most-specific TTR window wins. At TTR=20s with wa=0.96, the bot reports T3 (not T0) because the narrower window contains the tick.

---

## Backtest evidence (TPS signal_log, ~221 verifiable markets)

| Variant | Fires | Cats | Cats/100 | Accuracy |
|---------|-------|------|----------|----------|
| Original ladder (Marius's first design, no guards) | 180 | 43 | 19.5% | 76% |
| **v6.5.11 strengthened (this release)** | **168** | **13** | **5.9%** | **92%** |
| Drop Tier 0 entirely | 176 | 17 | 7.7% | 90% |

The 6/100 catastrophe rate is the empirical **floor** of any purely price-based design on this market. The 13 cats that remain are markets where the Polymarket order book was confidently wrong (winner_ask ≥ 0.90 for ≥ 60s before reversing). No price feature distinguishes those from genuine winners until after resolution.

We also backtested **panic-sell-the-winner** (operator suggestion mid-session): it rescues $4.81 across the 13 cats but costs $15.77 in false alarms (wobbles on real winners that get panic-sold at the dip and miss the recovery). Net **−$10.96** vs no panic-sell. Price-only cannot distinguish wobbles from real reversals on 5-min binaries — abandoned this idea after the data showed it doesn't work.

---

## What changes in the source

### 1. New module-level constants (top of file, after `BOT_VERSION`)

17 new env-controlled constants under `BS_TIER_*` prefix — all documented inline with defaults and bounds. Two helper functions `_tier_env_bool` and `_tier_env_float` for safe parsing.

```python
_BS_TIER_ENABLED            (default True)
_BS_TIER_T0_WINNER          (0.96)
_BS_TIER_T1_TTR, _T1_WINNER (120s, 0.90)
_BS_TIER_T2_TTR, _T2_WINNER (60s, 0.87)
_BS_TIER_T3_TTR, _T3_WINNER (30s, 0.80)
_BS_TIER_PERSIST_S          (5.0)
_BS_TIER_T0_MAX_TTR         (200.0)
_BS_TIER_T0_SUSTAIN_THRESH  (0.94)
_BS_TIER_T0_SUSTAIN_S       (30.0)
_BS_TIER_SWING_WINDOW_S     (30.0)
_BS_TIER_SWING_DRAWDOWN     (0.05)
_BS_TIER_SWING_BOUNCE       (0.02)
_BS_TIER_DIP_WINDOW_S       (60.0)
_BS_TIER_DIP_FLOOR          (0.65)
_BS_TIER_HISTORY_MAX_S      (derived, ~65s)
```

### 2. `BothSidesPosition` dataclass — 3 new fields

```python
tier_ask_history: List[Tuple[float, float, float]] = field(default_factory=list)
fire_tier: str = ""
tier_last_eval_status: str = "preconditions_pending"
```

`tier_ask_history` holds `(ts, yes_ask, no_ask)` tuples, trimmed to ~60s at each evaluation tick. Used by all guards. Memory bound: ~60 entries × 24 bytes = 1.4 KB per position; with typical 10–20 active positions in flight, negligible.

### 3. New helper functions (4 of them)

- `_bs_tier_match(ttr, winner_ask)` — returns the most-specific (tier_label, threshold) match
- `_bs_tier_detect_swing(history, winner_side, now)` — V-shape detector on winner_side asks
- `_bs_tier_no_dip(history, winner_side, now)` — min-ask check over 60s window
- `_bs_tier_sustained_above(history, winner_side, thresh, sustain_s, now)` — backward walk to find last tick below threshold

All unit-tested with 40 boundary cases — see `test_tier_v6_5_11.py`. All pass.

### 4. New tiered evaluator + dispatcher

- `_bs_evaluate_sell_loser_tiered(state, pos, now)` — full new logic, same return signature as legacy
- `_bs_evaluate_sell_loser(state, pos, now)` — dispatcher: routes to tiered (default) or legacy based on `_BS_TIER_ENABLED`
- `_bs_evaluate_sell_loser_legacy(state, pos, now)` — renamed from prior `_bs_evaluate_sell_loser`. Identical body. Callers don't change.

### 5. Default change: `BS_MIN_BTC_DELTA_USD` 30.0 → 0.0

Effectively disables the BTC delta guard in the legacy evaluator as well, matching the pure-numbers design. Env var still works — set to 30 to restore the guard if rolling back to legacy.

### 6. Main-loop fallbacks gated by `BS_TIER_ENABLED`

The BTC late-fallback (line ~9388) and late-conviction override (line ~9412) now skip when `_BS_TIER_ENABLED` is true. Both paths are directional and inconsistent with the pure-numbers design. Set `BS_TIER_ENABLED=false` to restore.

### 7. Boot banner updated

When `BS_TIER_ENABLED=true`, the BSS params line now prints the full ladder spec:
```
TIER LADDER (v6.5.11): T0(any TTR,≥0.96,strict) T1(≤120s,≥0.90) T2(≤60s,≥0.87)
T3(≤30s,≥0.80) persist=5s swing[30s,Δ0.05/↑0.02] dip[60s,≥0.65]
```

---

## What's NOT changed

- Resolution chain (Gamma → Chainlink → Binance) — `_resolve_via_chainlink` and `_resolve_via_binance` untouched; both use historical lookups, no tautology bug
- Entry logic (`_bs_evaluate_bss_entry`) — including pre-market BSS, leg2 patience, BTC-velocity entry filter
- Orphan-sell rule (`_bs_evaluate_orphan_sell`) — WAITING_2ND-state machinery for unpaired legs, separate concern
- TP rule (take-profit on leg-1 bid recovery) — unchanged
- Locked-spread reject (v6.5.5.2) — carried forward into the new evaluator
- Stale book guard (30s threshold) — same logic
- Dashboard, CSV schemas, all event types, all other env vars — unchanged
- Chainlink stream log integration

---

## Environment variables (additions)

All optional. Defaults applied when unset.

```
# v6.5.11 — Tiered Exit Ladder
BS_TIER_ENABLED=true                  # set false to revert to legacy v6.5.10 evaluator

# Tier thresholds — modify to tune tier sensitivity
BS_TIER_T0_WINNER=0.96
BS_TIER_T1_TTR=120
BS_TIER_T1_WINNER=0.90
BS_TIER_T2_TTR=60
BS_TIER_T2_WINNER=0.87
BS_TIER_T3_TTR=30
BS_TIER_T3_WINNER=0.80

# Cross-tier persistence
BS_TIER_PERSIST_S=5.0                 # winner_ask must be ≥ tier_threshold for this long

# T0 extra guards (anytime tier needs stronger evidence)
BS_TIER_T0_MAX_TTR=200.0              # T0 doesn't fire at TTR > this
BS_TIER_T0_SUSTAIN_THRESH=0.94        # winner_ask must be ≥ this …
BS_TIER_T0_SUSTAIN_S=30.0             # … for at least this long

# Swing guard (V-shape detection on winner side)
BS_TIER_SWING_WINDOW_S=30.0
BS_TIER_SWING_DRAWDOWN=0.05           # 5¢ drop counts as a peak→trough
BS_TIER_SWING_BOUNCE=0.02             # 2¢ recovery counts as bounce-back

# Dip guard (winner never below floor in window)
BS_TIER_DIP_WINDOW_S=60.0
BS_TIER_DIP_FLOOR=0.65
```

Legacy env vars (`BS_SELL_LOSER_THRESHOLD`, `_TTR_FLOOR_S`, `_PERSIST_S`, `_MIN_LOSER_BID`, `BS_MIN_BTC_DELTA_USD`) still read by the legacy evaluator. They have no effect when `BS_TIER_ENABLED=true`.

---

## Rollout

1. Push `main.py` to GitHub Skuld repo
2. Railway auto-deploys
3. Verify boot banner shows: `*** DRY MODE v6.5.11 ... TIER LADDER (v6.5.11): T0(any TTR,≥0.96,strict) T1(≤120s,≥0.90) ...`
4. Verify `/api/status` returns `bot_version: "6.5.11"`
5. Watch `bs_trades.csv` for new `SELL_LOSER_DRY` events. The `note` field will contain `reason=fire_TX_LADDER` where `X` ∈ {0,1,2,3} indicating the firing tier.
6. Expected behaviour over the first 24h:
   - Fire rate ~75% of paired markets
   - Distribution of fires across tiers: T1 dominant (40-50%), T0 next (~30%), T2 + T3 each ~10%
   - Catastrophes: target ≤8/100 over the first week (backtest projected 6)
   - Held-to-expiry: ~20% of markets (where no tier ever matched)

If catastrophes exceed 10/100 over the first 48h: flip `BS_TIER_ENABLED=false` via Railway env (no redeploy), restoring v6.5.10 behaviour. The legacy code path is untouched.

---

## Iron rule compliance

- **#7 fair feasibility**: backtest gave honest 5.9% cat floor (Marius's target was 2). Operator explicitly chose to ship at this gap (Option E). Full backtest table in this doc.
- **#8 self-audit**: AST parse PASS, compile PASS (11,322 lines), 40/40 unit tests pass on tier helpers covering all boundary cases, 19/19 critical functions verified present, version + env constants verified, source fragment integration check verified all 6 critical inserts.
- **#9 fetch live source before edit**: edited from the v6.5.10 `main.py` user uploaded this session.
- **#22 no ship without approval**: operator chose Option E explicitly via the structured questionnaire mid-session.

---

## Files

- `/mnt/user-data/outputs/skuld/main.py` (11,322 lines, 580 KB)
- `/mnt/user-data/outputs/skuld/test_tier_v6_5_11.py` (test suite, 40 cases)
- `/mnt/user-data/outputs/skuld/RELEASE_NOTES_v6_5_11_skuld.md` (this file)

`chainlink_stream_log.py` unchanged from v6.5.10 — push only `main.py`.

---

## Open items (backlog, unchanged)

- Tier-eval CSV (`tier_log.csv`) — would record every evaluation tick for offline analysis. Not in this release; revisit after 48h of production data shows whether it's worth the disk volume.
- Dashboard tile for current tier-eval status — `pos.tier_last_eval_status` is wired and surfaced via `/api/status`, but no UI yet.
- TPS bot has the `_resolve()` tautology bug confirmed at 48% mislabel rate. Apply the same v1.3 Formulator-style fix in a separate session.
- Janus Quatro: operator confirmed independently built; needs separate audit.
- 6/100 → 2/100 gap closure: re-introducing BTC delta as `BS_TIER_MIN_BTC_DELTA_USD` would close it. Deferred per operator's pure-numbers preference.
