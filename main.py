"""
V6.14 POLYMARKET ENDGAME TRADING ENGINE (main.py)
-------------------------------------------------
Features: OFA Velocity Override, $0.02 Offset Penetration, 
          Decoupled Post-Mortem Telemetry, Ghost UI, Cats Counter.
"""

import time
import collections
import logging
from typing import Dict, Optional

# ==========================================
# DASHBOARD UI IMPORTS
# ==========================================
# Ensure you have run: pip install rich
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

# Configure background logging (saved to file so it doesn't break the terminal UI)
logging.basicConfig(
    filename='v614_engine.log',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("V6.14_Engine")

# ==========================================
# 1. THE VELOCITY TRACKER (OFA ENGINE)
# ==========================================
class OFAVelocityTracker:
    def __init__(self, lookback_horizon_secs: int = 15, tick_interval_secs: int = 5):
        """Maintains a rolling memory buffer to compute order book velocity."""
        self.maxlen = max(1, int(lookback_horizon_secs / tick_interval_secs))
        self.history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=self.maxlen)
        )

    def update_snapshot(self, slug: str, bid_vol: float, ask_vol: float):
        self.history[slug].append((time.time(), bid_vol, ask_vol))

    def is_wall_fake(self, slug: str, target_side: str) -> bool:
        """Evaluates if the wall is a stagnant Market Maker spoof."""
        buffer = self.history[slug]
        if len(buffer) < self.maxlen:
            return False 

        initial_vol = buffer[0][1] if target_side == "YES" else buffer[0][2]
        latest_vol  = buffer[-1][1] if target_side == "YES" else buffer[-1][2]

        vol_delta = latest_vol - initial_vol
        growth_rate = (latest_vol / initial_vol) if initial_vol > 0 else 1.0

        # OVERRIDE RULE: Volume growth < 25% AND absolute delta < 1000 shares
        if growth_rate < 1.25 and abs(vol_delta) < 1000:
            return True
        return False


# ==========================================
# 2. THE MASTER TRADING ENGINE
# ==========================================
class V614_TradingEngine:
    def __init__(self):
        self.ofa_tracker = OFAVelocityTracker()
        
        # Core Parameters
        self.trigger_threshold = 0.86
        self.gradual_exit_pct = 0.50
        self.static_guard_ratio = 3.5
        
        # State Tracking
        self.cats_count = 0 
        self.dashboard_ui: Dict[str, dict] = {} 
        self.positions: Dict[str, Dict[str, float]] = collections.defaultdict(
            lambda: {"YES": 10.0, "NO": 10.0}
        )

    def process_market_tick(self, market_data: dict) -> Optional[dict]:
        slug = market_data['Slug']
        ttr = market_data['TTR']
        
        # 1. DECOUPLED TELEMETRY (Logs until 30s after expiration)
        if ttr >= -30:
            self._log_to_telemetry_shadow(slug, market_data)

        # 2. UPDATE GHOST UI & CATS COUNTER
        self._update_ghost_dashboard(slug, market_data)

        # 3. GHOST UI CLEANUP (Drop market from screen 5s after expiration)
        if ttr < -5 and slug in self.dashboard_ui:
            del self.dashboard_ui[slug]

        # 4. TRADING KILL-SWITCH
        if ttr <= 0:
            return None
            
        # 5. PORTFOLIO CHECK (Have we already exited?)
        if self.positions[slug]["YES"] <= 5.0 or self.positions[slug]["NO"] <= 5.0:
            return None

        # 6. EVALUATE SALVAGE DECISION
        return self.evaluate_salvage(market_data)

    def evaluate_salvage(self, market_data: dict) -> Optional[dict]:
        slug = market_data['Slug']
        ttr = market_data['TTR']
        mid_price = market_data['Mid_Price']
        imbalance = market_data['Imbalance_Ratio']
        losing_side = market_data['Losing_Side']

        # Update Memory
        self.ofa_tracker.update_snapshot(slug, market_data['Local_Bid_Vol'], market_data['Local_Ask_Vol'])

        if ttr > 60: return None
        if mid_price < self.trigger_threshold: return None

        static_guard_blocked = imbalance >= self.static_guard_ratio

        if not static_guard_blocked:
            return self.build_execution_payload(market_data, "CLEAN_BOOK")

        # OFA OVERRIDE LOGIC
        is_fake_wall = self.ofa_tracker.is_wall_fake(slug, target_side=losing_side)
        if is_fake_wall:
            return self.build_execution_payload(market_data, "OFA_SPOOF_OVERRIDE")

        return None

    def build_execution_payload(self, market_data: dict, reason: str) -> dict:
        """Constructs Payload with $0.02 Offset Penetration."""
        current_bid = market_data['Bid_Price']
        penetration_limit_price = max(0.01, round(current_bid - 0.02, 2))

        slug = market_data['Slug']
        token_to_sell = market_data['Losing_Side']
        shares_to_sell = self.positions[slug][token_to_sell] * self.gradual_exit_pct

        payload = {
            "slug": slug,
            "token": token_to_sell,
            "order_type": "LIMIT",
            "price": penetration_limit_price,
            "quantity": shares_to_sell,
            "metadata": {"execution_reason": reason}
        }
        
        self.positions[slug][token_to_sell] -= shares_to_sell
        
        self.dashboard_ui[slug]["Status"] = "[LOCKED IN]"
        self.dashboard_ui[slug]["Sold_Side"] = token_to_sell
        logger.info(f"EXECUTED: {slug} | {reason} | ${penetration_limit_price}")
        
        return payload

    def _update_ghost_dashboard(self, slug: str, market_data: dict):
        if slug not in self.dashboard_ui:
            self.dashboard_ui[slug] = {
                "Status": "ACTIVE", 
                "Sold_Side": None, 
                "Cat_Logged": False,
                "Mid_Price": 0.0,
                "Imbalance": 0.0,
                "TTR": 0
            }
            
        ui_state = self.dashboard_ui[slug]
        ui_state["Mid_Price"] = market_data['Mid_Price']
        ui_state["Imbalance"] = market_data['Imbalance_Ratio']
        ui_state["TTR"] = market_data['TTR']
        
        # Cats Counter Logic
        if market_data['TTR'] <= 0 and ui_state["Status"] == "[LOCKED IN]":
            sold_token = ui_state["Sold_Side"]
            ultimate_winner = market_data['Winning_Side']
            
            # Whipsaw: We sold the token, but it ended up winning at TTR 0
            if sold_token == ultimate_winner and not ui_state["Cat_Logged"]:
                self.cats_count += 1
                ui_state["Cat_Logged"] = True
                logger.warning(f"CAT WHIPSAW: {slug}")
            
            ui_state["Status"] = "[RESOLVED]"

    def _log_to_telemetry_shadow(self, slug: str, market_data: dict):
        # CSV Logging Logic goes here
        pass


