"""Where to place the maker inside the spread.

Strategy (DESIGN.md §4.2):
  - SELL maker: best_ask - 1 tick  (still strictly above best_bid)
  - BUY  maker: best_bid + 1 tick  (still strictly below best_ask)
  - Skip cycle if spread < min_spread_ticks * tick (no room for postOnly)
  - Skip cycle if spread / mid > max_spread_bps (broken market)

Returns (decision_kind, maker_price_or_None, reason_or_None).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from .pair_meta import PairMeta, round_to_tick


class QuoteDecision(str, Enum):
    PLACE = "PLACE"
    SKIP_SPREAD_TIGHT = "SKIP_SPREAD_TIGHT"
    SKIP_SPREAD_WIDE = "SKIP_SPREAD_WIDE"
    SKIP_NO_ROOM = "SKIP_NO_ROOM"


@dataclass(frozen=True)
class QuoteResult:
    decision: QuoteDecision
    price: Optional[Decimal] = None
    reason: Optional[str] = None


def decide(
    meta: PairMeta,
    best_bid: Decimal,
    best_ask: Decimal,
    side: str,
    *,
    min_spread_ticks: int = 2,
    max_spread_bps: int = 200,
) -> QuoteResult:
    """side: 'BUY' or 'SELL' (the MAKER's side)."""
    # Hard floor: a wash needs >=2 ticks of spread so the maker sits 1 tick
    # inside with room for our own IOC taker to fill at the maker price without
    # crossing or colliding with external orders. Never allow <2 (Blake 2026-06-18).
    if min_spread_ticks < 2:
        min_spread_ticks = 2
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return QuoteResult(QuoteDecision.SKIP_SPREAD_TIGHT, reason=f"degenerate book bid={best_bid} ask={best_ask}")

    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2

    # Tight spread — no room for postOnly inside
    if spread < meta.tick_size * min_spread_ticks:
        return QuoteResult(
            QuoteDecision.SKIP_SPREAD_TIGHT,
            reason=f"spread={spread} < {min_spread_ticks}*tick={meta.tick_size * min_spread_ticks}",
        )

    # Wide spread — likely broken or extremely illiquid; refuse
    if mid > 0:
        bps = (spread / mid) * 10000
        if bps > max_spread_bps:
            return QuoteResult(
                QuoteDecision.SKIP_SPREAD_WIDE,
                reason=f"spread={bps:.0f}bps > {max_spread_bps}bps",
            )

    side_u = side.upper()
    if side_u == "SELL":
        candidate = round_to_tick(best_ask - meta.tick_size, meta.tick_size)
        if candidate <= best_bid:
            return QuoteResult(QuoteDecision.SKIP_NO_ROOM, reason=f"sell candidate {candidate} <= bid {best_bid}")
        return QuoteResult(QuoteDecision.PLACE, price=candidate)
    elif side_u == "BUY":
        candidate = round_to_tick(best_bid + meta.tick_size, meta.tick_size)
        if candidate >= best_ask:
            return QuoteResult(QuoteDecision.SKIP_NO_ROOM, reason=f"buy candidate {candidate} >= ask {best_ask}")
        return QuoteResult(QuoteDecision.PLACE, price=candidate)
    else:
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
