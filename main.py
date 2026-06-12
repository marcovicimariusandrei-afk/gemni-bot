"""
main.py — BSS Bot v6.15 (Tiered Exit + Maker-First Hook + Stalk Abort + Vault)
FULL PRODUCTION BUILD
"""
import os
import sys
import time
import json
import threading
import socketserver
import http.server
import requests
import websocket
import csv
import collections
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone

# ─── CONFIGURATION & DATA VAULT ───
PORT = int(os.getenv("PORT", "8080"))
SYSTEM_BOOT_TIME = time.time()
SESSION_ID = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

CSV_TRADES = f"trades_full_{SESSION_ID}.csv"
CSV_SNAPS = f"snapshot_live_{SESSION_ID}.csv"
CSV_TELEMETRY = f"telemetry_shadow_{SESSION_ID}.csv"

BASE_CAPITAL_PER_LEG = 5.10  
TAKER_FEE_RATE = 0.018 

# Timeline Parameters
LOOKAHEAD_MINUTES = 60      # V6.15: 60-Minute Stalk
ENTRY_CUTOFF_TTR = 120      # V6.15: Naked Abort Trigger

# Cost Parameters
MAX_COMBINED_COST = 1.01    # V6.15: Strict Entry Defense

# Exit Parameters
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T2_THRESH = 0.95

# ─── V6.15 CORE BLOCKS ───
class OFAVelocityTracker:
    def __init__(self, lookback_horizon_secs: int = 15, tick_interval_secs: int = 5):
        self.maxlen = max(1, int(lookback_horizon_secs / tick_interval_secs))
        self.history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=self.maxlen)
        )

    def update_snapshot(self, slug: str, bid_vol: float, ask_vol: float):
        self.history[slug].append((time.time(), bid_vol, ask_vol))

class V615Engine:
    def __init__(self):
        self.ofa_tracker = OFAVelocityTracker()
        self.positions = collections.defaultdict(dict)
        self.dashboard_ui = {}
        self.cats_count = 0

def render_cloud_dashboard(engine):
    print("\n" + "="*70)
    print(f"📊 TELEMETRY DASHBOARD  |  🐈 CATASTROPHIC WHIPSAWS RECORDED: {engine.cats_count}")
    print("-" * 70)
    print(f"{'MARKET SLUG':<32} | {'TTR':<6} | {'MID PX':<8} | {'STATUS'}")
    print("-" * 70)
    
    if not engine.dashboard_ui: print("   No active markets tracked.")
    
    sorted_ui = sorted(engine.dashboard_ui.items(), key=lambda x: x[1].get('TTR', 999), reverse=True)
    for slug, state in sorted_ui:
        print(f"{slug:<32} | {state['TTR']:<6} | ${state['Mid_Price']:<7.2f} | {state['Status']}")
    print("="*70 + "\n", flush=True)

# ─── STATE MODELS ───
class MarketState:
    WATCH = "WATCH"; WAITING_NO = "WAITING_NO"; WAITING_YES = "WAITING_YES"; BOTH = "BOTH"; CLOSED = "CLOSED"

class MarketData:
    def __init__(self, condition_id: str, slug: str, yes_id: str, no_id: str, end_ts: float):
        self.condition_id = condition_id; self.slug = slug; self.yes_token = yes_id; self.no_token = no_id; self.end_ts = end_ts
        self.state = MarketState.WATCH
        self.yes_entry_price = 0.0; self.no_entry_price = 0.0; self.yes_shares = 0.0; self.no_shares = 0.0; self.total_fees_paid = 0.0
        
        # Lifecycle
        self.phase1_executed = False; self.phase2_executed = False; self.shares_sold_so_far = 0.0
        self.active_maker_ts = 0.0; self.pending_exit_shares = 0.0; self.pending_exit_reason = ""
        self.sold_side = ""; self.sold_price = 0.0; self.salvage_revenue = 0.0; self.realized_pnl = 0.0
        self.close_time = ""; self.close_reason = ""; self.expired_processed = False
        self.strike_price = 0.0; self.history_yes = []; self.history_no = []

class OrderBook:
    def __init__(self): self.bids = {}; self.asks = {}
    @property
    def bid(self): return max(self.bids.keys()) if self.bids else 0.0
    @property
    def ask(self): return min(self.asks.keys()) if self.asks else 0.0
    def get_local_vols(self, current_price: float, side: str, depth: float = 0.10) -> float:
        vol = 0.0
        if side == "bid":
            for p, s in self.bids.items():
                if p >= current_price - depth: vol += s
        else:
            for p, s in self.asks.items():
                if p <= current_price + depth: vol += s
        return vol

class BotState:
    def __init__(self):
        self.running = True; self.armed = False; self.markets = {}; self.books = {}; self.ws_connected = False
        self.ws_handle = None; self.total_pnl = 0.0; self.total_trades = 0; self.time_offset = 0.0; self.btc_live = 0.0
        self.engine = V615Engine()

GLOBAL_STATE = BotState()

