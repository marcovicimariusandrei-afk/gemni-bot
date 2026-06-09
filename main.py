"""
main.py — BSS Bot v6.2 (Rate-Limit Proof + Hybrid WS/REST Telemetry)
FULL PRODUCTION BUILD
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

# ─── CONFIGURATION ───
MODE = os.getenv("MODE", "dry").lower()
BASE_CAPITAL_PER_LEG = 5.1  
TAKER_FEE_RATE = 0.018 

# Maker Phase Timings
PHASE_1_TTR_START = 600   
ENTRY_CUTOFF_TTR = 120    
HEDGE_DEADLINE_TTR = 320

SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T1_TTR_MAX = 60
SELL_LOSER_T2_THRESH = 0.95

GUARD_IMBALANCE_THRESHOLD = 2.0
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "60"))
PORT = int(os.getenv("PORT", "8080"))
SYSTEM_BOOT_TIME = time.time()

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
        self.condition_id = condition_id
        self.slug = slug
        self.yes_token = yes_id
        self.no_token = no_id
        self.end_ts = end_ts
        self.state = MarketState.WATCH
        
        self.pending_target_price = 0.0
        self.yes_entry_price, self.no_entry_price, self.yes_shares, self.no_shares = 0.0, 0.0, 0.0, 0.0
        self.total_fees_paid = 0.0
        
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
        self.last_update = time.time()

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
        self.markets: Dict[str, MarketData] = {}
        self.books: Dict[str, OrderBook] = {}
        self.subscribed_tokens = set()
        self.ws_connected = False
        self.ws_handle = None
        self.total_pnl = 0.0
        self.total_trades, self.sold_losers, self.catastrophes = 0, 0, 0
        self.time_offset = 0.0

GLOBAL_STATE = BotState()

def sync_time_with_api():
    try:
        t0 = time.time()
        res = requests.get("https://gamma-api.polymarket.com/events?limit=1", timeout=5)
        t1 = time.time()
        if res.status_code == 200:
            server_time_str = res.headers.get("Date", "")
            if server_time_str:
                server_dt = datetime.strptime(server_time_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                GLOBAL_STATE.time_offset = (server_dt.timestamp() + ((t1 - t0) / 2.0)) - t1
    except Exception: pass

def get_synced_time() -> float: return time.time() + GLOBAL_STATE.time_offset

# ─── LOGGING & EXECUTION ───
def init_csv():
    files = [
        ("trades_full.csv", ["Timestamp", "Slug", "Action", "Side", "Executed_Price", "Share_Quantity", "Fees_Paid", "TTR_at_Execution", "Realized_PnL", "Verify_Link"]),
        ("snapshot_live.csv", ["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"]),
        ("telemetry_shadow.csv", ["Timestamp", "Slug", "Token", "TTR", "Ticker_Price", "Local_Bid_Vol", "Local_Ask_Vol", "Imbalance_Ratio", "Signal"])
    ]
    for f, headers in files:
        if not os.path.exists(f):
            with open(f, "w", newline="") as fp: csv.writer(fp).writerow(headers)

def log_trade_worker(ts, slug, action, side, price, shares, fees, ttr, pnl):
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}", f"https://polymarket.com/event/{slug}"])
    except Exception: pass

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
    threading.Thread(target=log_trade_worker, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

# ─── ENGINE ───
def check_imbalance(book: OrderBook) -> float:
    if not book: return 0.0
    b_vol, a_vol = book.get_local_vols(book.bid, "bid"), book.get_local_vols(book.ask, "ask")
    if a_vol == 0 and b_vol > 0: return 999.0 
    if a_vol == 0: return 0.0
    ratio = b_vol / a_vol
    return ratio if ratio >= GUARD_IMBALANCE_THRESHOLD else 0.0

def evaluate_market(mdm: MarketData, now: float):
    if getattr(mdm, 'expired_processed', False): return
    ttr = int(mdm.end_ts - now)
    
    if ttr < 0:
        mdm.expired_processed = True
        mdm.state = MarketState.CLOSED
        return

    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    
    if ttr <= 1:
        mdm.expired_processed = True
        if mdm.state != MarketState.CLOSED:
            mdm.state = MarketState.CLOSED
            if not yb or not nb: return
            winner_side = "YES" if yb.bid > nb.bid else "NO"
            if mdm.t1_side == winner_side or mdm.t2_side == winner_side: GLOBAL_STATE.catastrophes += 1
            cost_basis = mdm.total_fees_paid + (BASE_CAPITAL_PER_LEG * (1 if mdm.yes_shares == 0 or mdm.no_shares == 0 else 2))
            win_shares = mdm.yes_shares if winner_side == "YES" else mdm.no_shares 
            execute_trade(mdm, winner_side, 0.0, "EXPIRED_AT_BUZZER", 0.0, 0.0, ttr, (win_shares * 1.0) + mdm.salvage_revenue - cost_basis)
        return
        
    if not yb or not nb: return
    if mdm.state == MarketState.CLOSED: return
    
    if ttr > ENTRY_CUTOFF_TTR:
        if mdm.state == MarketState.WATCH and ttr >= PHASE_1_TTR_START:
            mdm.state = MarketState.PENDING_MAKER
            mdm.pending_target_price = 0.49
            print(f"[Entry Engine] {mdm.slug} | Rested Maker Orders at $0.49 (TTR: {ttr}s)", flush=True)

        if mdm.state == MarketState.PENDING_MAKER:
            if ttr < PHASE_1_TTR_START and mdm.pending_target_price == 0.49:
                mdm.pending_target_price = 0.50
                print(f"[Entry Engine] {mdm.slug} | Target un-filled. Stepping up to $0.50 Maker Order.", flush=True)

            if 0 < yb.ask <= mdm.pending_target_price and mdm.yes_shares == 0:
                mdm.yes_entry_price = mdm.pending_target_price
                mdm.yes_shares = BASE_CAPITAL_PER_LEG / mdm.yes_entry_price
                execute_trade(mdm, "YES", mdm.yes_entry_price, "MAKER_FILL_LEG_1", mdm.yes_shares, 0.0, ttr)

            if 0 < nb.ask <= mdm.pending_target_price and mdm.no_shares == 0:
                mdm.no_entry_price = mdm.pending_target_price
                mdm.no_shares = BASE_CAPITAL_PER_LEG / mdm.no_entry_price
                execute_trade(mdm, "NO", mdm.no_entry_price, "MAKER_FILL_LEG_2", mdm.no_shares, 0.0, ttr)

            if mdm.yes_shares > 0 and mdm.no_shares > 0: mdm.state = MarketState.BOTH
            elif mdm.yes_shares > 0: mdm.state = MarketState.WAITING_NO
            elif mdm.no_shares > 0: mdm.state = MarketState.WAITING_YES

        if mdm.state in [MarketState.WAITING_NO, MarketState.WAITING_YES] and ttr <= HEDGE_DEADLINE_TTR:
            active_book = nb if mdm.state == MarketState.WAITING_NO else yb
            if check_imbalance(active_book) > 0:
                side = "NO" if mdm.state == MarketState.WAITING_NO else "YES"
                price = nb.ask if side == "NO" else yb.ask
                if 0 < price <= 0.51:
                    fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
                    mdm.total_fees_paid += fee
                    if side == "NO": mdm.no_entry_price, mdm.no_shares = price, BASE_CAPITAL_PER_LEG / price
                    else: mdm.yes_entry_price, mdm.yes_shares = price, BASE_CAPITAL_PER_LEG / price
                    mdm.state = MarketState.BOTH
                    execute_trade(mdm, side, price, "TAKER_HEDGE_PROTECTION", BASE_CAPITAL_PER_LEG / price, fee, ttr)

    if mdm.state == MarketState.BOTH:
        mdm.guard_active_yes = mdm.guard_active_no = False
        winner_bid, loser_side, loser_bid, loser_shares, loser_book = (yb.bid, "NO", nb.bid, mdm.no_shares, nb) if yb.bid > nb.bid else (nb.bid, "YES", yb.bid, mdm.yes_shares, yb)
            
        if not mdm.t1_executed and winner_bid >= SELL_LOSER_T1_THRESH and 0 < ttr <= SELL_LOSER_T1_TTR_MAX:
            guard_ratio = check_imbalance(loser_book)
            if guard_ratio > 0:
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
            guard_ratio = check_imbalance(loser_book)
            if guard_ratio > 0:
                if loser_side == "YES": mdm.guard_active_yes = True
                else: mdm.guard_active_no = True
                mdm.t2_guarded, mdm.t2_guard_ratio = True, guard_ratio
            else:
                mdm.state = MarketState.CLOSED
                shares_to_sell = loser_shares * 0.99 
                fee = (shares_to_sell * loser_bid) * 0.001 
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T2", shares_to_sell, fee, ttr)
                cost_basis = (BASE_CAPITAL_PER_LEG * 2) + mdm.total_fees_paid
                win_shares = mdm.yes_shares if loser_side == "NO" else mdm.no_shares
                execute_trade(mdm, "CLOSED", winner_bid, "CLOSED_T2_RESOLVED", 0.0, 0.0, ttr, (win_shares * 1.0) + mdm.salvage_revenue - cost_basis)

# ─── THREAD LOOPS ───
def tick_loop():
    while GLOBAL_STATE.running:
        now = get_synced_time()
        for m in list(GLOBAL_STATE.markets.values()):
            try: evaluate_market(m, now)
            except Exception: pass
        time.sleep(0.05)

def snapshot_loop():
    while GLOBAL_STATE.running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("snapshot_live.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in GLOBAL_STATE.markets.values():
                    if m.end_ts >= get_synced_time() - 5:
                        yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                        ya, ybd = yb.ask if yb else 0, yb.bid if yb else 0
                        na, nbd = nb.ask if nb else 0, nb.bid if nb else 0
                        writer.writerow([ts, m.slug, m.state, f"{ya:.3f}", f"{ybd:.3f}", f"{na:.3f}", f"{nbd:.3f}"])
                        m.history_yes.append(ya)
                        m.history_no.append(na)
                        if len(m.history_yes) > 30: m.history_yes.pop(0)
                        if len(m.history_no) > 30: m.history_no.pop(0)
        except Exception: pass
        time.sleep(30)

def telemetry_loop():
    while GLOBAL_STATE.running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("telemetry_shadow.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in list(GLOBAL_STATE.markets.values()):
                    if m.state == MarketState.CLOSED: continue
                    ttr = int(m.end_ts - get_synced_time())
                    if ttr < 0: continue
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    for book, side, tk in [(yb, "YES", m.yes_token), (nb, "NO", m.no_token)]:
                        if book and book.bids and book.asks:
                            b_vol, a_vol = book.get_local_vols(book.bid, "bid"), book.get_local_vols(book.ask, "ask")
                            r = b_vol / a_vol if a_vol > 0 else 999.0
                            r_inv = a_vol / b_vol if b_vol > 0 else 999.0
                            if r >= 2.0: writer.writerow([ts, m.slug, side, ttr, f"{book.ask:.3f}", f"{b_vol:.0f}", f"{a_vol:.0f}", f"{r:.1f}", "BID_WALL"])
                            elif r_inv >= 2.0: writer.writerow([ts, m.slug, side, ttr, f"{book.ask:.3f}", f"{b_vol:.0f}", f"{a_vol:.0f}", f"{r_inv:.1f}", "ASK_WALL"])
        except Exception: pass
        time.sleep(5)

def hybrid_rest_fallback():
    while GLOBAL_STATE.running:
        for m in list(GLOBAL_STATE.markets.values()):
            if m.state == MarketState.CLOSED: continue
            for tk in [m.yes_token, m.no_token]:
                book = GLOBAL_STATE.books.get(tk)
                if book and time.time() - book.last_update > 10.0:
                    try:
                        res = requests.get(f"https://clob.polymarket.com/book?token_id={tk}", timeout=2)
                        if res.status_code == 200:
                            data = res.json()
                            book.bids = {float(b["price"]): float(b["size"]) for b in data.get("bids", [])}
                            book.asks = {float(a["price"]): float(a["size"]) for a in data.get("asks", [])}
                            book.last_update = time.time()
                    except Exception: pass
        time.sleep(2)

def discovery_thread():
    print("[inf] Discovery Loop Active", flush=True)
    while GLOBAL_STATE.running:
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
                            y_idx = 0 if json.loads(m_info["outcomes"])[0].lower() in ["yes", "up"] else 1
                            yes_tk, no_tk = tks[y_idx], tks[1-y_idx]
                            GLOBAL_STATE.markets[cid] = MarketData(cid, slug, yes_tk, no_tk, end_ts)
                            GLOBAL_STATE.books[yes_tk] = OrderBook()
                            GLOBAL_STATE.books[no_tk] = OrderBook()
                            GLOBAL_STATE.subscribed_tokens.add(yes_tk)
                            GLOBAL_STATE.subscribed_tokens.add(no_tk)
                            print(f"[Discovery] Tracking: {slug}", flush=True)
                            new_markets = True
            except Exception as e: print(f"[Discovery Warning] {slug} fetch failed: {e}", flush=True)
            time.sleep(0.5) 
            
        if new_markets and GLOBAL_STATE.ws_handle:
            try: GLOBAL_STATE.ws_handle.close()
            except Exception: pass
        time.sleep(15)

def polymarket_ws_thread():
    print("[inf] WS Manager Active", flush=True)
    def on_message(ws, msg):
        try:
            parsed = json.loads(msg)
            for event in (parsed if isinstance(parsed, list) else [parsed]):
                aid = event.get("asset_id") or event.get("market")
                if not aid or aid not in GLOBAL_STATE.books: continue
                book = GLOBAL_STATE.books[aid]
                book.last_update = time.time()
                if event.get("event_type") == "book":
                    book.bids = {float(b["price"]): float(b["size"]) for b in event.get("bids", [])}
                    book.asks = {float(a["price"]): float(a["size"]) for a in event.get("asks", [])}
                elif event.get("event_type") == "price_change":
                    for ch in event.get("changes", []):
                        s, p, sz = ch.get("side", ""), float(ch.get("price", 0)), float(ch.get("size", 0))
                        target = book.bids if s == "BUY" else book.asks
                        if sz == 0: target.pop(p, None)
                        else: target[p] = sz
        except Exception: pass

    def on_open(ws):
        GLOBAL_STATE.ws_connected = True
        print("[inf] WS Connected to Gamma", flush=True)
        if GLOBAL_STATE.subscribed_tokens:
            try: ws.send(json.dumps({"type": "market", "assets_ids": list(GLOBAL_STATE.subscribed_tokens)}))
            except Exception: pass

    while GLOBAL_STATE.running:
        try:
            ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market", on_message=on_message, on_open=on_open)
            GLOBAL_STATE.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception: pass
        GLOBAL_STATE.ws_handle, GLOBAL_STATE.ws_connected = None, False
        time.sleep(2)

# ─── DASHBOARD HTML & SERVER ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Maker Analysis Dashboard v6.2</title>
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
    .sec-title { background: var(--header-bg); font-size: 15px; font-weight: bold; text-align: center; padding: 12px; margin-bottom: 15px; border-radius: 6px; border: 1px solid var(--border-color);}
    .grid { display: grid; grid-template-columns: 1fr; gap: 20px; margin-bottom: 35px; }
    .card { background: var(--bg-panel); border: 1px solid var(--border-color); box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden;}
    .card-header { background: var(--sub-header-bg); padding: 12px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; font-weight: 800; font-size: 15px;}
    .leg-container { display: flex; width: 100%; }
    .leg-col { flex: 1; padding: 20px; border-right: 1px solid var(--border-color); }
    .leg-col:last-child { border-right: none; }
    .leg-title { font-size: 13px; font-weight: 800; text-align: center; margin-bottom: 15px; color: var(--text-light); }
    .data-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: var(--text-light); }
    .data-row b { color: var(--text-navy); font-family: monospace; font-size: 15px;}
    .table-container { background: var(--bg-panel); border: 1px solid var(--border-color); margin-bottom: 35px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: var(--sub-header-bg); color: var(--text-light); font-size: 11px; font-weight: 800; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border-color); text-align: center;}
    td { padding: 12px 10px; border-bottom: 1px solid var(--border-color); text-align: center; font-size: 13px; font-family: monospace; }
    .queue-container { background: var(--bg-panel); border: 1px solid var(--border-color); padding: 20px; font-family: monospace; font-size: 13px; color: var(--text-light); line-height: 1.8; border-radius: 6px; }
    .vault { display: flex; gap: 15px; background: var(--sub-header-bg); padding: 15px; border: 1px solid var(--border-color); align-items: center; justify-content: center; margin-bottom: 25px; border-radius: 6px;}
    .btn-action { background: #1E293B; color: var(--text-navy); border: 1px solid var(--border-color); padding: 8px 18px; cursor: pointer; font-weight: 700; border-radius: 4px; }
</style>
</head>
<body>

<div class="header-panel">
    <div class="brand-title">BSS Maker Analysis Dashboard v6.2
        <span class="status-tags" id="bot-uptime">[Uptime: 0h 0m 0s]</span>
        <span class="status-tags" id="ws-status">[WS: Checking...]</span>
    </div>
    <div class="vitals-row">
        <div class="vital-box"><div class="vital-label">Total Realized P&L</div><div class="vital-value" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Completed Trades</div><div class="vital-value" id="v-trades">0</div></div>
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
    <button class="btn-action" style="color: #FCD34D;" onclick="window.location.href='/api/dl_telemetry'">Download Telemetry</button>
</div>

<div class="sec-title">Observation Queue (Scouting & Maker Waiting)</div>
<div class="queue-container" id="obs-queue">Scanning...</div>

<script>
setInterval(async () => {
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        
        let up = s.uptime_s;
        document.getElementById('bot-uptime').textContent = `[Uptime: ${Math.floor(up/3600)}h ${Math.floor((up%3600)/60)}m ${up%60}s]`;
        document.getElementById('ws-status').textContent = s.ws_connected ? "[WS: CONNECTED]" : "[WS: DROPPED / REST FALLBACK]";
        document.getElementById('ws-status').style.color = s.ws_connected ? "#34d399" : "#f87171";
        
        const pnlBox = document.getElementById('v-pnl');
        pnlBox.textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        pnlBox.className = "vital-value " + (s.pnl > 0 ? "green" : (s.pnl < 0 ? "red" : ""));
        document.getElementById('v-trades').textContent = s.total_trades_count;
        
        let activeCount = 0; let htmlCards = ''; let htmlQueue = '';
        s.markets.forEach(m => {
            if (['WATCH', 'WAITING_NO', 'WAITING_YES', 'PENDING_MAKER'].includes(m.state)) {
                htmlQueue += `[TTR: ${m.ttr_s}s] | ${m.slug} | Target: $${m.target_price.toFixed(2)} | YES Ask: $${m.yes_ask.toFixed(3)} | NO Ask: $${m.no_ask.toFixed(3)} | Status: ${m.state}<br>`;
                return;
            }
            if (m.state === 'CLOSED') return;
            activeCount++;
            htmlCards += `<div class="card">
                <div class="card-header"><span>${m.slug}</span><span>TTR: ${m.ttr_s}s</span></div>
                <div class="leg-container">
                    <div class="leg-col">
                        <div class="leg-title">YES LEG</div>
                        <div class="data-row"><span>Entry:</span> <b>$${m.yes_entry.toFixed(3)}</b></div>
                        <div class="data-row"><span>Live Ask:</span> <b>$${m.yes_ask.toFixed(3)}</b></div>
                    </div>
                    <div class="leg-col">
                        <div class="leg-title">NO LEG</div>
                        <div class="data-row"><span>Entry:</span> <b>$${m.no_entry.toFixed(3)}</b></div>
                        <div class="data-row"><span>Live Ask:</span> <b>$${m.no_ask.toFixed(3)}</b></div>
                    </div>
                </div>
            </div>`;
        });
        
        document.getElementById('v-active').textContent = activeCount;
        if(htmlCards) document.getElementById('active-cards').innerHTML = htmlCards;
        else document.getElementById('active-cards').innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-light); font-weight: bold;">No Active Positions</div>';
        document.getElementById('obs-queue').innerHTML = htmlQueue || 'No upcoming markets in window.';

        let logHtml = '';
        s.history.reverse().forEach(h => {
            logHtml += `<tr>
                <td style="color:var(--text-light);">${h.time}</td><td>${h.slug}</td>
                <td>${h.yes_entry > 0 ? '$'+h.yes_entry.toFixed(3) : '--'}</td>
                <td>${h.no_entry > 0 ? '$'+h.no_entry.toFixed(3) : '--'}</td>
                <td>${h.t1_price > 0 ? h.t1_side + ' @ ' + h.t1_price.toFixed(3) : '--'}</td>
                <td>${h.t2_price > 0 ? h.t2_side + ' @ ' + h.t2_price.toFixed(3) : '--'}</td>
                <td style="color:${h.pnl>0?'var(--val-green)':'var(--val-red)'}">${h.pnl.toFixed(2)}</td>
            </tr>`;
        });
        if(logHtml) document.getElementById('log-body').innerHTML = logHtml;

    } catch(e) {}
}, 500); 
</script>
</body></html>
"""

