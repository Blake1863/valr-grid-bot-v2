#!/usr/bin/env python3
"""
Simulate randomized taker direction vs current deterministic pattern.
Analyzes last 100 cycles and shows what randomization would produce.
"""

import random
import re
from collections import Counter

def parse_log_line(line):
    """Extract cycle number and maker from log line."""
    # Pattern: Phase 0: CMS1 is SELL maker (cycle #5856)
    match = re.search(r'Phase [01]: (CMS[12]) is SELL maker \(cycle #(\d+)\)', line)
    if match:
        return {'cycle': int(match.group(2)), 'maker': match.group(1)}
    return None

def load_cycles(log_path, count=100):
    """Load last N cycles from log file."""
    cycles = []
    with open(log_path) as f:
        lines = f.readlines()
    
    # Find last N unique cycles
    seen_cycles = set()
    for line in reversed(lines):
        if 'Phase' in line and 'SELL maker' in line:
            parsed = parse_log_line(line)
            if parsed and parsed['cycle'] not in seen_cycles:
                cycles.append(parsed)
                seen_cycles.add(parsed['cycle'])
                if len(cycles) >= count:
                    break
    
    return list(reversed(cycles))

def simulate_random_option_b(cycles):
    """
    Option B: Random with balance tracking
    - Track last 10 makers
    - Bias toward underrepresented side
    - Hard cap: max 5 consecutive same side
    """
    simulated = []
    maker_history = []  # Last 10 makers
    consecutive_same = 0
    last_maker = None
    
    for cycle in cycles:
        cycle_num = cycle['cycle']
        
        # Calculate bias based on last 10 cycles
        if len(maker_history) >= 10:
            cms1_count = maker_history.count('CMS1')
            cms2_count = maker_history.count('CMS2')
            
            # Bias toward underrepresented side
            if cms1_count > cms2_count:
                # CMS1 sold more, bias toward CMS2
                cms2_probability = 0.5 + (cms1_count - cms2_count) / 20.0
            else:
                # CMS2 sold more, bias toward CMS1
                cms2_probability = 0.5 - (cms2_count - cms1_count) / 20.0
        else:
            cms2_probability = 0.5  # No history, 50/50
        
        # Check consecutive cap
        if consecutive_same >= 5:
            # Force flip
            maker = 'CMS1' if last_maker == 'CMS2' else 'CMS2'
            consecutive_same = 1
        else:
            # Random selection with bias
            if random.random() < cms2_probability:
                maker = 'CMS2'
            else:
                maker = 'CMS1'
            
            # Update consecutive counter
            if maker == last_maker:
                consecutive_same += 1
            else:
                consecutive_same = 1
        
        last_maker = maker
        
        # Update history
        maker_history.append(maker)
        if len(maker_history) > 10:
            maker_history.pop(0)
        
        simulated.append({
            'cycle': cycle_num,
            'maker': maker,
            'method': 'random-B'
        })
    
    return simulated

def analyze_pattern(cycles, label):
    """Analyze a pattern for statistics."""
    makers = [c['maker'] for c in cycles]
    
    # Count distribution
    cms1_count = makers.count('CMS1')
    cms2_count = makers.count('CMS2')
    total = len(makers)
    
    # Find longest streak
    max_streak = 1
    current_streak = 1
    for i in range(1, len(makers)):
        if makers[i] == makers[i-1]:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 1
    
    # Count direction changes
    changes = sum(1 for i in range(1, len(makers)) if makers[i] != makers[i-1])
    
    # Detectability score (lower = more organic)
    # Perfect 50/50 with no streaks = very suspicious
    # Some imbalance + some streaks = more organic
    balance_score = abs(cms1_count - cms2_count) / total * 100
    streak_score = max_streak  # Higher streaks = more organic
    change_score = changes / total * 100  # Higher changes = more random
    
    print(f"\n{'='*60}")
    print(f"{label}")
    print(f"{'='*60}")
    print(f"Total cycles: {total}")
    print(f"CMS1 as maker: {cms1_count} ({cms1_count/total*100:.1f}%)")
    print(f"CMS2 as maker: {cms2_count} ({cms2_count/total*100:.1f}%)")
    print(f"Balance: {'✅ Perfect' if balance_score < 5 else '⚠️ Slight' if balance_score < 15 else '❌ Heavy'} ({balance_score:.1f}% imbalance)")
    print(f"Longest streak: {max_streak} cycles same side")
    print(f"Direction changes: {changes} ({change_score:.1f}% of cycles)")
    print(f"\nOrganic score: {'✅ HIGH' if 2 <= max_streak <= 5 and 5 <= balance_score <= 20 else '⚠️ MEDIUM' if max_streak <= 8 else '❌ LOW'}")
    
    # Show last 20 cycles pattern
    print(f"\nLast 20 cycles (M=maker):")
    pattern = ''.join(['1' if m == 'CMS1' else '2' for m in makers[-20:]])
    print(f"  {pattern}")
    
    return {
        'cms1_pct': cms1_count / total * 100,
        'cms2_pct': cms2_count / total * 100,
        'max_streak': max_streak,
        'changes': changes,
        'balance_score': balance_score
    }

