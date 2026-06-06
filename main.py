"""
main.py — Opportunistic Both-Sides (BSS) Polymarket Bot
Fully independent concurrent state tracking for pre-market and live windows.
"""

import os
import sys
import time
import json
import threading
import signal
import requests
import websocket
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

# ─── STATE MODELS ───
class MarketState:
    WATCH = "WATCH"           # Hunting Leg 1 (<= 0.49)
    WAITING_NO = "WAITING_NO" # YES acquired, hunting NO
    WAITING_YES = "WAITING_YES"# NO acquired, hunting YES
    BOTH = "BOTH"             # Both acquired, hunting Sell-Loser
    CLOSED = "CLOSED"         # Exited or expired

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

# ─── CORE STRATEGY ENGINE ───
def evaluate_market(state: BotState, mdm: MarketData, now: float):
    if mdm.state == MarketState.CLOSED:
        return

    y_book = state.books.get(mdm.yes_token)
    n_book = state.books.get(mdm.no_token)
    if not y_book or not n_book:
        return  # Waiting for order book data

    ttr = mdm.end_ts - now
    if ttr <= 0:
        mdm.state = MarketState.CLOSED
        return

    # Determine thresholds based on window (Live = TTR <= 300s)
    is_live = ttr <= 300
    t2_current = T_SECOND