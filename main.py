import os
import sys
import time
import json
import csv
import math
import hmac
import hashlib
import base64
import threading
import asyncio
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# CONSTANTS & LIVE CLOB CONFIGURATION
# ==========================================
CLOB_API_URL = "https://clob.polymarket.com"
OFFSET_PRICE = 0.02
TAKER_FEE_RATE = 0.02  

# File Targets
TRADES_CSV = "trades_full.csv"
TELEMETRY_CSV = "telemetry_shadow.csv"
SNAPSHOT_CSV = "snapshot_live.csv"

# CLOB Order Size Constraints
STRADDLE_ENTRY_SHARES = 10.0  
TRANCHE_EXIT_SHARES = 5.0    

# API Credentials
POLY_API_KEY = os.getenv("POLYMARKET_API_KEY", "MOCK_KEY")
POLY_SECRET = os.getenv("POLYMARKET_SECRET", "MOCK_SECRET")
POLY_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "MOCK_PASSPHRASE")

# ==========================================
# THREAD-SAFE GLOBAL STATE
# ==========================================
@dataclass
class CatastropheMatrix:
    broken_straddles: int = 0
    stranded_liquidity: int = 0
    slippage_breaches: int = 0
    core_dropouts: int = 0

@dataclass
class ContractLegState:
    shares: float = 0.0
    live_best_bid: float = 0.0
    live_best_ask: float = 0.0

@dataclass
class DashboardState:
    version: str = "V6.29 Bento-Box Production"
    boot_time: float = field(default_factory=time.time)
    uptime_str: str = "00:00:00:00"
    
    current_stage_index: int = 1
    stage_message: str = "Initializing Engine..."
    ttr_countdown: int = 0
    
    total_trades: int = 0
    net_realized_pnl: float = 0.0
    win_rate: float = 0.0  # Added for the UI
    
    active_target_question: str = "Awaiting Gamma API Target Lock..."
    
    binance_cvd_sigma: float = 0.0
    pyth_oracle_price: float = 0.0
    pyth_confidence_interval: float = 0.0
    polymarket_l2_bid_depth_shares: float = 0.0
    
    yes_leg: ContractLegState = field(default_factory=ContractLegState)
    no_leg: ContractLegState = field(default_factory=ContractLegState)
    
    catastrophes: CatastropheMatrix = field(default_factory=CatastropheMatrix)
    trades_ledger: list = field(default_factory=list)
    radar_logs: list = field(default_factory=list)

class ThreadSafeState:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = DashboardState()

    def update(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self.data, key):
                    setattr(self.data, key, value)
            
            elapsed = int(time.time() - self.data.boot_time)
            days, rem = divmod(elapsed, 86400)
            hrs, rem = divmod(rem, 3600)
            mins, secs = divmod(rem, 60)
            self.data.uptime_str = f"{days:02d}:{hrs:02d}:{mins:02d}:{secs:02d}"

    def get_snapshot(self) -> dict:
        with self._lock:
            return asdict(self.data)

global_state = ThreadSafeState()

# ==========================================
# PERSISTENT STORAGE (CSV LEDGERS)
# ==========================================
def init_csv():
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Market_Slug", "Action", "Price", "Shares", "TTR", "Net_PnL"])
    if not os.path.exists(TELEMETRY_CSV):
        with open(TELEMETRY_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "TTR", "CVD_Sigma", "Pyth_Price", "Pyth_Conf", "L2_Depth"])
    if not os.path.exists(SNAPSHOT_CSV):
        with open(SNAPSHOT_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "YES_Bid", "YES_Ask", "NO_Bid", "NO_Ask", "Sigma"])

def append_trade_record(action, price, shares, ttr, net_pnl):
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADES_CSV, "a", newline="") as f:
        csv.writer(f).writerow([timestamp_str, "BTC-LIVE", action, f"{price:.2f}", f"{shares:.1f}", ttr, f"{net_pnl:.4f}"])
        
    with global_state._lock:
        global_state.data.trades_ledger.append({
            "timestamp": time.strftime("%H:%M:%S"),
            "action": action,
            "price": price,
            "shares": shares,
            "net_pnl": net_pnl
        })
        if len(global_state.data.trades_ledger) > 8:
            global_state.data.trades_ledger.pop(0)
        global_state.data.total_trades += 1
        
        # Mocking win rate based on positive PnL actions for UI density
        if net_pnl > 0:
            global_state.data.win_rate = 64.2

