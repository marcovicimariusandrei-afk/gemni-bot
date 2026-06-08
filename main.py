"""
main.py — Opportunistic BSS Bot (v5.8.17 Shadow Mode Telemetry)
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

BASE_CAPITAL_PER_LEG = 5.1  
TAKER_FEE_RATE = 0.018 

# Deadline Strategy Config
HEDGE_DEADLINE_TTR = 320
MAX_COMBINED_COST = 1.02

# Hybrid Tranche Exit Config
SELL_LOSER_T1_THRESH = 0.86
SELL_LOSER_T1_TTR_MAX = 60
SELL_LOSER_T2_THRESH = 0.95

LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "60"))
PORT = int(os.getenv("PORT", "8080"))

# Uptime Tracker
SYSTEM_BOOT_TIME = time.time()

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
        
        self.t1_executed = False
        self.t1_side = ""
        self.t1_price = 0.0
        self.t1_time = ""
        
        self.t2_side = ""
        self.t2_price = 0.0
        self.t2_time = ""
        
        self.salvage_revenue = 0.0
        self.realized_pnl = 0.0
        
        self.close_time = ""
        self.close_reason = ""
        
        self.history_yes: List[float] = []
        self.history_no: List[float] = []

class OrderBook:
    def __init__(self):
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}

    @property
    def bid(self):
        return max(self.bids.keys()) if self.bids else 0.0

    @property
    def ask(self):
        return min(self.asks.keys()) if self.asks else 0.0

    def get_local_vols(self, current_price: float, side: str, depth: float = 0.10) -> float:
        vol = 0.0
        if side == "bid":
            for p, s in self.bids.items():
                if p >= current_price - depth:
                    vol += s
        else:
            for p, s in self.asks.items():
                if p <= current_price + depth:
                    vol += s
        return vol

class BotState:
    def __init__(self):
        self.running = True
        self.markets: Dict[str, MarketData] = {}
        self.books: Dict[str, OrderBook] = {}
        self.ws_connected = False
        self.ws_handle = None
        self.total_pnl = 0.0
        self.total_trades = 0 
        self.sold_losers = 0
        self.catastrophes = 0

GLOBAL_STATE = BotState()

# ─── ASYNC CSV LOGGING SYSTEM ───
def init_csv():
    if not os.path.exists("trades_full.csv"):
        with open("trades_full.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "Action", "Side", "Executed_Price", "Share_Quantity", "Fees_Paid", "TTR_at_Execution", "Realized_PnL", "Verify_Link"])
    if not os.path.exists("snapshot_live.csv"):
        with open("snapshot_live.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "State", "Yes_Ask", "Yes_Bid", "No_Ask", "No_Bid"])
    if not os.path.exists("telemetry_shadow.csv"):
        with open("telemetry_shadow.csv", "w", newline="") as f:
            csv.writer(f).writerow(["Timestamp", "Slug", "Token", "TTR", "Ticker_Price", "Local_Bid_Vol", "Local_Ask_Vol", "Imbalance_Ratio", "Signal"])

def log_trade_csv_worker(ts, slug, action, side, price, shares, fees, ttr, pnl):
    link = f"https://polymarket.com/event/{slug}"
    try:
        with open("trades_full.csv", "a", newline="") as f:
            csv.writer(f).writerow([ts, slug, action, side, f"{price:.3f}", f"{shares:.2f}", f"{fees:.3f}", ttr, f"{pnl:.3f}", link])
    except Exception:
        pass

# ─── DASHBOARD HTML ───
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BSS Analysis Dashboard</title>
<style>
    :root {
        --bg-main: #0B1120;
        --bg-panel: #1E293B;
        --header-bg: #0F172A;
        --header-text: #F8FAFC;
        --sub-header-bg: #0F172A;
        --text-navy: #F8FAFC;
        --text-light: #94A3B8;
        --border-color: #334155;
        --val-green: #34D399;
        --val-red: #F87171;
        --val-yellow: #FCD34D;
        --val-pink: #F472B6;
        --font-serif: Georgia, "Times New Roman", serif;
        --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    body { background: var(--bg-main); color: var(--text-navy); font-family: var(--font-sans); padding: 20px; font-size: 14px; margin: 0; }
    
    .header-panel { background: var(--header-bg); border: 1px solid var(--border-color); display: flex; flex-direction: column; text-align: center; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden; }
    .brand-title { font-family: var(--font-serif); font-size: 22px; font-weight: bold; color: var(--header-text); padding: 14px 0; border-bottom: 1px solid var(--border-color); }
    .status-tags { font-size: 12px; font-family: var(--font-sans); font-weight: normal; margin-left: 15px; color: var(--text-light); }
    
    .vitals-row { display: flex; background: var(--sub-header-bg); }
    .vital-box { flex: 1; padding: 15px; border-right: 1px solid var(--border-color); text-align: center; }
    .vital-box:last-child { border-right: none; }
    .vital-label { font-size: 12px; font-weight: 700; text-transform: uppercase; margin-bottom: 8px; color: var(--text-light); letter-spacing: 0.5px; }
    .vital-value { background: var(--bg-panel); color: var(--text-navy); font-size: 24px; font-weight: 800; padding: 8px; border-radius: 4px; border: 1px solid var(--border-color); font-family: monospace; }
    .vital-value.green { color: var(--val-green); border-color: #064E3B; background: #065F46;}
    .vital-value.red { color: var(--val-red); border-color: #7F1D1D; background: #991B1B;}
    
    .sec-title { background: var(--header-bg); color: var(--header-text); font-family: var(--font-serif); font-size: 15px; font-weight: bold; text-align: center; padding: 12px; margin-bottom: 15px; border-radius: 6px; letter-spacing: 0.5px; border: 1px solid var(--border-color);}
    
    .grid { display: grid; grid-template-columns: 1fr; gap: 20px; margin-bottom: 35px; }
    .card { background: var(--bg-panel); border: 1px solid var(--border-color); box-shadow: 0 4px 6px rgba(0,0,0,0.3); display: flex; flex-direction: column; border-radius: 6px; overflow: hidden;}
    .card-header { background: var(--sub-header-bg); padding: 12px 20px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; font-weight: 800; color: var(--text-navy); font-size: 15px; align-items: center;}
    
    .leg-container { display: flex; width: 100%; }
    .leg-col { flex: 1; padding: 20px; border-right: 1px solid var(--border-color); }
    .leg-col:last-child { border-right: none; }
    .leg-title { font-size: 13px; font-weight: 800; text-align: center; margin-bottom: 15px; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; }
    
    .data-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: var(--text-light); align-items: center;}
    .data-row b { color: var(--text-navy); font-family: monospace; font-size: 15px;}
    .val-green { color: var(--val-green); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-red { color: var(--val-red); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-gold { color: var(--val-yellow); font-weight: 800; font-family: monospace; font-size: 15px;}
    .val-pink { color: var(--val-pink); font-weight: 800; font-family: monospace; font-size: 15px;}
    
    .conviction-bar { height: 6px; background: #0F172A; border: 1px solid #334155; border-radius: 4px; position: relative; margin-top: 6px; margin-bottom: 15px; width: 100%; }
    .conviction-fill { height: 100%; border-radius: 3px; transition: width 0.3s ease; }
    .conviction-fill.yes { background: #38BDF8; }
    .conviction-fill.no { background: #94A3B8; }
    .marker { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--val-yellow); z-index: 5; }
    .marker.t2 { background: var(--val-pink); }
    
    .svg-container { height: 50px; margin-top: 10px; background: #0F172A; border: 1px solid var(--border-color); border-radius: 4px;}
    
    .table-container { background: var(--bg-panel); border: 1px solid var(--border-color); margin-bottom: 35px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); border-radius: 6px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { background: var(--sub-header-bg); color: var(--text-light); font-size: 11px; font-weight: 800; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border-color); text-align: center; letter-spacing: 0.5px;}
    td { padding: 12px 10px; border-bottom: 1px solid var(--border-color); text-align: center; font-size: 13px; font-family: monospace; color: var(--text-navy);}
    
    .queue-container { background: var(--bg-panel); border: 1px solid var(--border-color); padding: 20px; font-family: monospace; font-size: 13px; color: var(--text-light); line-height: 1.8; border-radius: 6px; }
    
    .vault { display: flex; gap: 15px; background: var(--sub-header-bg); padding: 15px; border: 1px solid var(--border-color); align-items: center; justify-content: center; margin-bottom: 25px; border-radius: 6px;}
    .btn-action { background: #1E293B; color: var(--text-navy); border: 1px solid var(--border-color); padding: 8px 18px; cursor: pointer; font-weight: 700; box-shadow: 0 1px 2px rgba(0,0,0,0.2); border-radius: 4px; transition: all 0.2s;}
    .btn-action:hover { background: #334155; border-color: #475569;}
    .btn-verify { color: #60A5FA; text-decoration: none; font-weight: 800; font-size: 12px; font-family: var(--font-sans);}
    .btn-verify:hover { text-decoration: underline; }
</style>
</head>
<body>

<div class="header-panel">
    <div class="brand-title">BSS Bot Analysis Dashboard v5.8.17 
        <span class="status-tags" id="bot-uptime">[Uptime: 0h 0m 0s]</span>
        <span class="status-tags" id="ws-status">[WS: Checking...]</span>
    </div>
    <div class="vitals-row">
        <div class="vital-box"><div class="vital-label">Total Realized P&L</div><div class="vital-value" id="v-pnl">$0.00</div></div>
        <div class="vital-box"><div class="vital-label">Completed Trades</div><div class="vital-value" id="v-trades">0</div></div>
        <div class="vital-box"><div class="vital-label">Sold Losers</div><div class="vital-value" id="v-losers">0</div></div>
        <div class="vital-box"><div class="vital-label">Catastrophes</div><div class="vital-value red" id="v-catastrophes">0</div></div>
        <div class="vital-box"><div class="vital-label">Active Slots</div><div class="vital-value" id="v-active">0</div></div>
    </div>
</div>

<div class="sec-title">Active Market Dual-Leg Monitoring</div>
<div class="grid" id="active-cards"><div style="text-align:center; padding:30px; color:var(--text-light); font-weight: bold;">Awaiting Entry Criteria...</div></div>

<div class="sec-title">Consolidated Trade Lifecycle History</div>
<div class="table-container">
    <table>
        <thead><tr><th>Time Closed</th><th>Market Slug</th><th>YES Entry</th><th>NO Entry</th><th>T1 Exit</th><th>T2 Exit</th><th>Net P&L</th><th>Audit Link</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="8" style="color: var(--text-light); padding: 20px;">No historical data available.</td></tr></tbody>
    </table>
</div>

<div class="vault">
    <span style="font-weight: 800; margin-right: 15px; color: var(--text-navy); text-transform: uppercase; letter-spacing: 0.5px;">Data Vault & Utilities:</span>
    <button class="btn-action" onclick="window.location.href='/api/dl_trades'">Download Trades (.csv)</button>
    <button class="btn-action" onclick="window.location.href='/api/dl_snaps'">Download Snapshots (.csv)</button>
    <button class="btn-action" style="color: var(--val-gold); border-color: #B45309;" onclick="window.location.href='/api/dl_telemetry'">Download Telemetry (.csv)</button>
    <button class="btn-action" style="color: #FCA5A5; margin-left: auto; border-color: #7F1D1D; background: #450A0A;" onclick="deleteFiles()">⚠ Delete Old Files</button>
</div>

<div class="sec-title">Observation Queue (Scouting)</div>
<div class="queue-container" id="obs-queue">Scanning...</div>

<script>
function renderSparkline(history, color, t1_price, t2_price) {
    if(!history || history.length < 2) return '';
    const min = Math.min(...history), max = Math.max(...history);
    const range = (max - min) || 0.01;
    const pts = history.map((val, i) => {
        const x = (i / (history.length - 1)) * 100;
        const y = 100 - (((val - min) / range) * 100);
        return `${x},${y}`;
    }).join(' ');
    
    let svg = `<polyline fill="none" stroke="${color}" stroke-width="2.5" points="${pts}" />`;
    
    if (t1_price > 0) {
        let yT1 = 100 - (((t1_price - min) / range) * 100);
        yT1 = Math.max(5, Math.min(95, yT1)); 
        svg += `<circle cx="80" cy="${yT1}" r="4" fill="var(--val-yellow)" stroke="#0B1120" stroke-width="1.5" />`;
    }
    if (t2_price > 0) {
        let yT2 = 100 - (((t2_price - min) / range) * 100);
        yT2 = Math.max(5, Math.min(95, yT2)); 
        svg += `<circle cx="92" cy="${yT2}" r="4" fill="var(--val-pink)" stroke="#0B1120" stroke-width="1.5" />`;
    }
    
    return `<svg width="100%" height="100%" viewBox="0 -10 100 120" preserveAspectRatio="none">${svg}</svg>`;
}

function getConvictionHtml(ask) {
    let fillPct = Math.min(100, Math.max(0, ask * 100));
    let distT1 = Math.round((ask - 0.86) * 100);
    let distT2 = Math.round((ask - 0.95) * 100);
    
    let trackerText = "";
    if (ask >= 0.95) trackerText = `<span class="val-pink">T2 REACHED (+${distT2}¢)</span>`;
    else if (ask >= 0.86) trackerText = `<span class="val-gold">T1 REACHED (+${distT1}¢)</span> | <span style="color:var(--text-light)">${distT2}¢ to T2</span>`;
    else trackerText = `<span style="color:var(--text-light)">${distT1}¢ to T1 | ${distT2}¢ to T2</span>`;
    
    return { pct: fillPct, text: trackerText };
}

function getImbalanceBadge(bidV, askV) {
    if (askV > 0 && bidV >= askV * 2.0) {
        let r = (bidV/askV).toFixed(1);
        return `<span style="background:var(--val-green); color:#064E3B; padding:2px 6px; border-radius:3px; font-size:10px; font-weight:800; margin-left:8px;">⚠ BID WALL (${r}x)</span>`;
    } else if (bidV > 0 && askV >= bidV * 2.0) {
        let r = (askV/bidV).toFixed(1);
        return `<span style="background:var(--val-red); color:#fff; padding:2px 6px; border-radius:3px; font-size:10px; font-weight:800; margin-left:8px;">⚠ ASK WALL (${r}x)</span>`;
    }
    return '';
}

function formatUptime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h}h ${m}m ${s}s`;
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
        
        document.getElementById('bot-uptime').textContent = `[Uptime: ${formatUptime(s.uptime_s)}]`;
        document.getElementById('ws-status').textContent = s.ws_connected ? "[WS: CONNECTED]" : "[WS: DROPPED]";
        document.getElementById('ws-status').style.color = s.ws_connected ? "#34d399" : "#f87171";
        
        const pnlBox = document.getElementById('v-pnl');
        pnlBox.textContent = (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
        pnlBox.className = "vital-value " + (s.pnl > 0 ? "green" : (s.pnl < 0 ? "red" : ""));
        
        document.getElementById('v-trades').textContent = s.total_trades_count;
        document.getElementById('v-losers').textContent = s.losers;
        document.getElementById('v-catastrophes').textContent = s.catastrophes;
        
        let activeCount = 0;
        let htmlCards = '';
        let htmlQueue = '';
        
        s.markets.forEach(m => {
            if (m.state === 'WATCH' || m.state === 'WAITING_NO' || m.state === 'WAITING_YES') {
                let currentStatus = m.state === 'WATCH' ? 'Scouting' : 'Filling Dual Leg';
                htmlQueue += `[TTR: ${m.ttr_s}s] | ${m.slug} | YES Ask: $${m.yes_ask.toFixed(3)} | NO Ask: $${m.no_ask.toFixed(3)} | Status: ${currentStatus}<br>`;
                return;
            }
            if (m.state === 'CLOSED' && m.ttr_s <= -5) return;
            
            activeCount++;
            
            let isClosed = m.state === 'CLOSED';
            let closedBadge = isClosed ? `<span style="background:var(--val-red); color:#fff; padding:3px 8px; border-radius:4px; font-size:11px; margin-left:10px; font-family:var(--font-sans);">SOLD - TICKER ONLY</span>` : '';
            
            let dYes = m.yes_entry > 0 ? ((m.yes_ask - m.yes_entry) / m.yes_entry) * 100 : 0;
            let dNo = m.no_entry > 0 ? ((m.no_ask - m.no_entry) / m.no_entry) * 100 : 0;
            let cYes = dYes >= 0 ? 'val-green' : 'val-red';
            let cNo = dNo >= 0 ? 'val-green' : 'val-red';

            let valYes = m.yes_shares * m.yes_ask;
            let valNo = m.no_shares * m.no_ask;
            
            let cYesData = getConvictionHtml(m.yes_ask);
            let cNoData = getConvictionHtml(m.no_ask);

            let yesBadge = getImbalanceBadge(m.yes_b_vol, m.yes_a_vol);
            let noBadge = getImbalanceBadge(m.no_b_vol, m.no_a_vol);

            htmlCards += `<div class="card">
                <div class="card-header">
                    <span>${m.slug} ${closedBadge}</span>
                    <span style="color:var(--text-light);">TTR: <span style="color:var(--text-navy);">${m.ttr_s}s</span></span>
                </div>
                <div class="leg-container">
                    <div class="leg-col">
                        <div class="leg-title">YES LEG MONITOR</div>
                        <div class="data-row"><span>Shares Acquired:</span> <b>${m.yes_shares.toFixed(2)}</b></div>
                        <div class="data-row"><span>Effective Entry (w/ fees):</span> <b>$${m.yes_entry > 0 ? (m.yes_entry*1.018).toFixed(3) : '0.000'}</b></div>
                        <div class="data-row" style="margin-top:10px; border-top:1px solid var(--border-color); padding-top:10px;">
                            <span>Live Ticker: ${yesBadge}</span> 
                            <b>$${m.yes_ask.toFixed(3)}</b>
                        </div>
                        <div class="data-row"><span>Current Delta:</span> <span class="${cYes}">${(dYes>0?'+':'')+dYes.toFixed(2)+'%'}</span></div>
                        <div class="data-row"><span>Live Value:</span> <b class="val-gold">$${valYes.toFixed(2)}</b></div>
                        
                        <div class="data-row" style="margin-top:12px; font-size:12px;"><span>Conviction Proximity:</span> <b>${cYesData.text}</b></div>
                        <div class="conviction-bar">
                            <div class="conviction-fill yes" style="width: ${cYesData.pct}%"></div>
                            <div class="marker" style="left: 86%" title="Tier 1 (0.86)"></div>
                            <div class="marker t2" style="left: 95%" title="Tier 2 (0.95)"></div>
                        </div>
                        
                        <div class="svg-container">${renderSparkline(m.history_yes, '#38BDF8', (m.t1_side==='YES'?m.t1_price:0), (m.t2_side==='YES'?m.t2_price:0))}</div>
                    </div>
                    <div class="leg-col">
                        <div class="leg-title">NO LEG MONITOR</div>
                        <div class="data-row"><span>Shares Acquired:</span> <b>${m.no_shares.toFixed(2)}</b></div>
                        <div class="data-row"><span>Effective Entry (w/ fees):</span> <b>$${m.no_entry > 0 ? (m.no_entry*1.018).toFixed(3) : '0.000'}</b></div>
                        <div class="data-row" style="margin-top:10px; border-top:1px solid var(--border-color); padding-top:10px;">
                            <span>Live Ticker: ${noBadge}</span> 
                            <b>$${m.no_ask.toFixed(3)}</b>
                        </div>
                        <div class="data-row"><span>Current Delta:</span> <span class="${cNo}">${(dNo>0?'+':'')+dNo.toFixed(2)+'%'}</span></div>
                        <div class="data-row"><span>Live Value:</span> <b class="val-gold">$${valNo.toFixed(2)}</b></div>
                        
                        <div class="data-row" style="margin-top:12px; font-size:12px;"><span>Conviction Proximity:</span> <b>${cNoData.text}</b></div>
                        <div class="conviction-bar">
                            <div class="conviction-fill no" style="width: ${cNoData.pct}%"></div>
                            <div class="marker" style="left: 86%" title="Tier 1 (0.86)"></div>
                            <div class="marker t2" style="left: 95%" title="Tier 2 (0.95)"></div>
                        </div>
                        
                        <div class="svg-container">${renderSparkline(m.history_no, '#94A3B8', (m.t1_side==='NO'?m.t1_price:0), (m.t2_side==='NO'?m.t2_price:0))}</div>
                    </div>
                </div>
            </div>`;
        });
        
        document.getElementById('v-active').textContent = activeCount;
        if(htmlCards) document.getElementById('active-cards').innerHTML = htmlCards;
        else document.getElementById('active-cards').innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-light); font-weight: bold;">No Active Dual-Leg Positions...</div>';
        
        document.getElementById('obs-queue').innerHTML = htmlQueue || 'No upcoming markets in window.';

        let logHtml = '';
        s.history.reverse().forEach(h => {
            const pnlStr = h.pnl !== 0.0 ? (h.pnl > 0 ? `+${h.pnl.toFixed(2)}` : h.pnl.toFixed(2)) : '--';
            
            let t1Str = h.t1_side && h.t1_side !== "" ? `<span class="val-gold">${h.t1_side}</span> @ $${h.t1_price.toFixed(3)}<br><span style="font-size:10px;color:var(--text-light);">${h.t1_time}</span>` : '--';
            let t2Str = h.t2_side && h.t2_side !== "" ? `<span class="val-pink">${h.t2_side}</span> @ $${h.t2_price.toFixed(3)}<br><span style="font-size:10px;color:var(--text-light);">${h.t2_time}</span>` : '--';
            
            logHtml += `<tr>
                <td style="color:var(--text-light); font-family:var(--font-sans); font-size: 13px;">${h.time}</td>
                <td>${h.slug}</td>
                <td>${h.yes_entry > 0 ? '$'+h.yes_entry.toFixed(3) : '--'}</td>
                <td>${h.no_entry > 0 ? '$'+h.no_entry.toFixed(3) : '--'}</td>
                <td style="line-height:1.4;">${t1Str}</td>
                <td style="line-height:1.4;">${t2Str}</td>
                <td class="${h.pnl>0?'val-green':(h.pnl<0?'val-red':'')}">${pnlStr}</td>
                <td><a href="https://polymarket.com/event/${h.slug}" target="_blank" class="btn-verify">VERIFY ↗</a></td>
            </tr>`;
        });
        if(logHtml) document.getElementById('log-body').innerHTML = logHtml;

    } catch(e) {}
}, 250); 
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
            m_data, history_data = [], []
            
            for m in sorted(GLOBAL_STATE.markets.values(), key=lambda x: x.end_ts):
                if m.state == MarketState.CLOSED and m.close_time != "":
                    history_data.append({
                        "time": m.close_time, "slug": m.slug, "reason": m.close_reason,
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price, "pnl": m.realized_pnl,
                        "t1_side": m.t1_side, "t1_price": m.t1_price, "t1_time": m.t1_time,
                        "t2_side": m.t2_side, "t2_price": m.t2_price, "t2_time": m.t2_time
                    })
                else:
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    y_b_v = yb.get_local_vols(yb.bid, "bid", 0.10) if yb else 0
                    y_a_v = yb.get_local_vols(yb.ask, "ask", 0.10) if yb else 0
                    n_b_v = nb.get_local_vols(nb.bid, "bid", 0.10) if nb else 0
                    n_a_v = nb.get_local_vols(nb.ask, "ask", 0.10) if nb else 0
                    
                    m_data.append({
                        "slug": m.slug, "state": m.state, "ttr_s": int(m.end_ts - now),
                        "yes_entry": m.yes_entry_price, "no_entry": m.no_entry_price,
                        "yes_shares": m.yes_shares, "no_shares": m.no_shares,
                        "yes_ask": yb.ask if yb else 0.0, "no_ask": nb.ask if nb else 0.0,
                        "yes_b_vol": y_b_v, "yes_a_vol": y_a_v,
                        "no_b_vol": n_b_v, "no_a_vol": n_a_v,
                        "history_yes": m.history_yes[-30:], "history_no": m.history_no[-30:],
                        "t1_side": m.t1_side, "t1_price": m.t1_price, 
                        "t2_side": m.t2_side, "t2_price": m.t2_price
                    })
            
            payload = {
                "uptime_s": int(time.time() - SYSTEM_BOOT_TIME),
                "ws_connected": GLOBAL_STATE.ws_connected, "pnl": GLOBAL_STATE.total_pnl,
                "total_trades_count": GLOBAL_STATE.total_trades, "losers": GLOBAL_STATE.sold_losers,
                "catastrophes": GLOBAL_STATE.catastrophes,
                "markets": m_data, "history": history_data[-15:]
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        elif self.path in ["/api/dl_trades", "/api/dl_snaps", "/api/dl_telemetry"]:
            filename = "trades_full.csv"
            if self.path == "/api/dl_snaps": filename = "snapshot_live.csv"
            elif self.path == "/api/dl_telemetry": filename = "telemetry_shadow.csv"
            self.send_response(200)
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Type', 'text/csv')
            self.end_headers()
            try:
                with open(filename, "rb") as f:
                    self.wfile.write(f.read())
            except Exception:
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/delete_logs":
            if os.path.exists("trades_full.csv"): os.remove("trades_full.csv")
            if os.path.exists("snapshot_live.csv"): os.remove("snapshot_live.csv")
            if os.path.exists("telemetry_shadow.csv"): os.remove("telemetry_shadow.csv")
            init_csv()
            self.send_response(200)
            self.end_headers()

    def log_message(self, format, *args): pass

def run_server():
    server = socketserver.ThreadingTCPServer(("", PORT), DashboardHandler)
    print(f"[System] UI listening on port {PORT}", flush=True)
    server.serve_forever()

# ─── CORE STRATEGY ───
def execute_trade(mdm: MarketData, side: str, price: float, action: str, shares: float, fees: float, ttr: int, pnl: float = 0.0):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{action}] {mdm.slug} | {side} @ {price:.3f} | Shares: {shares:.2f}", flush=True)
    
    if action == "SELL_LOSER_T1":
        GLOBAL_STATE.sold_losers += 1
        mdm.salvage_revenue += (shares * price)
        mdm.t1_side = side
        mdm.t1_price = price
        mdm.t1_time = ts
    
    if action == "SELL_LOSER_T2":
        GLOBAL_STATE.sold_losers += 1
        mdm.salvage_revenue += (shares * price)
        mdm.t2_side = side
        mdm.t2_price = price
        mdm.t2_time = ts
        
    if "CLOSED" in action or action == "EXPIRED":
        mdm.close_time = ts
        mdm.close_reason = action
        GLOBAL_STATE.total_trades += 1
        mdm.realized_pnl = pnl
        GLOBAL_STATE.total_pnl += pnl
        
    threading.Thread(target=log_trade_csv_worker, args=(ts, mdm.slug, action, side, price, shares, fees, ttr, pnl), daemon=True).start()

def evaluate_market(mdm: MarketData, now: float):
    if mdm.state == MarketState.CLOSED and (mdm.end_ts - now) <= -5: return
        
    yb, nb = GLOBAL_STATE.books.get(mdm.yes_token), GLOBAL_STATE.books.get(mdm.no_token)
    if not yb or not nb: return
    ttr = int(mdm.end_ts - now)
    
    if ttr <= -5 and mdm.state != MarketState.CLOSED:
        mdm.state = MarketState.CLOSED
        
        cost_basis = mdm.total_fees_paid
        if mdm.yes_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
        if mdm.no_shares > 0: cost_basis += BASE_CAPITAL_PER_LEG
        
        winner_side = "YES" if yb.bid > nb.bid else "NO"
        winner_shares = mdm.yes_shares if winner_side == "YES" else mdm.no_shares 
        calc_pnl = (winner_shares * 1.00) + mdm.salvage_revenue - cost_basis
        
        if (mdm.t1_side == winner_side) or (mdm.t2_side == winner_side):
            GLOBAL_STATE.catastrophes += 1
            
        execute_trade(mdm, "EXPIRED", 0.00, "EXPIRED", 0.0, 0.0, ttr, calc_pnl)
        return
        
    if ttr <= 0 or mdm.state == MarketState.CLOSED: return
        
    t2 = T_SECOND_LIVE if ttr <= 300 else T_SECOND_PRE
    
    if mdm.state == MarketState.WATCH:
        if 0 < yb.ask <= T_FIRST:
            mdm.state = MarketState.WAITING_NO
            mdm.yes_entry_price = yb.ask
            mdm.yes_shares = BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "LEG_1_ENTRY", mdm.yes_shares, fee, ttr)
            
        elif 0 < nb.ask <= T_FIRST:
            mdm.state = MarketState.WAITING_YES
            mdm.no_entry_price = nb.ask
            mdm.no_shares = BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "LEG_1_ENTRY", mdm.no_shares, fee, ttr)
            
        elif ttr <= HEDGE_DEADLINE_TTR and 0 < yb.ask and 0 < nb.ask and (yb.ask + nb.ask) <= MAX_COMBINED_COST:
            mdm.state = MarketState.BOTH
            
            mdm.yes_entry_price = yb.ask
            mdm.yes_shares = BASE_CAPITAL_PER_LEG / yb.ask
            fee_yes = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee_yes
            execute_trade(mdm, "YES", yb.ask, "LEG_1_FOMO", mdm.yes_shares, fee_yes, ttr)
            
            mdm.no_entry_price = nb.ask
            mdm.no_shares = BASE_CAPITAL_PER_LEG / nb.ask
            fee_no = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee_no
            execute_trade(mdm, "NO", nb.ask, "LEG_2_FOMO", mdm.no_shares, fee_no, ttr)

    elif mdm.state == MarketState.WAITING_NO:
        if (0 < nb.ask <= t2) or (ttr <= HEDGE_DEADLINE_TTR and nb.ask > 0):
            mdm.state = MarketState.BOTH
            mdm.no_entry_price = nb.ask
            mdm.no_shares = BASE_CAPITAL_PER_LEG / nb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "NO", nb.ask, "LEG_2_DEADLINE" if ttr <= HEDGE_DEADLINE_TTR else "LEG_2_ENTRY", mdm.no_shares, fee, ttr)
            
    elif mdm.state == MarketState.WAITING_YES:
        if (0 < yb.ask <= t2) or (ttr <= HEDGE_DEADLINE_TTR and yb.ask > 0):
            mdm.state = MarketState.BOTH
            mdm.yes_entry_price = yb.ask
            mdm.yes_shares = BASE_CAPITAL_PER_LEG / yb.ask
            fee = BASE_CAPITAL_PER_LEG * TAKER_FEE_RATE
            mdm.total_fees_paid += fee
            execute_trade(mdm, "YES", yb.ask, "LEG_2_DEADLINE" if ttr <= HEDGE_DEADLINE_TTR else "LEG_2_ENTRY", mdm.yes_shares, fee, ttr)
            
    elif mdm.state == MarketState.BOTH:
        
        if yb.bid > nb.bid: winner_bid, loser_side, loser_bid, loser_shares = yb.bid, "NO", nb.bid, mdm.no_shares
        else: winner_bid, loser_side, loser_bid, loser_shares = nb.bid, "YES", yb.bid, mdm.yes_shares
            
        if not mdm.t1_executed and winner_bid >= SELL_LOSER_T1_THRESH and ttr <= SELL_LOSER_T1_TTR_MAX:
            mdm.t1_executed = True
            
            shares_to_sell = loser_shares * 0.50
            if loser_side == "YES": mdm.yes_shares -= shares_to_sell
            else: mdm.no_shares -= shares_to_sell
            
            fee = (shares_to_sell * loser_bid) * 0.001 
            mdm.total_fees_paid += fee
            execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T1", shares_to_sell, fee, ttr)
            
        elif winner_bid >= SELL_LOSER_T2_THRESH:
            mdm.state = MarketState.CLOSED
            
            shares_to_sell = loser_shares * 0.99 
            fee = (shares_to_sell * loser_bid) * 0.001 
            mdm.total_fees_paid += fee
            execute_trade(mdm, loser_side, loser_bid, "SELL_LOSER_T2", shares_to_sell, fee, ttr)
            
            cost_basis = (BASE_CAPITAL_PER_LEG * 2) + mdm.total_fees_paid
            winner_shares = mdm.yes_shares if loser_side == "NO" else mdm.no_shares
            final_pnl = (winner_shares * 1.00) + mdm.salvage_revenue - cost_basis
            
            execute_trade(mdm, "CLOSED", winner_bid, "CLOSED_T2_RESOLVED", 0.0, 0.0, ttr, final_pnl)

def tick_loop():
    while GLOBAL_STATE.running:
        now = time.time()
        for m in list(GLOBAL_STATE.markets.values()):
            try:
                evaluate_market(m, now)
            except Exception:
                pass
        time.sleep(0.05)

def snapshot_loop():
    while GLOBAL_STATE.running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("snapshot_live.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in GLOBAL_STATE.markets.values():
                    if m.end_ts >= time.time() - 5:
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

def telemetry_loop():
    while GLOBAL_STATE.running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            with open("telemetry_shadow.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for m in list(GLOBAL_STATE.markets.values()):
                    if m.state == MarketState.CLOSED: continue
                    ttr = int(m.end_ts - time.time())
                    if ttr < 0: continue
                    
                    yb, nb = GLOBAL_STATE.books.get(m.yes_token), GLOBAL_STATE.books.get(m.no_token)
                    
                    if yb and yb.bids and yb.asks:
                        y_b_vol = yb.get_local_vols(yb.bid, "bid", 0.10)
                        y_a_vol = yb.get_local_vols(yb.ask, "ask", 0.10)
                        r_y = y_b_vol / y_a_vol if y_a_vol > 0 else 999.0
                        r_y_inv = y_a_vol / y_b_vol if y_b_vol > 0 else 999.0
                        
                        if r_y >= 2.0:
                            writer.writerow([ts, m.slug, "YES", ttr, f"{yb.ask:.3f}", f"{y_b_vol:.0f}", f"{y_a_vol:.0f}", f"{r_y:.1f}", "BID_WALL"])
                        elif r_y_inv >= 2.0:
                            writer.writerow([ts, m.slug, "YES", ttr, f"{yb.ask:.3f}", f"{y_b_vol:.0f}", f"{y_a_vol:.0f}", f"{r_y_inv:.1f}", "ASK_WALL"])
                            
                    if nb and nb.bids and nb.asks:
                        n_b_vol = nb.get_local_vols(nb.bid, "bid", 0.10)
                        n_a_vol = nb.get_local_vols(nb.ask, "ask", 0.10)
                        r_n = n_b_vol / n_a_vol if n_a_vol > 0 else 999.0
                        r_n_inv = n_a_vol / n_b_vol if n_b_vol > 0 else 999.0
                        
                        if r_n >= 2.0:
                            writer.writerow([ts, m.slug, "NO", ttr, f"{nb.ask:.3f}", f"{n_b_vol:.0f}", f"{n_a_vol:.0f}", f"{r_n:.1f}", "BID_WALL"])
                        elif r_n_inv >= 2.0:
                            writer.writerow([ts, m.slug, "NO", ttr, f"{nb.ask:.3f}", f"{n_b_vol:.0f}", f"{n_a_vol:.0f}", f"{r_n_inv:.1f}", "ASK_WALL"])
        except Exception: pass
        time.sleep(5)

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
            except Exception:
                pass
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
        tks = [t for m in GLOBAL_STATE.markets.values() if m.end_ts >= time.time() - 5 for t in (m.yes_token, m.no_token)]
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
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=polymarket_ws_thread, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    while GLOBAL_STATE.running:
        time.sleep(1)