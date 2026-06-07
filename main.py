"""
main.py — Opportunistic BSS Bot (v5.8.2 Corporate Analytical UI)
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
        self.history_yes: List[float] = []
        self.history_no: List[float] = []

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
            csv.writer(f).writerow(["Timestamp", "Slug", "Action", "Side", "Price", "Realized_PnL", "Verify_Link"])
    if not os.path.exists("snapshot_live.csv"):
        with open("snapshot_live.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"])

def log_trade_csv(ts, slug, action, side, price, pnl):
    link = f"https://polymarket.com/event/{slug}"
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{pnl:.3f}", link])
    except Exception: pass

# ─── DASHBOARD HTML (v5.8.2 Corporate UI) ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Analysis Dashboard</title>
<style>
    :root {
        --bg-main: #e6edf2;
        --bg-panel: #ffffff;
        --header-bg: #aecad6;
        --text-navy: #002060;
        --text-light: #597387;
        --border-color: #c4d7e0;
        --font-serif: Georgia, "Times New Roman", serif;
        --font-sans: Calibri, "Segoe UI", Arial, sans-serif;
    }
    body { background: var(--bg-main); color: var(--text-navy); font-family: var(--font-sans); padding: 20px; font-size: 14px; margin: 0; }
    
    .header-panel { background: var(--header-bg); border: 1px solid var(--border-color); display: flex; flex-direction: column; text-align: center; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .brand-title { font-family: var(--font-serif); font-size: 24px; font-weight: bold; color: var(--text-navy); padding: 10px 0; border-bottom: 1px solid var(--border-color); background: rgba(255,255,255,0.3); }
    
    .vitals-row { display: flex; background: var(--header-bg); }
    .vital-box { flex: 1; padding: 10px; border-right: 1px solid var(--border-color); text-align: center; }
    .vital-box:last-child { border-right: none; }
    .vital-label { font-family: var(--font-serif); font-size: 13px; font-weight: bold; margin-bottom: 5px; }
    .vital-value { background: var(--bg-panel); color: var(--text-navy); font-size: 22px; font-weight: bold; padding: 5px; border-radius: 2px; }
    .vital-value.green { color: #006600; }
    
    .sec-title { background: var(--header-bg); border: 1px solid var(--border-color); font-family: var(--font-serif); font-size: 16px; font-weight: bold; text-align: center; padding: 8px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    
    .grid { display: grid; grid-template-columns: 1fr; gap: 15px; margin-bottom: 30px; }
    .card { background: var(--bg-panel); border: 1px solid var(--border-color); box-shadow: 0 2px 4px rgba(0,0,0,0.05); display: flex; flex-direction: column; }
    .card-header { background: #d9e6eb; padding: 8px 15px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; font-weight: bold; }
    
    .leg-container { display: flex; width: 100%; }
    .leg-col { flex: 1; padding: 15px; border-right: 1px solid var(--border-color); }
    .leg-col:last-child { border-right: none; }
    .leg-title { font-family: var(--font-serif); font-size: 14px; font-weight: bold; text-align: center; margin-bottom: 10px; color: var(--text-navy); text-decoration: underline; }
    
    .data-row { display: flex; justify-content: space-between; margin-bottom: 5px; font-size: 13px; }
    .val-green { color: #006600; font-weight: bold; }
    .val-red { color: #cc0000; font-weight: bold; }
    
    .svg-container { height: 40px; margin-top: 10px; background: #f5f8fa; border: 1px solid #e0e8f0; }
    
    .table-container { background: var(--bg-panel); border: 1px solid var(--border-color); margin-bottom: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: #d9e6eb; font-family: var(--font-serif); font-size: 13px; padding: 10px; border-bottom: 1px solid var(--border-color); border-right: 1px solid var(--border-color); text-align: center; }
    td { padding: 8px 10px; border-bottom: 1px solid #e0e8f0; border-right: 1px solid #e0e8f0; text-align: center; font-size: 13px; }
    
    .queue-container { background: var(--bg-panel); border: 1px solid var(--border-color); padding: 15px; font-family: monospace; font-size: 12px; color: var(--text-light); line-height: 1.6; }
    
    .vault { display: flex; gap: 10px; background: var(--header-bg); padding: 10px; border: 1px solid var(--border-color); align-items: center; justify-content: center; margin-bottom: 20px;}
    .btn-action { background: #ffffff; color: var(--text-navy); border: 1px solid var(--border-color); padding: 5px 15px; cursor: pointer; font-family: var(--font-sans); font-weight: bold; box-shadow: 1px 1px 2px rgba(0,0,0,0.1); }
    .btn-action:hover { background: #f0f0f0; }
    .btn-verify { color: #0055aa; text-decoration: underline; font-weight: bold; font-size: 12px; }
</style>
</head>
<body>

<div class="header-panel">
    <div class="brand-title">BSS Bot Analysis Dashboard v5.8.2 <span id="ws-status" style="font-size: 12px; font-family: var(--font-sans); margin-left: 10px;">[WS: Checking...]</span></div>
    <div class="vitals-row">
        <div class="vital-box"><div class="vital-label">Total P&L</div><div class="vital-value green" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Total Trades Executed</div><div class="vital-value" id="v-trades">0</div></div>
        <div class="vital-box"><div class="vital-label">Sold Losers</div><div class="vital-value" id="v-losers">0</div></div>
        <div class="vital-box"><div class="vital-label">Active Slots</div><div class="vital-value" id="v-active">0</div></div>
    </div>
</div>

<div class="sec-title">Active Market Dual-Leg Monitoring</div>
<div class="grid" id="active-cards"><div style="text-align:center; padding:20px; color:var(--text-light);">Awaiting Entry Criteria...</div></div>

<div class="sec-title">Consolidated Trade Lifecycle History</div>
<div class="table-container">
    <table>
        <thead><tr><th>Time</th><th>Market Slug</th><th>Action</th><th>Side</th><th>Price</th><th>Net P&L</th><th>Audit Link</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="7" style="color: var(--text-light);">No historical data available.</td></tr></tbody>
    </table>
</div>

<div class="vault">
    <span style="font-family: var(--font-serif); font-weight: bold; margin-right: 15px;">Data Vault & Utilities:</span>
    <button class="btn-action" onclick="window.location.href='/api/dl_trades'">Download Trades (.csv)</button>
    <button class="btn-action" onclick="window.location.href='/api/dl_snaps'">Download Snapshots (.csv)</button>
    <button class="btn-action" style="color: #cc0000; margin-left: 30px;" onclick="deleteFiles()">⚠ Delete Old Files</button>
</div>

<div class="sec-title">Observation Queue (Scouting)</div>
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
    return `<svg width="100%" height="100%" viewBox="0 -10 100 120" preserveAspectRatio="none">
        <polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}" />
    </svg>`;
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
        
        document.getElementById('ws-status').textContent = s.ws_connected ? "[WS: CONNECTED]" : "[WS: DROPPED]";
        document.getElementById('ws-status').style.color = s.ws_connected ? "#006600" : "#cc0000";
        document.getElementById('v-pnl').textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        document.getElementById('v-trades').textContent = s.trades;
        document.getElementById('v-losers').textContent = s.losers;
        
        let activeCount = 0;
        let htmlCards = '';
        let htmlQueue = '';
        
        s.markets.forEach(m => {
            if (m.state === 'WATCH') {
                htmlQueue += `[TTR: ${m.ttr_s}s] | ${m.slug} | YES Ask: $${m.yes_ask.toFixed(3)} | NO Ask: $${m.no_ask.toFixed(3)} | Status: Scouting<br>`;
                return;
            }
            if (m.state === 'CLOSED') return;
            
            activeCount++;
            
            // Calc Deltas
            let dYes = 0, dNo = 0;
            if(m.state === 'BOTH' || m.state === 'WAITING_NO') {
                dYes = ((m.yes_ask - m.leg1_price) / m.leg1_price) * 100;
            }
            if(m.state === 'BOTH' || m.state === 'WAITING_YES') {
                let eNo = m.state==='BOTH'? m.leg2_price : m.leg1_price;
                dNo = ((m.no_ask - eNo) / eNo) * 100;
            }
            
            let cYes = dYes >= 0 ? 'val-green' : 'val-red';
            let cNo = dNo >= 0 ? 'val-green' : 'val-red';

            htmlCards += `<div class="card">
                <div class="card-header">
                    <span>${m.slug}</span>
                    <span>TTR: ${m.ttr_s}s</span>
                </div>
                <div class="leg-container">
                    <div class="leg-col">
                        <div class="leg-title">YES LEG MONITOR</div>
                        <div class="data-row"><span>Entry Price:</span> <b>$${m.state==='WAITING_NO'||m.state==='BOTH'?m.leg1_price.toFixed(3):'--'}</b></div>
                        <div class="data-row"><span>Live Ticker:</span> <b>$${m.yes_ask.toFixed(3)}</b></div>
                        <div class="data-row"><span>Current Delta:</span> <span class="${cYes}">${dYes>0?'+':''}${dYes.toFixed(2)}%</span></div>
                        <div class="svg-container">${renderSparkline(m.history_yes, '#002060')}</div>
                    </div>
                    <div class="leg-col">
                        <div class="leg-title">NO LEG MONITOR</div>
                        <div class="data-row"><span>Entry Price:</span> <b>$${m.state==='WAITING_YES'||m.state==='BOTH'?(m.state==='BOTH'?m.leg2_price.toFixed(3):m.leg1_price.toFixed(3)):'--'}</b></div>
                        <div class="data-row"><span>Live Ticker:</span> <b>$${m.no_ask.toFixed(3)}</b></div>
                        <div class="data-row"><span>Current Delta:</span> <span class="${cNo}">${dNo>0?'+':''}${dNo.toFixed(2)}%</span></div>
                        <div class="svg-container">${renderSparkline(m.history_no, '#597387')}</div>
                    </div>
                </div>
            </div>`;
        });
        
        document.getElementById('v-active').textContent = activeCount;
        if(htmlCards) document.getElementById('active-cards').innerHTML = htmlCards;
        else document.getElementById('active-cards').innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-light);">Awaiting Entry Criteria...</div>';
        
        document.getElementById('obs-queue').innerHTML = htmlQueue || 'No upcoming markets in window.';

        let logHtml = '';
        [...s.trades].reverse().forEach(t => {
            const pnlStr = t.pnl !== 0.0 ? (t.pnl > 0 ? `+${t.pnl.toFixed(2)}` : t.pnl.toFixed(2)) : '--';
            logHtml += `<tr>
                <td>${t.ts}</td>
                <td>${t.slug}</td>
                <td>${t.action}</td>
                <td>${t.side}</td>
                <td>$${t.price.toFixed(3)}</td>
                <td class="${t.pnl>0?'val-green':(t.pnl<0?'val-red':'')}">${pnlStr}</td>
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
                    "leg1_price": m.leg1_price, "leg2_price": m.leg2_price,
                    "yes_ask": yb.ask if yb else 0.0, "no_ask": nb.ask if nb else 0.0,
                    "history_yes": m.history_yes[-30:], "history_no": m.history_no[-30:]
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
    server = socketserver.ThreadingTCPServer(("", PORT), DashboardHandler)
    print(f"[System] UI listening on port {PORT}", flush=True)
    server.serve_forever()

# ─── CORE STRATEGY ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    link = f"https://polymarket.com/event/{mdm.slug}"
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f}", flush=True)
    GLOBAL_STATE.total_trades += 1
    if "SELL" in action: GLOBAL_STATE.total_pnl += pnl
    if action == "SELL_LOSER": GLOBAL_STATE.sold_losers += 1
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
                        m.history_yes.append(ya)
                        m.history_no.append(na)
                        if len(m.history_yes) > 30: m.history_yes.pop(0)
                        if len(m.history_no) > 30: m.history_no.pop(0)
        except Exception: pass
        time.sleep(30)

# ─── DATA THREADS ───
def discovery_thread():
    while GLOBAL_STATE.running:
        now = time.time()
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
                        tks = json.loads(m_info["clobTokenIds"])
                        outcomes = json.loads(m_info["outcomes"])
                        y_idx = 0 if outcomes[0].lower() in ["yes", "up"] else 1
                        end_ts = datetime.fromisoformat(m_info["endDate"].replace("Z", "+00:00")).timestamp()
                        GLOBAL_STATE.markets[cid] = MarketData(cid, slug, tks[y_idx], tks[1-y_idx], end_ts)
                        print(f"[Discovery] Tracking: {slug}", flush=True)
                        new_markets = True
            except Exception: pass
        if new_markets and GLOBAL_STATE.ws_handle: GLOBAL_STATE.ws_handle.close()
        time.sleep(30)

def polymarket_ws_thread():
    def on_message(ws, msg):
        try:
            for event in (json.loads(msg) if isinstance(json.loads(msg), list) else [json.loads(msg)]):
                if not isinstance(event, dict): continue
                aid = event.get("asset_id") or event.get("market")
                if not aid: continue
                if event.get("event_type") == "book":
                    book = GLOBAL_STATE.books.setdefault(aid, OrderBook())
                    book.bid = max((float(b["price"]) for b in event.get("bids", [])), default=0.0)
                    book.ask = min((float(a["price"]) for a in event.get("asks", [])), default=0.0)
                elif event.get("event_type") == "price_change":
                    book = GLOBAL_STATE.books.get(aid)
                    if not book: continue
                    for ch in event.get("changes", []):
                        s, p = ch.get("side", ""), float(ch.get("price", 0))
                        if s == "BUY" and p > book.bid: book.bid = p
                        elif s == "SELL" and (book.ask == 0 or p < book.ask): book.ask = p
        except Exception: pass

    def on_open(ws):
        GLOBAL_STATE.ws_connected = True
        tks = [t for m in GLOBAL_STATE.markets.values() if m.state != MarketState.CLOSED for t in (m.yes_token, m.no_token)]
        if tks: ws.send(json.dumps({"type": "Market", "assets_ids": tks}))

    while GLOBAL_STATE.running:
        try:
            ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market", on_message=on_message, on_open=on_open)
            GLOBAL_STATE.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception: pass
        GLOBAL_STATE.ws_handle, GLOBAL_STATE.ws_connected = None, False
        time.sleep(2)

if __name__ == "__main__":
    init_csv()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    while GLOBAL_STATE.running: time.sleep(1)