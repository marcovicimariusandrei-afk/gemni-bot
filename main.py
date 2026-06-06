"""
main.py — Opportunistic Both-Sides (BSS) Polymarket Bot
Includes an embedded web server to handle Railway health checks.
"""

import os
import sys
import time
import json
import threading
import signal
import requests
import websocket
import http.server
import socketserver
from typing import Dict
from datetime import datetime, timezone

# ─── CONFIGURATION ───
MODE = os.getenv("MODE", "dry").lower()
T_FIRST = float(os.getenv("BS_BSS_T_FIRST", "0.49"))
T_SECOND_PRE = float(os.getenv("BS_BSS_T_SECOND_PRE", "0.50"))
T_SECOND_LIVE = float(os.getenv("BS_BSS_T_SECOND_LIVE", "0.51"))
SELL_LOSER_THRESH = float(os.getenv("BS_SELL_LOSER_THRESHOLD", "0.93"))
SELL_LOSER_FLOOR_S = float(os.getenv("BS_SELL_LOSER_TTR_FLOOR_S", "75"))
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "60"))

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
        self.leg1_price = 0.0
        self.leg2_price = 0.0

class OrderBook:
    def __init__(self):
        self.ask = 1.0
        self.bid = 0.0

class BotState:
    def __init__(self):
        self.running = True
        self.markets: Dict[str, MarketData] = {}
        self.books: Dict[str, OrderBook] = {}
        self.ws_connected = False
        self.ws_handle = None

# ─── EMBEDDED WEB SERVER FOR RAILWAY HEALTH CHECKS ───
def run_dummy_server():
    port = int(os.getenv("PORT", "8080"))
    class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is healthy and tracking markets.")
            
        def log_message(self, format, *args):
            pass # Suppress log clutter
            
    try:
        with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
            print(f"[System] Health check server listening on port {port}", flush=True)
            httpd.serve_forever()
    except Exception as e:
        print(f"[System] Health check server error: {e}", flush=True)

# ─── CORE STRATEGY ENGINE ───
def evaluate_market(state: BotState, mdm: MarketData, now: float):
    if mdm.state == MarketState.CLOSED:
        return

    y_book = state.books.get(mdm.yes_token)
    n_book = state.books.get(mdm.no_token)
    if not y_book or not n_book:
        return 

    ttr = mdm.end_ts - now
    if ttr <= 0:
        mdm.state = MarketState.CLOSED
        return

    is_live = ttr <= 300
    t2_current = T_SECOND_LIVE if is_live else T_SECOND_PRE

    ya, yb = y_book.ask, y_book.bid
    na, nb = n_book.ask, n_book.bid

    if mdm.state == MarketState.WATCH:
        if 0 < ya <= T_FIRST:
            execute_trade(mdm, "YES", ya, "LEG_1_ENTRY")
            mdm.leg1_price = ya
            mdm.state = MarketState.WAITING_NO
        elif 0 < na <= T_FIRST:
            execute_trade(mdm, "NO", na, "LEG_1_ENTRY")
            mdm.leg1_price = na
            mdm.state = MarketState.WAITING_YES

    elif mdm.state == MarketState.WAITING_NO:
        if 0 < na <= t2_current:
            execute_trade(mdm, "NO", na, "LEG_2_ENTRY")
            mdm.leg2_price = na
            mdm.state = MarketState.BOTH

    elif mdm.state == MarketState.WAITING_YES:
        if 0 < ya <= t2_current:
            execute_trade(mdm, "YES", ya, "LEG_2_ENTRY")
            mdm.leg2_price = ya
            mdm.state = MarketState.BOTH

    elif mdm.state == MarketState.BOTH:
        if ttr <= SELL_LOSER_FLOOR_S:
            if yb >= SELL_LOSER_THRESH:
                execute_trade(mdm, "NO", nb, "SELL_LOSER")
                mdm.state = MarketState.CLOSED
            elif nb >= SELL_LOSER_THRESH:
                execute_trade(mdm, "YES", yb, "SELL_LOSER")
                mdm.state = MarketState.CLOSED

