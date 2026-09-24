"""Maker-account/side selection (DESIGN.md §4.1).

Per pair:
  - Random base 50/50 between CMS1 and CMS2.
  - 10-cycle history bias: if one account has been maker > 60% of recent cycles,
    weight selection toward the other (up to 70/30 cap).
  - Hard cap: max 5 consecutive same-account makers in a row.
  - Random base 50/50 BUY vs SELL, with 10-cycle history bias (same logic as account selection).
  - Hard cap: max 3 consecutive same-side makers in a row.

Inventory feedback is applied at the cycle layer, not here, since it requires
a balance fetch the selector shouldn't be coupled to.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class MakerState:
    history: Deque[bool] = field(default_factory=lambda: deque(maxlen=10))  # True=CMS1
    consecutive_same: int = 0
    last_maker_is_cms1: bool | None = None
    side_history: Deque[bool] = field(default_factory=lambda: deque(maxlen=10))  # True=SELL
    side_consecutive: int = 0
    last_side_is_sell: bool | None = None

    def select_account(self, max_consecutive: int = 5) -> bool:
        # Hard cap: force flip
        if self.last_maker_is_cms1 is not None and self.consecutive_same >= max_consecutive:
            new_is_cms1 = not self.last_maker_is_cms1
        else:
            cms1_prob = 0.5
            if len(self.history) >= 10:
                cms1_count = sum(1 for x in self.history if x)
                cms2_count = len(self.history) - cms1_count
                if cms1_count > cms2_count:
                    cms1_prob = max(0.30, 0.5 - (cms1_count - cms2_count) / 20.0)
                elif cms2_count > cms1_count:
                    cms1_prob = min(0.70, 0.5 + (cms2_count - cms1_count) / 20.0)
            new_is_cms1 = random.random() < cms1_prob

        if new_is_cms1 == self.last_maker_is_cms1:
            self.consecutive_same += 1
        else:
            self.consecutive_same = 1
        self.last_maker_is_cms1 = new_is_cms1
        self.history.append(new_is_cms1)
        return new_is_cms1

    def select_side(self) -> str:
        """Random BUY/SELL with history bias and consecutive-side cap.

        - Base 50/50.
        - If one side > 60% of last 10 cycles, bias toward the other (up to 70/30).
        - Hard cap: max 3 consecutive same-side selections → force flip.
        """
        if self.last_side_is_sell is not None and self.side_consecutive >= 3:
            is_sell = not self.last_side_is_sell
        else:
            sell_prob = 0.5
            if len(self.side_history) >= 10:
                sell_count = sum(1 for x in self.side_history if x)
                buy_count = len(self.side_history) - sell_count
                if sell_count > buy_count:
                    sell_prob = max(0.30, 0.5 - (sell_count - buy_count) / 20.0)
                elif buy_count > sell_count:
                    sell_prob = min(0.70, 0.5 + (buy_count - sell_count) / 20.0)
            is_sell = random.random() < sell_prob

        if is_sell == self.last_side_is_sell:
            self.side_consecutive += 1
        else:
            self.side_consecutive = 1
        self.last_side_is_sell = is_sell
        self.side_history.append(is_sell)
        return "SELL" if is_sell else "BUY"
