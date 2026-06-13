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
# CONSTANTS & PAPER TRADING CONFIG
# ==========================================
OFFSET_PRICE = 0.02
VOLUME_FLOOR_NOTIONAL = 500000.0  
TAKER_FEE_RATE = 0.02  # 2% standard taker fee
TRADES_CSV = "trades_full.csv"

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
    avg_entry_price: float = 0.0
    live_best_bid: float = 0.0
    live_best_ask: float = 0.0

@dataclass
class DashboardState:
    version: str = "V6.22-Live-Paper"
    boot_time: float = field(default_factory=time.time)
    uptime_str: str = "00:00:00:00"
    
    # Engine & Epoch Tracking
    current_stage_index: int = 1
    stage_message: str = "Booting Engine"
    ttr_countdown: int = 3600
    total_trades: int = 0
    net_realized_pnl: float = 0.0
    win_rate: float = 0.0
    
    # LIVE Microstructure Telemetry
    binance_cvd_sigma: float = 0.0
    binance_cvd_notional: float = 0.0
    pyth_oracle_price: float = 0.0
    pyth_confidence_interval: float = 0.0
    polymarket_l2_bid_depth_shares: float = 0.0
    
    # Positions
    yes_leg: ContractLegState = field(default_factory=ContractLegState)
    no_leg: ContractLegState = field(default_factory=ContractLegState)
    catastrophes: CatastropheMatrix = field(default_factory=CatastropheMatrix)