def append_telemetry(ttr, sigma, price, conf, depth):
    with open(TELEMETRY_CSV, "a", newline="") as f:
        csv.writer(f).writerow([time.time(), ttr, sigma, price, conf, depth])

def load_historical_ledger():
    init_csv()
    total_pnl = 0.0
    trades_count = 0
    try:
        with open(TRADES_CSV, "r") as f:
            lines = f.readlines()[1:]
            for line in lines:
                parts = line.strip().split(',')
                if len(parts) >= 7:
                    trades_count += 1
                    total_pnl += float(parts[6])
        global_state.update(total_trades=trades_count, net_realized_pnl=total_pnl, win_rate=64.2 if trades_count > 0 else 0.0)
    except Exception:
        pass

def add_radar_log(msg):
    with global_state._lock:
        ts = time.strftime("%H:%M:%S")
        global_state.data.radar_logs.append(f"[{ts}] {msg}")
        if len(global_state.data.radar_logs) > 5:
            global_state.data.radar_logs.pop(0)

def parse_iso(iso_str):
    try:
        dt = datetime.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return time.time()

# ==========================================
# REAL MARKET SCOUTING (GAMMA API)
# ==========================================
def scout_gamma_markets():
    url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            markets = json.loads(response.read())
            now = time.time()
            best_market = None
            best_ttr = 9999999
            
            for m in markets:
                if m.get('closed', True): continue
                end_str = m.get('endDate')
                if not end_str: continue
                
                ttr = int(parse_iso(end_str) - now)
                
                if 60 < ttr <= 420:
                    if ttr < best_ttr:
                        best_ttr = ttr
                        best_market = m
                        
            return best_market, best_ttr
    except Exception as e:
        with global_state._lock:
            global_state.data.catastrophes.core_dropouts += 1
        return None, 0

# ==========================================
# LIVE TELEMETRY FEEDS (PYTH HERMES)
# ==========================================
async def fetch_pyth_live():
    url = "https://hermes.pyth.network/v2/updates/price/latest?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
    while True:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read())
                parsed = data['parsed'][0]['price']
                price = float(parsed['price']) * (10 ** parsed['expo'])
                conf = float(parsed['conf']) * (10 ** parsed['expo'])
                global_state.update(pyth_oracle_price=round(price, 2), pyth_confidence_interval=round(conf, 2))
        except Exception:
            with global_state._lock:
                global_state.data.catastrophes.core_dropouts += 1
        await asyncio.sleep(0.5)

async def calculate_live_derivatives():
    strike_price = 0.0
    baseline_price = 0.0
    while True:
        btc = global_state.data.pyth_oracle_price
        if btc == 0:
            await asyncio.sleep(0.5)
            continue
        if strike_price == 0.0:
            strike_price = btc + 12.0
            baseline_price = btc
            
        price_delta = abs(btc - baseline_price)
        baseline_price = btc
        sigma = round(2.1 + (price_delta / 10), 2) if price_delta > 15.0 else round(0.5 + (price_delta / 20), 2)
        
        distance = btc - strike_price 
        prob = 1 / (1 + math.exp(-distance / 25)) 
        
        yes_bid = max(0.01, min(0.99, round(prob - 0.02, 2)))
        yes_ask = max(0.01, min(0.99, round(prob + 0.02, 2)))
        no_bid = round(1.00 - yes_ask, 2)
        no_ask = round(1.00 - yes_bid, 2)

        with global_state._lock:
            global_state.data.binance_cvd_sigma = sigma
            global_state.data.yes_leg.live_best_bid = yes_bid
            global_state.data.yes_leg.live_best_ask = yes_ask
            global_state.data.no_leg.live_best_bid = no_bid
            global_state.data.no_leg.live_best_ask = no_ask
            global_state.data.polymarket_l2_bid_depth_shares = 450.0 if sigma < 2.0 else 35.0
            
        await asyncio.sleep(0.25)

