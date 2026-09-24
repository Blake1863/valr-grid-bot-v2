from decimal import Decimal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pair_meta import PairMeta
from src.quoting import decide, QuoteDecision


def _meta(tick="0.01"):
    return PairMeta("BTCZAR", "BTC", "ZAR", Decimal(tick), 2, Decimal("0.0001"), Decimal("10"), 8)


def test_sell_inside_spread():
    r = decide(_meta(), Decimal("100.00"), Decimal("100.10"), "SELL")
    assert r.decision == QuoteDecision.PLACE
    assert r.price == Decimal("100.09")


def test_buy_inside_spread():
    r = decide(_meta(), Decimal("100.00"), Decimal("100.10"), "BUY")
    assert r.decision == QuoteDecision.PLACE
    assert r.price == Decimal("100.01")


def test_skip_tight_spread_one_tick():
    r = decide(_meta(), Decimal("100.00"), Decimal("100.01"), "SELL")
    assert r.decision == QuoteDecision.SKIP_SPREAD_TIGHT


def test_skip_wide_spread():
    r = decide(_meta(), Decimal("100.00"), Decimal("103.00"), "SELL", max_spread_bps=200)
    assert r.decision == QuoteDecision.SKIP_SPREAD_WIDE


def test_min_ticks_bigger():
    # Spread = 2 ticks, but min_spread_ticks=3 → skip
    r = decide(_meta(), Decimal("100.00"), Decimal("100.02"), "SELL", min_spread_ticks=3)
    assert r.decision == QuoteDecision.SKIP_SPREAD_TIGHT


def test_invalid_book():
    r = decide(_meta(), Decimal("100.00"), Decimal("99.00"), "SELL")
    assert r.decision == QuoteDecision.SKIP_SPREAD_TIGHT
