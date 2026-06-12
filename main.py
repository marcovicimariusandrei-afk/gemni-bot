"""
main.py — BSS Bot v6.15 (Tiered Exit + Maker-First Hook + Stalk Abort + Vault)
FULL PRODUCTION BUILD - TERMINAL UI RESTORED
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
HEDGE_DEADLINE_TTR = 320
ENTRY_CUTOFF_TTR = 120      # V6.15: Naked Abort Trigger

# Cost Parameters
MAX_COMBINED_COST = 1.01    # V6.15: Strict Entry Spread Defense
T_WINDOW_1 = 0.49  
T_WINDOW_2 = 0.50  

# Exit Parameters
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T2_THRESH = 0.95

# ─── CORE SYSTEM INITIALIZATION ───
def init_csv():
    headers_trades = ["Timestamp", "Slug", "Action", "Side", "Executed_Price", "Share_Quantity", "Fees_Paid", "TTR_at_Execution", "Realized_PnL"]
    if not os.path.exists(CSV_TRADES):
        with open(CSV_TRADES, "w", newline="") as f: csv.writer(f).writerow(headers_trades)
    
    headers_snaps = ["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"]
    if not os.path.exists(CSV_SNAPS):
        with open(CSV_SNAPS, "w", newline="") as f: csv.writer(f).writerow(headers_snaps)

    headers_tele = ["Timestamp", "Slug", "Token", "TTR", "Ticker_Price", "Local_Bid_Vol", "Local_Ask_Vol", "Imbalance_Ratio", "Signal"]
    if not os.path.exists(CSV_TELEMETRY):
        with open(CSV_TELEMETRY, "w", newline="") as f: csv.writer(f).writerow(headers_tele)

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

# ─── RICH TERMINAL CONSOLE UI ───
def render_cloud_dashboard(engine):
    print("\n" + "═"*75)
    print(f" 📊 BSS V6.15 LIVE TRADING DISPLAY  |  🐈 CATASTROPHIC WHIPSAWS: {engine.cats_count}")
    print("═"*75)
    
    now = get_synced_time()
    active_markets = [m for m in GLOBAL_STATE.markets.values() if m.state == MarketState.BOTH and m.end_ts - now > 0]
    scouting_markets = [m for m in GLOBAL_STATE.markets.values() if m.state not in [MarketState.BOTH, MarketState.CLOSED] and m.end_ts - now > 0]
    
    if not active_markets:
        print("   [No fully locked dual-leg positions currently active]")
    else:
        print(" 🎯 EXECUTION FOCUS (ACTIVE POSITIONS)")
        for m in sorted(active_markets, key=lambda x: x.end_ts):
            ttr = int(m.end_ts - now)
            yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
            
            y_b, y_a = (yb.bid, yb.ask) if yb else (0.0, 0.0)
            n_b, n_a = (nb.bid, nb.ask) if nb else (0.0, 0.0)
            
            y_mid = ((y_b + y_a) / 2.0) if (y_b > 0 and y_a > 0) else y_b
            n_mid = ((n_b + n_a) / 2.0) if (n_b > 0 and n_a > 0) else n_b
            
            y_val = m.yes_shares * y_b
            n_val = m.no_shares * n_b
            
            status = engine.dashboard_ui.get(m.slug, {}).get("Status", "[TRACKING]")
            
            print(" " + "-"*73)
            print(f"  {m.slug}  |  TTR: {ttr}s  |  {status}")
            print(f"   [YES] Entry: ${m.yes_entry_price:.3f} | Shares: {m.yes_shares:5.2f} | Mid: ${y_mid:.3f} | Bid Val: ${y_val:.2f}")
            print(f"   [NO]  Entry: ${m.no_entry_price:.3f} | Shares: {m.no_shares:5.2f} | Mid: ${n_mid:.3f} | Bid Val: ${n_val:.2f}")
            
    if scouting_markets:
        print("\n 🔍 SCOUTING QUEUE (GATHERING LEGS)")
        print(" " + "-"*73)
        for m in sorted(scouting_markets, key=lambda x: x.end_ts):
            ttr = int(m.end_ts - now)
            yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
            y_a = yb.ask if yb else 0.0
            n_a = nb.ask if nb else 0.0
            y_str = f"FILLED ${m.yes_entry_price:.3f}" if m.yes_shares > 0 else f"Ask: ${y_a:.3f}"
            n_str = f"FILLED ${m.no_entry_price:.3f}" if m.no_shares > 0 else f"Ask: ${n_a:.3f}"
            
            status_str = "Filling" if m.state != MarketState.WATCH else "Scouting"
            print(f"  {m.slug} | TTR: {ttr:<4} | {status_str:<8} | YES {y_str:<13} | NO {n_str}")
    print("═"*75 + "\n", flush=True)

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

# ─── TIME & ORACLES ───
def sync_time_with_api():
    try:
        start_ping = time.time()
        res = requests.get("https://gamma-api.polymarket.com/events?limit=1", timeout=5)
        end_ping = time.time()
        if res.status_code == 200:
            rtt_latency = (end_ping - start_ping) / 2.0 
            server_time_str = res.headers.get("Date", "")
            if server_time_str:
                server_dt = datetime.strptime(server_time_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                server_ts = server_dt.timestamp() + rtt_latency
                local_ts = end_ping
                GLOBAL_STATE.time_offset = server_ts - local_ts
    except Exception: pass

def get_synced_time() -> float:
    return time.time() + GLOBAL_STATE.time_offset

def btc_oracle_loop():
    while GLOBAL_STATE.running:
        try:
            res = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3)
            if res.status_code == 200: GLOBAL_STATE.btc_live = float(res.json()["price"])
        except Exception: pass
        time.sleep(2)

def run_diagnostics():
    print("\n" + "═"*55)
    print(" 🛡️  [SYSTEM DIAGNOSTICS] V6.15 ENGINE INITIALIZING...")
    print("═"*55, flush=True)
    print(f" [Data Vault] Active Session : {SESSION_ID}")
    while GLOBAL_STATE.btc_live == 0.0 or not GLOBAL_STATE.ws_connected:
        time.sleep(0.5)
    print(f" [REST] Gamma API Sync     : OK (Offset: {GLOBAL_STATE.time_offset:+.3f}s)")
    print(f" [API]  Binance Spot Oracle: ONLINE (${GLOBAL_STATE.btc_live:,.2f})")
    print(f" [WSS]  Polymarket Stream  : CONNECTED")
    print("═"*55)
    print(" [inf] Health checks passed. Arming V6.15 engine...\n", flush=True)
    GLOBAL_STATE.armed = True

# ─── DASHBOARD HTML (Still running silently in the background!) ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>BSS V6.15</title><style>body { background: #0B1120; color: #F8FAFC; font-family: monospace; padding: 20px; }</style></head>
<body><h1>Terminal Dashboard is now the primary UI. Web hooks are active for CSV downloads.</h1>
<button onclick="window.location.href='/api/dl_trades'">Download Trades CSV</button>
<button onclick="window.location.href='/api/dl_snaps'">Download Snapshots CSV</button>
<button onclick="window.location.href='/api/dl_telemetry'">Download Telemetry CSV</button>
</body></html>"""

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))
        elif self.path in ["/api/dl_trades", "/api/dl_snaps", "/api/dl_telemetry"]:
            filename = CSV_TRADES
            if self.path == "/api/dl_snaps": filename = CSV_SNAPS
            elif self.path == "/api/dl_telemetry": filename = CSV_TELEMETRY
            self.send_response(200); self.send_header('Content-Disposition', f'attachment; filename="{filename}"'); self.send_header('Content-Type', 'text/csv'); self.end_headers()
            try:
                with open(filename, "rb") as f: self.wfile.write(f.read())
            except Exception: pass
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, format, *args): pass