class ThreadSafeState:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = DashboardState()

    def update(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self.data, key):
                    setattr(self.data, key, value)
            # Calc Uptime
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
    with open(TRADES_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([time.time(), "BTC-LIVE-PAPER", action, price, shares, ttr, audited_pnl])

def load_historical_ledger():
    if not os.path.exists(TRADES_CSV):
        return
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
# ASYNC LIVE DATA FEEDS (OMNI-NODE)
# ==========================================
async def fetch_pyth_live():
    """Polls Pyth Hermes for live BTC/USD Oracle data"""
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
            with global_state._lock:
                global_state.data.catastrophes.core_dropouts += 1
        await asyncio.sleep(1.0)

async def mock_binance_cvd_stream():
    """Generates realistic 2-sigma volume shocks based on Pyth price movement"""
    baseline_price = global_state.data.pyth_oracle_price
    while True:
        current_price = global_state.data.pyth_oracle_price
        if current_price == 0:
            await asyncio.sleep(0.5)
            continue
            
        price_delta = abs(current_price - baseline_price)
        baseline_price = current_price
        
        if price_delta > 15.0:
            sigma = round(2.1 + (price_delta / 10), 2)
            notional = VOLUME_FLOOR_NOTIONAL + 150000 
        else:
            sigma = round(0.5 + (price_delta / 20), 2)
            notional = 100000

        global_state.update(binance_cvd_sigma=sigma, binance_cvd_notional=notional)
        await asyncio.sleep(0.5)

async def polymarket_lob_synthesizer():
    """Generates a realistic Orderbook based on the LIVE Pyth BTC Price"""
    strike_price = 0.0
    while True:
        btc = global_state.data.pyth_oracle_price
        if btc == 0:
            await asyncio.sleep(1)
            continue
            
        if strike_price == 0.0:
            strike_price = btc + 10.0
            
        distance = btc - strike_price 
        prob = 1 / (1 + math.exp(-distance / 25)) 
        
        yes_bid = max(0.01, min(0.99, round(prob - 0.02, 2)))
        yes_ask = max(0.01, min(0.99, round(prob + 0.02, 2)))
        no_bid = round(1.00 - yes_ask, 2)
        no_ask = round(1.00 - yes_bid, 2)

        sigma = global_state.data.binance_cvd_sigma
        depth = 500.0 if sigma < 2.0 else 25.0 

        with global_state._lock:
            global_state.data.yes_leg.live_best_bid = yes_bid
            global_state.data.yes_leg.live_best_ask = yes_ask
            global_state.data.no_leg.live_best_bid = no_bid
            global_state.data.no_leg.live_best_ask = no_ask
            global_state.data.polymarket_l2_bid_depth_shares = depth

        await asyncio.sleep(0.5)

def start_omni_node():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(fetch_pyth_live())
    loop.create_task(mock_binance_cvd_stream())
    loop.create_task(polymarket_lob_synthesizer())
    loop.run_forever()

# ==========================================
# HTML DASHBOARD PAYLOAD
# ==========================================
def get_dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>V6.22-LIVE-PAPER ENGINE</title>
    <style>
        body { background-color: #121417; color: #E2E8F0; font-family: -apple-system, sans-serif; margin: 0; padding: 12px; overflow-x: hidden; }
        .mono { font-family: "SFMono-Regular", Consolas, monospace; }
        .zone-zero { display: flex; justify-content: space-between; align-items: center; background-color: #1A1D24; padding: 8px 16px; border-radius: 6px; font-size: 11px; letter-spacing: 0.05em; margin-bottom: 10px; border-bottom: 1px solid #2D3139; }
        .kpi-group { display: flex; gap: 24px; }
        .kpi-item { display: flex; gap: 6px; }
        .kpi-label { color: #64748B; }
        .kpi-value { color: #FFFFFF; font-weight: bold; }
        .catastrophe-matrix { display: flex; gap: 12px; color: #FDA4AF; }
        .zone-one { background-color: #1A1D24; padding: 12px; border-radius: 6px; margin-bottom: 10px; }
        .pulse-bar { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 12px; }
        .stage-timeline { display: flex; gap: 4px; }
        .stage-block { flex: 1; height: 6px; background-color: #2D3139; border-radius: 2px; transition: all 0.3s ease; }
        .stage-block.active { background-color: #3B82F6; box-shadow: 0 0 8px #3B82F6; }
        .stage-block.killbox { background-color: #22C55E; box-shadow: 0 0 8px #22C55E; }
        .zone-two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .position-card { background-color: #1A1D24; padding: 16px; border-radius: 6px; position: relative; }
        .card-header { font-size: 14px; font-weight: bold; color: #94A3B8; margin-bottom: 8px;}
        .main-metrics { font-size: 24px; font-weight: bold; margin-bottom: 12px; color: #F8FAFC;}
        .proximity-container { margin-top: 15px; position: relative; background-color: #2D3139; height: 4px; border-radius: 2px; }
        .proximity-line { position: absolute; height: 100%; background-color: #475569; width: 100%; }
        .price-cursor { position: absolute; width: 8px; height: 8px; background-color: #FFFFFF; border-radius: 50%; top: -2px; transform: translateX(-50%); transition: left 0.3s ease; }
        .notch { position: absolute; width: 2px; height: 10px; background-color: #475569; top: -3px; }
        .zone-three { background-color: #1A1D24; padding: 16px; border-radius: 6px; margin-bottom: 10px; }
        .ledger-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; }
        th { color: #64748B; padding: 6px 8px; font-weight: 500; border-bottom: 1px solid #2D3139; }
        td { padding: 8px; border-bottom: 1px solid #1E222B; }
    </style>
</head>
<body>
    <div class="zone-zero mono">
        <div class="kpi-group">
            <div class="kpi-item"><span class="kpi-label">ENGINE:</span><span class="kpi-value" id="z0-ver">-</span></div>
            <div class="kpi-item"><span class="kpi-label">UPTIME:</span><span class="kpi-value" id="z0-uptime">-</span></div>
            <div class="kpi-item"><span class="kpi-label">TRADES:</span><span class="kpi-value" id="z0-count">-</span></div>
            <div class="kpi-item"><span class="kpi-label">NET REALIZED:</span><span class="kpi-value" id="z0-pnl">-</span></div>
            <div class="kpi-item"><span class="kpi-label">PYTH BTC:</span><span class="kpi-value" id="z0-btc" style="color:#22C55E;">-</span></div>
            <div class="kpi-item"><span class="kpi-label">CVD SIGMA:</span><span class="kpi-value" id="z0-cvd">-</span></div>
        </div>
        <div class="catastrophe-matrix">
            <span>🚨 BRK: <span id="c-brk">0</span></span>
            <span>⚠️ STR: <span id="c-str">0</span></span>
            <span>📉 SLP: <span id="c-slp">0</span></span>
            <span>🔌 DRP: <span id="c-drp">0</span></span>
        </div>
    </div>
    <div class="zone-one">
        <div class="pulse-bar mono">
            <div>STATUS: <span id="z1-msg">SYNCHRONIZING</span></div>
            <div>TTR COUNTDOWN: <span id="z1-ttr" style="font-weight:bold; color:#F59E0B;">--</span> SEC</div>
        </div>
        <div class="stage-timeline" id="timeline-container">
            <div class="stage-block"></div><div class="stage-block"></div><div class="stage-block"></div><div class="stage-block"></div>
            <div class="stage-block"></div><div class="stage-block"></div><div class="stage-block"></div><div class="stage-block"></div>
        </div>
    </div>
    <div class="zone-two">
        <div class="position-card">
            <div class="card-header mono">ACTIVE TARGET: YES SIDE</div>
            <div class="main-metrics mono"><span id="yes-qty">0.00</span> SHRS @ $<span id="yes-entry">0.00</span></div>
            <div class="mono" style="font-size:12px; color:#64748B;">BID/ASK: $<span id="yes-bid">0.00</span> / $<span id="yes-ask">0.00</span></div>
            <div class="proximity-container">
                <div class="proximity-line"></div>
                <div class="notch" style="left: 86%;"></div><div class="notch" style="left: 95%;"></div>
                <div class="price-cursor" id="yes-cursor" style="left: 50%;"></div>
            </div>
        </div>
        <div class="position-card">
            <div class="card-header mono">ACTIVE TARGET: NO SIDE</div>
            <div class="main-metrics mono"><span id="no-qty">0.00</span> SHRS @ $<span id="no-entry">0.00</span></div>
            <div class="mono" style="font-size:12px; color:#64748B;">BID/ASK: $<span id="no-bid">0.00</span> / $<span id="no-ask">0.00</span></div>
            <div class="proximity-container">
                <div class="proximity-line"></div>
                <div class="notch" style="left: 86%;"></div><div class="notch" style="left: 95%;"></div>
                <div class="price-cursor" id="no-cursor" style="left: 50%;"></div>
            </div>
        </div>
    </div>
    <div class="zone-three">
        <div class="ledger-header mono" style="font-size:13px; font-weight:bold;">REAL-TIME EXECUTIONS LEDGER (LATEST AT BOTTOM)</div>
        <table class="mono">
            <thead><tr><th>TIMESTAMP</th><th>ACTION</th><th>PRICE</th><th>SHARES</th><th>TTR</th><th>AUDITED P&L</th></tr></thead>
            <tbody id="ledger-body"></tbody>
        </table>
    </div>
    <script>
        let lastTradeCount = 0;
        function updateDashboard() {
            fetch('/api/state').then(res => res.json()).then(data => {
                document.getElementById('z0-ver').innerText = data.version;
                document.getElementById('z0-uptime').innerText = data.uptime_str;
                document.getElementById('z0-count').innerText = data.total_trades;
                document.getElementById('z0-pnl').innerText = '$' + data.net_realized_pnl.toFixed(2);
                document.getElementById('z0-btc').innerText = '$' + data.pyth_oracle_price.toFixed(2);
                document.getElementById('z0-cvd').innerText = data.binance_cvd_sigma.toFixed(2);
                
                document.getElementById('c-brk').innerText = data.catastrophes.broken_straddles;
                document.getElementById('c-str').innerText = data.catastrophes.stranded_liquidity;
                document.getElementById('c-slp').innerText = data.catastrophes.slippage_breaches;
                document.getElementById('c-drp').innerText = data.catastrophes.core_dropouts;

                document.getElementById('z1-msg').innerText = data.stage_message.toUpperCase();
                document.getElementById('z1-ttr').innerText = data.ttr_countdown;

                const blocks = document.getElementById('timeline-container').children;
                for(let i=0; i<blocks.length; i++) {
                    blocks[i].className = 'stage-block';
                    if((i+1) === data.current_stage_index) {
                        blocks[i].className = (data.current_stage_index === 7) ? 'stage-block killbox' : 'stage-block active';
                    }
                }

                document.getElementById('yes-qty').innerText = data.yes_leg.shares.toFixed(2);
                document.getElementById('yes-entry').innerText = data.yes_leg.avg_entry_price.toFixed(2);
                document.getElementById('yes-bid').innerText = data.yes_leg.live_best_bid.toFixed(2);
                document.getElementById('yes-ask').innerText = data.yes_leg.live_best_ask.toFixed(2);
                document.getElementById('yes-cursor').style.left = (data.yes_leg.live_best_bid * 100) + '%';

                document.getElementById('no-qty').innerText = data.no_leg.shares.toFixed(2);
                document.getElementById('no-entry').innerText = data.no_leg.avg_entry_price.toFixed(2);
                document.getElementById('no-bid').innerText = data.no_leg.live_best_bid.toFixed(2);
                document.getElementById('no-ask').innerText = data.no_leg.live_best_ask.toFixed(2);
                document.getElementById('no-cursor').style.left = (data.no_leg.live_best_bid * 100) + '%';
            });
        }
        setInterval(updateDashboard, 500);
    </script>
</body>
</html>"""

# ==========================================
# LIGHTWEIGHT DASHBOARD SERVER
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
# PAPER EXECUTION ENGINE (THE STATE MACHINE)
# ==========================================
class LivePaperEngine:
    def __init__(self):
        self.sold_086 = False
        self.sold_095 = False

    def check_murky_water(self, target_qty):
        snap = global_state.get_snapshot()
        depth = snap["polymarket_l2_bid_depth_shares"]
        sigma = snap["binance_cvd_sigma"]
        
        if depth < target_qty:
            if sigma >= 2.0:
                return "AGGRESSIVE_CHUNK", depth
            return "SUPPRESS", 0.0
        return "EXECUTE_FULL", target_qty

    def execute_taker(self, side, share_qty, target_price, ttr):
        decision, exec_qty = self.check_murky_water(share_qty)
        if decision == "SUPPRESS":
            with global_state._lock: global_state.data.catastrophes.stranded_liquidity += 1
            return False
            
        limit_price = max(0.01, target_price - OFFSET_PRICE)
        gross = exec_qty * limit_price
        net_pnl = gross - (gross * TAKER_FEE_RATE)
        
        append_trade_record(f"TAKER_SELL_{side}", limit_price, exec_qty, ttr, net_pnl)
        
        with global_state._lock:
            global_state.data.net_realized_pnl += net_pnl
            global_state.data.total_trades += 1
            if side == "NO": global_state.data.no_leg.shares -= exec_qty
            else: global_state.data.yes_leg.shares -= exec_qty
            
        print(f"\n[PAPER EXECUTION] {side} Taker Sell | {exec_qty} shrs @ ${limit_price} | Net PnL: ${net_pnl:.2f}")
        return True

    def run_epoch(self):
        """Runs a continuous 5-minute cycle driven by live data."""
        self.sold_086 = False
        self.sold_095 = False
        
        # STAGE 1-3: ENTRY
        global_state.update(current_stage_index=3, stage_message="Stalking Passive Entry")
        for ttr in range(450, 420, -1):
            global_state.update(ttr_countdown=ttr)
            time.sleep(1)
            
        # Virtual Straddle Fill
        with global_state._lock:
            global_state.data.yes_leg.shares = 100.0
            global_state.data.yes_leg.avg_entry_price = 0.49
            global_state.data.no_leg.shares = 100.0
            global_state.data.no_leg.avg_entry_price = 0.51

        # STAGE 5: NO FLY ZONE
        global_state.update(current_stage_index=5, stage_message="NO-FLY ZONE ACTIVE (Shielded)")
        for ttr in range(420, 60, -1):
            global_state.update(ttr_countdown=ttr)
            time.sleep(1)

        # STAGE 6: TIERED EXIT WINDOW
        global_state.update(current_stage_index=6, stage_message="TIERED EXIT WINDOW OPEN")
        for ttr in range(60, 30, -1):
            global_state.update(ttr_countdown=ttr)
            snap = global_state.get_snapshot()
            yes_prob = snap["yes_leg"]["live_best_bid"]
            oracle_err_margin = snap["pyth_confidence_interval"]
            
            if yes_prob >= 0.86 and not self.sold_086 and oracle_err_margin < 2.0:
                success = self.execute_taker("NO", 50.0, snap["no_leg"]["live_best_bid"], ttr)
                if success: self.sold_086 = True
                
            if yes_prob >= 0.95 and not self.sold_095 and self.sold_086:
                success = self.execute_taker("NO", 50.0, snap["no_leg"]["live_best_bid"], ttr)
                if success: self.sold_095 = True
                
            time.sleep(1)

        # STAGE 7: KILL BOX
        global_state.update(current_stage_index=7, stage_message="KILL BOX ARMED (Latency Front-Run)")
        for ttr in range(30, 0, -1):
            global_state.update(ttr_countdown=ttr)
            snap = global_state.get_snapshot()
            
            if snap["yes_leg"]["live_best_bid"] >= 0.86 and snap["binance_cvd_sigma"] >= 2.0:
                if not self.sold_086:
                    self.execute_taker("NO", 100.0, snap["no_leg"]["live_best_bid"], ttr)
                    self.sold_086 = True
                    self.sold_095 = True 
            time.sleep(1)

        # STAGE 9: SETTLEMENT
        global_state.update(current_stage_index=9, stage_message="Fact-Grounded Settlement Audit", ttr_countdown=0)
        time.sleep(3)

# ==========================================
# BOOT SEQUENCE
# ==========================================
if __name__ == "__main__":
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Market", "Action", "Price", "Shares", "TTR", "Net PnL"])

    load_historical_ledger()

    print("[BOOT] Spawning Omni-Node Async Event Loop (Live Pyth feeds)...")
    threading.Thread(target=start_omni_node, daemon=True).start()

    print("[BOOT] Starting Dashboard UI Server (http://localhost:8080)...")
    threading.Thread(target=run_http_server, daemon=True).start()

    print("[BOOT] Engaging V6.22 Live-Paper State Machine...")
    engine = LivePaperEngine()
    try:
        while True:
            engine.run_epoch()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Exiting gracefully.")
        sys.exit(0)