def execute_trade(mdm: MarketData, side: str, price: float, action: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Mode: {MODE}", flush=True)

def strategy_tick_thread(state: BotState):
    while state.running:
        now = time.time()
        for mdm in list(state.markets.values()):
            try:
                evaluate_market(state, mdm, now)
            except Exception as e:
                print(f"[Strategy Error] {mdm.slug}: {e}", flush=True)
        time.sleep(0.05) 

# ─── MARKET DISCOVERY ───
def discovery_thread(state: BotState):
    while state.running:
        now = time.time()
        current_b = int((now // 300) * 300)
        lookahead_count = LOOKAHEAD_MINUTES // 5
        boundaries = [current_b + (i * 300) for i in range(1, lookahead_count + 1)]

        new_markets_found = False
        for ts in boundaries:
            slug = f"btc-updown-5m-{ts}"
            try:
                res = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
                if res.status_code != 200: continue
                data = res.json()
                if not data: continue
                
                market_info = data[0].get("markets", [])[0]
                cid = market_info["conditionId"]
                
                if cid not in state.markets:
                    tokens = json.loads(market_info["clobTokenIds"])
                    outcomes = json.loads(market_info["outcomes"])
                    
                    yes_idx = 0 if outcomes[0].lower() in ["yes", "up"] else 1
                    no_idx = 1 if yes_idx == 0 else 0
                    
                    end_ts = datetime.fromisoformat(market_info["endDate"].replace("Z", "+00:00")).timestamp()
                    
                    state.markets[cid] = MarketData(cid, slug, tokens[yes_idx], tokens[no_idx], end_ts)
                    print(f"[Discovery] Tracking new market: {slug} (TTR: {end_ts - now:.0f}s)", flush=True)
                    new_markets_found = True
            except Exception:
                pass
        
        if new_markets_found and state.ws_handle:
            state.ws_handle.close()

        time.sleep(30)

# ─── WEBSOCKETS ───
def polymarket_ws_thread(state: BotState):
    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            for event in (data if isinstance(data, list) else [data]):
                if not isinstance(event, dict): continue
                
                asset_id = event.get("asset_id") or event.get("market")
                if not asset_id: continue

                if event.get("event_type") == "book":
                    book = state.books.setdefault(asset_id, OrderBook())
                    bids, asks = event.get("bids", []), event.get("asks", [])
                    book.bid = max((float(b["price"]) for b in bids), default=0.0)
                    book.ask = min((float(a["price"]) for a in asks), default=0.0)

                elif event.get("event_type") == "price_change":
                    book = state.books.get(asset_id)
                    if not book: continue
                    for ch in event.get("changes", []):
                        side, price = ch.get("side", ""), float(ch.get("price", 0))
                        if side == "BUY" and price > book.bid: book.bid = price
                        elif side == "SELL" and (book.ask == 0 or price < book.ask): book.ask = price
        except Exception:
            pass

    def on_open(ws):
        state.ws_connected = True
        tokens = []
        for mdm in state.markets.values():
            if mdm.state != MarketState.CLOSED:
                tokens.extend([mdm.yes_token, mdm.no_token])
        
        if tokens:
            ws.send(json.dumps({"type": "Market", "assets_ids": tokens}))
            print(f"[WS] Subscribed to {len(tokens)} active tokens.", flush=True)

    while state.running:
        try:
            ws = websocket.WebSocketApp(
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                on_message=on_message,
                on_open=on_open
            )
            state.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[WS Error] {e}", flush=True)
        
        state.ws_handle = None
        state.ws_connected = False
        time.sleep(2)

# ─── LIFECYCLE ───
if __name__ == "__main__":
    print(f"=== Booting BSS Predictive Bot ===", flush=True)
    bot_state = BotState()

    def shutdown(sig, frame):
        print("\n[System] Shutting down gracefully...", flush=True)
        bot_state.running = False
        if bot_state.ws_handle: bot_state.ws_handle.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Launch background processing tracks
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=discovery_thread, args=(bot_state,), daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, args=(bot_state,), daemon=True).start()
    threading.Thread(target=strategy_tick_thread, args=(bot_state,), daemon=True).start()

    while bot_state.running:
        time.sleep(1)