"""Quantity selection for a cycle (DESIGN.md §4.3)."""
from __future__ import annotations

import random
from decimal import Decimal

from .pair_meta import PairMeta, round_up_to_step


def pick_qty(
    meta: PairMeta,
    price: Decimal,
    *,
    print_value_usd_min: float,
    print_value_usd_max: float,
    quote_to_usd: float = 1.0,
) -> Decimal:
    """Pick a base-quantity for a cycle.

    quote_to_usd: multiplier to convert pair's quote currency into USD-equivalent
    so the USD-value range applies uniformly across ZAR/USDT/USDC pairs.
    Default 1.0 (works for USDT/USDC; ZAR pairs should pass ~0.055 or fetch live).
    """
    value_usd = random.uniform(print_value_usd_min, print_value_usd_max)
    value_quote = Decimal(str(value_usd / max(quote_to_usd, 1e-9)))

    qty = value_quote / price
    if qty < meta.min_base_amount:
        qty = meta.min_base_amount

    qty = round_up_to_step(qty, meta.base_decimals)
    notional = qty * price
    if notional < meta.min_quote_amount:
        # Bump qty to satisfy quote minimum (with 5% headroom)
        target = (meta.min_quote_amount * Decimal("1.05")) / price
        qty = round_up_to_step(target, meta.base_decimals)
    return qty
