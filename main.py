"""
main.py — Opportunistic BSS Bot (Classic v5.8 Dashboard Look)
"""
import os, sys, time, json, threading, signal, http.server, socketserver
import requests, websocket
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
BOOT_TS = time.time()

# ─── STATE MODELS ───
class MarketState:
    WATCH = "WATCH"; WAITING_NO = "WAITING_NO"; WAITING_YES = "WAITING_YES"; BOTH = "BOTH"; CLOSED = "CLOSED"

class MarketData:
    def __init__(self, condition_id, slug, yes_id, no_id, end_ts):
        self.condition_id, self.slug, self.yes_token, self.no_token, self.end_ts = condition_id, slug, yes_id, no_id, end_ts
        self.state, self.leg1_price, self.leg2_price = MarketState.WATCH, 0.0, 0.0

class BotState:
    def __init__(self):
        self.running = True
        self.markets: Dict[str, MarketData] = {}
        self.books = {}
        self.ws_connected = False
        self.ws_handle = None
        self.trades_log = []

GLOBAL_STATE = BotState()

# ─── CLASSIC DASHBOARD HTML ───
DASHBOARD_HTML = r"""<!doctype html><html><head>
<style>
body{background:#0a0a0a;color:#e0e0e0;font-family:monospace;padding:20px;}
.header{border-bottom:1px solid #333;padding-bottom:10px;margin-bottom:20px;}
.card{background:#141414;border:1px solid #333;padding:15px;margin-bottom:10px;border-left:4px solid #7aa5d2;}
</style></head><body>
<div class="header"><h1>BSS Dashboard v5.8</h1></div>
<div id="content">Loading dashboard...</div>
<script>
setInterval(async () => {
    const r = await fetch('/api/status');
    const s = await r.json();
    let html = `<div>Mode: ${s.mode} | WS: ${s.ws_connected ? 'OK' : 'ERR'}</div>`;
    s.markets.forEach(m => {
        html += `<div class="card">
            <div>${m.slug} | State: <b>${m.state}</b></div>
            <div>YES: ${m.yes_ask.toFixed(3)} | NO: ${m.no_ask.toFixed(3)}</div>
        </div>`;
    });
    document.getElementById('content').innerHTML = html;
}, 1000);
</script></body></html>
"""

# ─── WEB SERVER & API ───
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200); self.end_headers(); self.wfile.write(DASHBOARD_HTML.encode())
        elif self.path == "/api/status":
            now = time.time()
            m_data = [{"slug": m.slug, "state": m.state, "yes_ask": GLOBAL_STATE.books.get(m.yes_token, type('O',(),{'ask':0})).ask, "no_ask": GLOBAL_STATE.books.get(m.no_token, type('O',(),{'ask':0})).ask} for m in GLOBAL_STATE.markets.values()]
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps({"mode":MODE, "ws_connected":GLOBAL_STATE.ws_connected, "markets":m_data}).encode())
    def log_message(self, format, *args): pass

def run_server():
    httpd = socketserver.TCPServer(("", int(os.getenv("PORT", "8080"))), Handler)
    httpd.serve_forever()

# ─── CORE LOGIC (STRATEGY) ───
def evaluate_market(mdm, now):
    if mdm.state == MarketState.CLOSED: return
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb: return
    ttr = mdm.end_ts - now
    if ttr <= 0: mdm.state = MarketState.CLOSED; return
    t2 = T_SECOND_LIVE if ttr <= 300 else T_SECOND_PRE
    
    if mdm.state == MarketState.WATCH:
        if 0 < yb.ask <= T_FIRST: mdm.leg1_price=yb.ask; mdm.state=MarketState.WAITING_NO; print(f"LEG1_YES: {mdm.slug}")
        elif 0 < nb.ask <= T_FIRST: mdm.leg1_price=nb.ask; mdm.state=MarketState.WAITING_YES; print(f"LEG1_NO: {mdm.slug}")
    elif mdm.state == MarketState.WAITING_NO and 0 < nb.ask <= t2: mdm.leg2_price=nb.ask; mdm.state=MarketState.BOTH
    elif mdm.state == MarketState.WAITING_YES and 0 < yb.ask <= t2: mdm.leg2_price=yb.ask; mdm.state=MarketState.BOTH
    elif mdm.state == MarketState.BOTH and ttr <= SELL_LOSER_FLOOR_S:
        if yb.bid >= SELL_LOSER_THRESH or nb.bid >= SELL_LOSER_THRESH: mdm.state=MarketState.CLOSED

def tick_loop():
    while GLOBAL_STATE.running:
        now = time.time()
        for m in list(GLOBAL_STATE.markets.values()): evaluate_market(m, now)
        time.sleep(0.1)

# ─── THREADS (DISCOVERY, WS) ───
# [Note: Keep the discovery_thread and polymarket_ws_thread logic from previous steps]

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    # threading.Thread(target=discovery_thread, daemon=True).start()
    # threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    while True: time.sleep(1)