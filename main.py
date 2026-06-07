"""
main.py — Opportunistic BSS Bot (v5.8.2 Command Center)
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
import csv
from typing import Dict, List
from datetime import datetime, timezone

# ─── CONFIGURATION ───
MODE = os.getenv("MODE", "dry").lower()
T_FIRST = float(os.getenv("BS_BSS_T_FIRST", "0.49"))
T_SECOND_PRE = float(os.getenv("BS_BSS_T_SECOND_PRE", "0.50"))
T_SECOND_LIVE = float(os.getenv("BS_BSS_T_SECOND_LIVE", "0.51"))
SELL_LOSER_THRESH = float(os.getenv("BS_SELL_LOSER_THRESHOLD", "0.93"))
SELL_LOSER_FLOOR_S = float(os.getenv("BS_SELL_LOSER_TTR_FLOOR_S", "75"))
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "60"))
PORT = int(os.getenv("PORT", "8080"))

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
        self.history: List[float] = [] # For candle chart

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
        self.total_pnl = 0.0
        self.total_trades = 0
        self.sold_losers = 0

GLOBAL_STATE = BotState()

# ─── CSV LOGGING SYSTEM ───
def init_csv():
    if not os.path.exists("trades_full.csv"):
        with open("trades_full.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Slug", "Action", "Side", "Price", "Realized_PnL", "Verify_Link"])
    if not os.path.exists("snapshot_live.csv"):
        with open("snapshot_live.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"])

def log_trade_csv(ts, slug, action, side, price, pnl):
    link = f"https://polymarket.com/event/{slug}"
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{pnl:.3f}", link])
    except Exception as e:
        print(f"CSV Error: {e}")

# ─── DASHBOARD HTML (v5.8.2) ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Command Center v5.8.2</title>
<style>
    body { background: #0a0a0a; color: #e0e0e0; font-family: ui-monospace, Menlo, Consolas, monospace; padding: 20px; font-size: 13px; margin: 0; }
    .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 20px; }
    .brand { font-size: 18px; font-weight: 600; color: #fff; }
    .vitals { display: flex; gap: 20px; }
    .vital-box { background: #141414; border: 1px solid #262626; padding: 10px 15px; border-radius: 6px; text-align: center; min-width: 100px; }
    .vital-label { color: #7a7a7a; font-size: 10px; text-transform: uppercase; margin-bottom: 4px; }
    .vital-value { font-size: 18px; font-weight: bold; color: #fff; }
    .vital-value.green { color: #5cbd5c; }
    
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 15px; margin-bottom: 30px; }
    .card { background: #141414; border: 1px solid #262626; border-radius: 6px; padding: 15px; position: relative; }
    .card-header { display: flex; justify-content: space-between; border-bottom: 1px solid #1f1f1f; padding-bottom: 8px; margin-bottom: 8px; }
    .card-title { font-weight: bold; color: #fff; }
    .card-ttr { color: #e0b340; font-size: 12px; }
    .svg-container { height: 40px; margin: 10px 0; background: #0d0d0d; border-radius: 4px; border: 1px solid #1f1f1f; }
    
    .table-container { background: #141414; border: 1px solid #262626; border-radius: 6px; padding: 15px; margin-bottom: 30px; }
    .sec-title { font-weight: bold; font-size: 14px; color: #fff; margin-bottom: 15px; text-transform: uppercase; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { color: #7a7a7a; font-size: 11px; text-transform: uppercase; padding: 8px; border-bottom: 1px solid #333; }
    td { padding: 8px; border-bottom: 1px solid #1f1f1f; }
    .btn-verify { background: rgba(88,166,255,0.1); border: 1px solid rgba(88,166,255,0.4); color: #58a6ff; padding: 3px 8px; border-radius: 4px; text-decoration: none; font-size: 10px; font-weight: bold; }
    .btn-verify:hover { background: rgba(88,166,255,0.2); }
    
    .vault { display: flex; gap: 10px; background: #141414; padding: 15px; border-radius: 6px; border: 1px solid #262626; align-items: center;}
    .btn-action { background: #262626; color: #fff; border: 1px solid #333; padding: 8px 15px; border-radius: 4px; cursor: pointer; font-size: 12px; font-family: inherit; }
    .btn-action:hover { background: #333; }
    .btn-danger { background: rgba(248,81,73,0.1); color: #d96666; border: 1px solid rgba(217,102,102,0.4); margin-left: auto; }
    .btn-danger:hover { background: rgba(248,81,73,0.2); }
</style>
</head>
<body>

<div class="top-bar">
    <div class="brand">BSS Bot v5.8.2 <span id="ws-status" style="font-size: 10px; margin-left: 10px; color: #7a7a7a;">WS: Checking...</span></div>
    <div class="vitals">
        <div class="vital-box"><div class="vital-label">Total P&L</div><div class="vital-value green" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Total Trades</div><div class="vital-value" id="v-trades">0</div></div>
        <div class="vital-box"><div class="vital-label">Sold Losers</div><div class="vital-value" id="v-losers">0</div></div>
        <div class="vital-box"><div class="vital-label">Active Slots</div><div class="vital-value" id="v-active">0</div></div>
    </div>
</div>

<div class="grid" id="active-cards"><div style="color: #7a7a7a;">Scanning order books...</div></div>

<div class="table-container">
    <div class="sec-title">Execution History & Assessment</div>
    <table>
        <thead><tr><th>Time</th><th>Market</th><th>Action</th><th>Side</th><th>Price</th><th>P&L</th><th>Audit</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="7" style="color: #7a7a7a;">No trades executed yet.</td></tr></tbody>
    </table>
</div>

<div class="vault">
    <div class="sec-title" style="margin: 0; margin-right: 20px;">Data Vault</div>
    <button class="btn-action" onclick="window.location.href='/api/dl_trades'">↓ Download Trades (.csv)</button>
    <button class="btn-action" onclick="window.location.href='/api/dl_snaps'">↓ Download State Dumps (.csv)</button>
    <button class="btn-action btn-danger" onclick="deleteFiles()">⚠ Delete Old Files</button>
</div>

<script>
function renderSparkline(history) {
    if(!history || history.length < 2) return '';
    const min = Math.min(...history), max = Math.max(...history);
    const range = (max - min) || 0.01;
    const pts = history.map((val, i) => {
        const x = (i / (history.length - 1)) * 100;
        const y = 100 - (((val - min) / range) * 100);
        return `${x},${y}`;
    }).join(' ');
    return `<svg width="100%" height="100%" viewBox="0 -10 100 120" preserveAspectRatio="none">
        <polyline fill="none" stroke="#58a6ff" stroke-width="2" points="${pts}" />
    </svg>`;
}

async function deleteFiles() {
    if(confirm("Delete all CSV logs? This cannot be undone.")) {
        await fetch('/api/delete_logs', {method: 'POST'});
        alert("Logs deleted and reset.");
    }
}

setInterval(async () => {
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        
        document.getElementById('ws-status').innerHTML = s.ws_connected ? '<span style="color:#5cbd5c">WS: LIVE</span>' : '<span style="color:#d96666">WS: DROP</span>';
        document.getElementById('v-pnl').textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        document.getElementById('v-trades').textContent = s.trades;
        document.getElementById('v-losers').textContent = s.losers;
        
        let activeCount = 0;
        let html = '';
        s.markets.forEach(m => {
            if (m.state === 'WATCH' || m.state === 'CLOSED') return;
            activeCount++;
            const delta = m.state === 'BOTH' ? 0.0 : (((m.leg1_side === 'YES' ? m.yes_ask : m.no_ask) - m.leg1_price) / m.leg1_price * 100);
            const deltaStr = delta > 0 ? `+${delta.toFixed(2)}%` : `${delta.toFixed(2)}%`;
            const deltaCol = delta > 0 ? '#d96666' : '#5cbd5c'; // Red if price went up (bad for entry)
            
            html += `<div class="card">
                <div class="card-header">
                    <span class="card-title">${m.slug.replace('btc-updown-5m-','')}</span>
                    <span class="card-ttr">${m.ttr_s}s</span>
                </div>
                <div style="display:flex; justify-content: space-between; font-size: 11px; color: #7a7a7a;">
                    <span>State: <b style="color:#fff">${m.state}</b></span>
                    <span>Delta: <b style="color:${deltaCol}">${deltaStr}</b></span>
                </div>
                <div class="svg-container">${renderSparkline(m.history)}</div>
                <div style="font-size: 12px;">Leg 1 Entry: $${m.leg1_price.toFixed(3)}</div>
            </div>`;
        });
        document.getElementById('v-active').textContent = activeCount;
        if(html) document.getElementById('active-cards').innerHTML = html;

        let logHtml = '';
        [...s.trades].reverse().forEach(t => {
            const pnlStr = t.pnl !== 0.0 ? (t.pnl > 0 ? `+${t.pnl.toFixed(2)}` : t.pnl.toFixed(2)) : '--';
            logHtml += `<tr>
                <td style="color:#7a7a7a">${t.ts}</td>
                <td>${t.slug}</td>
                <td>${t.action}</td>
                <td style="color:${t.side==='YES'?'#5cbd5c':'#d96666'}">${t.side}</td>
                <td>$${t.price.toFixed(3)}</td>
                <td style="color:${t.pnl>0?'#5cbd5c':(t.pnl<0?'#d96666':'#fff')}">${pnlStr}</td>
                <td><a href="${t.link}" target="_blank" class="btn-verify">VERIFY ↗</a></td>
            </tr>`;
        });
        if(logHtml) document.getElementById('log-body').innerHTML = logHtml;

    } catch(e) {}
}, 1000);
</script>
</body>
</html>
"""

