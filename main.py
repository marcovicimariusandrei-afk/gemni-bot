"""
main.py — Opportunistic BSS Bot (v5.8.1 Dashboard)
"""
import os
import sys
import time
import json
import threading
import signal
import http.server
import socketserver
import requests
import websocket
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
        self.trades_log = []

GLOBAL_STATE = BotState()

# ─── CLASSIC DASHBOARD HTML (v5.8.1) ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Dashboard v5.8.1</title>
<style>
body { background: #0a0a0a; color: #e0e0e0; font-family: ui-monospace, Menlo, Consolas, monospace; padding: 20px; font-size: 13px; margin: 0; }
.header { display: flex; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 20px; }
h1 { margin: 0; font-size: 18px; font-weight: 600; color: #fff; }
.badge { padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-left: 15px; text-transform: uppercase; }
.badge-dry { background: rgba(88,166,255,0.15); color: #7aa5d2; border: 1px solid rgba(122,165,210,0.4); }
.badge-live { background: rgba(248,81,73,0.15); color: #d96666; border: 1px solid rgba(217,102,102,0.4); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 15px; margin-bottom: 25px; }
.card { background: #141414; border: 1px solid #262626; border-radius: 6px; padding: 15px; border-left: 3px solid #7aa5d2; }
.card-title { font-weight: 600; font-size: 14px; margin-bottom: 8px; color: #fff; }
.card-ttr { color: #e0b340; font-size: 12px; margin-bottom: 12px; }
.state-badge { display: inline-block; padding: 2px 6px; background: #262626; border-radius: 3px; font-size: 11px; color: #ccc; }
.state-WATCH { color: #7a7a7a; }
.state-WAITING_NO, .state-WAITING_YES { color: #e0b340; background: rgba(224,179,64,0.1); }
.state-BOTH { color: #5cbd5c; background: rgba(92,189,92,0.1); }
.state-CLOSED { color: #d96666; text-decoration: line-through; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #1f1f1f; }
th { color: #7a7a7a; font-weight: normal; font-size: 11px; text-transform: uppercase; }
.trade-log { background: #141414; border: 1px solid #262626; border-radius: 6px; padding: 15px; }
.trade-row { display: grid; grid-template-columns: 80px 1fr 100px 60px 80px; gap: 10px; align-items: center; padding: 8px; border-bottom: 1px solid #1f1f1f; }
.trade-row:last-child { border-bottom: none; }
.text-yes { color: #5cbd5c; font-weight: bold; }
.text-no { color: #d96666; font-weight: bold; }
</style>
</head>
<body>
<div class="header">
    <h1>BSS Bot v5.8.1</h1>
    <span id="mode-badge" class="badge badge-dry">DRY</span>
    <div style="margin-left: auto; color: #7a7a7a;" id="ws-status">WS: Checking...</div>
</div>
<div class="grid" id="content">
    <div style="color: #7a7a7a;">Scanning markets...</div>
</div>
<div class="trade-log">
    <div style="font-weight: 600; margin-bottom: 10px; color: #fff;">Execution Log</div>
    <div id="trades">
        <div style="color: #7a7a7a; padding: 8px;">No trades executed yet.</div>
    </div>
</div>
<script>
setInterval(async () => {
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        
        document.getElementById('mode-badge').textContent = s.mode.toUpperCase();
        document.getElementById('mode-badge').className = s.mode === 'live' ? 'badge badge-live' : 'badge badge-dry';
        document.getElementById('ws-status').innerHTML = s.ws_connected ? '<span style="color:#5cbd5c">WS: CONNECTED</span>' : '<span style="color:#d96666">WS: DISCONNECTED</span>';
        
        let html = '';
        s.markets.forEach(m => {
            html += `<div class="card">
                <div class="card-title">${m.slug}</div>
                <div class="card-ttr">TTR: ${m.ttr_s}s <span class="state-badge state-${m.state}" style="float:right;">${m.state}</span></div>
                <table>
                    <tr><th>Side</th><th>Ask</th><th>Bid</th><th>Leg Cost</th></tr>
                    <tr><td class="text-yes">YES</td><td>${m.yes_ask.toFixed(3)}</td><td>${m.yes_bid.toFixed(3)}</td><td>${m.state==='WAITING_NO'||m.state==='BOTH'?m.leg1_price.toFixed(3):'--'}</td></tr>
                    <tr><td class="text-no">NO</td><td>${m.no_ask.toFixed(3)}</td><td>${m.no_bid.toFixed(3)}</td><td>${m.state==='WAITING_YES'||m.state==='BOTH'?(m.state==='BOTH'?m.leg2_price.toFixed(3):m.leg1_price.toFixed(3)):'--'}</td></tr>
                </table>
            </div>`;
        });
        document.getElementById('content').innerHTML = html || '<div style="color: #7a7a7a;">No active markets in window.</div>';

        let tradesHtml = '';
        [...s.trades].reverse().forEach(t => {
            const colorClass = t.side === 'YES' ? 'text-yes' : 'text-no';
            tradesHtml += `<div class="trade-row">
                <span style="color: #7a7a7a;">${t.ts}</span>
                <span>${t.slug}</span>
                <span style="background: #262626; padding: 2px 6px; border-radius: 3px; font-size: 11px; text-align: center;">${t.action}</span>
                <span class="${colorClass}">${t.side}</span>
                <span>$${t.price.toFixed(3)}</span>
            </div>`;
        });
        if(tradesHtml) document.getElementById('trades').innerHTML = tradesHtml;

    } catch(e) {}
}, 1000);
</script>
</body>
</html>
"""

# ─── API & SERVER ───
class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))
        elif self.path == "/api/status":
            now = time.time()
            m_data = []
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                yb = GLOBAL_STATE.books.get(m.yes_token)
                nb = GLOBAL_STATE.books.get(m.no_token)
                m_data.append({
                    "slug": m.slug,
                    "state": m.state,
                    "ttr_s": max(0, int(m.end_ts - now)),
                    "leg1_price": m.leg1_price,
                    "leg2_price": m.leg2_price,
                    "yes_ask": yb.ask if yb else 0.0,
                    "yes_bid": yb.bid if yb else 0.0,
                    "no_ask": nb.ask if nb else 0.0,
                    "no_bid": nb.bid if nb else 0.0
                })
            
            payload = {
                "mode": MODE,
                "ws_connected": GLOBAL_STATE.ws_connected,
                "markets": m_data,
                "trades": GLOBAL_STATE.trades_log[-20:]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), DashboardHandler)
    print(f"[System] Live UI Dashboard web server listening on port {port}", flush=True)
    server.serve_forever()

# ─── CORE STRATEGY ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Mode: {MODE}", flush=True)
    GLOBAL_STATE.trades_log.append({
        "ts": ts, "slug": mdm.slug, "action": action, "side": side, "price": price
    })

def evaluate_market(mdm: MarketData, now: float):
    if mdm.state == MarketState.CLOSED:
        return
        
    yb = GLOBAL_STATE.books.get(mdm.yes_token)
    nb = GLOBAL_STATE.books.get(m.no_token)
    
    if not yb or not nb:
        return
    
    ttr = mdm.end_ts - now
    if ttr <= 0:
        mdm.state = MarketState.CLOSED
        return
        
    t2 = T_SECOND_LIVE if ttr <= 300 else T_SECOND_PRE
    
    if mdm.state == MarketState.WATCH:
        if 0 < yb.ask <= T_FIRST:
            mdm.leg1_price = yb.ask
            mdm.state = MarketState.WAITING_NO
            execute_trade(mdm, "YES", yb.ask, "LEG_1_ENTRY")
        elif 0 < nb.ask <= T_FIRST:
            mdm.leg1_price = nb.ask
            mdm.state = MarketState.WAITING_YES
            execute_trade(mdm, "NO", nb.ask, "LEG_1_ENTRY")
            
    elif mdm.state == MarketState.WAITING_NO:
        if 0 < nb.ask <= t2:
            mdm.leg2_price = nb.ask
            mdm.state = MarketState.BOTH
            execute_trade(mdm, "NO", nb.ask, "LEG_2_ENTRY")
            
    elif mdm.state == MarketState.WAITING_YES:
        if 0 < yb.ask <= t2:
            mdm.leg2_price = yb.ask
            mdm.state = MarketState.BOTH
            execute_trade(mdm, "YES", yb.ask, "LEG_2_ENTRY")
            
    elif mdm.state == MarketState.BOTH:
        if ttr <= SELL_LOSER_FLOOR_S:
            if yb.bid >= SELL_LOSER_THRESH:
                mdm.state = MarketState.CLOSED
                execute_trade(mdm, "NO", nb.bid, "SELL_LOSER")
            elif nb.bid >= SELL_LOSER_THRESH:
                mdm.state = MarketState.CLOSED
                execute_trade(mdm, "YES", yb.bid, "SELL_LOSER")

def tick_loop():
    while GLOBAL_STATE.running:
        now = time.time()
        for m in list(GLOBAL_STATE.markets.values()):
            try:
                evaluate_market(m, now)
            except Exception:
                pass
        time.sleep(0.05)

# ─── DATA THREADS ───
def discovery_thread():
    while GLOBAL_STATE.running:
        now = time.time()
        current_b = int((now // 300) * 300)
        lookahead_count = LOOKAHEAD_MINUTES // 5
        boundaries = [current_b + (i * 300) for i in range(1, lookahead_count + 1)]

        new_markets = False
        for ts in boundaries:
            slug = f"btc-updown-5m-{ts}"
            try:
                res = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5)
                if res.status_code == 200 and res.json():
                    market_info = res.json()[0].get("markets", [])[0]
                    cid = market_info["conditionId"]
                    if cid not in GLOBAL_STATE.markets:
                        tokens = json.loads(market_info["clobTokenIds"])
                        outcomes = json.loads(market_info["outcomes"])
                        y_idx = 0 if outcomes[0].lower() in ["yes", "up"] else 1
                        n_idx = 1 if y_idx == 0 else 0
                        end_ts = datetime.fromisoformat(market_info["endDate"].replace("Z", "+00:00")).timestamp()
                        
                        GLOBAL_STATE.markets[cid] = MarketData(cid, slug, tokens[y_idx], tokens[n_idx], end_ts)
                        print(f"[Discovery] Tracking new market: {slug} (TTR: {int(end_ts - now)}s)", flush=True)
                        new_markets = True
            except Exception:
                pass
        
        if new_markets and GLOBAL_STATE.ws_handle:
            GLOBAL_STATE.ws_handle.close()
            
        time.sleep(30)

def polymarket_ws_thread():
    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            for event in (data if isinstance(data, list) else [data]):
                if not isinstance(event, dict): continue
                asset_id = event.get("asset_id") or event.get("market")
                if not asset_id: continue

                if event.get("event_type") == "book":
                    book = GLOBAL_STATE.books.setdefault(asset_id, OrderBook())
                    bids, asks = event.get("bids", []), event.get("asks", [])
                    book.bid = max((float(b["price"]) for b in bids), default=0.0)
                    book.ask = min((float(a["price"]) for a in asks), default=0.0)

                elif event.get("event_type") == "price_change":
                    book = GLOBAL_STATE.books.get(asset_id)
                    if not book: continue
                    for ch in event.get("changes", []):
                        side, price = ch.get("side", ""), float(ch.get("price", 0))
                        if side == "BUY" and price > book.bid: book.bid = price
                        elif side == "SELL" and (book.ask == 0 or price < book.ask): book.ask = price
        except Exception:
            pass

    def on_open(ws):
        GLOBAL_STATE.ws_connected = True
        tokens = []
        for m in GLOBAL_STATE.markets.values():
            if m.state != MarketState.CLOSED:
                tokens.extend([m.yes_token, m.no_token])
        if tokens:
         ws.send(json.dumps({"type": "Market", "assets_ids": tokens}))
