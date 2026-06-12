"""
main.py — BSS Bot v6.15 (Tiered Exit + Maker-First Hook + Stalk Abort + Vault)
FULL PRODUCTION BUILD - UI PATCHED
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

# ─── V6.15 CORE BLOCKS ───
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

class V615Engine:
    def __init__(self):
        self.ofa_tracker = OFAVelocityTracker()
        self.positions = collections.defaultdict(dict)
        self.dashboard_ui = {}
        self.cats_count = 0

def render_cloud_dashboard(engine):
    print("\n" + "="*70)
    print(f"📊 TELEMETRY DASHBOARD  |  🐈 CATASTROPHIC WHIPSAWS RECORDED: {engine.cats_count}")
    print("-" * 70)
    print(f"{'MARKET SLUG':<32} | {'TTR':<6} | {'MID PX':<8} | {'STATUS'}")
    print("-" * 70)
    
    if not engine.dashboard_ui: 
        print("   No active markets tracked.")
    
    sorted_ui = sorted(engine.dashboard_ui.items(), key=lambda x: x[1].get('TTR', 999), reverse=True)
    for slug, state in sorted_ui:
        print(f"{slug:<32} | {state['TTR']:<6} | ${state['Mid_Price']:<7.2f} | {state['Status']}")
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
        
        # V6.15 Lifecycle Flags
        self.phase1_executed = False
        self.phase2_executed = False
        self.shares_sold_so_far = 0.0
        self.active_maker_ts = 0.0
        self.pending_exit_shares = 0.0
        self.pending_exit_reason = ""
        self.sold_side = ""
        self.sold_price = 0.0
        
        self.salvage_revenue = 0.0
        self.realized_pnl = 0.0
        
        self.close_time = ""
        self.close_reason = ""
        self.expired_processed = False
        
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
            if res.status_code == 200:
                GLOBAL_STATE.btc_live = float(res.json()["price"])
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

# ─── DASHBOARD HTML ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Dashboard v6.15 (Production Engine)</title>
<style>
    :root { --bg-main: #0B1120; --bg-panel: #1E293B; --header-bg: #0F172A; --header-text: #F8FAFC; --sub-header-bg: #0F172A; --text-navy: #F8FAFC; --text-light: #94A3B8; --border-color: #334155; --val-green: #34D399; --val-red: #F87171; --val-yellow: #FCD34D; --val-pink: #F472B6; --font-sans: system-ui, -apple-system, sans-serif; }
    body { background: var(--bg-main); color: var(--text-navy); font-family: var(--font-sans); padding: 20px; font-size: 14px; margin: 0; }
    .header-panel { background: var(--header-bg); border: 1px solid var(--border-color); display: flex; flex-direction: column; text-align: center; margin-bottom: 20px; border-radius: 6px; }
    .brand-title { font-size: 22px; font-weight: bold; color: var(--header-text); padding: 14px 0; border-bottom: 1px solid var(--border-color); }
    .status-tags { font-size: 12px; font-weight: normal; margin-left: 15px; color: var(--text-light); }
    .vitals-row { display: flex; background: var(--sub-header-bg); }
    .vital-box { flex: 1; padding: 15px; border-right: 1px solid var(--border-color); text-align: center; }
    .vital-box:last-child { border-right: none; }
    .vital-label { font-size: 12px; font-weight: 700; text-transform: uppercase; margin-bottom: 8px; color: var(--text-light); }
    .vital-value { background: var(--bg-panel); color: var(--text-navy); font-size: 24px; font-weight: 800; padding: 8px; border-radius: 4px; border: 1px solid var(--border-color); font-family: monospace; }
    .vital-value.green { color: var(--val-green); border-color: #064E3B; background: #065F46;}
    .vital-value.red { color: var(--val-red); border-color: #7F1D1D; background: #991B1B;}
    .sec-title { background: var(--header-bg); color: var(--header-text); font-size: 15px; font-weight: bold; text-align: center; padding: 12px; margin-bottom: 15px; border-radius: 6px; border: 1px solid var(--border-color);}
    .grid { display: grid; grid-template-columns: 1fr; gap: 20px; margin-bottom: 15px; }
    .card { background: var(--bg-panel); border: 1px solid var(--border-color); display: flex; flex-direction: column; border-radius: 6px; overflow: hidden;}
    .card-header { background: var(--sub-header-bg); padding: 12px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; font-weight: 800; font-size: 15px; align-items: center;}
    .ref-bar { background:#0F172A; border-bottom:1px solid var(--border-color); padding:10px 20px; display:flex; justify-content:space-between; font-family:monospace; font-size:13px; }
    .leg-container { display: flex; width: 100%; }
    .leg-col { flex: 1; padding: 20px; border-right: 1px solid var(--border-color); }
    .leg-col:last-child { border-right: none; }
    .leg-title { font-size: 13px; font-weight: 800; text-align: center; margin-bottom: 15px; color: var(--text-light); text-transform: uppercase; }
    .data-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: var(--text-light); align-items: center;}
    .data-row b { color: var(--text-navy); font-family: monospace; font-size: 15px;}
    .svg-container { height: 50px; margin-top: 15px; background: #0F172A; border: 1px solid var(--border-color); border-radius: 4px; overflow: hidden;}
    .val-green { color: var(--val-green); font-weight: 800; font-family: monospace; }
    .val-red { color: var(--val-red); font-weight: 800; font-family: monospace; }
    .val-gold { color: var(--val-yellow); font-weight: 800; font-family: monospace; }
    .table-container { background: var(--bg-panel); border: 1px solid var(--border-color); margin-bottom: 35px; border-radius: 6px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: var(--sub-header-bg); color: var(--text-light); font-size: 11px; font-weight: 800; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border-color); text-align: center;}
    td { padding: 12px 10px; border-bottom: 1px solid var(--border-color); text-align: center; font-size: 13px; font-family: monospace; color: var(--text-navy);}
    .queue-container { background: var(--bg-panel); border: 1px solid var(--border-color); padding: 15px 20px; font-family: monospace; font-size: 13px; color: var(--text-light); line-height: 1.8; border-radius: 6px; margin-bottom:35px;}
    .bg-market-row { display: flex; justify-content: space-between; padding: 10px 15px; border-bottom: 1px solid var(--border-color); font-family: monospace; }
    .bg-market-row:last-child { border-bottom: none; }
    .vault { display: flex; gap: 15px; background: var(--sub-header-bg); padding: 15px; border: 1px solid var(--border-color); align-items: center; justify-content: center; margin-bottom: 25px; border-radius: 6px;}
    .btn-action { background: #1E293B; color: var(--text-navy); border: 1px solid var(--border-color); padding: 8px 18px; cursor: pointer; font-weight: 700; border-radius: 4px; transition: all 0.2s;}
    .btn-action:hover { background: #334155; }
</style>
</head>
<body>

<div class="header-panel">
    <div class="brand-title">BSS Dashboard v6.15 (Production Engine)
        <span class="status-tags" id="bot-uptime">[Uptime: 0h 0m 0s]</span>
        <span class="status-tags" id="ws-status">[WS: Checking...]</span>
    </div>
    <div class="vitals-row">
        <div class="vital-box"><div class="vital-label">Total Realized P&L</div><div class="vital-value" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Completed Trades</div><div class="vital-value" id="v-trades">0</div></div>
        <div class="vital-box"><div class="vital-label">Active Slots</div><div class="vital-value" id="v-active">0</div></div>
        <div class="vital-box"><div class="vital-label">Catastrophic Whipsaws</div><div class="vital-value red" id="v-cats">0</div></div>
    </div>
</div>

<div class="sec-title">Execution Focus (Primary Market)</div>
<div class="grid" id="active-cards"><div style="text-align:center; padding:30px; color:var(--text-light);">Awaiting Data...</div></div>

<div class="sec-title">Background Active & Scouting Queue</div>
<div class="queue-container" id="obs-queue">Scanning...</div>

<div class="sec-title">Consolidated Trade Lifecycle History</div>
<div class="table-container">
    <table>
        <thead><tr><th>Time Closed</th><th>Market Slug</th><th>YES Entry</th><th>NO Entry</th><th>T1 Exit</th><th>Net P&L</th><th>Verify</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="7" style="color: var(--text-light); padding: 20px;">No historical data available.</td></tr></tbody>
    </table>
</div>

<div class="vault">
    <span style="font-weight: 800; margin-right: 15px; color: var(--text-navy);">Data Vault:</span>
    <button class="btn-action" onclick="window.location.href='/api/dl_trades'">Download Trades</button>
    <button class="btn-action" onclick="window.location.href='/api/dl_snaps'">Download Snapshots</button>
    <button class="btn-action" style="color: #FCD34D;" onclick="window.location.href='/api/dl_telemetry'">Download Telemetry</button>
</div>

<script>
function renderSparkline(history, color, sold_price) {
    if(!history || history.length < 2) return '';
    const min = Math.min(...history), max = Math.max(...history);
    const range = (max - min) || 0.01;
    const pts = history.map((val, i) => {
        const x = (i / (history.length - 1)) * 100;
        const y = 100 - (((val - min) / range) * 100);
        return `${x},${y}`;
    }).join(' ');
    
    let svg = `<polyline fill="none" stroke="${color}" stroke-width="2.5" points="${pts}" />`;
    
    let y86 = 100 - (((0.86 - min) / range) * 100);
    if(y86 >= -10 && y86 <= 110) {
        svg += `<line x1="0" y1="${y86}" x2="100" y2="${y86}" stroke="#FCD34D" stroke-width="1.5" stroke-dasharray="4,4" />`;
    }

    let y95 = 100 - (((0.95 - min) / range) * 100);
    if(y95 >= -10 && y95 <= 110) {
        svg += `<line x1="0" y1="${y95}" x2="100" y2="${y95}" stroke="#F472B6" stroke-width="1.5" stroke-dasharray="4,4" />`;
    }

    if (sold_price > 0) {
        let ySold = 100 - (((sold_price - min) / range) * 100);
        ySold = Math.max(5, Math.min(95, ySold));
        svg += `<circle cx="95" cy="${ySold}" r="5" fill="#34D399" stroke="#0B1120" stroke-width="2" />`;
    }

    return `<svg width="100%" height="100%" viewBox="0 -10 100 120" preserveAspectRatio="none">${svg}</svg>`;
}

setInterval(async () => {
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        
        document.getElementById('bot-uptime').textContent = `[Uptime: ${Math.floor(s.uptime_s/3600)}h ${Math.floor((s.uptime_s%3600)/60)}m ${s.uptime_s%60}s]`;
        document.getElementById('ws-status').textContent = s.ws_connected ? "[WS: CONNECTED]" : "[WS: DROPPED]";
        document.getElementById('ws-status').style.color = s.ws_connected ? "#34d399" : "#f87171";
        
        const pnlBox = document.getElementById('v-pnl');
        pnlBox.textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        pnlBox.className = "vital-value " + (s.pnl > 0 ? "green" : (s.pnl < 0 ? "red" : ""));
        
        document.getElementById('v-trades').textContent = s.total_trades_count;
        document.getElementById('v-cats').textContent = s.cats_count;
        
        let activeMarkets = s.markets.filter(m => m.state === 'BOTH' && m.ttr_s > 0);
        let otherMarkets = s.markets.filter(m => m.state !== 'BOTH' && m.state !== 'CLOSED' && m.ttr_s > 0);
        document.getElementById('v-active').textContent = activeMarkets.length;
        
        let htmlCards = '';
        if (activeMarkets.length > 0) {
            let primary = activeMarkets[0]; 
            let valYes = primary.yes_shares * primary.yes_bid;
            let valNo = primary.no_shares * primary.no_bid;

            let strikeText = primary.strike > 0 ? '$' + primary.strike.toFixed(2) : 'Awaiting Mark';
            let liveText = primary.live_btc > 0 ? '$' + primary.live_btc.toFixed(2) : 'Loading...';
            let spotDeltaStr = '--';
            if (primary.strike > 0 && primary.live_btc > 0) {
                let diff = primary.live_btc - primary.strike;
                spotDeltaStr = `<span class="${diff >= 0 ? 'val-green' : 'val-red'}">${diff >= 0 ? '+$' : '-$'}${Math.abs(diff).toFixed(2)}</span>`;
            }

            let yes_sold = primary.sold_side === 'YES' ? primary.sold_price : 0;
            let no_sold = primary.sold_side === 'NO' ? primary.sold_price : 0;

            htmlCards = `<div class="card">
                <div class="card-header">
                    <span>${primary.slug}</span>
                    <span style="color:var(--text-light);">TTR: <span style="color:var(--text-navy);">${primary.ttr_s}s</span></span>
                </div>
                <div class="ref-bar">
                    <div><span style="color:var(--text-light)">Strike:</span> <b style="color:var(--text-navy)">${strikeText}</b></div>
                    <div><span style="color:var(--text-light)">Live:</span> <b style="color:var(--text-navy)">${liveText}</b></div>
                    <div><span style="color:var(--text-light)">Delta:</span> <b>${spotDeltaStr}</b></div>
                </div>
                <div class="leg-container">
                    <div class="leg-col">
                        <div class="leg-title">YES LEG</div>
                        <div class="data-row"><span>Shares:</span> <b>${primary.yes_shares.toFixed(2)}</b></div>
                        <div class="data-row"><span>Entry:</span> <b>$${primary.yes_entry > 0 ? primary.yes_entry.toFixed(3) : '0.000'}</b></div>
                        <div class="data-row" style="margin-top:10px; border-top:1px solid var(--border-color); padding-top:10px;">
                            <span>Mid:</span> <b class="val-gold">$${primary.yes_mid.toFixed(3)}</b>
                        </div>
                        <div class="data-row"><span>Bid Val:</span> <b class="val-green">$${valYes.toFixed(2)}</b></div>
                        <div class="svg-container">${renderSparkline(primary.history_yes, '#38BDF8', yes_sold)}</div>
                    </div>
                    <div class="leg-col">
                        <div class="leg-title">NO LEG</div>
                        <div class="data-row"><span>Shares:</span> <b>${primary.no_shares.toFixed(2)}</b></div>
                        <div class="data-row"><span>Entry:</span> <b>$${primary.no_entry > 0 ? primary.no_entry.toFixed(3) : '0.000'}</b></div>
                        <div class="data-row" style="margin-top:10px; border-top:1px solid var(--border-color); padding-top:10px;">
                            <span>Mid:</span> <b class="val-gold">$${primary.no_mid.toFixed(3)}</b>
                        </div>
                        <div class="data-row"><span>Bid Val:</span> <b class="val-green">$${valNo.toFixed(2)}</b></div>
                        <div class="svg-container">${renderSparkline(primary.history_no, '#94A3B8', no_sold)}</div>
                    </div>
                </div>
            </div>`;
        } else {
            htmlCards = '<div style="text-align:center; padding:30px; color:var(--text-light);">No active locked positions...</div>';
        }

        let htmlQueue = '';
        if (activeMarkets.length > 1) {
            htmlQueue += `<div style="font-weight:bold; color:var(--text-navy); margin-bottom:10px;">BACKGROUND LOCKED POSITIONS</div>`;
            for(let i=1; i<activeMarkets.length; i++) {
                let m = activeMarkets[i];
                let yVal = (m.yes_shares * m.yes_bid).toFixed(2);
                let nVal = (m.no_shares * m.no_bid).toFixed(2);
                htmlQueue += `<div class="bg-market-row" style="align-items: center; border-left: 3px solid #38BDF8; padding-left: 10px;">
                    <div style="flex: 2; display: flex; flex-direction: column;">
                        <b style="color:var(--text-navy)">${m.slug}</b>
                        <span style="font-size: 11px; color:var(--text-light)">Dual Leg Locked</span>
                    </div>
                    <div style="flex: 2; text-align: center; display: flex; flex-direction: column;">
                        <span style="color:var(--text-light); font-size:11px;">YES Mid / Val</span>
                        <b style="color:#38BDF8">$${m.yes_mid.toFixed(3)} / <span class="val-green">$${yVal}</span></b>
                    </div>
                    <div style="flex: 2; text-align: center; display: flex; flex-direction: column;">
                        <span style="color:var(--text-light); font-size:11px;">NO Mid / Val</span>
                        <b style="color:#94A3B8">$${m.no_mid.toFixed(3)} / <span class="val-green">$${nVal}</span></b>
                    </div>
                    <div style="flex: 1; text-align: right; font-size: 14px;">TTR: <b class="val-gold">${m.ttr_s}s</b></div>
                </div>`;
            }
            htmlQueue += `<div style="margin-bottom:15px; border-bottom:1px solid var(--border-color); padding-bottom:5px;"></div>`;
        }

        if(otherMarkets.length > 0) {
            htmlQueue += `<div style="font-weight:bold; color:var(--text-light); margin-bottom:10px;">SCOUTING / GATHERING LEGS</div>`;
            otherMarkets.forEach(m => {
                let status = m.state === 'WATCH' ? 'Scouting' : 'Filling Dual Leg';
                let yStr = m.yes_entry > 0 ? `<b class="val-green">FILLED $${m.yes_entry.toFixed(3)}</b>` : `$${m.yes_mid.toFixed(3)}`;
                let nStr = m.no_entry > 0 ? `<b class="val-green">FILLED $${m.no_entry.toFixed(3)}</b>` : `$${m.no_mid.toFixed(3)}`;
                htmlQueue += `<div class="bg-market-row" style="color:var(--text-light); align-items: center;">
                    <div style="flex: 2; display: flex; flex-direction: column;">
                        <span>${m.slug}</span>
                        <span style="font-size: 11px;">${status}</span>
                    </div>
                    <div style="flex: 1; text-align: center;">YES: ${yStr}</div>
                    <div style="flex: 1; text-align: center;">NO: ${nStr}</div>
                    <div style="flex: 1; text-align: right;">TTR: <b>${m.ttr_s}s</b></div>
                </div>`;
            });
        }
        document.getElementById('obs-queue').innerHTML = htmlQueue || 'No upcoming markets in window.';

        let logHtml = '';
        s.history.reverse().forEach(h => {
            const pnlStr = h.pnl !== 0.0 ? (h.pnl > 0 ? `+${h.pnl.toFixed(2)}` : h.pnl.toFixed(2)) : '--';
            let t1Str = '--';
            if (h.sold_side !== "" && h.sold_price > 0) t1Str = `<span class="val-gold">${h.sold_side}</span> @ $${h.sold_price.toFixed(3)}`;
            
            logHtml += `<tr>
                <td style="color:var(--text-light); font-size: 13px;">${h.time}</td>
                <td>${h.slug}</td>
                <td>${h.yes_entry > 0 ? '$'+h.yes_entry.toFixed(3) : '--'}</td>
                <td>${h.no_entry > 0 ? '$'+h.no_entry.toFixed(3) : '--'}</td>
                <td style="line-height:1.4;">${t1Str}</td>
                <td class="${h.pnl>0?'val-green':(h.pnl<0?'val-red':'')}">${pnlStr}</td>
                <td><a href="https://polymarket.com/event/${h.slug}" target="_blank" style="color: #60A5FA; text-decoration: none; font-weight: 800; font-size: 12px;">LINK ↗</a></td>
            </tr>`;
        });
        if(logHtml) document.getElementById('log-body').innerHTML = logHtml;

    } catch(e) {}
}, 500); 
</script>
</body>
</html>
"""

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))
        elif self.path == "/api/status":
            now = get_synced_time()
            m_data, history_data = [], []
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                ttr = int(m.end_ts - now)
                if m.state == MarketState.CLOSED and m.close_time != "" and m.expired_processed:
                    history_data.append({
                        "time": m.close_time, "slug": m.slug, "reason": m.close_reason,
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price, "pnl": m.realized_pnl,
                        "sold_side": m.sold_side, "sold_price": m.sold_price
                    })
                else:
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    m_data.append({
                        "slug": m.slug, "state": m.state, "ttr_s": ttr,
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price,
                        "yes_shares": m.yes_shares, "no_shares": m.no_shares,
                        "yes_bid": yb.bid if yb else 0.0, "no_bid": nb.bid if nb else 0.0,
                        "yes_mid": ((yb.bid + yb.ask) / 2.0) if (yb and yb.bid > 0 and yb.ask > 0) else (yb.bid if yb else 0.0),
                        "no_mid": ((nb.bid + nb.ask) / 2.0) if (nb and nb.bid > 0 and nb.ask > 0) else (nb.bid if nb else 0.0),
                        "strike": m.strike_price, "live_btc": GLOBAL_STATE.btc_live,
                        "history_yes": m.history_yes[-30:], "history_no": m.history_no[-30:],
                        "sold_side": m.sold_side, "sold_price": m.sold_price
                    })
            payload = {
                "uptime_s": int(time.time() - SYSTEM_BOOT_TIME),
                "ws_connected": GLOBAL_STATE.ws_connected, "pnl": GLOBAL_STATE.total_pnl,
                "total_trades_count": GLOBAL_STATE.total_trades,
                "cats_count": GLOBAL_STATE.engine.cats_count,
                "markets": m_data, "history": history_data[-15:]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        elif self.path in ["/api/dl_trades", "/api/dl_snaps", "/api/dl_telemetry"]:
            filename = CSV_TRADES
            if self.path == "/api/dl_snaps": filename = CSV_SNAPS
            elif self.path == "/api/dl_telemetry": filename = CSV_TELEMETRY
            self.send_response(200)
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Type', 'text/csv')
            self.end_headers()
            try:
                with open(filename, "rb") as f: self.wfile.write(f.read())
            except Exception: pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args): pass