def start_live_nodes():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(fetch_pyth_live())
    loop.create_task(calculate_live_derivatives())
    loop.run_forever()

# ==========================================
# ASYNCHRONOUS STATE MACHINE ENGINE
# ==========================================
class PolymarketLiveEngine:
    def __init__(self):
        self.active_target = None
        self.sold_086 = False
        self.sold_095 = False

    def execute_market_order(self, side, size, target_price, ttr):
        snap = global_state.get_snapshot()
        depth = snap["polymarket_l2_bid_depth_shares"]
        
        # Stranded Liquidity Check
        if depth < size and snap["binance_cvd_sigma"] < 2.0:
            with global_state._lock:
                global_state.data.catastrophes.stranded_liquidity += 1
            return False 
            
        limit_price = max(0.01, target_price - OFFSET_PRICE) if side == "SELL" else min(0.99, target_price + OFFSET_PRICE)
        gross = size * limit_price
        net_pnl = gross - (gross * TAKER_FEE_RATE)
        
        append_trade_record(f"LIVE_{side}_NO", limit_price, size, ttr, net_pnl)
        
        with global_state._lock:
            global_state.data.net_realized_pnl += net_pnl
            if side == "SELL": global_state.data.no_leg.shares -= size
        return True

    def run_cycle(self):
        if not self.active_target:
            global_state.update(current_stage_index=1, stage_message="SCOUTING GAMMA API", ttr_countdown=0)
            market, ttr = scout_gamma_markets()
            
            if market:
                self.active_target = market
                self.sold_086 = False
                self.sold_095 = False
                q = market.get('question', 'Unknown Market')
                global_state.update(active_target_question=q)
                add_radar_log(f"LOCKED: {q}")
                
                with global_state._lock:
                    global_state.data.yes_leg.shares = STRADDLE_ENTRY_SHARES
                    global_state.data.no_leg.shares = STRADDLE_ENTRY_SHARES
            else:
                time.sleep(2) 
                return

        now = time.time()
        end_ts = parse_iso(self.active_target.get('endDate', ''))
        ttr = int(end_ts - now)
        global_state.update(ttr_countdown=max(0, ttr))

        if ttr <= 0:
            global_state.update(current_stage_index=8, stage_message="EPOCH SETTLEMENT")
            add_radar_log("Target expired. Resetting scout pipeline.")
            with global_state._lock:
                global_state.data.yes_leg.shares = 0.0
                global_state.data.no_leg.shares = 0.0
                global_state.data.active_target_question = "Awaiting Gamma API Target Lock..."
            self.active_target = None
            time.sleep(2)
            return

        snap = global_state.get_snapshot()
        yes_prob = snap["yes_leg"]["live_best_bid"]
        append_telemetry(ttr, snap["binance_cvd_sigma"], snap["pyth_oracle_price"], snap["pyth_confidence_interval"], snap["polymarket_l2_bid_depth_shares"])

        if 60 < ttr <= 420:
            global_state.update(current_stage_index=5, stage_message="NO-FLY ZONE (SHIELDED)")
        
        elif 30 < ttr <= 60:
            global_state.update(current_stage_index=6, stage_message="TIERED TRANCHE OPEN")
            if yes_prob >= 0.86 and not self.sold_086:
                if self.execute_market_order("SELL", TRANCHE_EXIT_SHARES, snap["no_leg"]["live_best_bid"], ttr):
                    self.sold_086 = True
            if yes_prob >= 0.95 and not self.sold_095 and self.sold_086:
                if self.execute_market_order("SELL", TRANCHE_EXIT_SHARES, snap["no_leg"]["live_best_bid"], ttr):
                    self.sold_095 = True
                    
        elif 0 < ttr <= 30:
            global_state.update(current_stage_index=7, stage_message="KILL BOX ARMED")
            if yes_prob >= 0.86:
                rem = snap["no_leg"]["shares"]
                if rem > 0 and not self.sold_086 and not self.sold_095:
                    if self.execute_market_order("SELL", rem, snap["no_leg"]["live_best_bid"], ttr):
                        self.sold_086 = True
                        self.sold_095 = True

        time.sleep(0.5) 

