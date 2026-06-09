"""
main.py — BSS Bot v6.2 (Full Substantiated Maker Core + Full UI Dashboard)
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
        self.t1_guarded, self.t1_guard_ratio = False, 0.0
        self.t2_side, self.t2_price, self.t2_time = "", 0.0, ""
        self.t2_guarded, self.t2_guard_ratio = False, 0.0
        
        self.salvage_revenue, self.realized_pnl = 0.0, 0.0
        self.close_time, self.close_reason, self.expired_processed = "", "", False
        self.guard_active_yes, self.guard_active_no = False, False
        self.history_yes: List[float] = []
        self.history_no: List[float] = []

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
        self.ws_connected = False
        self.ws = None

GLOBAL_STATE = BotState()
SYSTEM_BOOT_TIME = time.time()

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
    
    # Absolute Buzzer Protection
    if ttr <= 1 and mdm.state != MarketState.CLOSED:
        mdm.state = MarketState.CLOSED
        cost = mdm.total_fees_paid + (BASE_CAPITAL_PER_LEG * (1 if (mdm.yes_shares == 0 or mdm.no_shares == 0) else 2))
        winner_side = "UNKNOWN"
        win_shares = 0.0
        yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
        if yb and nb:
            winner_side = "YES" if yb.bid > nb.bid else "NO"
            win_shares = mdm.yes_shares if winner_side == "YES" else mdm.no_shares
            if (mdm.t1_side == winner_side) or (mdm.t2_side == winner_side): 
                GLOBAL_STATE.catastrophes += 1
        execute_trade(mdm, winner_side, 0.0, "EXPIRED_AT_BUZZER", win_shares, 0.0, ttr, (win_shares * 1.00) + mdm.salvage_revenue - cost)
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
        mdm.guard_active_yes = mdm.guard_active_no = False
        winner_bid, loser_side, loser_bid, loser_shares, loser_book = (yb.bid, "NO", nb.bid, mdm.no_shares, nb) if yb.bid > nb.bid else (nb.bid, "YES", yb.bid, mdm.yes_shares, yb)
        
        if not mdm.t1_executed and winner_bid >= SELL_LOSER_T1_THRESH and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            guard_ratio = calculate_imbalance(loser_book)
            if guard_ratio >= GUARD_IMBALANCE_THRESHOLD:
                if loser_side == "YES": mdm.guard_active_yes = True
                else: mdm.guard_active_no = True
                mdm.t1_guarded, mdm.t1_guard_ratio = True, guard_ratio
            else:
                mdm.t1_executed = True
                shares_to_sell = loser_shares * 0.50
                if loser_side == "YES": mdm.yes_shares -= shares_to_sell
                else: mdm.no_shares -= shares_to_sell
                fee = (shares_to_sell * loser_bid) * 0.001 
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T1", shares_to_sell, fee, ttr)
            
        elif winner_bid >= SELL_LOSER_T2_THRESH and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            guard_ratio = calculate_imbalance(loser_book)
            if guard_ratio >= GUARD_IMBALANCE_THRESHOLD:
                if loser_side == "YES": mdm.guard_active_yes = True
                else: mdm.guard_active_no = True
                mdm.t2_guarded, mdm.t2_guard_ratio = True, guard_ratio
            else:
                mdm.state = MarketState.CLOSED
                shares_to_sell = loser_shares * 0.99 
                fee = (shares_to_sell * loser_bid) * 0.001 
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T2", shares_to_sell, fee, ttr)
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
            def on_open(ws):
                GLOBAL_STATE.ws_connected = True
                if GLOBAL_STATE.subscribed_tokens:
                    ws.send(json.dumps({
                        "type": "subscribe",
                        "assets_ids": list(GLOBAL_STATE.subscribed_tokens),
                        "channels": ["order_book"]
                    }))
            def on_close(ws, close_status_code, close_msg):
                GLOBAL_STATE.ws_connected = False
                
            GLOBAL_STATE.ws.on_open = on_open
            GLOBAL_STATE.ws.on_close = on_close
            GLOBAL_STATE.ws.run_forever()
        except: pass
        GLOBAL_STATE.ws_connected = False
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
            
            if new_tokens_found and GLOBAL_STATE.ws and GLOBAL_STATE.ws.sock and GLOBAL_STATE.ws.sock.connected:
                GLOBAL_STATE.ws.send(json.dumps({
                    "type": "subscribe",
                    "assets_ids": list(GLOBAL_STATE.subscribed_tokens),
                    "channels": ["order_book"]
                }))
        except: pass
        time.sleep(10)

def snapshot_loop():
    while GLOBAL_STATE.running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("snapshot_live.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in list(GLOBAL_STATE.markets.values()):
                    if m.end_ts >= get_synced_time() - 5:
                        yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                        ya, ybd = yb.ask if yb else 0, yb.bid if yb else 0
                        na, nbd = nb.ask if nb else 0, nb.bid if nb else 0
                        writer.writerow([ts, m.slug, m.state, f"{ya:.3f}", f"{ybd:.3f}", f"{na:.3f}", f"{nbd:.3f}"])
                        m.history_yes.append(ya)
                        m.history_no.append(na)
                        if len(m.history_yes) > 30: m.history_yes.pop(0)
                        if len(m.history_no) > 30: m.history_no.pop(0)
        except: pass
        time.sleep(30)


# ─── DASHBOARD HTML & SERVER ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Analysis Dashboard v6.2 (Maker Edition)</title>
<style>
    :root { --bg-main: #0B1120; --bg-panel: #1E293B; --header-bg: #0F172A; --header-text: #F8FAFC; --sub-header-bg: #0F172A; --text-navy: #F8FAFC; --text-light: #94A3B8; --border-color: #334155; --val-green: #34D399; --val-red: #F87171; --val-yellow: #FCD34D; --val-pink: #F472B6; --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg-main); color: var(--text-navy); font-family: var(--font-sans); padding: 20px; font-size: 14px; margin: 0; }
    .header-panel { background: var(--header-bg); border: 1px solid var(--border-color); display: flex; flex-direction: column; text-align: center; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden; }
    .brand-title { font-size: 22px; font-weight: bold; color: var(--header-text); padding: 14px 0; border-bottom: 1px solid var(--border-color); }
    .status-tags { font-size: 12px; font-weight: normal; margin-left: 15px; color: var(--text-light); }
    .vitals-row { display: flex; background: var(--sub-header-bg); }
    .vital-box { flex: 1; padding: 15px; border-right: 1px solid var(--border-color); text-align: center; }
    .vital-box:last-child { border-right: none; }
    .vital-label { font-size: 12px; font-weight: 700; text-transform: uppercase; margin-bottom: 8px; color: var(--text-light); letter-spacing: 0.5px; }
    .vital-value { background: var(--bg-panel); font-size: 24px; font-weight: 800; padding: 8px; border-radius: 4px; border: 1px solid var(--border-color); font-family: monospace; }
    .vital-value.green { color: var(--val-green); border-color: #064E3B; background: #065F46;}
    .vital-value.red { color: var(--val-red); border-color: #7F1D1D; background: #991B1B;}
    .sec-title { background: var(--header-bg); font-size: 15px; font-weight: bold; text-align: center; padding: 12px; margin-bottom: 15px; border-radius: 6px; letter-spacing: 0.5px; border: 1px solid var(--border-color);}
    .grid { display: grid; grid-template-columns: 1fr; gap: 20px; margin-bottom: 35px; }
    .card { background: var(--bg-panel); border: 1px solid var(--border-color); box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden;}
    .card-header { background: var(--sub-header-bg); padding: 12px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; font-weight: 800; font-size: 15px;}
    .leg-container { display: flex; width: 100%; }
    .leg-col { flex: 1; padding: 20px; border-right: 1px solid var(--border-color); }
    .leg-col:last-child { border-right: none; }
    .leg-title { font-size: 13px; font-weight: 800; text-align: center; margin-bottom: 15px; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; }
    .data-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: var(--text-light); }
    .data-row b { color: var(--text-navy); font-family: monospace; font-size: 15px;}
    .val-green { color: var(--val-green); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-red { color: var(--val-red); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-gold { color: var(--val-yellow); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-pink { color: var(--val-pink); font-weight: 800; font-family: monospace; font-size: 15px;}
    .table-container { background: var(--bg-panel); border: 1px solid var(--border-color); margin-bottom: 35px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: var(--sub-header-bg); color: var(--text-light); font-size: 11px; font-weight: 800; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border-color); text-align: center;}
    td { padding: 12px 10px; border-bottom: 1px solid var(--border-color); text-align: center; font-size: 13px; font-family: monospace; }
    .queue-container { background: var(--bg-panel); border: 1px solid var(--border-color); padding: 20px; font-family: monospace; font-size: 13px; color: var(--text-light); line-height: 1.8; border-radius: 6px; }
    .vault { display: flex; gap: 15px; background: var(--sub-header-bg); padding: 15px; border: 1px solid var(--border-color); align-items: center; justify-content: center; margin-bottom: 25px; border-radius: 6px;}
    .btn-action { background: #1E293B; color: var(--text-navy); border: 1px solid var(--border-color); padding: 8px 18px; cursor: pointer; font-weight: 700; border-radius: 4px; transition: all 0.2s;}
    .btn-action:hover { background: #334155;}
    .svg-container { height: 50px; margin-top: 10px; background: #0F172A; border: 1px solid var(--border-color); border-radius: 4px;}
    .guard-badge { background: #B45309; color: #FFFBEB; padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 800; margin-left: 8px; border: 1px solid #F59E0B;}
</style>
</head>
<body>

<div class="header-panel">
    <div class="brand-title">BSS Bot Analysis Dashboard v6.2
        <span class="status-tags" id="bot-uptime">[Uptime: 0h 0m 0s]</span>
        <span class="status-tags" id="ws-status">[WS: Checking...]</span>
        <span class="status-tags" id="ntp-status">[NTP Sync Delta: 0ms]</span>
    </div>
    <div class="vitals-row">
        <div class="vital-box"><div class="vital-label">Total Realized P&L</div><div class="vital-value" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Completed Trades</div><div class="vital-value" id="v-trades">0</div></div>
        <div class="vital-box"><div class="vital-label">Sold Losers</div><div class="vital-value" id="v-losers">0</div></div>
        <div class="vital-box"><div class="vital-label">Catastrophes Avoided</div><div class="vital-value red" id="v-catastrophes">0</div></div>
        <div class="vital-box"><div class="vital-label">Active Slots</div><div class="vital-value" id="v-active">0</div></div>
    </div>
</div>

<div class="sec-title">Active Market Dual-Leg Monitoring</div>
<div class="grid" id="active-cards"><div style="text-align:center; padding:30px; color:var(--text-light); font-weight: bold;">Awaiting Entry Criteria...</div></div>

<div class="sec-title">Consolidated Trade Lifecycle History</div>
<div class="table-container">
    <table>
        <thead><tr><th>Time Closed</th><th>Market Slug</th><th>YES Entry</th><th>NO Entry</th><th>T1 Exit</th><th>T2 Exit</th><th>Net P&L</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="7" style="color: var(--text-light); padding: 20px;">No historical data available.</td></tr></tbody>
    </table>
</div>

<div class="vault">
    <span style="font-weight: 800; margin-right: 15px; color: var(--text-navy);">Data Vault:</span>
    <button class="btn-action" onclick="window.location.href='/api/dl_trades'">Download Trades</button>
    <button class="btn-action" onclick="window.location.href='/api/dl_snaps'">Download Snapshots</button>
    <button class="btn-action" style="color: #FCA5A5; margin-left: auto; border-color: #7F1D1D; background: #450A0A;" onclick="deleteFiles()">⚠ Clear Logs</button>
</div>

<div class="sec-title">Observation Queue (Scouting & Maker Resting)</div>
<div class="queue-container" id="obs-queue">Scanning...</div>

<script>
function renderSparkline(history, color) {
    if(!history || history.length < 2) return '';
    const min = Math.min(...history), max = Math.max(...history);
    const range = (max - min) || 0.01;
    const pts = history.map((val, i) => {
        const x = (i / (history.length - 1)) * 100;
        const y = 100 - (((val - min) / range) * 100);
        return `${x},${y}`;
    }).join(' ');
    return `<svg width="100%" height="100%" viewBox="0 -10 100 120" preserveAspectRatio="none"><polyline fill="none" stroke="${color}" stroke-width="2.5" points="${pts}" /></svg>`;
}

function formatUptime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h}h ${m}m ${s}s`;
}

async function deleteFiles() {
    if(confirm("Confirm deletion of all server CSV logs?")) {
        await fetch('/api/delete_logs', {method: 'POST'});
        alert("Logs purged.");
    }
}

setInterval(async () => {
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        
        document.getElementById('bot-uptime').textContent = `[Uptime: ${formatUptime(s.uptime_s)}]`;
        document.getElementById('ws-status').textContent = s.ws_connected ? "[WS: CONNECTED]" : "[WS: DROPPED]";
        document.getElementById('ws-status').style.color = s.ws_connected ? "#34d399" : "#f87171";
        document.getElementById('ntp-status').textContent = `[NTP Drift: ${Math.round(s.time_offset * 1000)}ms]`;
        
        const pnlBox = document.getElementById('v-pnl');
        pnlBox.textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        pnlBox.className = "vital-value " + (s.pnl > 0 ? "green" : (s.pnl < 0 ? "red" : ""));
        
        document.getElementById('v-trades').textContent = s.total_trades_count;
        document.getElementById('v-losers').textContent = s.losers;
        document.getElementById('v-catastrophes').textContent = s.catastrophes;
        
        let activeCount = 0; let htmlCards = ''; let htmlQueue = '';
        
        s.markets.forEach(m => {
            if (['WATCH', 'WAITING_NO', 'WAITING_YES', 'PENDING_MAKER'].includes(m.state)) {
                let currentStatus = m.state === 'WATCH' ? 'Scouting' : (m.state === 'PENDING_MAKER' ? `Resting Maker Order @ $${m.target_price.toFixed(2)}` : 'Filling Dual Leg');
                htmlQueue += `[TTR: ${m.ttr_s}s] | ${m.slug} | Target: $${m.target_price.toFixed(2)} | Live YES Ask: $${m.yes_ask.toFixed(3)} | Live NO Ask: $${m.no_ask.toFixed(3)} | Status: <b>${currentStatus}</b><br>`;
                return;
            }
            if (m.state === 'CLOSED') return;
            
            activeCount++;
            let dYes = m.yes_entry > 0 ? ((m.yes_ask - m.yes_entry) / m.yes_entry) * 100 : 0;
            let dNo = m.no_entry > 0 ? ((m.no_ask - m.no_entry) / m.no_entry) * 100 : 0;

            htmlCards += `<div class="card">
                <div class="card-header"><span>${m.slug}</span><span>TTR: ${m.ttr_s}s</span></div>
                <div class="leg-container">
                    <div class="leg-col">
                        <div class="leg-title">YES LEG (${m.yes_shares.toFixed(1)} sh)</div>
                        <div class="data-row"><span>Entry:</span> <b>$${m.yes_entry.toFixed(3)}</b></div>
                        <div class="data-row"><span>Live Ask:</span> <b>$${m.yes_ask.toFixed(3)}</b></div>
                        <div class="data-row"><span>Delta:</span> <b class="${dYes>=0?'val-green':'val-red'}">${dYes.toFixed(1)}%</b></div>
                        <div class="svg-container">${renderSparkline(m.history_yes, '#38BDF8')}</div>
                    </div>
                    <div class="leg-col">
                        <div class="leg-title">NO LEG (${m.no_shares.toFixed(1)} sh)</div>
                        <div class="data-row"><span>Entry:</span> <b>$${m.no_entry.toFixed(3)}</b></div>
                        <div class="data-row"><span>Live Ask:</span> <b>$${m.no_ask.toFixed(3)}</b></div>
                        <div class="data-row"><span>Delta:</span> <b class="${dNo>=0?'val-green':'val-red'}">${dNo.toFixed(1)}%</b></div>
                        <div class="svg-container">${renderSparkline(m.history_no, '#94A3B8')}</div>
                    </div>
                </div>
            </div>`;
        });
        
        document.getElementById('v-active').textContent = activeCount;
        if(htmlCards) document.getElementById('active-cards').innerHTML = htmlCards;
        else document.getElementById('active-cards').innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-light); font-weight: bold;">No Active Dual-Leg Positions...</div>';
        
        document.getElementById('obs-queue').innerHTML = htmlQueue || 'No upcoming markets in window.';

        let logHtml = '';
        s.history.reverse().forEach(h => {
            const pnlStr = h.pnl !== 0.0 ? (h.pnl > 0 ? `+${h.pnl.toFixed(2)}` : h.pnl.toFixed(2)) : '--';
            logHtml += `<tr>
                <td style="color:var(--text-light);">${h.time}</td><td>${h.slug}</td>
                <td>${h.yes_entry > 0 ? '$'+h.yes_entry.toFixed(3) : '--'}</td>
                <td>${h.no_entry > 0 ? '$'+h.no_entry.toFixed(3) : '--'}</td>
                <td>${h.t1_price > 0 ? h.t1_side + ' @ ' + h.t1_price.toFixed(3) : '--'}</td>
                <td>${h.t2_price > 0 ? h.t2_side + ' @ ' + h.t2_price.toFixed(3) : '--'}</td>
                <td class="${h.pnl>0?'val-green':(h.pnl<0?'val-red':'')}">${pnlStr}</td>
            </tr>`;
        });
        if(logHtml) document.getElementById('log-body').innerHTML = logHtml;

    } catch(e) {}
}, 500); 
</script>
</body>
</html>
"""