# ─── LOGIC ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str, shares: float, fees: float, ttr: int, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if action.startswith("SELL_LOSER_") or "ABORT" in action:
        mdm.salvage_revenue += (shares * price); mdm.sold_side, mdm.sold_price = side, price
    if "CLOSED" in action or "EXPIRED" in action:
        mdm.close_time, mdm.close_reason = ts, action; GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl; GLOBAL_STATE.total_pnl += pnl
    threading.Thread(target=log_trade_csv_worker, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

def log_trade_csv_worker(ts, slug, action, side, price, shares, fees, ttr, pnl):
    try:
        with open(CSV_TRADES, "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}"])
    except Exception: pass

def evaluate_market(mdm: MarketData, now: float):
    if getattr(mdm, 'expired_processed', False): return
    ttr = int(mdm.end_ts - now)
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)

    if ttr < -30:
        mdm.expired_processed = True
        if mdm.state != MarketState.CLOSED:
            mdm.state = MarketState.CLOSED
            if mdm.slug in GLOBAL_STATE.engine.dashboard_ui: del GLOBAL_STATE.engine.dashboard_ui[mdm.slug]
            if not yb or not nb: return
            
            # Whipsaw Logic: Oracle Settlement
            winner_side = "YES" if yb.bid > nb.bid else "NO"
            if (mdm.phase1_executed or mdm.phase2_executed) and mdm.sold_side == winner_side:
                GLOBAL_STATE.engine.cats_count += 1
            
            cost_basis = mdm.total_fees_paid
            if mdm.yes_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
            if mdm.no_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
            winner_shares = mdm.yes_shares if winner_side == "YES" else mdm.no_shares 
            calc_pnl = (winner_shares * 1.00) + mdm.salvage_revenue - cost_basis
            execute_trade(mdm, winner_side, 0.00, "EXPIRED_SETTLED", 0.0, 0.0, ttr, calc_pnl)
        return

    # Entry Logic (Stalk & Abort)
    if mdm.state == MarketState.WATCH and yb and nb:
        if 0 < yb.ask <= T_WINDOW_1:
            mdm.state = MarketState.WAITING_NO; mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "MAKER_FILL_LEG_1", mdm.yes_shares, fee, ttr)
        elif 0 < nb.ask <= T_WINDOW_1:
            mdm.state = MarketState.WAITING_YES; mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "MAKER_FILL_LEG_1", mdm.no_shares, fee, ttr)

    elif mdm.state in [MarketState.WAITING_NO, MarketState.WAITING_YES]:
        if ttr <= ENTRY_CUTOFF_TTR:
            mdm.state = MarketState.CLOSED
            side = "YES" if mdm.state == MarketState.WAITING_NO else "NO"
            shares = mdm.yes_shares if side == "YES" else mdm.no_shares
            fee = shares * (yb.bid if side == "YES" else nb.bid) * TAKER_FEE_RATE
            pnl = (shares * (yb.bid if side == "YES" else nb.bid)) - (mdm.yes_entry_price if side == "YES" else mdm.no_entry_price) * shares - mdm.total_fees_paid - fee
            execute_trade(mdm, side, (yb.bid if side == "YES" else nb.bid), "ABORT_NAKED_LEG", shares, fee, ttr, pnl)
            return

    # V6.15 60/30 Tiered Exit Logic
    if mdm.state == MarketState.BOTH and yb and nb:
        if yb.bid + yb.ask > nb.bid + nb.ask: winner_mid, loser_side, loser_bid, loser_ask, loser_book = (yb.bid + yb.ask)/2, "NO", nb.bid, nb.ask, nb
        else: winner_mid, loser_side, loser_bid, loser_ask, loser_book = (nb.bid + nb.ask)/2, "YES", yb.bid, yb.ask, yb
        
        GLOBAL_STATE.engine.dashboard_ui[mdm.slug] = {"TTR": ttr, "Mid_Price": winner_mid, "Status": "[TRACKING]"}
        base_shares = mdm.no_shares if loser_side == "NO" else mdm.yes_shares
        rem = base_shares - mdm.shares_sold_so_far
        
        # Phase logic
        trigger = False
        if 0 < ttr <= 30 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True; mdm.phase2_executed = True; mdm.pending_exit_shares = base_shares; trigger = True
        elif 30 < ttr <= 60 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True; mdm.pending_exit_shares = base_shares * 0.5; trigger = True
        
        if trigger:
            mdm.shares_sold_so_far += mdm.pending_exit_shares
            if loser_book.get_local_vols(loser_bid, "bid") > 1500:
                mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid+0.01, 2), "MAKER_HOOK_PLACED", mdm.pending_exit_shares, 0, ttr)
            else:
                taker_price = max(0.01, round(loser_bid - 0.02, 2))
                execute_trade(mdm, loser_side, taker_price, "SELL_LOSER_TAKER", mdm.pending_exit_shares, 0, ttr)
                mdm.pending_exit_shares = 0

# (Infrastructure threads, Websockets, Server remain unchanged...)
if __name__ == "__main__":
    init_csv(); threading.Thread(target=run_server, daemon=True).start(); threading.Thread(target=btc_oracle_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start(); run_diagnostics(); threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start(); threading.Thread(target=snapshot_loop, daemon=True).start()
    while GLOBAL_STATE.running: time.sleep(1)