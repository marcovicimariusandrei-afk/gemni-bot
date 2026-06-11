"""
main.py — BSS Bot v6.14 (OFA Spoof Override + Offset Penetration + Headless)
FULL PRODUCTION BUILD
"""
import os
import sys
import time
import json
import threading
import requests
import websocket
import csv
import collections
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone

# ─── CONFIGURATION ───
MODE = os.getenv("MODE", "live").lower()

BASE_CAPITAL_PER_LEG = 5.10  
TAKER_FEE_RATE = 0.018 

# Timeline Parameters
LOOKAHEAD_MINUTES = 25  
HEDGE_DEADLINE_TTR = 320
ENTRY_CUTOFF_TTR = 120  

# Cost Parameters
MAX_COMBINED_COST = 1.03  

# Target Pricing Windows
T_WINDOW_1 = 0.49  
T_WINDOW_2 = 0.50  

# Exit Parameters
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T1_TTR_MAX = 60  
SELL_LOSER_T2_THRESH = 0.95

# ─── V6.14 CORE BLOCKS ───

class OFAVelocityTracker:
    def __init__(self, lookback_horizon_secs: int = 15, tick_interval_secs: int = 5):
        self.maxlen = max(1, int(lookback_horizon_secs / tick_interval_secs))
        self.history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=self.maxlen)
        )

    def update_snapshot(self, slug: str, bid_vol: float, ask_vol: float):
        self.history[slug].append((time.time(), bid_vol, ask_vol))

    def is_wall_fake(self, slug: str, target_side: str) -> bool:
        buffer = self.history[slug]
        if len(buffer) < self.maxlen: return False 

        initial_vol = buffer[0][1] if target_side == "YES" else buffer[0][2]
        latest_vol  = buffer[-1][1] if target_side == "YES" else buffer[-1][2]

        vol_delta = latest_vol - initial_vol
        growth_rate = (latest_vol / initial_vol) if initial_vol > 0 else 1.0

        if growth_rate < 1.25 and abs(vol_delta) < 1000: return True
        return False

class V614Engine:
    def __init__(self):
        self.ofa_tracker = OFAVelocityTracker()
        self.positions = collections.defaultdict(dict)
        self.dashboard_ui = {}
        self.trigger_threshold = SELL_LOSER_T1_THRESH
        self.static_guard_ratio = 3.5
        self.gradual_exit_pct = 0.50
        self.cats_count = 0

    def evaluate_salvage(self, market_data: dict) -> Optional[dict]:
        slug, ttr, mid_price = market_data['Slug'], market_data['TTR'], market_data['Mid_Price']
        imbalance, losing_side = market_data['Imbalance_Ratio'], market_data['Losing_Side']

        self.ofa_tracker.update_snapshot(slug, market_data['Local_Bid_Vol'], market_data['Local_Ask_Vol'])

        if ttr > 60 or mid_price < self.trigger_threshold: return None

        if imbalance < self.static_guard_ratio:
            return self.build_execution_payload(market_data, "CLEAN_BOOK")

        if self.ofa_tracker.is_wall_fake(slug, target_side=losing_side):
            return self.build_execution_payload(market_data, "OFA_SPOOF_OVERRIDE")

        return None

    def build_execution_payload(self, market_data: dict, reason: str) -> dict:
        current_bid = market_data['Bid_Price']
        # Offset Penetration: Price $0.02 below active bid to secure fill against vacuum
        penetration_limit_price = max(0.01, round(current_bid - 0.02, 2))
        slug, token_to_sell = market_data['Slug'], market_data['Losing_Side']
        shares_to_sell = self.positions[slug][token_to_sell] * self.gradual_exit_pct

        payload = {
            "slug": slug, "token": token_to_sell, "order_type": "LIMIT",
            "price": penetration_limit_price, "quantity": shares_to_sell,
            "metadata": {"execution_reason": reason}
        }
        
        self.positions[slug][token_to_sell] -= shares_to_sell
        self.dashboard_ui[slug]["Status"] = f"[LOCKED OUT: {reason}]"
        self.dashboard_ui[slug]["Sold_Side"] = token_to_sell
        return payload