class EmbeddedDashboard(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()

    def do_GET(self):
        # Health checks fast path
        if self.path in ["/health", "/healthz", "/ping"]:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
            
        if self.path == "/favicon.ico":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"")
            return
            
        if self.path == "/api/status":
            now = get_synced_time()
            m_data, history_data = [], []
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                ttr = int(m.end_ts - now)
                if m.state == MarketState.CLOSED and m.close_time != "":
                    history_data.append({
                        "time": m.close_time, "slug": m.slug, 
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price, 
                        "pnl": m.realized_pnl, "t1_side": m.t1_side, "t1_price": m.t1_price, 
                        "t2_side": m.t2_side, "t2_price": m.t2_price
                    })
                else:
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    m_data.append({
                        "slug": m.slug, "state": m.state, "ttr_s": ttr,
                        "target_price": getattr(m, 'pending_target_price', 0.0),
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price,
                        "yes_shares": m.yes_shares, "no_shares": m.no_shares,
                        "yes_ask": yb.ask if yb else 0.0, "no_ask": nb.ask if nb else 0.0,
                        "history_yes": m.history_yes[-30:], "history_no": m.history_no[-30:]
                    })
            payload = {
                "uptime_s": int(time.time() - SYSTEM_BOOT_TIME),
                "time_offset": GLOBAL_STATE.time_offset,
                "ws_connected": GLOBAL_STATE.ws_connected, 
                "pnl": GLOBAL_STATE.total_pnl,
                "total_trades_count": GLOBAL_STATE.total_trades, 
                "losers": GLOBAL_STATE.sold_losers,
                "catastrophes": GLOBAL_STATE.catastrophes,
                "markets": m_data, "history": history_data[-15:]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return
            
        if self.path in ["/api/dl_trades", "/api/dl_snaps", "/api/dl_telemetry"]:
            filename = "trades_full.csv"
            if self.path == "/api/dl_snaps": filename = "snapshot_live.csv"
            elif self.path == "/api/dl_telemetry": filename = "telemetry_shadow.csv"
            self.send_response(200)
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Type', 'text/csv')
            self.end_headers()
            try:
                with open(filename, "rb") as f:
                    self.wfile.write(f.read())
            except Exception: pass
            return

        # Default route serves the dashboard UI
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def do_POST(self):
        if self.path == "/api/delete_logs":
            if os.path.exists("trades_full.csv"): os.remove("trades_full.csv")
            if os.path.exists("snapshot_live.csv"): os.remove("snapshot_live.csv")
            if os.path.exists("telemetry_shadow.csv"): os.remove("telemetry_shadow.csv")
            init_csv()
            self.send_response(200)
            self.end_headers()

    def log_message(self, format, *args): 
        pass

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    init_csv()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    
    server = ThreadedHTTPServer(("0.0.0.0", PORT), EmbeddedDashboard)
    print(f"[System Engine] High-fidelity runner listening on 0.0.0.0:{PORT}", flush=True)
    
    try: 
        server.serve_forever()
    except KeyboardInterrupt: 
        GLOBAL_STATE.running = False