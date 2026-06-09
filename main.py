"""
main.py — BSS Bot v6.2 (Full Substantiated Maker Core + Live WebSocket Stream)
"""
import os
import sys
import time
import json
import threading
import http.server
import socketserver
import csv
from typing import Dict, List
from datetime import datetime, timezone
import requests
import websocket

# ─── ENVIRONMENT CONFIGURATION (RAILWAY FRIENDLY) ───
MODE = os.getenv("MODE", "dry").lower()  # 'dry' or 'live'
PORT = int(os.getenv("PORT", "8080"))
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "60"))

# Strategy Cost & Fee Structures
BASE_CAPITAL_PER_LEG = 5.1  
TAKER_FEE_RATE = 0.018 

# Strict Timing Thresholds (Seconds)
PHASE_1_TTR_START = 600   # 10 minutes out
ENTRY_CUTOFF_TTR = 120    # 2 minutes out (Absolute Deadzone)
HEDGE_DEADLINE_TTR = 320

# Exit Parameters
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T1_TTR_MAX = 60
SELL_LOSER_T2_THRESH = 0.95
GUARD_IMBALANCE_THRESHOLD = 2.0

# ─── STATE MODELS ───
class MarketState:
    WATCH = "WATCH"
    PENDING_MAKER = "PENDING_MAKER"  
    WAITING_NO = "WAITING_NO"
    WAITING_YES = "WAITING_YES"
    BOTH = "BOTH"
    CLOSED = "CLOSED"

class MarketData:
    def __init__(self, condition_id: str, slug: str, yes_id: str, no_id: str, end_ts: float):
        self.condition_id, self.slug = condition_id, slug
        self.yes_token, self.no_token = yes_id, no_id
        self.end_ts = end_ts
        self.state = MarketState.WATCH
        
        # Entry Tracking
        self.pending_target_price = 0.0
        self.yes_entry_price, self.no_entry_price = 0.0, 0.0
        self.yes_shares, self.no_shares = 0.0, 0.0
        self.total_fees_paid = 0.0
        
        # Exit Tracking
        self.t1_executed, self.t1_side, self.t1_price, self.t1_time = False, "", 0.0, ""
        self.t2_side, self.t2_price, self.t2_time = "", 0.0, ""
        self.salvage_revenue, self.realized_pnl = 0.0, 0.0
        self.close_time, self.close_reason = "", ""

class OrderBook:
    def __init__(self):
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
    @property
    def bid(self): return max(self.bids.keys()) if self.bids else 0.0
    @property
    def ask(self): return min(self.asks.keys()) if self.asks else 0.0
    
    def get_local_volume(self, current_price: float, side: str, depth: float = 0.10) -> float:
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
        self.markets: Dict[str, MarketData] = {}
        self.books: Dict[str, OrderBook] = {}
        self.subscribed_tokens = set()
        self.total_pnl, self.total_trades, self.sold_losers, self.catastrophes = 0.0, 0, 0, 0
        self.time_offset = 0.0
        self.ws = None

GLOBAL_STATE = BotState()

def get_synced_time() -> float:
    return time.time() + GLOBAL_STATE.time_offset

# ─── DATA GROUNDING & CSV INITIALIZATION ───
def init_csv():
    for file, headers in [
        ("trades_full.csv", ["Timestamp", "Slug", "Action", "Side", "Executed_Price", "Share_Quantity", "Fees_Paid", "TTR", "Realized_PnL", "Link"]),
        ("snapshot_live.csv", ["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"]),
        ("telemetry_shadow.csv", ["Timestamp", "Slug", "Token", "TTR", "Ticker_Price", "Local_Bid_Vol", "Local_Ask_Vol", "Ratio", "Signal"])
    ]:
        if not os.path.exists(file):
            with open(file, "w", newline="") as f:
                csv.writer(f).writerow(headers)

