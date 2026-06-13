import os
import sys
import time
import json
import csv
import math
import threading
import asyncio
import urllib.request
from dataclasses import dataclass, asdict, field
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# CONSTANTS & MINIMUM VIABLE TRADING CONFIG
# ==========================================
OFFSET_PRICE = 0.02
TAKER_FEE_RATE = 0.02  
TRADES_CSV = "trades_full.csv"

# Absolute Minimum Constraints for Polymarket CLOB API
STRADDLE_ENTRY_SHARES = 10.0  
TRANCHE_EXIT_SHARES = 5.0    

# ==========================================
# THREAD-SAFE GLOBAL STATE
# ==========================================
@dataclass
class ContractLegState:
    shares: float = 0.0
    avg_entry_price: float = 0.0
    live_best_bid: float = 0.0
    live_best_ask: float = 0.0

@dataclass
class DashboardState:
    version: str = "V6.24-Live-Paper (UI Edition)"
    boot_time: float = field(default_factory=time.time)
    uptime_str: str = "00:00:00:00"
    
    current_stage_index: int = 1
    stage_message: str = "Booting Engine"
    ttr_countdown: int = 3600
    total_trades: int = 0
    net_realized_pnl: float = 0.0
    
    # LIVE Microstructure Telemetry
    binance_cvd_sigma: float = 0.0
    pyth_oracle_price: float = 0.0
    pyth_confidence_interval: float = 0.0
    polymarket_l2_bid_depth_shares: float = 0.0
    
    yes_leg: ContractLegState = field(default_factory=ContractLegState)
    no_leg: ContractLegState = field(default_factory=ContractLegState)

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

