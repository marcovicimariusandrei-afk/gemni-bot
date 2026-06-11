import time
import collections
import logging
import sys
from typing import Dict, Optional

# Configure standard stream logging so it shows up in your cloud console
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("V6.14_Engine")

# ==========================================
# 1. THE VELOCITY TRACKER (OFA ENGINE)
# ==========================================
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
        if len(buffer) < self.maxlen:
            return False 

        initial_vol = buffer[0][1] if target_side == "YES" else buffer[0][2]
        latest_vol  = buffer[-1][1] if target_side == "YES" else buffer[-1][2]

        vol_delta = latest_vol - initial_vol
        growth_rate = (latest_vol / initial_vol) if initial_vol > 0 else 1.0

        if growth_rate < 1.25 and abs(vol_delta) < 1000:
            return True
        return False

# ==========================================
# 2. THE MASTER TRADING ENGINE
# ==========================================
class V614_TradingEngine:
    def __init__(self):
        self.ofa_tracker = OFAVelocityTracker()
        
        self.trigger_threshold = 0.86
        self.gradual_exit_pct = 0.50
        self.static_guard_ratio = 3.5
        
        self.cats_count = 0 
        self.dashboard_ui: Dict[str, dict] = {} 
        self.positions: Dict[str, Dict[str, float]] = collections.defaultdict(
            lambda: {"YES": 10.0, "NO": 10.0}
        )

    def process_market_tick(self, market_data: dict) -> Optional[dict]:
        slug = market_data['Slug']
        ttr = market_data['TTR']
        
        if ttr >= -30:
            pass # Hook for your CSV telemetry writer

        self._update_ghost_dashboard(slug, market_data)

        if ttr < -5 and slug in self.dashboard_ui:
            del self.dashboard_ui[slug]

        if ttr <= 0:
            return None
            
        if self.positions[slug]["YES"] <= 5.0 or self.positions[slug]["NO"] <= 5.0:
            return None

        return self.evaluate_salvage(market_data)

    def evaluate_salvage(self, market_data: dict) -> Optional[dict]:
        slug = market_data['Slug']
        ttr = market_data['TTR']
        mid_price = market_data['Mid_Price']
        imbalance = market_data['Imbalance_Ratio']
        losing_side = market_data['Losing_Side']

        self.ofa_tracker.update_snapshot(slug, market_data['Local_Bid_Vol'], market_data['Local_Ask_Vol'])

        if ttr > 60: return None
        if mid_price < self.trigger_threshold: return None

        static_guard_blocked = imbalance >= self.static_guard_ratio

        if not static_guard_blocked:
            return self.build_execution_payload(market_data, "CLEAN_BOOK")

        is_fake_wall = self.ofa_tracker.is_wall_fake(slug, target_side=losing_side)
        if is_fake_wall:
            return self.build_execution_payload(market_data, "OFA_SPOOF_OVERRIDE")

        return None

    def build_execution_payload(self, market_data: dict, reason: str) -> dict:
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
        logger.info(f"💥 EXECUTION TRIGGERED: {slug} | Reason: {reason} | Limit Price: ${penetration_limit_price}")
        
        return payload

    def _update_ghost_dashboard(self, slug: str, market_data: dict):
        if slug not in self.dashboard_ui:
            self.dashboard_ui[slug] = {
                "Status": "ACTIVE", "Sold_Side": None, "Cat_Logged": False,
                "Mid_Price": 0.0, "Imbalance": 0.0, "TTR": 0
            }
            
        ui_state = self.dashboard_ui[slug]
        ui_state["Mid_Price"] = market_data['Mid_Price']
        ui_state["Imbalance"] = market_data['Imbalance_Ratio']
        ui_state["TTR"] = market_data['TTR']
        
        if market_data['TTR'] <= 0 and ui_state["Status"] == "[LOCKED IN]":
            sold_token = ui_state["Sold_Side"]
            ultimate_winner = market_data['Winning_Side']
            
            if sold_token == ultimate_winner and not ui_state["Cat_Logged"]:
                self.cats_count += 1
                ui_state["Cat_Logged"] = True
                logger.warning(f"⚠️ CATASTROPHIC WHIPSAW DETECTED ON {slug}!")
            
            ui_state["Status"] = "[RESOLVED]"

# ==========================================
# 3. CLOUD-COMPATIBLE LOG DASHBOARD
# ==========================================
def render_cloud_dashboard(engine: V614_TradingEngine):
    """Outputs the UI matrix cleanly to your standard container logs."""
    print("\n" + "="*70)
    print(f"📊 TELEMETRY DASHBOARD  |  🐈 CATASTROPHIC WHIPSAWS RECORDED: {engine.cats_count}")
    print("-"*70)
    print(f"{'MARKET SLUG':<32} | {'TTR':<6} | {'MID PX':<8} | {'IMB':<6} | {'STATUS'}")
    print("-"*70)
    
    if not engine.dashboard_ui:
        print("   No active or ghost markets currently tracked.")
    
    for slug, state in engine.dashboard_ui.items():
        print(f"{slug:<32} | {state['TTR']:<6} | ${state['Mid_Price']:<7.2f} | {state['Imbalance']:<5.1f}x | {state['Status']}")
    print("="*70 + "\n")

# ==========================================
# 4. MAIN INGESTION LOOP
# ==========================================
def main():
    logger.info("Starting V6.14 Polymarket Endgame Engine [Cloud Edition]...")
    bot = V614_TradingEngine()
    
    # Mock data to simulate the exact final 60 seconds of a volatile market
    test_feed = [
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 65, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.85, 'Bid_Price': 0.14, 'Imbalance_Ratio': 2.2, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6000},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 60, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.88, 'Bid_Price': 0.12, 'Imbalance_Ratio': 4.2, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6050},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 55, 'Winning_Side': 'YES', 'Losing_Side': 'NO', 'Mid_Price': 0.89, 'Bid_Price': 0.11, 'Imbalance_Ratio': 4.3, 'Local_Bid_Vol': 1500, 'Local_Ask_Vol': 6100},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': 0,  'Winning_Side': 'NO',  'Losing_Side': 'YES','Mid_Price': 1.00, 'Bid_Price': 0.00, 'Imbalance_Ratio': 0.0, 'Local_Bid_Vol': 0,    'Local_Ask_Vol': 0},
        {'Slug': 'btc-updown-5m-1781178900', 'TTR': -3, 'Winning_Side': 'NO',  'Losing_Side': 'YES','Mid_Price': 1.00, 'Bid_Price': 0.00, 'Imbalance_Ratio': 0.0, 'Local_Bid_Vol': 0,    'Local_Ask_Vol': 0},
    ]

    for tick in test_feed:
        time.sleep(1) # Simulates data arrival
        
        payload = bot.process_market_tick(tick)
        
        if payload:
            logger.info(f"Generated Order Outbound: {payload}")
            
        # Prints the dashboard table directly to your Docker/Railway logs
        render_cloud_dashboard(bot)

if __name__ == "__main__":
    main()