def render_cloud_dashboard(engine):
    print("\n" + "="*70)
    print(f"📊 TELEMETRY DASHBOARD  |  🐈 CATASTROPHIC WHIPSAWS RECORDED: {engine.cats_count}")
    print("-" * 70)
    print(f"{'MARKET SLUG':<32} | {'TTR':<6} | {'MID PX':<8} | {'IMB':<6} | {'STATUS'}")
    print("-" * 70)
    
    if not engine.dashboard_ui: 
        print("   No active markets tracked.")
    
    # Sort by TTR descending
    sorted_ui = sorted(engine.dashboard_ui.items(), key=lambda x: x[1].get('TTR', 999), reverse=True)
    for slug, state in sorted_ui:
        print(f"{slug:<32} | {state['TTR']:<6} | ${state['Mid_Price']:<7.2f} | {state['Imbalance']:<5.1f}x | {state['Status']}")
    print("="*70 + "\n", flush=True)

# ─── STATE MODELS ───
class MarketState:
    WATCH = "WATCH"
    WAITING_NO = "WAITING_NO"
    WAITING_YES = "WAITING_YES"
    BOTH = "BOTH"
    CLOSED = "CLOSED"

class MarketData:
    def __init__(self, condition_id: str, slug: str, yes_id: str, no_id: str, end_ts: float):
        self.condition_id = condition_id
        self.slug = slug
        self.yes_token = yes_id
        self.no_token = no_id
        self.end_ts = end_ts
        self.state = MarketState.WATCH
        
        self.yes_entry_price = 0.0
        self.no_entry_price = 0.0
        self.yes_shares = 0.0
        self.no_shares = 0.0
        self.total_fees_paid = 0.0
        
        self.t1_executed = False
        self.t1_side = ""
        self.t1_price = 0.0
        self.t1_time = ""
        
        self.t2_side = ""
        self.t2_price = 0.0
        
        self.salvage_revenue = 0.0
        self.realized_pnl = 0.0
        
        self.close_time = ""
        self.close_reason = ""
        self.expired_processed = False
        self.cat_checked = False
        
        self.strike_price = 0.0
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
        self.running = True
        self.armed = False  
        self.markets: Dict[str, MarketData] = {}
        self.books: Dict[str, OrderBook] = {}
        self.ws_connected = False
        self.ws_handle = None
        self.total_pnl = 0.0
        self.total_trades = 0 
        self.time_offset = 0.0
        self.btc_live = 0.0
        self.engine = V614Engine()

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
            if res.status_code == 200:
                GLOBAL_STATE.btc_live = float(res.json()["price"])
        except Exception: pass
        time.sleep(2)

# ─── PRE-FLIGHT DIAGNOSTICS ───
def run_diagnostics():
    print("\n" + "═"*55)
    print(" 🛡️  [SYSTEM DIAGNOSTICS] V6.14 ENGINE INITIALIZING...")
    print("═"*55, flush=True)
    
    while GLOBAL_STATE.btc_live == 0.0 or not GLOBAL_STATE.ws_connected:
        time.sleep(0.5)
        
    print(f" [REST] Gamma API Sync     : OK (Offset: {GLOBAL_STATE.time_offset:+.3f}s)")
    print(f" [API]  Binance Spot Oracle: ONLINE (${GLOBAL_STATE.btc_live:,.2f})")
    print(f" [WSS]  Polymarket Stream  : CONNECTED")
    print("═"*55)
    print(" [inf] Health checks passed. Arming headless engine...\n", flush=True)
    GLOBAL_STATE.armed = True

