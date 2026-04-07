#!/usr/bin/env python3
"""Quick simulation of random taker direction."""
import random

print("="*60)
print("🎲 Randomized Taker - Dry Run (100 cycles)")
print("="*60)

# Simulate 100 cycles with Option B
makers = []
history = []
consecutive = 0
last = None

for i in range(100):
    # Bias based on last 10
    if len(history) >= 10:
        cms1_pct = history.count('CMS1') / 10
        # Bias toward underrepresented
        if cms1_pct > 0.6:
            prob_cms2 = 0.7
        elif cms1_pct < 0.4:
            prob_cms2 = 0.3
        else:
            prob_cms2 = 0.5
    else:
        prob_cms2 = 0.5
    
    # Force flip after 5 same
    if consecutive >= 5:
        maker = 'CMS1' if last == 'CMS2' else 'CMS2'
        consecutive = 1
    else:
        maker = 'CMS2' if random.random() < prob_cms2 else 'CMS1'
        consecutive = consecutive + 1 if maker == last else 1
    
    last = maker
    history.append(maker)
    if len(history) > 10:
        history.pop(0)
    makers.append(maker)

# Analyze
cms1 = makers.count('CMS1')
cms2 = makers.count('CMS2')

# Find max streak
max_streak = 1
streak = 1
for i in range(1, len(makers)):
    if makers[i] == makers[i-1]:
        streak += 1
        max_streak = max(max_streak, streak)
    else:
        streak = 1

# Count changes
changes = sum(1 for i in range(1, len(makers)) if makers[i] != makers[i-1])

print(f"\n📊 Results (100 cycles):")
print(f"  CMS1 as maker: {cms1} ({cms1}%)")
print(f"  CMS2 as maker: {cms2} ({cms2}%)")
print(f"  Balance: {'✅ Good' if 40 <= cms1 <= 60 else '⚠️ Drift'}")
print(f"  Max streak: {max_streak} cycles")
print(f"  Direction changes: {changes}")

print(f"\n📈 Last 30 cycles pattern:")
pattern = ''.join(['1' if m == 'CMS1' else '2' for m in makers[-30:]])
print(f"  {pattern}")

print(f"\n📈 Sample (cycles 1-20):")
sample = ''.join(['1' if m == 'CMS1' else '2' for m in makers[:20]])
print(f"  {sample}")

print(f"\n✅ Verdict: {'READY - Looks organic!' if 2 <= max_streak <= 5 and 40 <= cms1 <= 60 else '⚠️ Needs tuning'}")
print("="*60)