# ==========================================
# 3. TERMINAL UI RENDERER (THE DASHBOARD)
# ==========================================
def generate_dashboard(engine: V614_TradingEngine) -> Layout:
    """Draws the live terminal dashboard using the Rich library."""
    cats_text = Text(f"🐈 Catastrophic Whipsaws: {engine.cats_count}", style="bold red" if engine.cats_count > 0 else "bold green")
    header_panel = Panel(cats_text, title="V6.14 Telemetry Engine", border_style="cyan")

    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Market Slug", style="cyan", width=30)
    table.add_column("TTR (s)", justify="right", width=10)
    table.add_column("Mid Price", justify="right", width=10)
    table.add_column("Imbalance", justify="right", width=10)
    table.add_column("Status", justify="center", width=20)

    for slug, state in engine.dashboard_ui.items():
        row_style = "white"
        if state["Status"] == "[LOCKED IN]":
            row_style = "yellow"
        elif state["Status"] == "[RESOLVED]":
            row_style = "dim" 
            
        imb_str = f"{state['Imbalance']:.1f}x"
        if state['Imbalance'] >= engine.static_guard_ratio and state["Status"] == "ACTIVE":
            imb_str = f"[red]{imb_str}[/red]"

        table.add_row(
            slug, str(state['TTR']), f"${state['Mid_Price']:.2f}",
            imb_str, state["Status"], style=row_style
        )

    layout = Layout()
    layout.split(
        Layout(header_panel, size=3),
        Layout(Panel(table, title="Live Order Book Surveillance", border_style="blue"))
    )
    return layout


# ==========================================
# 4. MAIN EXECUTION LOOP (TESTBED)
# ==========================================
def main():
    bot = V614_TradingEngine()
    
    # MOCK DATA LOOP to prove the dashboard is working.
    test_feed = [
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 65, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.85, 'Bid_Price': 0.14, 'Imbalance_Ratio': 2.2, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6000},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 60, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.88, 'Bid_Price': 0.12, 'Imbalance_Ratio': 4.2, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6050},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 55, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.89, 'Bid_Price': 0.11, 'Imbalance_Ratio': 4.3, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6100},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 0,  'Winning_Side': 'NO',  'Losing_Side': 'YES','Mid_Price': 1.00, 'Bid_Price': 0.00, 'Imbalance_Ratio': 0.0, 'Local_Bid_Vol': 0,    'Local_Ask_Vol': 0},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': -3, 'Winning_Side': 'NO',  'Losing_Side': 'YES','Mid_Price': 1.00, 'Bid_Price': 0.00, 'Imbalance_Ratio': 0.0, 'Local_Bid_Vol': 0,    'Local_Ask_Vol': 0},
    ]

    # This 'Live' context block is what actively draws the dashboard to your screen
    with Live(generate_dashboard(bot), refresh_per_second=4, screen=True) as live:
        for tick in test_feed:
            time.sleep(2) 
            
            payload = bot.process_market_tick(tick)
            if payload:
                logger.info(f"API Payload Built: {payload}")
                
            live.update(generate_dashboard(bot))
            
        time.sleep(3) 

if __name__ == "__main__":
    main()