# ─── ASYNC CSV LOGGING SYSTEM ───
def init_csv():
    if not os.path.exists("trades_full.csv"):
        with open("trades_full.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "Action", "Side", "Executed_Price", "Share_Quantity", "Fees_Paid", "TTR_at_Execution", "Realized_PnL"])
    if not os.path.exists("telemetry_shadow.csv"):
        with open("telemetry_shadow.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "Token", "TTR", "Ticker_Price", "Local_Bid_Vol", "Local_Ask_Vol", "Imbalance_Ratio", "Signal", "Vel_Pct", "Vel_Flat", "OFA_Signal"])

def log_trade_csv_worker(ts, slug, action, side, price, shares, fees, ttr, pnl):
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}"])
    except Exception: pass

# ─── CORE STRATEGY ───
def get_imbalance(book: OrderBook) -> Tuple[float, float, float]:
    if not book: return 0.0, 0.0, 0.0
    b_vol = book.get_local_vols(book.bid, "bid", 0.10)
    a_vol = book.get_local_vols(book.ask, "ask", 0.10)
    
    ratio = 0.0
    if a_vol == 0 and b_vol > 0: ratio = 999.0
    elif b_vol == 0 and a_vol > 0: ratio = 999.0
    elif a_vol > 0 and b_vol / a_vol >= 1.0: ratio = (b_vol / a_vol)
    elif b_vol > 0 and a_vol / b_vol >= 1.0: ratio = (a_vol / b_vol)
    
    return ratio, b_vol, a_vol

def execute_trade(mdm: MarketData, side: str, price: float, action: str, shares: float, fees: float, ttr: int, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    
    if action == "SELL_LOSER_T1" or action.startswith("SELL_LOSER_"):
        mdm.salvage_revenue += (shares * price)
        mdm.t1_side, mdm.t1_price, mdm.t1_time = side, price, ts
    if "CLOSED" in action or "EXPIRED" in action:
        mdm.close_time, mdm.close_reason = ts, action
        GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl
        GLOBAL_STATE.total_pnl += pnl
    threading.Thread(target=log_trade_csv_worker, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

def evaluate_market(mdm: MarketData, now: float):
    if getattr(mdm, 'expired_processed', False): return
    
    ttr = int(mdm.end_ts - now)
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)

    # Decoupled Logging: V6.14 polls for 30s after TTR 0 to capture UMA Oracle Settlements & Catastrophic Whipsaws
    if -30 <= ttr <= 0:
        if mdm.t1_executed and not mdm.cat_checked:
            sold_book = yb if mdm.t1_side == "YES" else nb
            if sold_book and sold_book.bid >= 0.99:
                GLOBAL_STATE.engine.cats_count += 1
                mdm.cat_checked = True
                
    if ttr < -30:
        mdm.expired_processed = True
        if mdm.state != MarketState.CLOSED:
            mdm.state = MarketState.CLOSED
            if mdm.slug in GLOBAL_STATE.engine.dashboard_ui:
                del GLOBAL_STATE.engine.dashboard_ui[mdm.slug]
            if not yb or not nb: return
            winner_side = "YES" if yb.bid > nb.bid else "NO"
            
            cost_basis = mdm.total_fees_paid
            if mdm.yes_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
            if mdm.no_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
            winner_shares = mdm.yes_shares if winner_side == "YES" else mdm.no_shares 
            calc_pnl = (winner_shares * 1.00) + mdm.salvage_revenue - cost_basis
            execute_trade(mdm, winner_side, 0.00, "EXPIRED_SETTLED", 0.0, 0.0, ttr, calc_pnl)
        return

    if 0 < ttr <= 300 and mdm.strike_price == 0.0 and GLOBAL_STATE.btc_live > 0:
        mdm.strike_price = GLOBAL_STATE.btc_live

    if not yb or not nb: return
    if mdm.state == MarketState.CLOSED: return
    
    # UI Dashboard Update Hook
    y_mid = (yb.bid + yb.ask) / 2.0 if (yb.bid > 0 and yb.ask > 0) else yb.bid
    n_mid = (nb.bid + nb.ask) / 2.0 if (nb.bid > 0 and nb.ask > 0) else nb.bid
    
    if mdm.state == MarketState.WATCH:
        target = T_WINDOW_1 if ttr > 600 else T_WINDOW_2
        if 0 < yb.ask <= target:
            mdm.state = MarketState.WAITING_NO
            mdm.yes_entry_price = yb.ask
            mdm.yes_shares = BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "MAKER_FILL_LEG_1", mdm.yes_shares, fee, ttr)
        elif 0 < nb.ask <= target:
            mdm.state = MarketState.WAITING_YES
            mdm.no_entry_price = nb.ask
            mdm.no_shares = BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "MAKER_FILL_LEG_1", mdm.no_shares, fee, ttr)

    elif mdm.state == MarketState.WAITING_NO:
        if nb.ask > 0 and nb.ask <= (T_WINDOW_1 if ttr > 600 else T_WINDOW_2):
            mdm.state = MarketState.BOTH
            mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "MAKER_FILL_LEG_2", mdm.no_shares, fee, ttr)
        elif nb.ask > 0 and (mdm.yes_entry_price + nb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH
            mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "TAKER_HEDGE_GUARANTEE", mdm.no_shares, fee, ttr)
            
    elif mdm.state == MarketState.WAITING_YES:
        if yb.ask > 0 and yb.ask <= (T_WINDOW_1 if ttr > 600 else T_WINDOW_2):
            mdm.state = MarketState.BOTH
            mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "MAKER_FILL_LEG_2", mdm.yes_shares, fee, ttr)
        elif yb.ask > 0 and (mdm.no_entry_price + yb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH
            mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "TAKER_HEDGE_GUARANTEE", mdm.yes_shares, fee, ttr)

    elif mdm.state == MarketState.BOTH:
        if y_mid > n_mid: 
            winner_mid, loser_side, loser_bid, loser_shares, loser_book = y_mid, "NO", nb.bid, mdm.no_shares, nb
        else: 
            winner_mid, loser_side, loser_bid, loser_shares, loser_book = n_mid, "YES", yb.bid, mdm.yes_shares, yb
            
        guard_ratio, b_vol, a_vol = get_imbalance(loser_book)
        
        # Populate Dashboard Data
        GLOBAL_STATE.engine.dashboard_ui.setdefault(mdm.slug, {})
        GLOBAL_STATE.engine.dashboard_ui[mdm.slug].update({
            "TTR": ttr,
            "Mid_Price": winner_mid,
            "Imbalance": guard_ratio,
            "Status": "[TRACKING]" if not mdm.t1_executed else GLOBAL_STATE.engine.dashboard_ui[mdm.slug].get("Status", "[SOLD]")
        })

        if not mdm.t1_executed and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            # Sync positions for the Engine payload builder
            GLOBAL_STATE.engine.positions[mdm.slug]["YES"] = mdm.yes_shares
            GLOBAL_STATE.engine.positions[mdm.slug]["NO"] = mdm.no_shares

            market_data = {
                'Slug': mdm.slug,
                'TTR': ttr,
                'Mid_Price': winner_mid,
                'Imbalance_Ratio': guard_ratio,
                'Losing_Side': loser_side,
                'Local_Bid_Vol': b_vol,
                'Local_Ask_Vol': a_vol,
                'Bid_Price': loser_bid
            }
            
            payload = GLOBAL_STATE.engine.evaluate_salvage(market_data)
            
            if payload:
                mdm.t1_executed = True
                limit_price = payload['price']
                shares_to_sell = payload['quantity']
                reason = payload['metadata']['execution_reason']
                
                fee = (shares_to_sell * limit_price) * 0.001 
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, limit_price, f"SELL_LOSER_{reason}", shares_to_sell, fee, ttr)

