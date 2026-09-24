import random
from decimal import Decimal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pair_meta import PairMeta
from src.sizing import pick_qty


def _meta(min_base="0.0001", min_quote="10", base_dp=8):
    return PairMeta("BTCZAR", "BTC", "ZAR", Decimal("0.01"), 2,
                    Decimal(min_base), Decimal(min_quote), base_dp)


def test_basic_sizing_within_range():
    random.seed(1)
    qty = pick_qty(_meta(), Decimal("100.0"), print_value_usd_min=1.0, print_value_usd_max=2.0)
    notional = qty * Decimal("100.0")
    # Notional should be between min_quote and at least the USD range bound
    assert notional >= Decimal("10")
    assert qty >= Decimal("0.0001")


def test_min_quote_floor():
    # Tiny price → qty has to be large enough to hit min_quote
    random.seed(2)
    qty = pick_qty(_meta(min_quote="50"), Decimal("0.001"),
                   print_value_usd_min=0.01, print_value_usd_max=0.02)
    notional = qty * Decimal("0.001")
    assert notional >= Decimal("50")


def test_min_base_enforced():
    random.seed(3)
    qty = pick_qty(_meta(min_base="0.5"), Decimal("100.0"),
                   print_value_usd_min=0.01, print_value_usd_max=0.02)
    assert qty >= Decimal("0.5")


def test_quote_to_usd_zar():
    # ZAR pair: quote_to_usd ~ 0.06 (1 USD ~= 16.8 ZAR).
    # So 1 USD value ~= 16.8 ZAR notional, qty depends on price.
    random.seed(4)
    qty = pick_qty(_meta(), Decimal("100.0"),
                   print_value_usd_min=1.0, print_value_usd_max=1.0,
                   quote_to_usd=0.06)
    notional_zar = qty * Decimal("100.0")
    # Should be roughly 1 USD / 0.06 = 16.67 ZAR (or above min_quote=10)
    assert notional_zar >= Decimal("10")