def run_server():
    server = socketserver.ThreadingTCPServer(("", PORT), DashboardHandler)
    print(f"[System] Web UI listening on port {PORT}", flush=True)
    server.serve_forever()

# ─── CORE STRATEGY ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str, shares: float, fees: float, ttr: int, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if action.startswith("SELL_LOSER_") or "ABORT" in action:
        mdm.salvage_revenue += (shares * price)
        mdm.sold_side, mdm.sold_price = side, price
    if "CLOSED" in action or "EXPIRED" in action:
        mdm.close_time, mdm.close_reason = ts, action
        GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl
        GLOBAL_STATE.total_pnl += pnl
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
            if mdm.slug in GLOBAL_STATE.engine.dashboard_ui:
                del GLOBAL_STATE.engine.dashboard_ui[mdm.slug]
            if not yb or not nb: return
            
            # Whipsaw Logic: Oracle Settlement
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

    if 0 < ttr <= 300 and mdm.strike_price == 0.0 and GLOBAL_STATE.btc_live > 0:
        mdm.strike_price = GLOBAL_STATE.btc_live

    if not yb or not nb: return
    if mdm.state == MarketState.CLOSED: return
    
    y_mid = (yb.bid + yb.ask) / 2.0 if (yb.bid > 0 and yb.ask > 0) else yb.bid
    n_mid = (nb.bid + nb.ask) / 2.0 if (nb.bid > 0 and nb.ask > 0) else nb.bid
    
    # 1. 60-Minute Stalk & Entry
    if mdm.state == MarketState.WATCH:
        target = T_WINDOW_1 if ttr > 600 else T_WINDOW_2
        if 0 < yb.ask <= target:
            mdm.state = MarketState.WAITING_NO
            mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "MAKER_FILL_LEG_1", mdm.yes_shares, fee, ttr)
        elif 0 < nb.ask <= target:
            mdm.state = MarketState.WAITING_YES
            mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "MAKER_FILL_LEG_1", mdm.no_shares, fee, ttr)

    elif mdm.state == MarketState.WAITING_NO:
        # Abort Naked Exposure
        if ttr <= ENTRY_CUTOFF_TTR:
            mdm.state = MarketState.CLOSED
            fee = mdm.yes_shares * yb.bid * TAKER_FEE_RATE
            pnl = (mdm.yes_shares * yb.bid) - (mdm.yes_entry_price * mdm.yes_shares) - mdm.total_fees_paid - fee
            execute_trade(mdm, "YES", yb.bid, "ABORT_NAKED_LEG", mdm.yes_shares, fee, ttr, pnl)
            return
            
        if nb.ask > 0 and (mdm.yes_entry_price + nb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH
            mdm.no_entry_price, mdm.no_shares = nb.ask, BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "TAKER_HEDGE_GUARANTEE", mdm.no_shares, fee, ttr)
            
    elif mdm.state == MarketState.WAITING_YES:
        # Abort Naked Exposure
        if ttr <= ENTRY_CUTOFF_TTR:
            mdm.state = MarketState.CLOSED
            fee = mdm.no_shares * nb.bid * TAKER_FEE_RATE
            pnl = (mdm.no_shares * nb.bid) - (mdm.no_entry_price * mdm.no_shares) - mdm.total_fees_paid - fee
            execute_trade(mdm, "NO", nb.bid, "ABORT_NAKED_LEG", mdm.no_shares, fee, ttr, pnl)
            return
            
        if yb.ask > 0 and (mdm.no_entry_price + yb.ask <= MAX_COMBINED_COST):
            mdm.state = MarketState.BOTH
            mdm.yes_entry_price, mdm.yes_shares = yb.ask, BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "TAKER_HEDGE_GUARANTEE", mdm.yes_shares, fee, ttr)

    # 2. 60/30 Tiered Exit & Maker-First Hook
    elif mdm.state == MarketState.BOTH:
        if y_mid > n_mid: winner_mid, loser_side, loser_bid, loser_ask, loser_book = y_mid, "NO", nb.bid, nb.ask, nb
        else: winner_mid, loser_side, loser_bid, loser_ask, loser_book = n_mid, "YES", yb.bid, yb.ask, yb
        
        GLOBAL_STATE.engine.ofa_tracker.update_snapshot(mdm.slug, loser_book.get_local_vols(loser_bid, "bid"), loser_book.get_local_vols(loser_ask, "ask"))
        
        status_str = "[TRACKING]"
        if mdm.active_maker_ts > 0: status_str = "[MAKER HOOK PENDING]"
        elif mdm.phase1_executed or mdm.phase2_executed: status_str = "[SOLD]"
        
        GLOBAL_STATE.engine.dashboard_ui[mdm.slug] = {
            "TTR": ttr, "Mid_Price": winner_mid, "Status": status_str
        }

        # Handle Active Maker Hook
        if mdm.pending_exit_shares > 0:
            if mdm.active_maker_ts > 0:
                if now - mdm.active_maker_ts > 3.0:
                    # Maker Time Expired: Drop Taker Offset
                    taker_price = max(0.01, round(loser_bid - 0.02, 2))
                    fee = (mdm.pending_exit_shares * taker_price) * TAKER_FEE_RATE
                    mdm.total_fees_paid += fee
                    execute_trade(mdm, loser_side, taker_price, f"SELL_LOSER_TAKER_{mdm.pending_exit_reason}", mdm.pending_exit_shares, fee, ttr)
                    mdm.pending_exit_shares = 0
                    mdm.active_maker_ts = 0
            elif mdm.active_maker_ts == -1:
                # Direct Taker Drop
                taker_price = max(0.01, round(loser_bid - 0.02, 2))
                fee = (mdm.pending_exit_shares * taker_price) * TAKER_FEE_RATE
                mdm.total_fees_paid += fee
                execute_trade(mdm, loser_side, taker_price, f"SELL_LOSER_TAKER_{mdm.pending_exit_reason}", mdm.pending_exit_shares, fee, ttr)
                mdm.pending_exit_shares = 0
                mdm.active_maker_ts = 0
            return

        # Trigger Evaluation
        base_shares = mdm.no_shares if loser_side == "NO" else mdm.yes_shares
        remaining_shares = base_shares - mdm.shares_sold_so_far
        b_vol = loser_book.get_local_vols(loser_bid, "bid")
        
        # 1. The Runner Target ($0.95)
        if 0 < ttr <= 60 and winner_mid >= SELL_LOSER_T2_THRESH and mdm.phase1_executed and not mdm.phase2_executed:
            mdm.phase2_executed = True
            mdm.pending_exit_shares = remaining_shares
            mdm.pending_exit_reason = "T2_RUNNER_095"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK_PLACED", mdm.pending_exit_shares, 0.0, ttr)
            else: mdm.active_maker_ts = -1

        # 2. The Standard Play (50% at $0.86 in the 60s-31s window)
        elif 30 < ttr <= 60 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True
            mdm.pending_exit_shares = base_shares * 0.50
            mdm.shares_sold_so_far += mdm.pending_exit_shares
            mdm.pending_exit_reason = "T1_TRANCHE_086"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK_PLACED", mdm.pending_exit_shares, 0.0, ttr)
            else: mdm.active_maker_ts = -1

        # 3. The Late Bloomer (100% dump if $0.86 hits inside final 30s)
        elif 0 < ttr <= 30 and winner_mid >= SELL_LOSER_T1_THRESH and not mdm.phase1_executed:
            mdm.phase1_executed = True
            mdm.phase2_executed = True  
            mdm.pending_exit_shares = base_shares * 1.0  
            mdm.shares_sold_so_far += mdm.pending_exit_shares
            mdm.pending_exit_reason = "LATE_BLOOMER_100PCT"
            if b_vol > 1500: mdm.active_maker_ts = now; execute_trade(mdm, loser_side, round(loser_bid + 0.01, 2), "MAKER_HOOK_PLACED", mdm.pending_exit_shares, 0.0, ttr)
            else: mdm.active_maker_ts = -1

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

def snapshot_loop():
    while GLOBAL_STATE.running:
        if not GLOBAL_STATE.armed:
            time.sleep(1)
            continue
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
                        m.history_yes.append(ya)
                        m.history_no.append(na)
                        if len(m.history_yes) > 30: m.history_yes.pop(0)
                        if len(m.history_no) > 30: m.history_no.pop(0)
        except Exception: pass
        time.sleep(30)

def dashboard_render_loop():
    while GLOBAL_STATE.running:
        if GLOBAL_STATE.armed:
            render_cloud_dashboard(GLOBAL_STATE.engine)
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
    
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=btc_oracle_loop, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    
    run_diagnostics()
    
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    threading.Thread(target=dashboard_render_loop, daemon=True).start()
    
    while GLOBAL_STATE.running:
        time.sleep(1)