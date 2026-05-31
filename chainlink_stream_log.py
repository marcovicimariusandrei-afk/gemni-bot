"""
chainlink_stream_log.py — v2.9.2
================================
Records every Chainlink price update relayed by Polymarket's Real-Time Data
Socket (RTDS).

PURPOSE
-------
This is the data-capture layer for RESOLUTION VERIFICATION. Polymarket uses
Chainlink Data Streams to resolve crypto Up/Down markets (per market rules
text: "The resolution source for this market is information from Chainlink").

Polymarket exposes a free, unauthenticated relay of the same Chainlink
streams via their RTDS WebSocket. We subscribe, log every update to CSV,
and later (v2.9.3+) use that log to verify market resolutions by looking
up the Chainlink price at market_start_ts and market_close_ts.

ENDPOINT   : wss://ws-live-data.polymarket.com
TOPIC      : crypto_prices_chainlink
SYMBOLS    : btc/usd, eth/usd, sol/usd, xrp/usd
UPDATE RATE: per Polymarket docs, updates come as frequently as Chainlink
             itself publishes (typically every 10-30s per coin, or on
             0.5%+ deviations). Empirics TBD once we have data.

BNB GAP
-------
Polymarket's BNB Up/Down markets DO resolve via Chainlink BNB/USD per their
published market rules — but BNB is NOT exposed in this free RTDS relay.
For v2.9.2, BNB trades log as `no_chainlink_source_in_relay` at resolution
verify time (future gap to close via direct Chainlink API access or
Polymarket's sponsored API key program).

STAGE DISCIPLINE
----------------
Stage 1 (this module, v2.9.2): collect data only. No verification logic
  touches trading decisions. Pure instrumentation.
Stage 2 (v2.9.3+): rewrite resolution_verify.py to use get_price_at()
  instead of Binance REST.

STORAGE
-------
/data/chainlink_prices.csv (fallback /app/chainlink_prices.csv).
Estimated volume: ~4 coins × 4 updates/min × 60 min × 24 h = ~23k rows/day,
~1-2 MB/day. Negligible vs 1.8 GB/bot Railway allotment.

ZERO LOGIC IMPACT ON TRADING. This is pure data collection infrastructure.
"""
import csv
import json
import os
import threading
import time
from typing import Optional

try:
    import websocket
except ImportError:
    websocket = None


WS_URL = "wss://ws-live-data.polymarket.com"

# Symbols exposed in Polymarket's RTDS Chainlink relay.
# BNB is NOT here — see module docstring for BNB gap discussion.
SYMBOLS = ["btc/usd", "eth/usd", "sol/usd", "xrp/usd"]

_VOLUME_DIR   = "/data"
_FALLBACK_DIR = "/app"


def _detect_storage_path() -> str:
    override = os.environ.get("CHAINLINK_LOG_PATH")
    if override:
        try:
            os.makedirs(os.path.dirname(override) or ".", exist_ok=True)
            test = override + ".test"
            with open(test, "w") as f:
                f.write("")
            os.remove(test)
            return override
        except Exception:
            pass
    try:
        os.makedirs(_VOLUME_DIR, exist_ok=True)
        test = os.path.join(_VOLUME_DIR, ".cl_write_test")
        with open(test, "w") as f:
            f.write("")
        os.remove(test)
        return os.path.join(_VOLUME_DIR, "chainlink_prices.csv")
    except Exception:
        pass
    fallback = os.path.join(_FALLBACK_DIR, "chainlink_prices.csv")
    try:
        os.makedirs(os.path.dirname(fallback), exist_ok=True)
    except Exception:
        fallback = "chainlink_prices.csv"
    return fallback


CSV_PATH = _detect_storage_path()

FIELDS = ["ts_server_ms", "ts_payload_ms", "symbol", "value"]

_write_lock      = threading.Lock()
_header_written  = False

# WS thread state
_running = False
_ws      = None
_add_log = None


