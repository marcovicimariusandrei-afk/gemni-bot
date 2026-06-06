"""
main.py — Opportunistic BSS Bot (v5.8.1 Legacy Dashboard UI)
"""
import os, sys, time, json, threading, signal, http.server, socketserver
import requests, websocket
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

# ─── DATA MODELS ───
class MarketState:
    WATCH = "WATCH"; WAITING_NO = "WAITING_NO"; WAITING_YES = "WAITING_YES"; BOTH = "BOTH"; CLOSED = "CLOSED"

class MarketData:
    def __init__(self, cid, slug, yes_id, no_id, end_ts):
        self.condition_id, self.slug, self.yes_token, self.no_token, self.end_ts = cid, slug, yes_id, no_id, end_ts
        self.state, self.leg1, self.leg2 = MarketState.WATCH, 0.0, 0.0

class BotState:
    def __init__(self):
        self.running = True
        self.markets = {}
        self.books = {}
        self.ws_connected = False
        self.ws_handle = None
        self.trades = []

GLOBAL_STATE = BotState()

# ─── LEGACY DASHBOARD (Full Carbon Copy) ───
# This template renders the professional dark-mode grid you are used to.
DASHBOARD_HTML = r"""<!doctype html><html><head><style>
body{background:#0a0a0a;color:#ccc;font-family:monospace;padding:20px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:15px;}
.card{background:#111;border:1px solid #333;padding:15px;border-radius:4px;}
.status-bar{margin-bottom:20px;border-bottom:1px solid #333;padding-bottom:10px;}
table{width:100%;border-collapse:collapse;margin-top:10px;color:#fff;}
th{text-align:left;color:#777;font-size:12px;}
td{padding:8px 0;border-bottom:1px solid #222;}
</style></head><body>
<div class="status-bar"><h2 id="header">BSS Bot v5.8.1 Dashboard</h2><div id="meta"></div></div>
<div id="content" class="grid"></div>
<script>
setInterval(async () => {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('meta').innerHTML = `Mode: ${s.mode} | WS: ${s.ws_connected?'✅':'❌'}`;
    let html = '';
    s.markets.forEach(m => {
        html += `<div class="card">
            <div><b>${m.slug}</b></div>
            <div>State: ${m.state}</div>
            <table><tr><th>Yes</th><th>No</th></tr>
            <tr><td>Ask: ${m.yes_ask}</td><td>Ask: ${m.no_ask}</td></tr>
            </table>
        </div>`;
    });
    document.getElementById('content').innerHTML = html;
}, 1000);
</script></body></html>"""

# ─── API & SERVER ───
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(DASHBOARD_HTML.encode())
        elif self.path == "/api/status":
            m_data = [{"slug": m.slug, "state": m.state, "yes_ask": GLOBAL_STATE.books.get(m.yes_token, type('O',(),{'ask':0})).ask, "no_ask": GLOBAL_STATE.books.get(m.no_token, type('O',(),{'ask':0})).ask} for m in GLOBAL_STATE.markets.values()]
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps({"mode":MODE, "ws_connected":GLOBAL_STATE.ws_connected, "markets":m_data}).encode())
    def log_message(self, *args): pass

def run_server():
    socketserver.TCPServer(("", int(os.getenv("PORT", "8080"))), Handler).serve_forever()

# ─── CORE STRATEGY ───
def evaluate_market(mdm, now):
    if mdm.state == MarketState.CLOSED: return
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb: return
    ttr = mdm.end_ts - now
    if ttr <= 0: mdm.state = MarketState.CLOSED; return
    t2 = T_SECOND_LIVE if ttr <= 300 else T_SECOND_PRE
    
    if mdm.state == MarketState.WATCH:
        if 0 < yb.ask <= T_FIRST: mdm.leg1=yb.ask; mdm.state=MarketState.WAITING_NO
        elif 0 < nb.ask <= T_FIRST: mdm.leg1=nb.ask; mdm.state=MarketState.WAITING_YES
    elif mdm.state == MarketState.WAITING_NO and 0 < nb.ask <= t2: mdm.leg2=nb.ask; mdm.state=MarketState.BOTH
    elif mdm.state == MarketState.WAITING_YES and 0 < yb.ask <= t2: mdm.leg2=yb.ask; mdm.state=MarketState.BOTH
    elif mdm.state == MarketState.BOTH and ttr <= SELL_LOSER_FLOOR_S:
        if yb.bid >= SELL_LOSER_THRESH or nb.bid >= SELL_LOSER_THRESH: mdm.state=MarketState.CLOSED

def tick_loop():
    while GLOBAL_STATE.running:
        for m in list(GLOBAL_STATE.markets.values()): evaluate_market(m, time.time())
        time.sleep(0.1)

# ─── WEBSOCKET & DISCOVERY (Same as before) ───
# [Paste here your discovery_thread and polymarket_ws_thread functions]

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    # threading.Thread(target=discovery_thread, daemon=True).start()
    # threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    while True: time.sleep(1)