def main():
    print("="*60)
    print("🎲 Randomized Taker Direction - Dry Run Simulation")
    print("="*60)
    
    # Load actual cycles from spot bot (more cycles in logs)
    log_path = '/home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/cm-bot-spot.log'
    print(f"\n📊 Loading cycles from {log_path}...")
    
    cycles = load_cycles(log_path, count=100)
    print(f"Loaded {len(cycles)} cycles")
    
    if len(cycles) < 10:
        print("❌ Not enough cycles in log. Run bot longer first.")
        return
    
    # Analyze current deterministic pattern
    print("\n" + "="*60)
    print("CURRENT PATTERN (Deterministic 6-cycle rotation)")
    print("="*60)
    current_stats = analyze_pattern(cycles, "Current Pattern")
    
    # Simulate Option B (random with balance tracking)
    print("\n\n🎲 Simulating Option B (Random with Balance Tracking)...")
    random.seed()  # Use system entropy
    simulated = simulate_random_option_b(cycles)
    simulated_stats = analyze_pattern(simulated, "Simulated Pattern (Option B)")
    
    # Comparison
    print("\n" + "="*60)
    print("📈 COMPARISON")
    print("="*60)
    print(f"{'Metric':<25} {'Current':>12} {'Random-B':>12} {'Improvement':>15}")
    print(f"{'-'*60}")
    print(f"{'CMS1 %':<25} {current_stats['cms1_pct']:>11.1f}% {simulated_stats['cms1_pct']:>11.1f}% {'±' + str(abs(simulated_stats['cms1_pct'] - current_stats['cms1_pct'])):.1f}%")
    print(f"{'Max streak':<25} {current_stats['max_streak']:>12} {simulated_stats['max_streak']:>12} {'✓ More organic' if simulated_stats['max_streak'] > current_stats['max_streak'] else ''}")
    print(f"{'Direction changes':<25} {current_stats['changes']:>12} {simulated_stats['changes']:>12} {'✓ More random' if simulated_stats['changes'] > current_stats['changes'] else ''}")
    print(f"{'Balance score':<25} {current_stats['balance_score']:>11.1f}% {simulated_stats['balance_score']:>11.1f}% {'✓ Better' if 5 <= simulated_stats['balance_score'] <= 20 else ''}")
    
    print("\n" + "="*60)
    print("🎯 RECOMMENDATION")
    print("="*60)
    
    if simulated_stats['max_streak'] >= 2 and simulated_stats['balance_score'] <= 20:
        print("✅ Option B produces more organic-looking trades")
        print("   - Varied streak lengths (2-5 cycles)")
        print("   - Reasonable balance (40-60% split)")
        print("   - Hard to detect as wash trading")
        print("\n🚀 Ready to implement!")
    else:
        print("⚠️  Option B may need tuning")
        print("   - Adjust bias parameters or streak cap")
        print("   - Consider Option C (simpler weighted random)")
    
    print("\n" + "="*60)

if __name__ == "__main__":
    main()