# ─── API & SERVER ───
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
                yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                m_data.append({
                    "slug": m.slug, "state": m.state, "ttr_s": max(0, int(m.end_ts - now)),
                    "leg1_price": m.leg1_price, "leg1_side": "YES" if m.state == MarketState.WAITING_NO else ("NO" if m.state == MarketState.WAITING_YES else ""),
                    "yes_ask": yb.ask if yb else 0.0, "no_ask": nb.ask if nb else 0.0,
                    "history": m.history[-20:]
                })
            
            payload = {
                "ws_connected": GLOBAL_STATE.ws_connected, "pnl": GLOBAL_STATE.total_pnl,
                "trades": GLOBAL_STATE.total_trades, "losers": GLOBAL_STATE.sold_losers,
                "markets": m_data, "trades": GLOBAL_STATE.trades_log[-15:]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        elif self.path == "/api/dl_trades":
            self.send_response(200)
            self.send_header('Content-Disposition', 'attachment; filename="trades_full.csv"')
            self.send_header('Content-Type', 'text/csv')
            self.end_headers()
            with open("trades_full.csv", "rb") as f: self.wfile.write(f.read())
        elif self.path == "/api/dl_snaps":
            self.send_response(200)
            self.send_header('Content-Disposition', 'attachment; filename="snapshot_live.csv"')
            self.send_header('Content-Type', 'text/csv')
            self.end_headers()
            with open("snapshot_live.csv", "rb") as f: self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/delete_logs":
            if os.path.exists("trades_full.csv"): os.remove("trades_full.csv")
            if os.path.exists("snapshot_live.csv"): os.remove("snapshot_live.csv")
            init_csv()
            self.send_response(200)
            self.end_headers()

    def log_message(self, format, *args): pass

def run_server():
    server = ThreadingHTTPServer(("", PORT), DashboardHandler)
    print(f"[System] Command Center listening on port {PORT}", flush=True)
    server.serve_forever()

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

# ─── CORE STRATEGY ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    link = f"https://polymarket.com/event/{mdm.slug}"
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Mode: {MODE}", flush=True)
    
    GLOBAL_STATE.total_trades += 1
    if "SELL" in action:
        GLOBAL_STATE.total_pnl += pnl
    if action == "SELL_LOSER":
        GLOBAL_STATE.sold_losers += 1

    GLOBAL_STATE.trades_log.append({"ts": ts, "slug": mdm.slug, "action": action, "side": side, "price": price, "pnl": pnl, "link": link})
    log_trade_csv(ts, mdm.slug, action, side, price, pnl)

def evaluate_market(mdm: MarketData, now: float):
    if mdm.state == MarketState.CLOSED: return
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb: return
    
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
                execute_trade(mdm, "NO", nb.bid, "SELL_LOSER", -0.05)
            elif nb.bid >= SELL_LOSER_THRESH:
                mdm.state = MarketState.CLOSED
                execute_trade(mdm, "YES", yb.bid, "SELL_LOSER", -0.05)

def tick_loop():
    while GLOBAL_STATE.running:
        now = time.time()
        for m in list(GLOBAL_STATE.markets.values()):
            try: evaluate_market(m, now)
            except Exception: pass
        time.sleep(0.05)

def snapshot_loop():
    while GLOBAL_STATE.running:
        now = time.time()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("snapshot_live.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in GLOBAL_STATE.markets.values():
                    if m.state != MarketState.CLOSED:
                        yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                        ya, ybd = yb.ask if yb else 0, yb.bid if yb else 0
                        na, nbd = nb.ask if nb else 0, nb.bid if nb else 0
                        writer.writerow([ts, m.slug, m.state, f"{ya:.3f}", f"{ybd:.3f}", f"{na:.3f}", f"{nbd:.3f}"])
                        # Append to SVG history
                        active_price = ya if m.state == MarketState.WAITING_NO else na
                        m.history.append(active_price)
                        if len(m.history) > 60: m.history.pop(0)
        except Exception: pass
        time.sleep(30) # Dump every 30s

# ─── DATA THREADS ───
def discovery_thread():
    while GLOBAL_STATE.running:
        now = time.time()
        current_b = int((now // 300) * 300)
        boundaries = [current_b + (i * 300) for i in range(1, (LOOKAHEAD_MINUTES // 5) + 1)]
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
                        end_ts = datetime.fromisoformat(market_info["endDate"].replace("Z", "+00:00")).timestamp()
                        GLOBAL_STATE.markets[cid] = MarketData(cid, slug, tokens[y_idx], tokens[1-y_idx], end_ts)
                        print(f"[Discovery] Tracking: {slug}", flush=True)
                        new_markets = True
            except Exception: pass
        if new_markets and GLOBAL_STATE.ws_handle: GLOBAL_STATE.ws_handle.close() # Force WS reconnect
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
                    book.bid = max((float(b["price"]) for b in event.get("bids", [])), default=0.0)
                    book.ask = min((float(a["price"]) for a in event.get("asks", [])), default=0.0)
                elif event.get("event_type") == "price_change":
                    book = GLOBAL_STATE.books.get(asset_id)
                    if not book: continue
                    for ch in event.get("changes", []):
                        side, price = ch.get("side", ""), float(ch.get("price", 0))
                        if side == "BUY" and price > book.bid: book.bid = price
                        elif side == "SELL" and (book.ask == 0 or price < book.ask): book.ask = price
        except Exception: pass

    def on_open(ws):
        GLOBAL_STATE.ws_connected = True
        tokens = [t for m in GLOBAL_STATE.markets.values() if m.state != MarketState.CLOSED for t in (m.yes_token, m.no_token)]
        if tokens: ws.send(json.dumps({"type": "Market", "assets_ids": tokens}))

    while GLOBAL_STATE.running:
        try:
            ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market", on_message=on_message, on_open=on_open)
            GLOBAL_STATE.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception: pass
        GLOBAL_STATE.ws_handle, GLOBAL_STATE.ws_connected = None, False
        time.sleep(2) # Auto-reconnect

if __name__ == "__main__":
    init_csv()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()

    while GLOBAL_STATE.running: time.sleep(1)