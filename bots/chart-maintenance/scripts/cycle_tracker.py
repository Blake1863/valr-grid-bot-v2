#!/usr/bin/env python3
"""
Cycle Tracker - Tracks cycle counts per pair and manages role switching
"""
import json
import os
from typing import Dict, Optional
from datetime import datetime
from dataclasses import dataclass, asdict

@dataclass
class PairState:
    cycle_count: int  # Total cycles completed
    current_phase: int  # 0-2: CM1 maker, 3-5: CM2 maker
    last_cycle_time: str
    total_trades: int
    external_fills: int

class CycleTracker:
    def __init__(self, state_file: str = "state.json"):
        self.state_file = state_file
        self.pair_states: Dict[str, PairState] = {}
        self.load_state()
    
    def load_state(self):
        """Load state from file"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                
                for pair, state in data.items():
                    self.pair_states[pair] = PairState(**state)
                
                print(f"[{datetime.now().isoformat()}] Loaded state for {len(self.pair_states)} pairs")
            except Exception as e:
                print(f"[ERROR] Loading state: {e}")
                self.pair_states = {}
        else:
            print(f"[{datetime.now().isoformat()}] No existing state file, starting fresh")
    
    def save_state(self):
        """Save state to file"""
        try:
            data = {}
            for pair, state in self.pair_states.items():
                data[pair] = asdict(state)
            
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            print(f"[{datetime.now().isoformat()}] State saved")
        except Exception as e:
            print(f"[ERROR] Saving state: {e}")
    
    def get_pair_state(self, pair: str) -> PairState:
        """Get state for a pair, creating if doesn't exist"""
        if pair not in self.pair_states:
            self.pair_states[pair] = PairState(
                cycle_count=0,
                current_phase=0,
                last_cycle_time=datetime.now().isoformat(),
                total_trades=0,
                external_fills=0
            )
        
        return self.pair_states[pair]
    
    def get_maker_account(self, pair: str) -> str:
        """
        Determine which account should be the maker based on cycle count
        
        Cycles 0, 1, 2 (phase 0): CM1 is maker (buys), CM2 is taker (sells)
        Cycles 3, 4, 5 (phase 1): CM2 is maker (buys), CM1 is taker (sells)
        Then repeats
        
        Returns: "CM1" or "CM2"
        """
        state = self.get_pair_state(pair)
        phase = state.cycle_count // 3  # Integer division: 0-2=phase 0, 3-5=phase 1
        
        if phase % 2 == 0:
            return "CM1"  # Even phase: CM1 is maker
        else:
            return "CM2"  # Odd phase: CM2 is maker
    
    def get_taker_account(self, pair: str) -> str:
        """Get the taker account (opposite of maker)"""
        maker = self.get_maker_account(pair)
        return "CM2" if maker == "CM1" else "CM1"
    
    def get_maker_side(self, pair: str) -> str:
        """
        Determine which side the maker should take
        
        We alternate buy/sell for the maker to balance inventory
        Even cycles: maker buys
        Odd cycles: maker sells
        """
        state = self.get_pair_state(pair)
        if state.cycle_count % 2 == 0:
            return "BUY"
        else:
            return "SELL"
    
    def record_cycle(self, pair: str, external_fill: bool = False):
        """Record a completed cycle"""
        state = self.get_pair_state(pair)
        state.cycle_count += 1
        state.last_cycle_time = datetime.now().isoformat()
        state.total_trades += 1
        
        if external_fill:
            state.external_fills += 1
        
        # Update phase based on new cycle count
        state.current_phase = (state.cycle_count // 3) % 2
        
        print(f"[{datetime.now().isoformat()}] Cycle recorded for {pair}: cycle #{state.cycle_count}, phase {state.current_phase}")
    
    def get_cycle_summary(self, pair: str = None) -> dict:
        """Get summary of cycle counts"""
        if pair:
            state = self.get_pair_state(pair)
            return {
                "pair": pair,
                "cycle_count": state.cycle_count,
                "phase": state.current_phase,
                "maker_account": self.get_maker_account(pair),
                "taker_account": self.get_taker_account(pair),
                "maker_side": self.get_maker_side(pair),
                "last_cycle": state.last_cycle_time,
                "total_trades": state.total_trades,
                "external_fills": state.external_fills
            }
        else:
            summary = {}
            for pair in self.pair_states:
                summary[pair] = self.get_cycle_summary(pair)
            return summary
    
    def reset_pair(self, pair: str):
        """Reset state for a specific pair"""
        if pair in self.pair_states:
            del self.pair_states[pair]
            self.save_state()
            print(f"[{datetime.now().isoformat()}] Reset state for {pair}")
    
    def reset_all(self):
        """Reset all state"""
        self.pair_states = {}
        self.save_state()
        print(f"[{datetime.now().isoformat()}] Reset all state")

if __name__ == "__main__":
    # Test
    tracker = CycleTracker("test_state.json")
    
    # Test role switching
    for i in range(7):
        pair = "SOLUSDTPERP"
        maker = tracker.get_maker_account(pair)
        side = tracker.get_maker_side(pair)
        print(f"Cycle {i}: Maker={maker}, Side={side}")
        tracker.record_cycle(pair)
    
    tracker.save_state()
    
    print("\nSummary:")
    print(json.dumps(tracker.get_cycle_summary("SOLUSDTPERP"), indent=2))
