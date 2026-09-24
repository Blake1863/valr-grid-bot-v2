"""Pair metadata cache. Fetches once at startup, exposes typed accessors."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List


@dataclass(frozen=True)
class PairMeta:
    symbol: str
    base: str
    quote: str
    tick_size: Decimal           # price step
    tick_decimals: int           # number of dp in tick_size (e.g. 0.01 -> 2)
    min_base_amount: Decimal     # min order quantity in base
    min_quote_amount: Decimal    # min notional in quote
    base_decimals: int           # quantity dp


_QUOTES = ("USDTPERP", "USDCPERP", "ZARPERP", "USDT", "USDC", "ZAR", "BTC", "ETH")


def split_pair(pair: str) -> tuple[str, str]:
    for q in _QUOTES:
        if pair.endswith(q):
            return pair[: -len(q)], q
    raise ValueError(f"Cannot split pair: {pair}")


def _decimals_of(s: str) -> int:
    if not s or "." not in s:
        return 0
    frac = s.split(".", 1)[1].rstrip("0")
    return len(frac)


def parse(p: dict) -> PairMeta:
    """Parse one entry from /v1/public/pairs."""
    symbol = p["symbol"]
    base = p.get("baseCurrency") or split_pair(symbol)[0]
    quote = p.get("quoteCurrency") or split_pair(symbol)[1]
    tick_str = str(p.get("tickSize", "0.01"))
    tick = Decimal(tick_str)
    tick_dp = _decimals_of(tick_str)
    min_base = Decimal(str(p.get("minBaseAmount", "0")))
    min_quote = Decimal(str(p.get("minQuoteAmount", "0")))
    base_dp_raw = p.get("baseDecimalPlaces", "8")
    try:
        base_dp = int(base_dp_raw)
    except (TypeError, ValueError):
        base_dp = 8
    return PairMeta(
        symbol=symbol,
        base=base,
        quote=quote,
        tick_size=tick,
        tick_decimals=tick_dp,
        min_base_amount=min_base,
        min_quote_amount=min_quote,
        base_decimals=base_dp,
    )


def parse_all(pairs: List[dict]) -> Dict[str, PairMeta]:
    out: Dict[str, PairMeta] = {}
    for p in pairs:
        try:
            m = parse(p)
        except Exception:
            continue
        out[m.symbol] = m
    return out


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Round to nearest tick. Snap to avoid float drift."""
    if tick == 0:
        return price
    steps = (price / tick).to_integral_value()
    return (steps * tick).quantize(tick)


def format_price(price: Decimal, meta: PairMeta) -> str:
    p = round_to_tick(price, meta.tick_size)
    if meta.tick_decimals == 0:
        return str(int(p))
    return f"{p:.{meta.tick_decimals}f}"


def format_qty(qty: Decimal, meta: PairMeta) -> str:
    if meta.base_decimals == 0:
        return str(int(qty))
    return f"{qty:.{meta.base_decimals}f}"


def round_up_to_step(value: Decimal, decimals: int) -> Decimal:
    """Round UP to the given decimal places (so we always satisfy a minimum)."""
    if decimals <= 0:
        # Round up to integer
        if value == value.to_integral_value():
            return value
        return value.to_integral_value() + 1
    step = Decimal(10) ** -decimals
    # Ceiling division
    units = (value / step)
    if units == units.to_integral_value():
        return value.quantize(step)
    return (units.to_integral_value() + 1) * step