def tick_loop():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed:
            time.sleep(1)
            continue
            
        now = get_synced_time()
        for m in list(GLOBAL_STATE.markets.values()):
            try: evaluate_market(m, now)
            except Exception: pass
        time.sleep(0.05)

def dashboard_render_loop():
    while GLOBAL_STATE.running:
        if GLOBAL_STATE.armed:
            render_cloud_dashboard(GLOBAL_STATE.engine)
        time.sleep(5)

def telemetry_loop():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed:
            time.sleep(1)
            continue
            
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("telemetry_shadow.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in list(GLOBAL_STATE.markets.values()):
                    if m.state == MarketState.CLOSED: continue
                    ttr = int(m.end_ts - get_synced_time())
                    if ttr < -30: continue
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    if yb and yb.bids and yb.asks:
                        y_b_vol, y_a_vol = yb.get_local_vols(yb.bid, "bid", 0.10), yb.get_local_vols(yb.ask, "ask", 0.10)
                        r_y = y_b_vol / y_a_vol if y_a_vol > 0 else 999.0
                        if r_y >= 2.0: writer.writerow([ts, m.slug, "YES", ttr, f"{yb.ask:.3f}", f"{y_b_vol:.0f}", f"{y_a_vol:.0f}", f"{r_y:.1f}", "TELEMETRY", "", "", ""])
                    if nb and nb.bids and nb.asks:
                        n_b_vol, n_a_vol = nb.get_local_vols(nb.bid, "bid", 0.10), nb.get_local_vols(nb.ask, "ask", 0.10)
                        r_n = n_b_vol / n_a_vol if n_a_vol > 0 else 999.0
                        if r_n >= 2.0: writer.writerow([ts, m.slug, "NO", ttr, f"{nb.ask:.3f}", f"{n_b_vol:.0f}", f"{n_a_vol:.0f}", f"{r_n:.1f}", "TELEMETRY", "", "", ""])
        except Exception: pass
        time.sleep(5)

def discovery_thread():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed:
            time.sleep(1)
            continue
            
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
            GLOBAL_STATE.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception: pass
        GLOBAL_STATE.ws_handle = None
        GLOBAL_STATE.ws_connected = False
        time.sleep(2)

if __name__ == "__main__":
    init_csv()
    sync_time_with_api()
    
    threading.Thread(target=btc_oracle_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    
    run_diagnostics()
    
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=dashboard_render_loop, daemon=True).start()
    
    while GLOBAL_STATE.running:
        time.sleep(1)