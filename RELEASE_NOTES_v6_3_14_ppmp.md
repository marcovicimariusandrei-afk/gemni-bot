# polybot simple v1 — v6.3.14-ppmp

PPMP = Patient Pre-market Pair. Built on top of `v6.3.13-tier`. **DRY only** —
this is a data-collection / behavior test, it does not place live orders.

## What changed (5 things, all additive)

1. **60-min scan window** — `SLUG_LOOKAHEAD_5M` is now env-configurable
   (was hard-coded 4 ≈ 20 min). Set it to `12` for 60 min of pre-market
   markets. Default stays 4, so no env change = old behavior.

2. **Managed bail** (the new exit on an *unpaired* leg-1, replaces the old
   abort-at-270s dump). When the live window opens and leg-2 never filled:
   - **Complete first** if leg-2 is cheap: floor (≤0.40) immediately, or the
     open-grace ask (≤0.51) on a short sustain — "complete at 0.50/0.51 if you
     can, else sell."
   - else **sell-up**: resting offer at leg-1's ask; maker fill if the bid
     rises to meet it (0 fee + rebate-eligible).
   - **hard stop**: leg-1 bid < 0.46 → cash out now (taker).
   - **timeout**: 30 s elapsed → take the bid (taker).

3. **Fee-accurate bail** — the bail (and only the bail) models the Polymarket
   crypto taker fee `qty·rate·p·(1−p)` on the entry leg and on a taker exit;
   maker sell-ups pay 0. Controlled by `BS_USE_POLYMARKET_FEE_FORMULA` /
   `BS_POLYMARKET_TAKER_FEE_RATE`. NOTE: the rest of the DRY P&L (pair entry,
   resolution) stays fee-free as in `v6.3.13-tier` — so bail rows include
   fees and pair rows don't. Read the numbers with that asymmetry in mind.

4. **CSV-delete control** — dashboard button "delete CSVs >7d" + endpoint
   `GET /api/delete_old_csv?days=N`. Only removes files matching
   `dataset_YYYY-MM-DD.csv` older than N days by mtime; today's files are
   never touched.

5. **Bottom watching panel** — all WATCH / WAITING_2ND markets (now including
   pre-market WATCH, previously hidden) render in a quiet `#bss-watch-bottom`
   section pinned below the held pairs / active trade. Visible and live, but
   out of the way.

Exits/resolution (sell-loser TTR-floor-75, 0.93, BTC-late) are **untouched** —
a completed pair flows into the existing both-sides path and its existing
exits, exactly as before.

## Env vars to set (Railway → TPS service)

Turn PPMP on:
```
BS_STRATEGY="bss_entry"          # was v621 — switches on the see-saw path
SLUG_LOOKAHEAD_5M="12"           # 60-min scan (12 × 5-min boundaries)
BS_LEAD_TIME_MAX_S="3600"        # enter leg-1 up to 60 min ahead
LOG_WINDOW_MAX_S="3600"          # log pre-market books out to 60 min
BS_BSS_T_FIRST_PRE="0.49"        # leg-1 ≤ 0.48/0.49
BS_BSS_T_SECOND_PRE="0.50"       # leg-2 ≤ 0.50 pre-market
```
New PPMP bail knobs (defaults shown — only set to override):
```
BS_BSS_BAIL_WINDOW_S="30"        # managed-bail window after live open
BS_BSS_BAIL_HARD_STOP="0.46"     # leg-1 bid cashout floor
BS_BSS_T_SECOND_OPEN="0.51"      # leg-2 grace at trade open
```
Keep as-is: `BS_USE_POLYMARKET_FEE_FORMULA="true"`,
`BS_POLYMARKET_TAKER_FEE_RATE="0.07"`, `MODE="dry"`.

Rollback: `BS_STRATEGY="v621"` (instant revert to current behavior; the rest
become inert).

## Verify within 90 s of deploy
1. Deploy SUCCESS, no traceback in boot logs, heartbeat fresh.
2. Banner shows `6.3.14-ppmp`.
3. `/api/status` reflects `bss_entry` active.
4. Dashboard: a long, quiet **upcoming · watching** list at the bottom (≈12
   markets once the 60-min scan fills); held pairs/active trade up top; the
   **delete CSVs >7d** button in the CSV-logs header.
5. Over the next hours, watch `bs_trades.csv`: `BSS_FIRST_LEG_DRY` entries,
   then `BOTH` completions and/or `BSS_ABORT_DRY` rows tagged
   `reason=sold_up|hard_stop|timeout` with the fee breakdown in the note.

## What this answers — and what it doesn't
This collects the real **60-minute pair-completion rate**, which is the one
number that decides whether PPMP is viable (break-even ≈ 62% maker / 70%
taker; 14-min data sat at ~53%). It does **not** make money in DRY and does
not trade live. After 2–3 days of 60-min data, re-run the patient-pair
analysis to get the true completion rate and blended (fee-accurate) EV.

## Tested before ship
Full compile; real module import (config + new fields + fee fn verified);
9/9 bail-decision unit tests; state-machine replay on real 06-04 books
(transitions + fee-accurate bail + completion all fire); dashboard JS
brace/identifier check. Not tested: live WS behavior (no live feed here) —
that's what the DRY deploy verifies.
