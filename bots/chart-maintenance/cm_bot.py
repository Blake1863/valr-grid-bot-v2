#!/usr/bin/env python3
"""
Chart Maintenance Bot - Main Orchestrator

Coordinates inventory management, cycle tracking, and order execution
for chart maintenance across VALR spot and futures pairs.
"""
import os
import sys
import time
import json
import random
import signal
from datetime import datetime
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from dotenv import load_dotenv
from pair_registry import PairRegistry
from inventory_manager import InventoryManager
from order_executor import OrderExecutor
from cycle_tracker import CycleTracker
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from price_feed import get_price_feed

class ChartMaintenanceBot:
    def __init__(self, config_path: str = "config.json"):
        # Load config
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        
        # Load API keys from secure env file
        load_dotenv("/home/admin/.openclaw/secrets/cm_secrets.env")
        
        self.cm1_key = os.getenv("CM1_API_KEY")
        self.cm1_secret = os.getenv("CM1_API_SECRET")
        self.cm2_key = os.getenv("CM2_API_KEY")
        self.cm2_secret = os.getenv("CM2_API_SECRET")
        
        if not all([self.cm1_key, self.cm1_secret, self.cm2_key, self.cm2_secret]):
            raise ValueError("Missing API keys in secrets file")
        
        # Initialize components
        self.pair_registry = PairRegistry(
            self.cm1_key, self.cm1_secret, 
            self.config["api_base_url"]
        )
        
        self.inventory_manager = InventoryManager(
            self.cm1_key, self.cm1_secret,
            self.cm2_key, self.cm2_secret,
            self.config["api_base_url"]
        )
        
        self.order_executor = OrderExecutor(
            self.cm1_key, self.cm1_secret,
            self.cm2_key, self.cm2_secret,
            self.config["api_base_url"]
        )
        
        self.cycle_tracker = CycleTracker(
            state_file=str(Path(__file__).parent / "state.json")
        )
        
        # State
        self.running = False
        self.pairs = {}
        self.last_rebalance_time = 0
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        print(f"\n[{datetime.now().isoformat()}] Received signal {signum}, shutting down...")
        self.running = False
        self.cycle_tracker.save_state()
        sys.exit(0)
    
    def fetch_pairs(self, orderbook_only: bool = False):
        """Fetch all active pairs from VALR"""
        self.pairs = self.pair_registry.fetch_all_pairs(orderbook_only=orderbook_only)
        return len(self.pairs) > 0
    
    def acquire_initial_inventory(self, test_mode: bool = False):
        """
        Acquire initial inventory for all pairs
        Each account should have enough for 4 cycles
        In test mode, just check the test pair
        """
        print(f"\n[{datetime.now().isoformat()}] === INITIAL INVENTORY ACQUISITION ===")
        
        # Check current balances
        cm1_bals = self.inventory_manager.get_balances("CM1")
        cm2_bals = self.inventory_manager.get_balances("CM2")
        
        print(f"CM1 USDC: {cm1_bals.get('USDC', {}).available if cm1_bals.get('USDC') else 0}")
        print(f"CM2 USDC: {cm2_bals.get('USDC', {}).available if cm2_bals.get('USDC') else 0}")
        
        if test_mode:
            # Just check the test pair
            symbol = self.config["test_pair"]
            if symbol not in self.pairs:
                print(f"[ERROR] Test pair {symbol} not found")
                return
            
            pair_info = self.pairs[symbol]
            if pair_info.type == "futures":
                base_asset = symbol.replace("USDTPERP", "").replace("USDT", "")
            else:
                base_asset = symbol.replace("USDT", "")
            
            required_qty = self.inventory_manager.calculate_required_inventory(
                pair_info.min_qty,
                self.config["inventory_cycles_buffer"]
            )
            
            cm1_has = cm1_bals.get(base_asset)
            cm2_has = cm2_bals.get(base_asset)
            
            print(f"\n  {symbol}:")
            print(f"    Required: {required_qty} {base_asset}")
            print(f"    CM1 has: {cm1_has.available if cm1_has else 0} {base_asset}")
            print(f"    CM2 has: {cm2_has.available if cm2_has else 0} {base_asset}")
            
            if (not cm1_has or cm1_has.available < required_qty) or (not cm2_has or cm2_has.available < required_qty):
                print(f"    ⚠️  Insufficient inventory - will attempt trades anyway")
            else:
                print(f"    ✅ Inventory OK")
        else:
            # Full check for all pairs (production mode)
            for symbol, pair_info in self.pairs.items():
                if pair_info.type == "futures":
                    base_asset = symbol.replace("USDTPERP", "").replace("USDT", "")
                else:
                    base_asset = symbol.replace("USDT", "")
                
                required_qty = self.inventory_manager.calculate_required_inventory(
                    pair_info.min_qty,
                    self.config["inventory_cycles_buffer"]
                )
                
                cm1_has = cm1_bals.get(base_asset)
                cm2_has = cm2_bals.get(base_asset)
                
                cm1_ok = cm1_has and cm1_has.available >= required_qty
                cm2_ok = cm2_has and cm2_has.available >= required_qty
                
                if not cm1_ok or not cm2_ok:
                    print(f"\n  {symbol}: NEEDS INVENTORY")
                else:
                    print(f"  {symbol}: OK")
        
        print(f"\n[{datetime.now().isoformat()}] Initial inventory check complete")
    
    def check_and_rebalance(self):
        """Check inventory levels and rebalance if needed"""
        current_time = time.time()
        
        # Only rebalance if interval has passed
        if current_time - self.last_rebalance_time < self.config["rebalance_interval_seconds"]:
            return
        
        print(f"\n[{datetime.now().isoformat()}] === INVENTORY REBALANCE CHECK ===")
        
        rebalance_needed = False
        total_actions = 0
        
        for symbol, pair_info in self.pairs.items():
            result = self.inventory_manager.rebalance_if_needed(
                symbol, pair_info,
                self.config["rebalance_threshold_cycles"]
            )
            
            if result["needs_rebalance"]:
                rebalance_needed = True
                total_actions += len(result["actions"])
                
                print(f"\n  {symbol}: REBALANCE NEEDED")
                for action in result["actions"]:
                    print(f"    Transfer {action['amount']} {action['asset']} from {action['from']} to {action['to']}")
                
                # Execute rebalance
                self.inventory_manager.execute_rebalance(result["actions"])
        
        if not rebalance_needed:
            print(f"  No rebalancing needed")
        
        self.last_rebalance_time = current_time
        print(f"\n[{datetime.now().isoformat()}] Rebalance check complete ({total_actions} actions)")
    
    def execute_cycle_for_pair(self, symbol: str):
        """Execute a single chart maintenance cycle for a pair"""
        if symbol not in self.pairs:
            print(f"[ERROR] Unknown pair: {symbol}")
            return False
        
        pair_info = self.pairs[symbol]
        
        # Get current cycle state
        # CRITICAL: Use SAME account for both sides to avoid self-trade prevention
        maker_account = "CM1"  # Always use CM1 for wash trades
        taker_account = "CM1"  # Same account = true wash trades possible
        maker_side = self.cycle_tracker.get_maker_side(symbol)
        taker_side = "SELL" if maker_side == "BUY" else "BUY"
        
        # Get mark price first
        mark_price = self.order_executor.get_mark_price(symbol, pair_info.type)
        if not mark_price:
            print(f"[ERROR] Failed to get mark price for {symbol}")
            return False
        
        price = round(mark_price, pair_info.price_precision)
        
        # Generate random quantity: 1.1x to 3x minimum order size
        # ALWAYS round UP to ensure we meet minimum order size
        import time, math
        random.seed(int(time.time() * 1000000) % (2**32))
        
        min_qty = pair_info.min_qty
        multiplier = random.uniform(
            self.config["qty_range_min_multiplier"],
            self.config["qty_range_max_multiplier"]
        )
        raw_qty = min_qty * multiplier
        
        # ALWAYS round UP to ensure quantity >= minimum
        precision_factor = 10 ** pair_info.qty_precision
        qty = math.ceil(raw_qty * precision_factor) / precision_factor
        
        # Also ensure quantity meets 1 USDT minimum total value (round UP)
        min_value_qty_raw = 1.1 / mark_price
        min_value_qty = math.ceil(min_value_qty_raw * precision_factor) / precision_factor
        
        # Use the higher of calculated qty or minimum value qty
        qty = max(qty, min_value_qty)
        
        print(f"\n[{datetime.now().isoformat()}] === CYCLE: {symbol} ===")
        print(f"  Maker: {maker_account} ({maker_side})")
        print(f"  Taker: {taker_account} ({taker_side})")
        print(f"  Qty: {qty} (min={min_qty}, mult={multiplier:.3f})")
        print(f"  Price: {price}")
        
        # Execute the cycle - pass maker_side so orders use correct sides
        result = self.order_executor.execute_cycle(
            symbol, pair_info,
            maker_account, taker_account,
            qty, maker_side
        )
        
        if result["success"]:
            # Record cycle
            external_fill = result.get("external_fill", False)
            self.cycle_tracker.record_cycle(symbol, external_fill)
            self.cycle_tracker.save_state()
            
            if external_fill:
                print(f"  ⚠️  External fill detected - inventory will rebalance on next check")
            else:
                print(f"  ✅ Cycle complete")
        else:
            print(f"  ❌ Cycle failed: {result.get('error')}")
        
        return result["success"]
    
    def run_test_pair(self):
        """Run bot on a single test pair (perp or spot)"""
        # Support both test_pair (perp) and test_spot_pair (spot)
        test_pair = self.config.get("test_spot_pair") or self.config.get("test_pair")
        
        print(f"\n[{datetime.now().isoformat()}] === STARTING TEST MODE ===")
        print(f"  Test pair: {test_pair}")
        print(f"  Type: {self.pairs.get(test_pair, type('obj', (object,), {'type': 'unknown'})()).type}")
        print(f"  Cycle interval: {self.config['cycle_interval_seconds']}s")
        
        if test_pair not in self.pairs:
            print(f"[ERROR] Test pair {test_pair} not found in active pairs")
            return False
        
        self.running = True
        cycle_count = 0
        
        while self.running:
            try:
                # Execute cycle
                success = self.execute_cycle_for_pair(test_pair)
                cycle_count += 1
                
                # Check rebalance
                self.check_and_rebalance()
                
                # Wait for next cycle
                print(f"\n[{datetime.now().isoformat()}] Waiting {self.config['cycle_interval_seconds']}s for next cycle...")
                time.sleep(self.config["cycle_interval_seconds"])
                
            except Exception as e:
                print(f"[ERROR] Cycle failed: {e}")
                time.sleep(5)  # Brief pause before retry
        
        return True
    
    def run_all_pairs(self):
        """Run bot on all pairs"""
        print(f"\n[{datetime.now().isoformat()}] === STARTING ALL PAIRS MODE ===")
        print(f"  Total pairs: {len(self.pairs)}")
        print(f"  Cycle interval: {self.config['cycle_interval_seconds']}s per pair")
        
        self.running = True
        cycle_count = 0
        
        while self.running:
            try:
                # Execute cycle for each pair
                for symbol in self.pairs:
                    if not self.running:
                        break
                    
                    self.execute_cycle_for_pair(symbol)
                    
                    # Small delay between pairs to avoid rate limits
                    time.sleep(1)
                
                cycle_count += 1
                
                # Check rebalance
                self.check_and_rebalance()
                
                # Wait for next cycle
                print(f"\n[{datetime.now().isoformat()}] Waiting {self.config['cycle_interval_seconds']}s for next cycle round...")
                time.sleep(self.config["cycle_interval_seconds"])
                
            except Exception as e:
                print(f"[ERROR] Cycle failed: {e}")
                time.sleep(5)
        
        return True
    
    def start(self, mode: str = "test"):
        """Start the bot"""
        print(f"\n{'='*60}")
        print(f"CHART MAINTENANCE BOT - Starting")
        print(f"{'='*60}")
        print(f"Time: {datetime.now().isoformat()}")
        
        # Initialize WebSocket price feed
        print(f"\n[{datetime.now().isoformat()}] === INITIALIZING PRICE FEED ===")
        self.price_feed = get_price_feed()
        if not self.price_feed.connect():
            print("[WARN] Price feed connection failed, will use REST fallback")
        
        # Fetch pairs based on mode
        if mode == "test":
            print(f"Mode: TEST (single perp pair)")
            print(f"Pair: {self.config['test_pair']}")
            self.fetch_pairs(test_mode=True)
            self._subscribe_to_price_feed()
            self.acquire_initial_inventory(test_mode=True)
            return self.run_test_pair()
        
        elif mode == "test_spot":
            print(f"Mode: TEST SPOT (single spot pair)")
            print(f"Pair: {self.config['test_spot_pair']}")
            # Fetch all pairs to get the test spot pair info
            self.pair_registry.fetch_all_pairs()
            symbol = self.config['test_spot_pair']
            self.pairs = {symbol: self.pair_registry.pairs.get(symbol)} if self.pair_registry.pairs.get(symbol) else {}
            if not self.pairs:
                print(f"[ERROR] Test spot pair {symbol} not found in registry")
                return
            print(f"Active pairs: {list(self.pairs.keys())}")
            self._subscribe_to_price_feed()
            self.acquire_initial_inventory(test_mode=True)
            return self.run_test_pair()
        
        elif mode == "phase1":
            print(f"Mode: PHASE 1 (Perp Pairs)")
            print(f"Pairs: {len(self.config['phase1_pairs'])} perp pairs")
            # Fetch all pairs first
            self.pair_registry.fetch_all_pairs()
            # Filter to phase1 pairs
            self.pairs = {p: self.pair_registry.pairs.get(p) for p in self.config['phase1_pairs'] if self.pair_registry.pairs.get(p)}
            print(f"Active pairs: {list(self.pairs.keys())}")
            self._subscribe_to_price_feed()
            self.acquire_initial_inventory(test_mode=False)
            return self.run_all_pairs()
        
        elif mode == "phase2":
            print(f"Mode: PHASE 2 (Spot Pairs)")
            # Fetch all pairs first
            self.pair_registry.fetch_all_pairs()
            
            # Use enabled pairs list (disabled pairs are marked for later)
            enabled = self.config.get('phase2_pairs_enabled', [])
            disabled = self.config.get('phase2_pairs_disabled', [])
            
            self.pairs = {p: self.pair_registry.pairs.get(p) for p in enabled if self.pair_registry.pairs.get(p)}
            
            print(f"Enabled: {len(enabled)} pairs")
            print(f"Disabled (marked for later): {len(disabled)} pairs")
            print(f"Active pairs: {list(self.pairs.keys())}")
            self._subscribe_to_price_feed()
            self.acquire_initial_inventory(test_mode=False)
            return self.run_all_pairs()
        
        elif mode == "all":
            print(f"Mode: ALL PAIRS")
            self.fetch_pairs()
            self._subscribe_to_price_feed()
            self.acquire_initial_inventory(test_mode=False)
            return self.run_all_pairs()
    
    def _subscribe_to_price_feed(self):
        """Subscribe to WebSocket price feed for all active pairs"""
        if not hasattr(self, 'price_feed') or not self.price_feed:
            return
        
        symbols = list(self.pairs.keys())
        print(f"[{datetime.now().isoformat()}] Subscribing to {len(symbols)} price feeds...")
        self.price_feed.subscribe_many(symbols)
        
        # Wait for initial prices (up to 10 seconds)
        print(f"[{datetime.now().isoformat()}] Waiting for price updates...")
        got_prices = self.price_feed.wait_for_prices(symbols, timeout_seconds=10.0)
        
        # Show subscribed prices
        print(f"\n[{datetime.now().isoformat()}] === INITIAL PRICES ===")
        ws_count = 0
        for symbol in symbols[:10]:  # Show first 10
            price = self.price_feed.get_price(symbol)
            if price:
                print(f"  {symbol}: {price:.8f} (WS)")
                ws_count += 1
            else:
                print(f"  {symbol}: (no price yet)")
        if len(symbols) > 10:
            print(f"  ... and {len(symbols) - 10} more pairs")
        
        if ws_count > 0:
            print(f"\n✅ Got {ws_count}/{len(symbols)} prices from WebSocket")
        else:
            print(f"\n⚠️  No WebSocket prices received, will use REST fallback")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Chart Maintenance Bot for VALR")
    parser.add_argument("--test", action="store_true", help="Run in test mode (single perp pair)")
    parser.add_argument("--test-spot", action="store_true", help="Run in test mode (single spot pair)")
    parser.add_argument("--phase1", action="store_true", help="Run on perp pairs (phase 1)")
    parser.add_argument("--phase2", action="store_true", help="Run on spot pairs (phase 2)")
    parser.add_argument("--all", action="store_true", help="Run on all pairs")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    
    args = parser.parse_args()
    
    # Determine mode
    if args.phase1:
        mode = "phase1"
    elif args.phase2:
        mode = "phase2"
    elif args.all:
        mode = "all"
    elif args.test_spot:
        mode = "test_spot"
    else:
        mode = "test"
    
    # Change to bot directory
    os.chdir(Path(__file__).parent)
    
    bot = ChartMaintenanceBot(args.config)
    bot.start(mode=mode)

if __name__ == "__main__":
    main()
