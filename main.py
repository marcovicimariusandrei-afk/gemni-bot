"""
main.py — Opportunistic BSS Bot with Legacy Visual Dashboard
Combines new concurrent state machines with the classic v5.8 dashboard UI.
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

BOOT_TS = time.time()

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
        self.price_history = []
        self.last_hist_ts = 0.0

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

# ─── LEGACY HTML/CSS DASHBOARD ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BSS Opportunistic Bot</title>
<style id="theme-style">
:root{--bg:#0a0a0a;--panel:#141414;--border:#262626;--border-soft:#1f1f1f;--text:#e0e0e0;--muted:#7a7a7a;--green:#5cbd5c;--yellow:#e0b340;--red:#d96666;--blue:#7aa5d2;--mono:ui-monospace,Menlo,Consolas,monospace;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.45;}
header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
header h1{margin:0;font-size:18px;font-weight:600;}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:600;text-transform:uppercase;}
.badge-dry{background:rgba(88,166,255,.15);color:var(--blue);}
.badge-live{background:rgba(248,81,73,.15);color:var(--red);}
.badge-bs{background:rgba(122,165,210,.15);color:var(--blue);border:1px solid rgba(122,165,210,.4);font-family:var(--mono);}
.uptime{margin-left:auto;color:var(--muted);font-family:var(--mono);font-size:12px;}
main{padding:20px;max-width:1100px;margin:0 auto;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:20px;}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;}
.card-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;}
.card-value{font-size:22px;font-weight:600;font-family:var(--mono);}
.card-detail{margin-top:4px;font-size:12px;color:var(--muted);font-family:var(--mono);}
.bs-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px; border-left:3px solid var(--blue);}
.bs-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px;font-weight:600;}
.bs-pos{background:#1a1a1a;border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px; border-left:3px solid var(--muted);}
.bs-pos-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:8px;}
.bs-pos-id{font-weight:600;color:var(--text);font-size:12px;}
.bs-pos-ttr{color:var(--yellow);font-weight:600;}
.bs-pos-status{font-weight:600; font-size:11px; padding:2px 8px; border-radius:4px;}
.recent-trades{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}
.trade-row{display:grid;grid-template-columns:70px 1fr 100px 60px 80px;gap:10px;align-items:center;padding:8px 10px;background:#1a1a1a;border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:12px; margin-bottom:6px;}
.trade-row.yes{border-left:3px solid var(--green);}
.trade-row.no{border-left:3px solid var(--red);}
.trade-action{background:#262626; padding:2px 6px; border-radius:4px; text-align:center; font-size:10px; color:var(--text);}
.up{color:var(--green)!important; font-weight:bold;}
.down{color:var(--red)!important; font-weight:bold;}
</style>
</head>
<body>
<header>
  <h1>BSS Core Dashboard</h1>
  <span id="mode-badge" class="badge badge-dry">DRY</span>
  <span class="badge badge-bs" style="margin-left:8px;">OPPORTUNISTIC</span>
  <span class="uptime" id="uptime">uptime —</span>
</header>
<main>
  <div class="grid">
    <div class="card">
      <div class="card-title">Polymarket WS</div>
      <div class="card-value" id="poly-state">—</div>
      <div class="card-detail" id="poly-detail">Waiting for data...</div>
    </div>
    <div class="card">
      <div class="card-title">Tracked Markets</div>
      <div class="card-value" id="tracked-count">0</div>
      <div class="card-detail">in 60m discovery window</div>
    </div>
  </div>

  <div class="bs-panel">
    <div class="bs-title">Active Market States & Orderbooks</div>
    <div id="bs-positions">
      <div style="color:var(--muted); font-size:12px; font-family:var(--mono); padding:10px;">Loading markets...</div>
    </div>
  </div>

  <div class="recent-trades">
    <div class="bs-title">Recent Execution Log</div>
    <div id="recent-trades-list">
       <div style="color:var(--muted); font-size:12px; font-family:var(--mono); padding:10px;">No trades yet</div>
    </div>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
function fmtUptime(s){
  if(!s) return '—'; 
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=Math.floor(s%60); 
  return h?`${h}h ${m}m ${sec}s` : m?`${m}m ${sec}s` : `${sec}s`;
}
function fmtCountdown(s){
  if(s==null)return '—';
  const x=Math.round(s);
  if(x<=0)return 'now';
  if(x<60)return x+'s';
  return Math.floor(x/60)+'m '+(x%60)+'s';
}

function renderSparkline(history) {
  if(!history || history.length < 2) return '<div style="height:44px; margin-top:8px;"></div>';
  const W=320, H=44, padX=4, padY=4;
  const ts = history.map(s => s[0]);
  const tMin = ts[0], tMax = ts[ts.length-1], tRange = Math.max(1, tMax-tMin);
  
  // Dynamic scaling for 0.49/0.50 strategy
  const yMin = 0.35, yMax = 0.65; 
  const xOf = t => padX + ((t-tMin)/tRange)*(W-2*padX);
  const yOf = v => padY + (1 - (v-yMin)/(yMax-yMin))*(H-2*padY);
  
  const yesPath = history.map((s,i) => `${i?'L':'M'}${xOf(s[0]).toFixed(1)},${yOf(s[1]).toFixed(1)}`).join('');
  const noPath  = history.map((s,i) => `${i?'L':'M'}${xOf(s[0]).toFixed(1)},${yOf(s[2]).toFixed(1)}`).join('');
  
  const y49 = yOf(0.49).toFixed(1);
  const y50 = yOf(0.50).toFixed(1);
  
  return `<div style="margin-top:12px;height:44px;">
    <svg width="100%" height="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block; background:#111; border-radius:4px;">
      <line x1="0" y1="${y50}" x2="${W}" y2="${y50}" stroke="#444" stroke-width="1" stroke-dasharray="2,2"/>
      <line x1="0" y1="${y49}" x2="${W}" y2="${y49}" stroke="#444" stroke-width="1" stroke-dasharray="2,2"/>
      <path d="${yesPath}" fill="none" stroke="#5cbd5c" stroke-width="1.5"/>
      <path d="${noPath}"  fill="none" stroke="#d96666" stroke-width="1.5"/>
    </svg>
  </div>`;
}

async function tick() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    
    $('uptime').textContent = 'uptime ' + fmtUptime(s.uptime_s);
    $('mode-badge').textContent = s.mode.toUpperCase();
    $('mode-badge').className = s.mode === 'live' ? 'badge badge-live' : 'badge badge-dry';
    
    $('poly-state').textContent = s.ws_connected ? 'Connected' : 'Disconnected';
    $('poly-state').style.color = s.ws_connected ? 'var(--green)' : 'var(--red)';
    $('tracked-count').textContent = s.markets.length;
    
    const mHtml = s.markets.map(m => {
      let bColor = 'var(--muted)';
      let bgState = 'rgba(255,255,255,0.06)';
      let txtState = 'var(--muted)';
      
      if(m.state === 'WAITING_NO' || m.state === 'WAITING_YES') {
          bColor = 'var(--yellow)'; bgState = 'rgba(255,193,7,0.15)'; txtState = 'var(--yellow)';
      }
      if(m.state === 'BOTH') {
          bColor = 'var(--green)'; bgState = 'rgba(63,185,80,0.15)'; txtState = 'var(--green)';
      }
      
      let detail = `YES Ask: ${m.yes_ask.toFixed(3)} · NO Ask: ${m.no_ask.toFixed(3)} · Hunting ≤ 0.49`;
      if(m.state === 'WAITING_NO') detail = `<span class="up">Leg 1 YES @ ${m.leg1_price.toFixed(3)}</span> · Hunting NO Ask ≤ 0.50 (Now: ${m.no_ask.toFixed(3)})`;
      if(m.state === 'WAITING_YES') detail = `<span class="down">Leg 1 NO @ ${m.leg1_price.toFixed(3)}</span> · Hunting YES Ask ≤ 0.50 (Now: ${m.yes_ask.toFixed(3)})`;
      if(m.state === 'BOTH') detail = `Cost: $${(m.leg1_price + m.leg2_price).toFixed(4)} · Hunting Sell-Loser Conviction ≥ 0.93`;
      if(m.state === 'CLOSED') detail = `Trade Exited / Expired`;

      return `<div class="bs-pos" style="border-left-color:${bColor}">
        <div class="bs-pos-head">
          <span class="bs-pos-id">${m.slug}</span>
          <span class="bs-pos-status" style="background:${bgState}; color:${txtState}">${m.state}</span>
          <span class="bs-pos-ttr">TTR ${fmtCountdown(m.ttr_s)}</span>
        </div>
        <div style="font-size:12px; color:var(--text); font-family:var(--mono);">${detail}</div>
        ${renderSparkline(m.price_history)}
      </div>`;
    }).join('');
    $('bs-positions').innerHTML = mHtml || '<div style="color:var(--muted); font-size:12px; font-family:var(--mono); padding:10px;">No active markets in window</div>';
    
    const tHtml = s.trades.slice().reverse().map(t => {
      let sColor = t.side === 'YES' ? 'yes' : 'no';
      let tColor = t.side === 'YES' ? 'up' : 'down';
      return `<div class="trade-row ${sColor}">
        <span class="trade-time" style="color:var(--muted)">${t.ts}</span>
        <span style="color:var(--text)">${t.slug.substring(0,25)}...</span>
        <span class="trade-action">${t.action}</span>
        <span class="${tColor}">${t.side}</span>
        <span style="text-align:right;">$${t.price.toFixed(3)}</span>
      </div>`;
    }).join('');
    $('recent-trades-list').innerHTML = tHtml || '<div style="color:var(--muted); font-size:12px; font-family:var(--mono); padding:10px;">No trades yet</div>';
    
  } catch(e) {}
}
setInterval(tick, 1000);
tick();
</script>
</body>
</html>
"""

