"""Per-cycle leak classification and rolling-window tracking (DESIGN.md §4.4).

Classifications:
  internal      - both maker and taker filled at same price+qty (self-match)
  external      - maker filled but taker did not (leak A)
  no_fill       - neither filled (B or C, no leak)
  inconclusive  - couldn't tell (transient error)

Rolling window per pair. Alert fires when (in last `window` cycles)
external/total > alert_threshold AND total >= min_samples.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, Optional


class Classification(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"
    NO_FILL = "no_fill"
    INCONCLUSIVE = "inconclusive"


@dataclass
class LeakState:
    window: Deque[Classification] = field(default_factory=lambda: deque(maxlen=100))
    last_alert_ts: float = 0.0  # epoch seconds, 0 = never

    def record(self, c: Classification) -> None:
        self.window.append(c)

    def stats(self) -> dict:
        total = len(self.window)
        internal = sum(1 for x in self.window if x == Classification.INTERNAL)
        external = sum(1 for x in self.window if x == Classification.EXTERNAL)
        no_fill = sum(1 for x in self.window if x == Classification.NO_FILL)
        inconclusive = sum(1 for x in self.window if x == Classification.INCONCLUSIVE)
        fills = internal + external
        internal_ratio = internal / fills if fills > 0 else 1.0
        external_ratio = external / fills if fills > 0 else 0.0
        return {
            "total": total,
            "internal": internal,
            "external": external,
            "no_fill": no_fill,
            "inconclusive": inconclusive,
            "internal_ratio": internal_ratio,
            "external_ratio": external_ratio,
        }


class LeakMonitor:
    def __init__(
        self,
        *,
        external_alert_threshold: float = 0.10,  # 10% external triggers alert
        alert_cooldown_seconds: float = 1800,
        min_samples_for_alert: int = 20,
    ):
        self.threshold = external_alert_threshold
        self.cooldown = alert_cooldown_seconds
        self.min_samples = min_samples_for_alert
        self.states: Dict[str, LeakState] = {}

    def get(self, pair: str) -> LeakState:
        if pair not in self.states:
            self.states[pair] = LeakState()
        return self.states[pair]

    def record(self, pair: str, c: Classification) -> None:
        self.get(pair).record(c)

    def should_alert(self, pair: str) -> Optional[dict]:
        st = self.get(pair)
        s = st.stats()
        if s["total"] < self.min_samples:
            return None
        fills = s["internal"] + s["external"]
        if fills < self.min_samples:
            return None
        if s["external_ratio"] < self.threshold:
            return None
        now = time.time()
        if now - st.last_alert_ts < self.cooldown:
            return None
        st.last_alert_ts = now
        return s