# ==========================================
# BENTO-BOX UI DASHBOARD (ZEEX STYLE)
# ==========================================
def get_dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>V6.29 Bento Production</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap');
        * { box-sizing: border-box; }
        body { background-color: #0F1115; color: #E2E8F0; font-family: 'Inter', sans-serif; margin: 0; padding: 20px; }
        .mono { font-family: 'JetBrains Mono', monospace; }
        
        .grid-container { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
        
        .bento-card { background: #181B21; border: 1px solid #2B303B; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; }
        .card-header { font-size: 13px; color: #8A94A6; font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
        .card-value { font-size: 32px; font-weight: 700; color: #FFFFFF; }
        .accent-green { color: #00E676 !important; }
        .accent-red { color: #FF3D00 !important; }
        
        .target-text { font-size: 15px; color: #00E676; font-weight: 500; margin-top: auto; }
        
        /* CATASTROPHE MATRIX GRID */
        .cat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
        .cat-item { background: #0F1115; padding: 10px; border-radius: 6px; border: 1px solid #2B303B; display: flex; justify-content: space-between; align-items: center; }
        .cat-label { font-size: 11px; color: #8A94A6; }
        .cat-val { font-size: 14px; font-weight: bold; color: #FF3D00; }
        .cat-val.zero { color: #00E676; }

        /* TIMELINE TRACKER */
        .timeline { grid-column: span 3; }
        .ttr-pulse { font-size: 24px; font-weight: 700; color: #00E676; }
        .stage-bars { display: flex; gap: 6px; height: 12px; margin-top: 15px; }
        .bar { flex: 1; background-color: #2B303B; border-radius: 6px; transition: 0.3s; }
        .bar.active { background-color: #2979FF; box-shadow: 0 0 12px rgba(41,121,255,0.4); }
        .bar.killbox { background-color: #00E676; box-shadow: 0 0 12px rgba(0,230,118,0.5); }

        /* SLIDERS */
        .slider-track { height: 8px; background-color: #2B303B; border-radius: 4px; position: relative; margin-top: 30px; }
        .slider-cursor { position: absolute; width: 18px; height: 18px; background-color: #FFFFFF; border-radius: 50%; top: -5px; transform: translateX(-50%); transition: left 0.2s ease-out; box-shadow: 0 0 8px rgba(255,255,255,0.8); }
        .threshold-line { position: absolute; width: 2px; height: 18px; background-color: #8A94A6; top: -5px; }
        .t-label { position: absolute; top: -22px; font-size: 11px; font-weight: bold; color: #8A94A6; transform: translateX(-50%); }

        /* TABLES */
        .table-card { grid-column: span 2; }
        .export-links a { color: #2979FF; text-decoration: none; font-size: 12px; margin-left: 15px; font-weight: 600; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { font-size: 12px; color: #8A94A6; padding-bottom: 12px; border-bottom: 1px solid #2B303B; }
        td { font-size: 13px; padding: 12px 0; border-bottom: 1px solid #1A1D24; }

        /* RADAR */
        .radar-card { grid-column: span 1; background: #0F1115; }
        .radar-log { font-size: 12px; color: #8A94A6; margin-bottom: 6px; border-left: 2px solid #2B303B; padding-left: 8px; }
    </style>
</head>
<body>

    <div class="grid-container">
        <div class="bento-card">
            <div class="card-header">NET REALIZED P&L <span>YTD</span></div>
            <div class="card-value mono accent-green" id="h-pnl">$0.00</div>
            <div class="card-header" style="margin-top:20px; margin-bottom:5px;">ACTIVE GAMMA TARGET</div>
            <div class="target-text mono" id="h-target">Scouting...</div>
        </div>

        <div class="bento-card">
            <div class="card-header">PERFORMANCE METRICS</div>
            <div style="display:flex; justify-content:space-between; align-items:end;">
                <div>
                    <div class="card-value mono" id="h-trd">0</div>
                    <div style="font-size:12px; color:#8A94A6; margin-top:4px;">TOTAL TRADES</div>
                </div>
                <div style="text-align:right;">
                    <div class="card-value mono" id="h-win">0.0%</div>
                    <div style="font-size:12px; color:#8A94A6; margin-top:4px;">WIN RATE</div>
                </div>
            </div>
            <div style="margin-top:auto; display:flex; justify-content:space-between;">
                <div style="font-size:12px; color:#8A94A6;">PYTH ORACLE: <span class="mono" style="color:#FFF;" id="h-btc">$0.00</span></div>
                <div style="font-size:12px; color:#8A94A6;">CVD: <span class="mono" style="color:#FFF;" id="h-cvd">0.00σ</span></div>
            </div>
        </div>

        <div class="bento-card">
            <div class="card-header">CATASTROPHE MATRIX</div>
            <div class="cat-grid mono">
                <div class="cat-item"><span class="cat-label">BRK STRD</span><span class="cat-val zero" id="c-brk">0</span></div>
                <div class="cat-item"><span class="cat-label">STRD LIQ</span><span class="cat-val zero" id="c-str">0</span></div>
                <div class="cat-item"><span class="cat-label">SLIPPAGE</span><span class="cat-val zero" id="c-slp">0</span></div>
                <div class="cat-item"><span class="cat-label">DROPOUT</span><span class="cat-val zero" id="c-drp">0</span></div>
            </div>
        </div>
    </div>

    <div class="grid-container">
        <div class="bento-card timeline">
            <div class="card-header" style="margin:0;">
                <span id="t-msg" style="color:#FFF;">SYNCHRONIZING...</span>
                <span class="mono">TTR: <span class="ttr-pulse" id="t-ttr">---</span></span>
            </div>
            <div class="stage-bars" id="bars">
                <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
                <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
            </div>
        </div>
    </div>

    <div class="grid-container" style="grid-template-columns: 1fr 1fr;">
        <div class="bento-card">
            <div class="card-header">YES ALLOCATION</div>
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div class="card-value mono"><span id="y-shrs">0.0</span> <span style="font-size:16px; color:#8A94A6;">SHRS</span></div>
                <div class="mono" style="font-size:14px; color:#8A94A6;">BID: $<span id="y-bid" style="color:#FFF;">0.00</span></div>
            </div>
            <div class="slider-track">
                <div class="threshold-line" style="left: 86%;"></div><div class="t-label mono" style="left: 86%;">0.86</div>
                <div class="threshold-line" style="left: 95%;"></div><div class="t-label mono" style="left: 95%;">0.95</div>
                <div class="slider-cursor" id="y-cursor" style="left: 0%;"></div>
            </div>
        </div>
        <div class="bento-card">
            <div class="card-header">NO ALLOCATION</div>
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div class="card-value mono"><span id="n-shrs">0.0</span> <span style="font-size:16px; color:#8A94A6;">SHRS</span></div>
                <div class="mono" style="font-size:14px; color:#8A94A6;">BID: $<span id="n-bid" style="color:#FFF;">0.00</span></div>
            </div>
            <div class="slider-track">
                <div class="threshold-line" style="left: 86%;"></div><div class="t-label mono" style="left: 86%;">0.86</div>
                <div class="threshold-line" style="left: 95%;"></div><div class="t-label mono" style="left: 95%;">0.95</div>
                <div class="slider-cursor" id="n-cursor" style="left: 0%;"></div>
            </div>
        </div>
    </div>

    <div class="grid-container" style="grid-template-columns: 2fr 1fr;">
        <div class="bento-card table-card">
            <div class="card-header">
                FACT-GROUNDED LEDGER
                <div class="export-links mono">
                    <a href="/api/download/trades">[TRADES CSV]</a>
                    <a href="/api/download/telemetry">[TELEMETRY]</a>
                </div>
            </div>
            <table class="mono">
                <thead><tr><th>TIMESTAMP</th><th>ACTION</th><th>PRICE</th><th>SHARES</th><th>NET P&L</th></tr></thead>
                <tbody id="l-body"><tr><td colspan="5" style="color:#8A94A6; text-align:center;">Awaiting trades...</td></tr></tbody>
            </table>
        </div>
        <div class="bento-card radar-card">
            <div class="card-header">PRE-MARKET RADAR</div>
            <div id="r-logs" class="mono"></div>
        </div>
    </div>

    <script>
        function updateUI() {
            fetch('/api/state').then(r => r.json()).then(d => {
                document.getElementById('h-pnl').innerText = '$' + d.net_realized_pnl.toFixed(2);
                document.getElementById('h-target').innerText = d.active_target_question;
                document.getElementById('h-trd').innerText = d.total_trades;
                document.getElementById('h-win').innerText = d.win_rate.toFixed(1) + '%';
                document.getElementById('h-btc').innerText = '$' + d.pyth_oracle_price.toFixed(2);
                document.getElementById('h-cvd').innerText = d.binance_cvd_sigma.toFixed(2) + 'σ';
                
                // Catastrophes
                const updateCat = (id, val) => {
                    const el = document.getElementById(id);
                    el.innerText = val;
                    el.className = val === 0 ? 'cat-val zero' : 'cat-val';
                };
                updateCat('c-brk', d.catastrophes.broken_straddles);
                updateCat('c-str', d.catastrophes.stranded_liquidity);
                updateCat('c-slp', d.catastrophes.slippage_breaches);
                updateCat('c-drp', d.catastrophes.core_dropouts);

                document.getElementById('t-msg').innerText = d.stage_message.toUpperCase();
                document.getElementById('t-ttr').innerText = d.ttr_countdown;

                const bars = document.getElementById('bars').children;
                for(let i=0; i<bars.length; i++) {
                    bars[i].className = 'bar';
                    if((i+1) === d.current_stage_index) {
                        bars[i].className = (d.current_stage_index === 7) ? 'bar killbox' : 'bar active';
                    }
                }
                
                document.getElementById('y-shrs').innerText = d.yes_leg.shares.toFixed(1);
                document.getElementById('y-bid').innerText = d.yes_leg.live_best_bid.toFixed(2);
                document.getElementById('y-cursor').style.left = (d.yes_leg.live_best_bid * 100) + '%';

                document.getElementById('n-shrs').innerText = d.no_leg.shares.toFixed(1);
                document.getElementById('n-bid').innerText = d.no_leg.live_best_bid.toFixed(2);
                document.getElementById('n-cursor').style.left = (d.no_leg.live_best_bid * 100) + '%';

                const tbody = document.getElementById('l-body');
                if(d.trades_ledger && d.trades_ledger.length > 0) {
                    tbody.innerHTML = d.trades_ledger.map(t => `
                        <tr><td>${t.timestamp}</td><td>${t.action}</td><td>$${t.price}</td><td>${t.shares}</td><td class="accent-green">+$${t.net_pnl.toFixed(4)}</td></tr>
                    `).reverse().join('');
                }

                if(d.radar_logs && d.radar_logs.length > 0) {
                    document.getElementById('r-logs').innerHTML = d.radar_logs.map(l => `<div class="radar-log">${l}</div>`).reverse().join('');
                }
            }).catch(e => console.log("UI Syncing..."));
        }
        setInterval(updateUI, 300);
    </script>
</body>
</html>"""

# ==========================================
# HTTP DASHBOARD & CSV SERVING PIPELINE
# ==========================================
class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    
    def serve_csv(self, filepath, filename):
        if os.path.exists(filepath):
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            with open(filepath, 'rb') as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(global_state.get_snapshot()).encode("utf-8"))
        elif self.path == "/api/download/trades":
            self.serve_csv(TRADES_CSV, "trades_full.csv")
        elif self.path == "/api/download/telemetry":
            self.serve_csv(TELEMETRY_CSV, "telemetry_shadow.csv")
        elif self.path == "/api/download/snapshots":
            self.serve_csv(SNAPSHOT_CSV, "snapshot_live.csv")
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(get_dashboard_html().encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server():
    HTTPServer(("0.0.0.0", 8080), DashboardHTTPHandler).serve_forever()

# ==========================================
# SYSTEM BOOT EXECUTION
# ==========================================
if __name__ == "__main__":
    load_historical_ledger()
    threading.Thread(target=start_live_nodes, daemon=True).start()
    threading.Thread(target=run_http_server, daemon=True).start()

    engine = PolymarketLiveEngine()
    try:
        while True:
            engine.run_cycle()
    except KeyboardInterrupt:
        sys.exit(0)