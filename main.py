"""
main.py — BSS Bot v5.8.21 (Telemetry Guard + Buzzer Precision)
Fully Unified Production Build
"""
import os
import sys
import time
import json
import threading
import http.server
import socketserver
import requests
import websocket
import csv
from typing import Dict, List
from datetime import datetime, timezone

# ─── CONFIGURATION ───
BASE_CAPITAL_PER_LEG = 5.1  
TAKER_FEE_RATE = 0.018 
HEDGE_DEADLINE_TTR = 320
MAX_COMBINED_COST = 1.02
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T1_TTR_MAX = 60
SELL_LOSER_T2_THRESH = 0.95
GUARD_IMBALANCE_THRESHOLD = 2.5
LOOKAHEAD_MINUTES = 60
PORT = int(os.environ.get("PORT", 8080))
SYSTEM_BOOT_TIME = time.time()

# ─── MODELS & STATE ───
class MarketState:
    WATCH, WAITING_NO, WAITING_YES, BOTH, CLOSED = "WATCH", "WAITING_NO", "WAITING_YES", "BOTH", "CLOSED"

class MarketData:
    def __init__(self, condition_id, slug, yes_id, no_id, end_ts):
        self.condition_id, self.slug = condition_id, slug
        self.yes_token, self.no_token = yes_id, no_id
        self.end_ts = end_ts
        self.state = MarketState.WATCH
        self.yes_entry_price, self.no_entry_price = 0.0, 0.0
        self.yes_shares, self.no_shares = 0.0, 0.0
        self.total_fees_paid = 0.0
        self.t1_executed = False
        self.t1_side, self.t1_price, self.t1_time = "", 0.0, ""
        self.t1_guarded, self.t1_guard_ratio = False, 0.0
        self.t2_side, self.t2_price, self.t2_time = "", 0.0, ""
        self.t2_guarded, self.t2_guard_ratio = False, 0.0
        self.salvage_revenue, self.realized_pnl = 0.0, 0.0
        self.close_time, self.close_reason, self.expired_processed = "", "", False
        self.guard_active_yes, self.guard_active_no = False, False

class OrderBook:
    def __init__(self):
        self.bids, self.asks = {}, {}
    @property
    def bid(self): return max(self.bids.keys()) if self.bids else 0.0
    @property
    def ask(self): return min(self.asks.keys()) if self.asks else 0.0
    def get_local_vols(self, current_price, side, depth=0.10):
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
        self.running = True
        self.markets, self.books = {}, {}
        self.ws_connected = False
        self.total_pnl, self.total_trades = 0.0, 0
        self.sold_losers, self.catastrophes = 0, 0

GLOBAL_STATE = BotState()

# ─── CORE LOGIC ───
def check_guard_imbalance(book: OrderBook) -> float:
    if not book: return 0.0
    b_vol = book.get_local_vols(book.bid, "bid", 0.10)
    a_vol = book.get_local_vols(book.ask, "ask", 0.10)
    if a_vol == 0 and b_vol > 0: return 999.0 
    if a_vol == 0: return 0.0
    ratio = b_vol / a_vol
    return ratio if ratio >= GUARD_IMBALANCE_THRESHOLD else 0.0