def log_trade_row(ts, slug, action, side, price, shares, fees, ttr, pnl):
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}", f"https://polymarket.com/event/{slug}"])
    except: pass

def execute_trade(mdm: MarketData, side: str, price: float, action: str, shares: float, fees: float, ttr: int, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Shares: {shares:.2f}", flush=True)
    if "SELL" in action or "DUMP" in action:
        GLOBAL_STATE.sold_losers += 1
        mdm.salvage_revenue += (shares * price)
    if action == "SELL_LOSER_T1": mdm.t1_side, mdm.t1_price, mdm.t1_time = side, price, ts
    if action == "SELL_LOSER_T2": mdm.t2_side, mdm.t2_price, mdm.t2_time = side, price, ts
    if "CLOSED" in action or "EXPIRED" in action:
        mdm.close_time, mdm.close_reason = ts, action
        GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl
        GLOBAL_STATE.total_pnl += pnl
    threading.Thread(target=log_trade_row, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

# ─── REAL-TIME ORDER BOOK TELEMETRY ───
def calculate_imbalance(book: OrderBook) -> float:
    if not book: return 0.0
    b_vol = book.get_local_volume(book.bid, "bid", 0.10)
    a_vol = book.get_local_volume(book.ask, "ask", 0.10)
    if a_vol == 0: return 999.0 if b_vol > 0 else 0.0
    return b_vol / a_vol

# ─── CORE STRATEGY EVALUATION ENGINE ───
def evaluate_market(mdm: MarketData, now: float):
    ttr = int(mdm.end_ts - now)
    
    if ttr <= 1 and mdm.state != MarketState.CLOSED:
        mdm.state = MarketState.CLOSED
        cost = mdm.total_fees_paid + (BASE_CAPITAL_PER_LEG * (1 if (mdm.yes_shares == 0 or mdm.no_shares == 0) else 2))
        execute_trade(mdm, "EXPIRED", 0.0, "EXPIRED_AT_BUZZER", 0.0, 0.0, ttr, mdm.salvage_revenue - cost)
        return
        
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb or mdm.state == MarketState.CLOSED: return
    
    # ─── REALISTIC SUBSTANTIATED ENTRY MECHANICS ───
    if ttr > ENTRY_CUTOFF_TTR:
        
        # PHASE 1: Placing the Anchor ($0.49 Maker Net)
        if mdm.state == MarketState.WATCH and ttr >= PHASE_1_TTR_START:
            mdm.state = MarketState.PENDING_MAKER
            mdm.pending_target_price = 0.49
            print(f"[Entry Engine] {mdm.slug} | Rested Maker Orders at $0.49 (TTR: {ttr}s)", flush=True)

        # PHASE 2: Time Decay Shift & Reality Check
        if mdm.state == MarketState.PENDING_MAKER:
            if ttr < PHASE_1_TTR_START and mdm.pending_target_price == 0.49:
                mdm.pending_target_price = 0.50
                print(f"[Entry Engine] {mdm.slug} | Target un-filled. Stepping up to $0.50 Maker Order.", flush=True)

            # SUBSTANTIATION CHECK
            if yb.ask <= mdm.pending_target_price and mdm.yes_shares == 0:
                mdm.yes_entry_price = mdm.pending_target_price
                mdm.yes_shares = BASE_CAPITAL_PER_LEG / mdm.yes_entry_price
                execute_trade(mdm, "YES", mdm.yes_entry_price, "MAKER_FILL_LEG_1", mdm.yes_shares, 0.0, ttr)

            if nb.ask <= mdm.pending_target_price and mdm.no_shares == 0:
                mdm.no_entry_price = mdm.pending_target_price
                mdm.no_shares = BASE_CAPITAL_PER_LEG / mdm.no_entry_price
                execute_trade(mdm, "NO", mdm.no_entry_price, "MAKER_FILL_LEG_2", mdm.no_shares, 0.0, ttr)

            if mdm.yes_shares > 0 and mdm.no_shares > 0:
                mdm.state = MarketState.BOTH
            elif mdm.yes_shares > 0:
                mdm.state = MarketState.WAITING_NO
            elif mdm.no_shares > 0:
                mdm.state = MarketState.WAITING_YES

        # TELEMETRY IMBALANCE TRIGGER (Taker Fallback)
        if mdm.state in [MarketState.WAITING_NO, MarketState.WAITING_YES] and ttr <= HEDGE_DEADLINE_TTR:
            active_book = nb if mdm.state == MarketState.WAITING_NO else yb
            imbalance = calculate_imbalance(active_book)
            
            if imbalance >= GUARD_IMBALANCE_THRESHOLD:
                side = "NO" if mdm.state == MarketState.WAITING_NO else "YES"
                price = nb.ask if side == "NO" else yb.ask
                
                if price <= 0.51:
                    fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
                    mdm.total_fees_paid += fee
                    if side == "NO":
                        mdm.no_entry_price, mdm.no_shares = price, BASE_CAPITAL_PER_LEG / price
                    else:
                        mdm.yes_entry_price, mdm.yes_shares = price, BASE_CAPITAL_PER_LEG / price
                    mdm.state = MarketState.BOTH
                    execute_trade(mdm, side, price, "TAKER_HEDGE_PROTECTION", BASE_CAPITAL_PER_LEG / price, fee, ttr)

    # ─── FLAWLESS v6.2 EXITS ───
    if mdm.state == MarketState.BOTH:
        winner_bid, loser_side, loser_bid, loser_shares, loser_book = (yb.bid, "NO", nb.bid, mdm.no_shares, nb) if yb.bid > nb.bid else (nb.bid, "YES", yb.bid, mdm.yes_shares, yb)
        
        if not mdm.t1_executed and winner_bid >= SELL_LOSER_T1_THRESH and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            if calculate_imbalance(loser_book) < GUARD_IMBALANCE_THRESHOLD:
                mdm.t1_executed = True
                shares_to_sell = loser_shares * 0.50
                if loser_side == "YES": mdm.yes_shares -= shares_to_sell
                else: mdm.no_shares -= shares_to_sell
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T1", shares_to_sell, 0.0, ttr)
            
        elif winner_bid >= SELL_LOSER_T2_THRESH and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            if calculate_imbalance(loser_book) < GUARD_IMBALANCE_THRESHOLD:
                mdm.state = MarketState.CLOSED
                shares_to_sell = loser_shares * 0.99 
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T2", shares_to_sell, 0.0, ttr)
                cost = (BASE_CAPITAL_PER_LEG * 2) + mdm.total_fees_paid
                win_shares = mdm.yes_shares if loser_side == "NO" else mdm.no_shares
                execute_trade(mdm, "CLOSED", winner_bid, "CLOSED_T2_RESOLVED", 0.0, 0.0, ttr, (win_shares * 1.00) + mdm.salvage_revenue - cost)

# ─── LIVE WEBSOCKET & DISCOVERY PIPELINE ───
def on_ws_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, list):
            for event in data:
                token_id = event.get("asset_id")
                if token_id in GLOBAL_STATE.books:
                    book = GLOBAL_STATE.books[token_id]
                    if event.get("side") == "buy":
                        book.bids[float(event["price"])] = float(event["size"])
                    else:
                        book.asks[float(event["price"])] = float(event["size"])
        
        # Periodic evaluation execution pulse
        now = get_synced_time()
        for m in list(GLOBAL_STATE.markets.values()):
            evaluate_market(m, now)
    except: pass

def polymarket_ws_thread():
    while GLOBAL_STATE.running:
        try:
            GLOBAL_STATE.ws = websocket.WebSocketApp(
                "wss://clob.polymarket.com/ws/",
                on_message=on_ws_message
            )
            # Send subscription payloads whenever new markets appear
            def on_open(ws):
                if GLOBAL_STATE.subscribed_tokens:
                    ws.send(json.dumps({
                        "type": "subscribe",
                        "assets_ids": list(GLOBAL_STATE.subscribed_tokens),
                        "channels": ["order_book"]
                    }))
            GLOBAL_STATE.ws.on_open = on_open
            GLOBAL_STATE.ws.run_forever()
        except: pass
        time.sleep(2)

def discovery_loop():
    while GLOBAL_STATE.running:
        try:
            res = requests.get("https://gamma-api.polymarket.com/events?limit=1", timeout=5)
            if res.status_code == 200:
                server_time_str = res.headers.get("Date", "")
                if server_time_str:
                    server_dt = datetime.strptime(server_time_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                    GLOBAL_STATE.time_offset = server_dt.timestamp() - time.time()
            
            now = get_synced_time()
            boundaries = [int((now // 300) * 300) + (i * 300) for i in range(1, (LOOKAHEAD_MINUTES // 5) + 1)]
            
            new_tokens_found = False
            for ts in boundaries:
                slug = f"btc-updown-5m-{ts}"
                res = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
                if res.status_code == 200 and res.json():
                    m_info = res.json()[0].get("markets", [])[0]
                    cid = m_info["conditionId"]
                    if cid not in GLOBAL_STATE.markets:
                        end_ts = datetime.fromisoformat(m_info["endDate"].replace("Z", "+00:00")).timestamp()
                        tks = json.loads(m_info["clobTokenIds"])
                        outcomes = json.loads(m_info["outcomes"])
                        y_idx = 0 if outcomes[0].lower() in ["yes", "up"] else 1
                        
                        yes_tk, no_tk = tks[y_idx], tks[1-y_idx]
                        GLOBAL_STATE.markets[cid] = MarketData(cid, slug, yes_tk, no_tk, end_ts)
                        GLOBAL_STATE.books[yes_tk] = OrderBook()
                        GLOBAL_STATE.books[no_tk] = OrderBook()
                        GLOBAL_STATE.subscribed_tokens.add(yes_tk)
                        GLOBAL_STATE.subscribed_tokens.add(no_tk)
                        new_tokens_found = True
            
            # Dynamic resubscription block
            if new_tokens_found and GLOBAL_STATE.ws and GLOBAL_STATE.ws.sock and GLOBAL_STATE.ws.sock.connected:
                GLOBAL_STATE.ws.send(json.dumps({
                    "type": "subscribe",
                    "assets_ids": list(GLOBAL_STATE.subscribed_tokens),
                    "channels": ["order_book"]
                }))
        except: pass
        time.sleep(10)

# ─── COMPACT LIVE PRODUCTION WEB UI (RAILWAY HEALTH-CHECK PROOF) ───
class EmbeddedDashboard(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        
        if self.path == "/favicon.ico":
            self.wfile.write(b"")
            return
            
        html = f"<html><body style='font-family:monospace;background:#111;color:#eee;padding:30px;'>"
        html += f"<h2>BSS v6.2 (Substantiated Maker Core)</h2>"
        html += f"<p>Total Realized P&L: ${GLOBAL_STATE.total_pnl:.2f} | Cycles completed: {GLOBAL_STATE.total_trades}</p>"
        html += "<h3>Active Markets Sequence:</h3><ul>"
        for m in list(GLOBAL_STATE.markets.values()):
            if m.state != MarketState.CLOSED:
                html += f"<li>{m.slug} [{m.state}] - Target: ${m.pending_target_price:.2f} | YES Shares: {m.yes_shares:.1f} | NO Shares: {m.no_shares:.1f}</li>"
        html += "</ul></body></html>"
        self.wfile.write(html.encode())

    def log_message(self, format, *args): 
        pass

if __name__ == "__main__":
    init_csv()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    server = socketserver.ThreadingTCPServer(("", PORT), EmbeddedDashboard)
    print(f"[System Engine] High-fidelity runner listening on port {PORT}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: GLOBAL_STATE.running = False