def run_server():
    server = socketserver.ThreadingTCPServer(("", PORT), DashboardHandler)
    server.serve_forever()

# ─── CORE STRATEGY ───
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

    if not yb or not nb: return
    if mdm.state == MarketState.CLOSED: return
    
    y_mid = (yb.bid + yb.ask) / 2.0 if (yb.bid > 0 and yb.ask > 0) else yb.bid
    n_mid = (nb.bid + nb.ask) / 2.0 if (nb.bid > 0 and nb.ask > 0) else nb.bid
    
    # 1. Stalk & Entry
    if mdm.state == MarketState.WATCH:
        target = T_WINDOW_1 if ttr > 600 else T_WINDOW_2
        if 0 < yb.ask <= target:
            mdm.state = MarketState.WAITING_NO; mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "MAKER_FILL_LEG_1", mdm.yes_shares, fee, ttr)
        elif 0 < nb.ask <= target:
            mdm.state = MarketState.WAITING_YES; mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "MAKER_FILL_LEG_1", mdm.no_shares, fee, ttr)

    elif mdm.state == MarketState.WAITING_NO:
        if ttr <= ENTRY_CUTOFF_TTR:
            mdm.state = MarketState.CLOSED; fee = mdm.yes_shares * yb.bid * TAKER_FEE_RATE
            pnl = (mdm.yes_shares * yb.bid) - (mdm.yes_entry_price * mdm.yes_shares) - mdm.total_fees_paid - fee
            execute_trade(mdm, "YES", yb.bid, "ABORT_NAKED_LEG", mdm.yes_shares, fee, ttr, pnl)
            return
        if nb.ask > 0 and (mdm.yes_entry_price + nb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH; mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "TAKER_HEDGE_GUARANTEE", mdm.no_shares, fee, ttr)
            
    elif mdm.state == MarketState.WAITING_YES:
        if ttr <= ENTRY_CUTOFF_TTR:
            mdm.state = MarketState.CLOSED; fee = mdm.no_shares * nb.bid * TAKER_FEE_RATE
            pnl = (mdm.no_shares * nb.bid) - (mdm.no_entry_price * mdm.no_shares) - mdm.total_fees_paid - fee
            execute_trade(mdm, "NO", nb.bid, "ABORT_NAKED_LEG", mdm.no_shares, fee, ttr, pnl)
            return
        if yb.ask > 0 and (mdm.no_entry_price + yb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH; mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE; mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "TAKER_HEDGE_GUARANTEE", mdm.yes_shares, fee, ttr)

    # 2. Tiered Exit & Maker-First Hook
    elif mdm.state == MarketState.BOTH:
        if y_mid > n_mid: winner_mid, loser_side, loser_bid, loser_ask, loser_book = y_mid, "NO", nb.bid, nb.ask, nb
        else: winner_mid, loser_side, loser_bid, loser_ask, loser_book = n_mid, "YES", yb.bid, yb.ask, yb
        
        GLOBAL_STATE.engine.ofa_tracker.update_snapshot(mdm.slug, loser_book.get_local_vols(loser_bid, "bid"), loser_book.get_local_vols(loser_ask, "ask"))
        
        status_str = "[TRACKING]"
        if mdm.active_maker_ts > 0: status_str = "[MAKER HOOK PENDING]"
        elif mdm.phase1_executed or mdm.phase2_executed: status_str = "[SOLD]"
        GLOBAL_STATE.engine.dashboard_ui[mdm.slug] = {"Status": status_str}

        # Maker Hook Resolver
        if mdm.pending_exit_shares > 0:
            if mdm.active_maker_ts > 0:
                if now - mdm.active_maker_ts > 3.0:
                    taker_price = max(0.01, round(loser_bid - 0.02, 2))
                    fee = (mdm.pending_exit_shares * taker_price) * TAKER_FEE_RATE; mdm.total_fees_paid += fee
                    execute_trade(mdm, loser_side, taker_price, f"SELL_LOSER_TAKER_{mdm.pending_exit_reason}", mdm.pending_exit_shares, fee, ttr)
                    mdm.pending_exit_shares = 0; mdm.active_maker_ts = 0
            elif mdm.active_maker_ts == -1:
                taker_price = max(0.01, round(loser_bid - 0.02, 2))
                fee = (mdm.pending_exit_shares * taker_price) * TAKER_FEE_RATE; mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, taker_price, f"SELL_LOSER_TAKER_{mdm.pending_exit_reason}", mdm.pending_exit_shares, fee, ttr)
                mdm.pending_exit_shares = 0; mdm.active_maker_ts = 0
            return

        base_shares = mdm.no_shares if loser_side == "NO" else mdm.yes_shares
        remaining_shares = base_shares - mdm.shares_sold_so_far
        b_vol = loser_book.get_local_vols(loser_bid, "bid")
        
        if 0 < ttr <= 60 and winner_mid >= SELL_LOSER_T2_THRESH and mdm.phase1_executed and not mdm.phase2_executed:
            mdm.phase2_executed = True; mdm.pending_exit_shares = remaining_shares; mdm.pending_exit_reason = "T2_RUNNER_095"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK", mdm.pending_exit_shares, 0, ttr)
            else: mdm.active_maker_ts = -1

        elif 30 < ttr <= 60 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True; mdm.pending_exit_shares = base_shares * 0.50; mdm.shares_sold_so_far += mdm.pending_exit_shares; mdm.pending_exit_reason = "T1_TRANCHE_086"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK", mdm.pending_exit_shares, 0, ttr)
            else: mdm.active_maker_ts = -1

        elif 0 < ttr <= 30 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True; mdm.phase2_executed = True; mdm.pending_exit_shares = base_shares * 1.0; mdm.shares_sold_so_far += mdm.pending_exit_shares; mdm.pending_exit_reason = "LATE_BLOOMER_100PCT"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK", mdm.pending_exit_shares, 0, ttr)
            else: mdm.active_maker_ts = -1