def execute_trade(mdm, side, price, action, shares, fees, ttr, pnl=0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if action == "SELL_LOSER_T1":
        GLOBAL_STATE.sold_losers += 1
        mdm.salvage_revenue += (shares * price)
        mdm.t1_side, mdm.t1_price, mdm.t1_time = side, price, ts
    if action == "SELL_LOSER_T2":
        GLOBAL_STATE.sold_losers += 1
        mdm.salvage_revenue += (shares * price)
        mdm.t2_side, mdm.t2_price, mdm.t2_time = side, price, ts
    if "CLOSED" in action or "EXPIRED" in action:
        mdm.close_time, mdm.close_reason = ts, action
        GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl
        GLOBAL_STATE.total_pnl += pnl
    threading.Thread(target=log_trade_csv_worker, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

def log_trade_csv_worker(ts, slug, action, side, price, shares, fees, ttr, pnl):
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}", f"https://polymarket.com/event/{slug}"])
    except: pass

def evaluate_market(mdm, now):
    if getattr(mdm, 'expired_processed', False): return
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb: return
    ttr = int(mdm.end_ts - now)
    
    # BUZZER SNAPSHOT TRIGGER (Fixes round overlaps)
    if ttr <= 1:
        mdm.expired_processed = True
        winner = "YES" if yb.bid > nb.bid else "NO"
        if mdm.state != MarketState.CLOSED:
            mdm.state = MarketState.CLOSED
            cost = mdm.total_fees_paid + (BASE_CAPITAL_PER_LEG * (2 if (mdm.yes_shares > 0 and mdm.no_shares > 0) else 1))
            win_s = mdm.yes_shares if winner == "YES" else mdm.no_shares
            execute_trade(mdm, winner, 0.0, "EXPIRED", 0.0, 0.0, ttr, (win_s * 1.0) + mdm.salvage_revenue - cost)
        return

    if mdm.state == MarketState.BOTH:
        mdm.guard_active_yes = mdm.guard_active_no = False
        if yb.bid > nb.bid: winner_bid, loser_side, loser_bid, loser_shares, loser_book = yb.bid, "NO", nb.bid, mdm.no_shares, nb
        else: winner_bid, loser_side, loser_bid, loser_shares, loser_book = nb.bid, "YES", yb.bid, mdm.yes_shares, yb
            
        if not mdm.t1_executed and winner_bid >= SELL_LOSER_T1_THRESH and ttr <= SELL_LOSER_T1_TTR_MAX:
            ratio = check_guard_imbalance(loser_book)
            if ratio > 0:
                if loser_side == "YES": mdm.guard_active_yes = True
                else: mdm.guard_active_no = True
                mdm.t1_guarded, mdm.t1_guard_ratio = True, ratio
            else:
                mdm.t1_executed = True
                fee = (loser_shares * 0.5 * loser_bid) * 0.001
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T1", loser_shares * 0.5, fee, ttr)
        elif winner_bid >= SELL_LOSER_T2_THRESH:
            ratio = check_guard_imbalance(loser_book)
            if ratio > 0:
                if loser_side == "YES": mdm.guard_active_yes = True
                else: mdm.guard_active_no = True
                mdm.t2_guarded, mdm.t2_guard_ratio = True, ratio
            else:
                mdm.state = MarketState.CLOSED
                execute_trade(mdm, "CLOSED", winner_bid, "CLOSED_T2_RESOLVED", 0.0, 0.0, ttr, (loser_shares * 0.99 * 1.0) + mdm.salvage_revenue - (BASE_CAPITAL_PER_LEG * 2 + mdm.total_fees_paid))

def engine_loop():
    print("[inf] Microstructure Engine Thread Active")
    while GLOBAL_STATE.running:
        try:
            now = time.time()
            for mdm in list(GLOBAL_STATE.markets.values()):
                evaluate_market(mdm, now)
            time.sleep(0.5)
        except Exception as e:
            print(f"[err] Loop iteration failure: {e}")

# ─── DEPLOYMENT HEALTH DASHBOARD ───
class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            uptime = int(time.time() - SYSTEM_BOOT_TIME)
            
            # Simple, scannable dashboard rendering for monitoring guards
            html = f"""<html><head><meta http-equiv='refresh' content='5'></head><body style='font-family:sans-serif;padding:20px;'>
            <h2>BSS Engine v5.8.21 — Live Telemetry</h2>
            <p>Uptime: {uptime}s | Active Connections: {"YES" if GLOBAL_STATE.ws_connected else "NO"}</p>
            <h3>Global Stats</h3>
            <ul>
                <li>Total PnL: {GLOBAL_STATE.total_pnl:.3f} USDC</li>
                <li>Total Rounds Parsed: {GLOBAL_STATE.total_trades}</li>
                <li>Sold Loser Actions: {GLOBAL_STATE.sold_losers}</li>
            </ul>
            </body></html>"""
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        print(f"[inf] Health Dashboard hosting on port {PORT}")
        httpd.serve_forever()

if __name__ == "__main__":
    # Ensure baseline CSV existence
    if not os.path.exists("trades_full.csv"):
        with open("trades_full.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "Action", "Side", "Price", "Shares", "Fees", "TTR", "PnL", "URL"])
            
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=engine_loop, daemon=True).start()
    
    # Keep main alive
    while True:
        time.sleep(1)