def _ensure_header():
    global _header_written
    if _header_written:
        return
    with _write_lock:
        if _header_written:
            return
        try:
            if os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0:
                _header_written = True
                return
        except Exception:
            pass
        try:
            with open(CSV_PATH, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
            _header_written = True
        except Exception:
            pass


def _append_row(row: dict):
    _ensure_header()
    try:
        with _write_lock:
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writerow(row)
    except Exception:
        pass  # never crash the WS thread


def _on_message(ws, message):
    try:
        # RTDS server can send "PONG" in response to our "PING"
        if isinstance(message, str) and message.strip().upper() == "PONG":
            return
        msg = json.loads(message)
        if msg.get("topic") != "crypto_prices_chainlink":
            return
        if msg.get("type") not in ("update", "subscribe"):
            return
        payload = msg.get("payload", {}) or {}
        # Subscribe-type messages sometimes carry a backfill array — not
        # documented for Chainlink topic but handle defensively.
        if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], list):
            sym = payload.get("symbol", "")
            for item in payload["data"]:
                _append_row({
                    "ts_server_ms":  msg.get("timestamp", ""),
                    "ts_payload_ms": item.get("timestamp", ""),
                    "symbol":        sym,
                    "value":         item.get("value", ""),
                })
            return
        # Standard update payload
        _append_row({
            "ts_server_ms":  msg.get("timestamp", ""),
            "ts_payload_ms": payload.get("timestamp", ""),
            "symbol":        payload.get("symbol", ""),
            "value":         payload.get("value", ""),
        })
    except Exception:
        pass  # never crash the WS thread


def _on_open(ws):
    if _add_log:
        _add_log(f"🔗 Chainlink stream connected ({','.join(SYMBOLS)})", "info")
    # Subscribe to each symbol. RTDS supports per-symbol JSON filters.
    for sym in SYMBOLS:
        sub_msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic":   "crypto_prices_chainlink",
                "type":    "*",
                "filters": json.dumps({"symbol": sym}),
            }],
        }
        try:
            ws.send(json.dumps(sub_msg))
        except Exception:
            pass


def _on_error(ws, error):
    if _add_log:
        try:
            _add_log(f"⚠️ chainlink stream err: {type(error).__name__}: {error}", "warn")
        except Exception:
            pass


def _on_close(ws, close_status_code, close_msg):
    if _add_log:
        try:
            _add_log(f"🔗 chainlink stream closed (code={close_status_code})", "warn")
        except Exception:
            pass


def _ping_thread(ws):
    """Send PING every 5s to keep the RTDS connection alive (per docs)."""
    while _running:
        try:
            time.sleep(5)
            try:
                if ws and getattr(ws, "sock", None) and ws.sock.connected:
                    ws.send("PING")
            except Exception:
                return
        except Exception:
            return


def _ws_thread():
    """Run WebSocket with auto-reconnect."""
    global _ws
    while _running:
        try:
            _ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            threading.Thread(target=_ping_thread, args=(_ws,), daemon=True).start()
            _ws.run_forever(ping_interval=0, ping_timeout=None)
        except Exception:
            pass
        if _running:
            time.sleep(5)  # backoff before reconnect


def start(add_log_fn=None):
    """Start logging Chainlink prices. Call once at bot startup."""
    global _running, _add_log
    if _running:
        return
    if websocket is None:
        if add_log_fn:
            add_log_fn("⚠️ websocket-client not installed, Chainlink logging disabled", "warn")
        return
    _add_log = add_log_fn
    _running = True
    _ensure_header()
    threading.Thread(target=_ws_thread, daemon=True).start()


def stop():
    global _running, _ws
    _running = False
    try:
        if _ws:
            _ws.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC LOOKUP API — used by resolution_verify.py in Stage 2 (v2.9.3+)