# ─── API SERVER ───
class DashboardServerHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return
            
        if self.path == "/api/status":
            now = time.time()
            markets_data = []
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                yb = GLOBAL_STATE.books.get(m.yes_token)
                nb = GLOBAL_STATE.books.get(m.no_token)
                markets_data.append({
                    "slug": m.slug,
                    "state": m.state,
                    "ttr_s": max(0, m.end_ts - now),
                    "leg1_price": m.leg1_price,
                    "leg2_price": m.leg2_price,
                    "yes_ask": yb.ask if yb else 0.0,
                    "no_ask": nb.ask if nb else 0.0,
                    "price_history": m.price_history
                })
            
            payload = {
                "uptime_s": now - BOOT_TS,
                "mode": MODE,
                "ws_connected": GLOBAL_STATE.ws_connected,
                "markets": markets_data,
                "trades": GLOBAL_STATE.trades_log[-15:]
            }
            
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
            
        self.send_response(404)
        self.end_headers()
        
    def log_message(self, format, *args):
        pass

def run_dummy_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        with socketserver.TCPServer(("", port), DashboardServerHandler) as httpd:
            print(f"[System] Live UI Dashboard web server listening on port {port}", flush=True)
            httpd.serve_forever()
    except Exception as e:
        print(f"[System] Dashboard server startup crash: {e}", flush=True)


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

    ya, yb = y_book.ask, y_book.bid
    na, nb = n_book.ask, n_book.bid

    # Record sparkline history (every 3s)
    if now - mdm.last_hist_ts > 3.0:
        if ya > 0 and na > 0:
            mdm.price_history.append((now, ya, na))
            if len(mdm.price_history) > 120:
                mdm.price_history.pop(0)
            mdm.last_hist_ts = now

    is_live = ttr <= 300
    t2_current = T_SECOND_LIVE if is_live else T_SECOND_PRE

    if mdm.state == MarketState.WATCH:
        if 0 < ya <= T_FIRST:
            execute_trade(state, mdm, "YES", ya, "LEG_1_ENTRY")
            mdm.leg1_price = ya
            mdm.state = MarketState.WAITING_NO
        elif 0 < na <= T_FIRST:
            execute_trade(state, mdm, "NO", na, "LEG_1_ENTRY")
            mdm.leg1_price = na
            mdm.state = MarketState.WAITING_YES

    elif mdm.state == MarketState.WAITING_NO:
        if 0 < na <= t2_current:
            execute_trade(state, mdm, "NO", na, "LEG_2_ENTRY")
            mdm.leg2_price = na
            mdm.state = MarketState.BOTH

    elif mdm.state == MarketState.WAITING_YES:
        if 0 < ya <= t2_current:
            execute_trade(state, mdm, "YES", ya, "LEG_2_ENTRY")
            mdm.leg2_price = ya
            mdm.state = MarketState.BOTH

    elif mdm.state == MarketState.BOTH:
        if ttr <= SELL_LOSER_FLOOR_S:
            if yb >= SELL_LOSER_THRESH:
                execute_trade(state, mdm, "NO", nb, "SELL_LOSER")
                mdm.state = MarketState.CLOSED
            elif nb >= SELL_LOSER_THRESH:
                execute_trade(state, mdm, "YES", yb, "SELL_LOSER")
                mdm.state = MarketState.CLOSED

