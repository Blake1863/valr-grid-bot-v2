import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.maker_selector import MakerState


def test_side_random_distribution():
    random.seed(42)
    s = MakerState()
    sides = [s.select_side() for _ in range(200)]
    sell_count = sides.count("SELL")
    buy_count = sides.count("BUY")
    # Should be roughly 50/50; allow wide margin (30-70%)
    assert 0.30 <= sell_count / 200 <= 0.70
    assert sell_count + buy_count == 200


def test_side_hard_cap_max_3_consecutive():
    random.seed(99)
    s = MakerState()
    # Force 3 consecutive SELLs
    s.last_side_is_sell = True
    s.side_consecutive = 3
    # Next pick must be BUY (forced flip)
    assert s.select_side() == "BUY"
    # Now force 3 consecutive BUYs
    s.last_side_is_sell = False
    s.side_consecutive = 3
    assert s.select_side() == "SELL"


def test_side_history_bias():
    random.seed(55)
    s = MakerState()
    # Pretend last 10 cycles were all SELL
    for _ in range(10):
        s.side_history.append(True)
    s.last_side_is_sell = True
    s.side_consecutive = 1
    # Run 200 selections with fresh bias each time
    buy_count = 0
    n = 200
    for _ in range(n):
        s.side_history.clear()
        for _ in range(10):
            s.side_history.append(True)
        s.side_consecutive = 0
        if s.select_side() == "BUY":
            buy_count += 1
    # With history all-SELL, sell_prob = 0.30, so buy_prob = 0.70
    assert buy_count > n // 2


def test_consecutive_cap():
    random.seed(0)
    s = MakerState()
    # Force cap: prime history with 4 consecutive same
    s.last_maker_is_cms1 = True
    s.consecutive_same = 5
    new_pick = s.select_account(max_consecutive=5)
    # Should force-flip
    assert new_pick is False


def test_history_bias_pushes_balance():
    random.seed(123)
    s = MakerState()
    # Pretend last 10 cycles were all CMS1
    for _ in range(10):
        s.history.append(True)
    # Run 50 selections, count CMS2 picks; with bias > 50% should pick CMS2
    cms2_count = 0
    n = 200
    for _ in range(n):
        # Reset consecutive_same so it doesn't trigger flip
        s.consecutive_same = 0
        # Re-prime history every iteration so bias keeps applying
        s.history.clear()
        for _ in range(10):
            s.history.append(True)
        if not s.select_account():
            cms2_count += 1
    # With history all-CMS1, cms1_prob = 0.5 - 10/20 = 0.0 (clamped to 0.30),
    # so cms2_prob = 0.70. Expect ~140 of 200 to be CMS2; allow wide margin.
    assert cms2_count > n // 2