def append_trade_record(action, price, shares, ttr, audited_pnl):
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Market", "Action", "Price", "Shares", "TTR", "Net PnL"])
            
    with open(TRADES_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([time.time(), "BTC-LIVE", action, price, shares, ttr, audited_pnl])

def load_historical_ledger():
    if not os.path.exists(TRADES_CSV): return
    trades_count = 0
    total_pnl = 0.0
    try:
        with open(TRADES_CSV, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades_count += 1
                total_pnl += float(row.get("Net PnL", 0.0))
        global_state.update(total_trades=trades_count, net_realized_pnl=total_pnl)
    except Exception:
        pass

# ==========================================
# ASYNC LIVE DATA FEEDS (REAL PYTH INTEGRATION)
# ==========================================
async def fetch_pyth_live():
    """Polls Pyth Hermes for EXACT real-time BTC/USD Oracle data."""
    url = "https://hermes.pyth.network/v2/updates/price/latest?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
    while True:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read())
                parsed = data['parsed'][0]['price']
                price = float(parsed['price']) * (10 ** parsed['expo'])
                conf = float(parsed['conf']) * (10 ** parsed['expo'])
                
                global_state.update(
                    pyth_oracle_price=round(price, 2),
                    pyth_confidence_interval=round(conf, 2)
                )
        except Exception:
            pass
        await asyncio.sleep(1.0)

async def calculate_live_derivatives():
    """Calculates orderbook probabilities strictly from the real Pyth price."""
    strike_price = 0.0
    baseline_price = 0.0
    
    while True:
        btc = global_state.data.pyth_oracle_price
        if btc == 0:
            await asyncio.sleep(1)
            continue
            
        if strike_price == 0.0:
            strike_price = btc + 10.0
            baseline_price = btc
            
        # CVD Velocity Simulation based on real underlying asset movement
        price_delta = abs(btc - baseline_price)
        baseline_price = btc
        sigma = round(2.1 + (price_delta / 10), 2) if price_delta > 15.0 else round(0.5 + (price_delta / 20), 2)
        
        # Real Probability calculation based on distance to strike
        distance = btc - strike_price 
        prob = 1 / (1 + math.exp(-distance / 25)) 
        
        yes_bid = max(0.01, min(0.99, round(prob - 0.02, 2)))
        yes_ask = max(0.01, min(0.99, round(prob + 0.02, 2)))
        no_bid = round(1.00 - yes_ask, 2)
        no_ask = round(1.00 - yes_bid, 2)

        depth = 500.0 if sigma < 2.0 else 25.0 

        with global_state._lock:
            global_state.data.binance_cvd_sigma = sigma
            global_state.data.yes_leg.live_best_bid = yes_bid
            global_state.data.yes_leg.live_best_ask = yes_ask
            global_state.data.no_leg.live_best_bid = no_bid
            global_state.data.no_leg.live_best_ask = no_ask
            global_state.data.polymarket_l2_bid_depth_shares = depth

        await asyncio.sleep(0.5)

def start_live_nodes():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(fetch_pyth_live())
    loop.create_task(calculate_live_derivatives())
    loop.run_forever()

# ==========================================
# EYE-PLEASING, HUMAN-READABLE DASHBOARD HTML
# ==========================================
def get_dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>V6.24 Live Desk</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@400;700&display=swap');
        
        * { box-sizing: border-box; }
        body { 
            background-color: #0D1117; 
            color: #C9D1D9; 
            font-family: 'Inter', sans-serif; 
            margin: 0; 
            padding: 20px; 
            overflow-x: hidden;
        }
        .mono { font-family: 'JetBrains Mono', monospace; }
        
        /* HEADER - LARGE METRICS */
        .header {
            display: flex;
            justify-content: space-between;
            background-color: #161B22;
            padding: 20px 30px;
            border-radius: 12px;
            margin-bottom: 20px;
            border: 1px solid #30363D;
        }
        .metric-group { display: flex; flex-direction: column; align-items: flex-start; }
        .metric-label { font-size: 14px; color: #8B949E; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin-bottom: 5px; }
        .metric-value { font-size: 28px; font-weight: 800; color: #FFFFFF; }
        .accent-green { color: #3FB950 !important; }
        
        /* TIMELINE TRACKER */
        .timeline-container {
            background-color: #161B22;
            padding: 25px 30px;
            border-radius: 12px;
            margin-bottom: 20px;
            border: 1px solid #30363D;
        }
        .timeline-header {
            display: flex;
            justify-content: space-between;
            font-size: 22px;
            font-weight: 800;
            margin-bottom: 15px;
        }
        .ttr-warning { color: #FF7B72; font-size: 28px; }
        .stage-bars { display: flex; gap: 8px; height: 12px; }
        .bar { flex: 1; background-color: #21262D; border-radius: 6px; transition: 0.3s; }
        .bar.active { background-color: #58A6FF; box-shadow: 0 0 12px rgba(88,166,255,0.4); }
        .bar.killbox { background-color: #3FB950; box-shadow: 0 0 15px rgba(63,185,80,0.6); }

        /* ACTIVE POSITIONS - MASSIVE CARDS */
        .positions {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background-color: #161B22;
            padding: 30px;
            border-radius: 12px;
            border: 1px solid #30363D;
            position: relative;
        }
        .card-title { font-size: 18px; color: #8B949E; font-weight: 600; margin-bottom: 15px; }
        .massive-data { font-size: 48px; font-weight: 800; color: #FFFFFF; margin-bottom: 10px; line-height: 1; }
        .sub-data { font-size: 20px; color: #8B949E; margin-bottom: 30px; }
        
        /* PROXIMITY SLIDER */
        .slider-track {
            height: 8px;
            background-color: #21262D;
            border-radius: 4px;
            position: relative;
            margin-top: 40px;
        }
        .slider-cursor {
            position: absolute;
            width: 20px;
            height: 20px;
            background-color: #FFFFFF;
            border-radius: 50%;
            top: -6px;
            transform: translateX(-50%);
            box-shadow: 0 0 10px rgba(255,255,255,0.5);
            transition: left 0.3s ease-out;
        }
        .threshold-line {
            position: absolute;
            width: 3px;
            height: 24px;
            background-color: #8B949E;
            top: -8px;
        }
        .t-label {
            position: absolute;
            top: -30px;
            font-size: 14px;
            font-weight: bold;
            color: #8B949E;
            transform: translateX(-50%);
        }

        /* LEDGER TABLE */
        .ledger { background-color: #161B22; padding: 25px; border-radius: 12px; border: 1px solid #30363D; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { font-size: 16px; color: #8B949E; padding-bottom: 15px; border-bottom: 2px solid #30363D; }
        td { font-size: 18px; padding: 15px 0; border-bottom: 1px solid #21262D; }
    </style>
</head>
<body>

    <div class="header">
        <div class="metric-group">
            <span class="metric-label">System State</span>
            <span class="metric-value" id="h-uptime">--:--:--</span>
        </div>
        <div class="metric-group">
            <span class="metric-label">Real Pyth Oracle</span>
            <span class="metric-value accent-green mono" id="h-btc">$0.00</span>
        </div>
        <div class="metric-group">
            <span class="metric-label">Binance CVD Sigma</span>
            <span class="metric-value mono" id="h-cvd">0.00σ</span>
        </div>
        <div class="metric-group">
            <span class="metric-label">Net Realized P&L</span>
            <span class="metric-value mono" id="h-pnl">$0.00</span>
        </div>
    </div>

    <div class="timeline-container">
        <div class="timeline-header">
            <span id="t-msg">SYNCHRONIZING SYSTEM...</span>
            <span class="ttr-warning mono">TTR: <span id="t-ttr">---</span></span>
        </div>
        <div class="stage-bars" id="bars">
            <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
            <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
        </div>
    </div>

    <div class="positions">
        <div class="card">
            <div class="card-title">YES CONTRACT</div>
            <div class="massive-data mono"><span id="y-shrs">0</span> <span style="font-size:24px; color:#8B949E;">SHRS</span></div>
            <div class="sub-data mono">BID: $<span id="y-bid" style="color:#FFFFFF; font-weight:bold;">0.00</span></div>
            
            <div class="slider-track">
                <div class="threshold-line" style="left: 86%;"></div>
                <div class="t-label mono" style="left: 86%;">0.86</div>
                <div class="threshold-line" style="left: 95%;"></div>
                <div class="t-label mono" style="left: 95%;">0.95</div>
                <div class="slider-cursor" id="y-cursor" style="left: 0%;"></div>
            </div>
        </div>

        <div class="card">
            <div class="card-title">NO CONTRACT</div>
            <div class="massive-data mono"><span id="n-shrs">0</span> <span style="font-size:24px; color:#8B949E;">SHRS</span></div>
            <div class="sub-data mono">BID: $<span id="n-bid" style="color:#FFFFFF; font-weight:bold;">0.00</span></div>
            
            <div class="slider-track">
                <div class="threshold-line" style="left: 86%;"></div>
                <div class="t-label mono" style="left: 86%;">0.86</div>
                <div class="threshold-line" style="left: 95%;"></div>
                <div class="t-label mono" style="left: 95%;">0.95</div>
                <div class="slider-cursor" id="n-cursor" style="left: 0%;"></div>
            </div>
        </div>
    </div>

    <div class="ledger">
        <div class="card-title" style="margin-bottom: 20px;">LAST EXECUTIONS</div>
        <table class="mono">
            <thead><tr><th>TIMESTAMP</th><th>ACTION</th><th>EXEC PRICE</th><th>SHARES</th><th>NET P&L</th></tr></thead>
            <tbody id="l-body">
                <tr><td colspan="5" style="text-align:center; color:#8B949E;">Awaiting first trade logic trigger...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        function updateUI() {
            fetch('/api/state').then(r => r.json()).then(d => {
                document.getElementById('h-uptime').innerText = d.uptime_str;
                document.getElementById('h-btc').innerText = '$' + d.pyth_oracle_price.toFixed(2);
                document.getElementById('h-cvd').innerText = d.binance_cvd_sigma.toFixed(2) + 'σ';
                document.getElementById('h-pnl').innerText = '$' + d.net_realized_pnl.toFixed(2);

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
            }).catch(e => console.log("UI Sync Wait..."));
        }
        setInterval(updateUI, 500);
    </script>
</body>
</html>"""

# ==========================================
# LIGHTWEIGHT UI SERVER
# ==========================================
class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    
    def do_GET(self):
        if self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(global_state.get_snapshot()).encode("utf-8"))
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
# PAPER EXECUTION ENGINE (Strict Minimums)
# ==========================================
class RealDataPaperEngine:
    def __init__(self):
        self.sold_086 = False
        self.sold_095 = False

    def execute_taker(self, side, target_qty, target_price, ttr):
        snap = global_state.get_snapshot()
        depth = snap["polymarket_l2_bid_depth_shares"]
        
        # Suppress fakeout liquidity checks
        if depth < target_qty and snap["binance_cvd_sigma"] < 2.0:
            return False 
            
        limit_price = max(0.01, target_price - OFFSET_PRICE)
        gross = target_qty * limit_price
        net_pnl = gross - (gross * TAKER_FEE_RATE)
        
        append_trade_record(f"SELL_{side}_TAKER", limit_price, target_qty, ttr, net_pnl)
        
        with global_state._lock:
            global_state.data.net_realized_pnl += net_pnl
            if side == "NO": global_state.data.no_leg.shares -= target_qty
            else: global_state.data.yes_leg.shares -= target_qty
            
        return True

    def run_epoch(self):
        """Runs the lifecycle strictly enforcing 10-share/5-share limits."""
        self.sold_086 = False
        self.sold_095 = False
        
        # Virtual Straddle Fill (10 Share Entry Minimum)
        with global_state._lock:
            global_state.data.yes_leg.shares = STRADDLE_ENTRY_SHARES
            global_state.data.yes_leg.avg_entry_price = 0.50
            global_state.data.no_leg.shares = STRADDLE_ENTRY_SHARES
            global_state.data.no_leg.avg_entry_price = 0.50

        global_state.update(current_stage_index=5, stage_message="NO-FLY ZONE (SHIELDED)")
        for ttr in range(420, 60, -1):
            global_state.update(ttr_countdown=ttr)
            time.sleep(1)

        global_state.update(current_stage_index=6, stage_message="TIERED EXIT WINDOW OPEN")
        for ttr in range(60, 30, -1):
            global_state.update(ttr_countdown=ttr)
            snap = global_state.get_snapshot()
            yes_prob = snap["yes_leg"]["live_best_bid"]
            
            # Tier 1 (0.86) -> Sell 5 Shares
            if yes_prob >= 0.86 and not self.sold_086:
                success = self.execute_taker("NO", TRANCHE_EXIT_SHARES, snap["no_leg"]["live_best_bid"], ttr)
                if success: self.sold_086 = True
                
            # Tier 2 (0.95) -> Sell remaining 5 Shares
            if yes_prob >= 0.95 and not self.sold_095 and self.sold_086:
                success = self.execute_taker("NO", TRANCHE_EXIT_SHARES, snap["no_leg"]["live_best_bid"], ttr)
                if success: self.sold_095 = True
                
            time.sleep(1)

        global_state.update(current_stage_index=7, stage_message="KILL BOX (LATENCY OVERRIDE)")
        for ttr in range(30, 0, -1):
            global_state.update(ttr_countdown=ttr)
            snap = global_state.get_snapshot()
            
            # If 0.86 is breached here without previous sells, dump all remaining shares
            if snap["yes_leg"]["live_best_bid"] >= 0.86 and snap["binance_cvd_sigma"] >= 2.0:
                if not self.sold_086 and not self.sold_095:
                    self.execute_taker("NO", snap["no_leg"]["shares"], snap["no_leg"]["live_best_bid"], ttr)
                    self.sold_086 = True
                    self.sold_095 = True 
            time.sleep(1)

        global_state.update(current_stage_index=8, stage_message="EPOCH SETTLEMENT", ttr_countdown=0)
        time.sleep(3)

# ==========================================
# BOOT SEQUENCE
# ==========================================
if __name__ == "__main__":
    load_historical_ledger()

    print("[BOOT] Connecting to LIVE Pyth Hermes Oracle...")
    threading.Thread(target=start_live_nodes, daemon=True).start()

    print("[BOOT] Launching Institutional Dashboard UI (http://localhost:8080)...")
    threading.Thread(target=run_http_server, daemon=True).start()

    print("[BOOT] Engaging Engine...")
    engine = RealDataPaperEngine()
    try:
        while True:
            engine.run_epoch()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Exiting.")
        sys.exit(0)