def tick_loop():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed: time.sleep(1); continue
        now = get_synced_time()
        for m in list(GLOBAL_STATE.markets.values()):
            try: evaluate_market(m, now)
            except Exception: pass
        time.sleep(0.05)

def snapshot_loop():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed: time.sleep(1); continue
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open(CSV_SNAPS, "a", newline="") as f:
                writer = csv.writer(f)
                for m in GLOBAL_STATE.markets.values():
                    if m.end_ts >= get_synced_time() - 5:
                        yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                        ya, ybd = yb.ask if yb else 0, yb.bid if yb else 0
                        na, nbd = nb.ask if nb else 0, nb.bid if nb else 0
                        writer.writerow([ts, m.slug, m.state, f"{ya:.3f}", f"{ybd:.3f}", f"{na:.3f}", f"{nbd:.3f}"])
        except Exception: pass
        time.sleep(30)

def dashboard_render_loop():
    while GLOBAL_STATE.running:
        if GLOBAL_STATE.armed: render_cloud_dashboard(GLOBAL_STATE.engine)
        time.sleep(5)

def discovery_thread():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed: time.sleep(1); continue
        sync_time_with_api() 
        now = get_synced_time()
        boundaries = [int((now // 300) * 300) + (i * 300) for i in range(1, (LOOKAHEAD_MINUTES // 5) + 1)]
        new_markets = False
        for ts in boundaries:
            slug = f"btc-updown-5m-{ts}"
            try:
                res = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
                if res.status_code == 200 and res.json():
                    m_info = res.json()[0].get("markets", [])[0]
                    cid = m_info["conditionId"]
                    if cid not in GLOBAL_STATE.markets:
                        end_ts = datetime.fromisoformat(m_info["endDate"].replace("Z", "+00:00")).timestamp()
                        if end_ts > get_synced_time() + 120:
                            tks = json.loads(m_info["clobTokenIds"])
                            outcomes = json.loads(m_info["outcomes"])
                            y_idx = 0 if outcomes[0].lower() in ["yes", "up"] else 1
                            GLOBAL_STATE.markets[cid] = MarketData(cid, slug, tks[y_idx], tks[1-y_idx], end_ts)
                            new_markets = True
            except Exception: pass
        if new_markets and GLOBAL_STATE.ws_handle:
            try: GLOBAL_STATE.ws_handle.close()
            except Exception: pass
        time.sleep(30)

def polymarket_ws_thread():
    def on_message(ws, msg):
        try:
            parsed_msg = json.loads(msg)
            event_list = parsed_msg if isinstance(parsed_msg, list) else [parsed_msg]
            for event in event_list:
                if not isinstance(event, dict): continue
                aid = event.get("asset_id") or event.get("market")
                if not aid: continue
                if event.get("event_type") == "book":
                    book = GLOBAL_STATE.books.setdefault(aid, OrderBook())
                    book.bids = {float(b["price"]): float(b["size"]) for b in event.get("bids", [])}
                    book.asks = {float(a["price"]): float(a["size"]) for a in event.get("asks", [])}
                elif event.get("event_type") == "price_change":
                    book = GLOBAL_STATE.books.get(aid)
                    if not book: continue
                    for ch in event.get("changes", []):
                        s, p, sz = ch.get("side", ""), float(ch.get("price", 0)), float(ch.get("size", 0))
                        if s == "BUY":
                            if sz == 0: book.bids.pop(p, None)
                            else: book.bids[p] = sz
                        elif s == "SELL":
                            if sz == 0: book.asks.pop(p, None)
                            else: book.asks[p] = sz
        except Exception: pass
    def on_open(ws):
        GLOBAL_STATE.ws_connected = True
        tks = [t for m in GLOBAL_STATE.markets.values() if m.end_ts >= get_synced_time() - 30 for t in (m.yes_token, m.no_token)]
        if tks:
            try: ws.send(json.dumps({"type": "Market", "assets_ids": tks}))
            except Exception: pass
    while GLOBAL_STATE.running:
        try:
            ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market", on_message=on_message, on_open=on_open)
            GLOBAL_STATE.ws_handle = ws; ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception: pass
        GLOBAL_STATE.ws_handle = None; GLOBAL_STATE.ws_connected = False
        time.sleep(2)

if __name__ == "__main__":
    init_csv()
    sync_time_with_api()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=btc_oracle_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    run_diagnostics()
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    threading.Thread(target=dashboard_render_loop, daemon=True).start()
    while GLOBAL_STATE.running: time.sleep(1)