def execute_trade(state: BotState, mdm: MarketData, side: str, price: float, action: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Mode: {MODE}", flush=True)
    
    state.trades_log.append({
        "ts": ts,
        "slug": mdm.slug,
        "action": action,
        "side": side,
        "price": price
    })

def strategy_tick_thread(state: BotState):
    while state.running:
        now = time.time()
        for mdm in list(state.markets.values()):
            try:
                evaluate_market(state, mdm, now)
            except Exception as e:
                pass
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

    while state.running:
        try:
            ws = websocket.WebSocketApp(
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                on_message=on_message,
                on_open=on_open
            )
            state.ws_handle = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            pass
        
        state.ws_handle = None
        state.ws_connected = False
        time.sleep(2)

# ─── LIFECYCLE ───
if __name__ == "__main__":
    print(f"=== Booting BSS Bot with Legacy UI Dashboard ===", flush=True)

    def shutdown(sig, frame):
        print("\n[System] Shutting down gracefully...", flush=True)
        GLOBAL_STATE.running = False
        if GLOBAL_STATE.ws_handle: GLOBAL_STATE.ws_handle.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Launch processing tracks
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=discovery_thread, args=(GLOBAL_STATE,), daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, args=(GLOBAL_STATE,), daemon=True).start()
    threading.Thread(target=strategy_tick_thread, args=(GLOBAL_STATE,), daemon=True).start()

    while GLOBAL_STATE.running:
        time.sleep(1)