# ─────────────────────────────────────────────────────────────────────────────
def get_price_at(symbol: str, ts_unix: float, tolerance_s: float = 120.0):
    """Find the last Chainlink price for `symbol` at-or-before `ts_unix`.

    Args:
      symbol      : e.g. "btc/usd" (case-insensitive match)
      ts_unix     : target timestamp in Unix seconds (float)
      tolerance_s : max allowed staleness. If the nearest-before update is
                    older than this, return None (treat as no data).

    Returns:
      dict with keys {symbol, value, ts_payload_ms, age_s} or None.

    Implementation note: linear CSV scan. Fine for our volume (~25k rows/day).
    If this becomes hot later, index by (symbol, ts_bucket).
    """
    try:
        if not os.path.exists(CSV_PATH):
            return None
        target_ms = ts_unix * 1000.0
        best_ts  = 0.0
        best_row = None
        with open(CSV_PATH, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if str(row.get("symbol", "")).strip().lower() != symbol.strip().lower():
                    continue
                try:
                    row_ts = float(row.get("ts_payload_ms", 0) or 0)
                except Exception:
                    continue
                if row_ts <= 0:
                    continue
                if row_ts <= target_ms and row_ts > best_ts:
                    best_ts  = row_ts
                    best_row = row
        if best_row is None:
            return None
        age_s = (target_ms - best_ts) / 1000.0
        if age_s > tolerance_s:
            return None
        try:
            val = float(best_row.get("value", 0) or 0)
        except Exception:
            return None
        return {
            "symbol":        best_row.get("symbol"),
            "value":         val,
            "ts_payload_ms": best_ts,
            "age_s":         age_s,
        }
    except Exception:
        return None


def get_symbol_for_coin(coin: str):
    """Map bot coin code ('btc','eth','sol','xrp','bnb','doge') to Chainlink
    symbol used in this stream. Returns None if not in the relay.
    BNB and DOGE return None — caller should log as no_chainlink_source_in_relay.
    """
    coin = (coin or "").lower()
    mapping = {
        "btc": "btc/usd",
        "eth": "eth/usd",
        "sol": "sol/usd",
        "xrp": "xrp/usd",
    }
    return mapping.get(coin)


def get_status():
    """Summarize capture health for the dashboard.

    Returns dict:
      running       : bool — WS thread believes it's alive
      ws_connected  : bool — underlying socket connected right now
      symbols       : list — symbols subscribed
      rows          : int  — total rows in CSV
      by_symbol     : dict — {symbol: {count, last_ts_ms, last_value}}
      csv_path      : str
      stage         : "1 — collecting data (verify pending v2.9.3)"
    """
    out = {
        "running":       bool(_running),
        "ws_connected":  False,
        "symbols":       list(SYMBOLS),
        "rows":          0,
        "by_symbol":     {},
        "csv_path":      CSV_PATH,
        "stage":         "1 — collecting data (verify pending v2.9.3)",
    }
    try:
        if _ws is not None and getattr(_ws, "sock", None) is not None:
            out["ws_connected"] = bool(_ws.sock.connected)
    except Exception:
        pass
    try:
        if not os.path.exists(CSV_PATH):
            return out
        # Count rows + track last per symbol
        last = {}  # symbol -> (ts_payload_ms, value)
        counts = {}
        total = 0
        with open(CSV_PATH, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                total += 1
                sym = str(row.get("symbol", "")).strip().lower()
                if not sym:
                    continue
                counts[sym] = counts.get(sym, 0) + 1
                try:
                    ts = float(row.get("ts_payload_ms", 0) or 0)
                except Exception:
                    continue
                cur = last.get(sym)
                if cur is None or ts > cur[0]:
                    try:
                        val = float(row.get("value", 0) or 0)
                    except Exception:
                        val = None
                    last[sym] = (ts, val)
        out["rows"] = total
        out["by_symbol"] = {
            sym: {
                "count":         counts.get(sym, 0),
                "last_ts_ms":    last.get(sym, (0, None))[0],
                "last_value":    last.get(sym, (0, None))[1],
            }
            for sym in set(list(SYMBOLS) + list(counts.keys()))
        }
    except Exception:
        pass
    return out