class EmbeddedDashboard(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def do_GET(self):
        if self.path in ["/health", "/ping"]: self.send_response(200); self.end_headers(); self.wfile.write(b"OK"); return
        if self.path == "/favicon.ico": self.send_response(200); self.end_headers(); return
        if self.path == "/api/status":
            now = get_synced_time()
            m_data, history_data = [], []
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                ttr = int(m.end_ts - now)
                if m.state == MarketState.CLOSED and m.close_time != "":
                    history_data.append({
                        "time": m.close_time, "slug": m.slug, "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price, "pnl": m.realized_pnl,
                        "t1_side": m.t1_side, "t1_price": m.t1_price, "t2_side": m.t2_side, "t2_price": m.t2_price
                    })
                else:
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    m_data.append({
                        "slug": m.slug, "state": m.state, "ttr_s": ttr, "target_price": m.pending_target_price,
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price,
                        "yes_ask": yb.ask if yb else 0.0, "no_ask": nb.ask if nb else 0.0,
                    })
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"uptime_s": int(time.time() - SYSTEM_BOOT_TIME), "ws_connected": GLOBAL_STATE.ws_connected, "pnl": GLOBAL_STATE.total_pnl, "total_trades_count": GLOBAL_STATE.total_trades, "markets": m_data, "history": history_data[-15:]}).encode())
            return
        if self.path in ["/api/dl_trades", "/api/dl_snaps", "/api/dl_telemetry"]:
            fn = "trades_full.csv" if self.path == "/api/dl_trades" else ("snapshot_live.csv" if self.path == "/api/dl_snaps" else "telemetry_shadow.csv")
            self.send_response(200); self.send_header('Content-Disposition', f'attachment; filename="{fn}"'); self.send_header('Content-Type', 'text/csv'); self.end_headers()
            try:
                with open(fn, "rb") as f: self.wfile.write(f.read())
            except Exception: pass
            return
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(DASHBOARD_HTML.encode())
    def log_message(self, format, *args): pass

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads, allow_reuse_address = True, True

if __name__ == "__main__":
    init_csv()
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    threading.Thread(target=hybrid_rest_fallback, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), EmbeddedDashboard)
    print(f"[System Engine] Listening on 0.0.0.0:{PORT}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: GLOBAL_STATE.running = False