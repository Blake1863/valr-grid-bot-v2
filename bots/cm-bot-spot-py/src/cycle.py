"""One pair's wash cycle.

Steps:
  1. Read book (from price feed).
  2. Quoting: pick maker price (skip if no room).
  3. Maker selection (account, side).
  4. Sizing: pick qty.
  5. Inventory preflight.
  6. Place maker (postOnly, GTC) on maker WS.
  7. Fire taker IOC limit at exact maker price on taker WS.
  8. Cancel maker (best effort).
  9. Classify outcome and record to leak monitor.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional

from .config import PairDefaults
from .inventory import InventoryClient, preflight
from .leak_monitor import Classification, LeakMonitor
from .maker_selector import MakerState
from .pair_meta import PairMeta, format_price, format_qty, split_pair
from .quoting import QuoteDecision, decide
from .sizing import pick_qty
from .valr_account_ws import AccountWS
from .valr_rest import ValrRest


@dataclass
class CycleStats:
    prints: int = 0
    skipped_spread: int = 0
    skipped_balance: int = 0
    maker_failed: int = 0
    taker_failed_after_maker_filled: int = 0  # external-fill (leak A)
    inconclusive: int = 0
    last_error: str = ""
    last_print_ts: float = 0.0


@dataclass
class CycleContext:
    """Per-pair runtime state needed by execute()."""
    pair: str
    meta: PairMeta
    pcfg: PairDefaults
    maker_state: MakerState = field(default_factory=MakerState)
    stats: CycleStats = field(default_factory=CycleStats)
    backoff_until: float = 0.0


@dataclass
class Accounts:
    cms1_id: str
    cms2_id: str
    cms1_label: str
    cms2_label: str
    cms1_ws: Optional[AccountWS]
    cms2_ws: Optional[AccountWS]
    rest: ValrRest


async def _place(
    ws: Optional[AccountWS],
    rest: ValrRest,
    sub_id: str,
    *,
    pair: str,
    side: str,
    price_str: str,
    qty_str: str,
    post_only: bool,
    cid: str,
    tif: str,
):
    """Try WS first, fall back to REST. Returns (ok: bool, info: dict)."""
    if ws is not None:
        try:
            ok, info = await ws.place_limit(
                pair=pair, side=side, price=price_str, quantity=qty_str,
                post_only=post_only, customer_order_id=cid, time_in_force=tif,
            )
            if ok or "WS not ready" not in str(info.get("error", "")):
                return ok, info
        except Exception as e:
            info = {"error": f"WS exception: {e}"}
    # REST fallback
    st, data = await rest.place_limit_rest(
        sub_id, pair=pair, side=side, price=price_str, quantity=qty_str,
        post_only=post_only, customer_order_id=cid, time_in_force=tif,
    )
    if st == 202 and isinstance(data, dict) and "id" in data:
        return True, {"orderId": data["id"], "data": data, "via": "rest"}
    return False, {"error": f"REST status {st}", "data": data, "via": "rest"}


async def execute(
    ctx: CycleContext,
    feed,  # PriceFeed
    inv: InventoryClient,
    accts: Accounts,
    leaks: LeakMonitor,
    log: logging.Logger,
    *,
    dry_run: bool = False,
) -> None:
    pair = ctx.pair
    meta = ctx.meta
    pcfg = ctx.pcfg

    # 1. Read book
    book = feed.get(pair)
    if book is None:
        ctx.stats.skipped_spread += 1
        ctx.stats.last_error = "no orderbook (feed not yet primed)"
        return
    bid, ask, _ts = book

    # 3a. Pick side first (so we know what we're quoting)
    maker_side = ctx.maker_state.select_side()

    # 2. Quoting
    q = decide(meta, bid, ask, maker_side,
               min_spread_ticks=pcfg.min_spread_ticks, max_spread_bps=pcfg.max_spread_bps)
    if q.decision != QuoteDecision.PLACE:
        ctx.stats.skipped_spread += 1
        ctx.stats.last_error = f"{q.decision}: {q.reason}"
        return
    maker_price: Decimal = q.price  # type: ignore[assignment]

    # 3b. Pick maker account
    maker_is_cms1 = ctx.maker_state.select_account(pcfg.max_consecutive_same_maker)
    if maker_is_cms1:
        maker_sub, taker_sub = accts.cms1_id, accts.cms2_id
        maker_tag, taker_tag = accts.cms1_label, accts.cms2_label
        maker_ws, taker_ws = accts.cms1_ws, accts.cms2_ws
    else:
        maker_sub, taker_sub = accts.cms2_id, accts.cms1_id
        maker_tag, taker_tag = accts.cms2_label, accts.cms1_label
        maker_ws, taker_ws = accts.cms2_ws, accts.cms1_ws

    # 4. Sizing
    qty = pick_qty(
        meta, maker_price,
        print_value_usd_min=pcfg.print_value_usd_min,
        print_value_usd_max=pcfg.print_value_usd_max,
        quote_to_usd=pcfg.quote_to_usd,
    )
    notional = qty * maker_price
    base, quote = meta.base, meta.quote

    # 5. Preflight balances
    pf = await preflight(
        inv,
        maker_sub=maker_sub, taker_sub=taker_sub,
        maker_side=maker_side,
        base=base, quote=quote,
        qty=qty, notional=notional,
    )
    if not pf.ok:
        ctx.stats.skipped_balance += 1
        ctx.stats.last_error = pf.reason
        return

    if dry_run:
        log.info(
            "DRY %s: would PLACE %s %s @ %s x %s (~%.2f) maker=%s",
            pair, maker_side, maker_tag, format_price(maker_price, meta),
            format_qty(qty, meta), float(notional), maker_tag,
        )
        return

    # 6. Place maker (postOnly, GTC)
    cid_m = f"py-m-{uuid.uuid4().hex[:14]}"
    cid_t = f"py-t-{uuid.uuid4().hex[:14]}"
    price_s = format_price(maker_price, meta)
    qty_s = format_qty(qty, meta)

    import time as _t
    t0 = _t.monotonic()
    m_ok, m_info = await _place(
        maker_ws, accts.rest, maker_sub,
        pair=pair, side=maker_side, price_str=price_s, qty_str=qty_s,
        post_only=True, cid=cid_m, tif="GTC",
    )
    maker_ms = (_t.monotonic() - t0) * 1000

    if not m_ok:
        ctx.stats.maker_failed += 1
        ctx.stats.last_error = f"maker {maker_tag} {maker_side}: {m_info}"
        leaks.record(pair, Classification.NO_FILL)
        # Invalidate maker balance cache (placement may have reserved funds)
        inv.invalidate(maker_sub)
        return

    # 7. Fire taker IOC at exact maker price
    taker_side = "BUY" if maker_side == "SELL" else "SELL"
    t1 = _t.monotonic()
    t_ok, t_info = await _place(
        taker_ws, accts.rest, taker_sub,
        pair=pair, side=taker_side, price_str=price_s, qty_str=qty_s,
        post_only=False, cid=cid_t, tif="IOC",
    )
    taker_ms = (_t.monotonic() - t1) * 1000

    # 8. Cancel maker (best effort, non-blocking on errors)
    try:
        await accts.rest.cancel_order(maker_sub, pair=pair, customer_order_id=cid_m)
    except Exception:
        pass

    # 9. Classify outcome
    # Authoritative classification requires checking fills via order history.
    # Best-effort heuristic: if taker IOC succeeded, treat as INTERNAL.
    # If taker failed, treat as EXTERNAL (maker likely got hit before our IOC).
    # If maker placement returned but taker is also "no fill" / "no liquidity",
    # we mark inconclusive for now and rely on the order-history detail check
    # below to refine.
    classification: Classification

    def _executed_qty(hist) -> Optional[Decimal]:
        """Return executed quantity for an order from history-detail.
        Detail may be a dict (final state) or a list of status events.
        Prefers totalExecutedQuantity; falls back to original - remaining."""
        def _from(d: dict) -> Optional[Decimal]:
            for k in ("totalExecutedQuantity", "executedQuantity"):
                v = d.get(k)
                if v not in (None, ""):
                    try:
                        return Decimal(str(v))
                    except Exception:
                        pass
            orig = d.get("originalQuantity")
            rem = d.get("remainingQuantity")
            if orig not in (None, "") and rem not in (None, ""):
                try:
                    return Decimal(str(orig)) - Decimal(str(rem))
                except Exception:
                    return None
            return None
        if isinstance(hist, dict):
            return _from(hist)
        if isinstance(hist, list):
            best = None
            for ev in hist:
                if isinstance(ev, dict):
                    q = _from(ev)
                    if q is not None and (best is None or q > best):
                        best = q
            return best
        return None

    async def _fill_qty(sub: str, cid: str, *, want_fill: bool = True) -> Optional[Decimal]:
        """Read executed qty from order history, retrying while VALR settles.

        VALR's order-history-detail can lag the actual fill by a few hundred ms.
        At 50ms most reads returned 0/None -> everything classified INCONCLUSIVE.
        We now retry with backoff: if want_fill and we still see 0, keep polling
        up to ~1.2s total before giving up. Returns executed qty (may be 0)."""
        delays = (0.15, 0.2, 0.3, 0.5)  # cumulative ~1.15s, on top of initial settle
        last: Optional[Decimal] = None
        for i, d in enumerate(delays):
            try:
                st, hist = await accts.rest.order_history_by_cid(sub, cid)
                if st == 200:
                    q = _executed_qty(hist)
                    if q is not None:
                        last = q
                        # got a positive fill, or we don't require one -> done
                        if q > 0 or not want_fill:
                            return q
            except Exception:
                pass
            # not yet filled/settled; wait and retry (skip wait after last attempt)
            if i < len(delays) - 1:
                await asyncio.sleep(d)
        return last

    # Brief pause to let VALR's order history begin settling before first query
    await asyncio.sleep(0.2)

    # A clean internal wash requires BOTH legs to execute the SAME quantity
    # (our placed qty). If the maker executed but the taker executed a different
    # amount, or the maker shows more executed than our taker took, the cycle
    # traded against EXTERNAL liquidity (leak A/D) -> count as external, not a print.
    placed_qty = qty
    qty_tol = placed_qty * Decimal("0.02")  # 2% tolerance for rounding

    if t_ok:
        m_q = await _fill_qty(maker_sub, cid_m)
        t_q = await _fill_qty(taker_sub, cid_t)
        m_filled = (m_q is not None and m_q > 0)
        t_filled = (t_q is not None and t_q > 0)
        if m_q is None or t_q is None:
            classification = Classification.INCONCLUSIVE
            ctx.stats.inconclusive += 1
        elif m_filled and t_filled:
            # both executed — verify quantities match (true internal wash)
            if abs(m_q - t_q) <= qty_tol and abs(m_q - placed_qty) <= qty_tol:
                classification = Classification.INTERNAL
                ctx.stats.prints += 1
            else:
                # mismatched fill sizes => external contamination on one leg
                classification = Classification.EXTERNAL
                ctx.stats.taker_failed_after_maker_filled += 1
                log.warning(
                    "%s: ⚠ QTY MISMATCH maker=%s taker=%s placed=%s -> external leak",
                    pair, m_q, t_q, placed_qty,
                )
        elif m_filled and not t_filled:
            classification = Classification.EXTERNAL
            ctx.stats.taker_failed_after_maker_filled += 1
        else:
            classification = Classification.NO_FILL
    else:
        m_q = await _fill_qty(maker_sub, cid_m)
        if m_q is None:
            classification = Classification.INCONCLUSIVE
            ctx.stats.inconclusive += 1
        elif m_q > 0:
            classification = Classification.EXTERNAL
            ctx.stats.taker_failed_after_maker_filled += 1
        else:
            classification = Classification.NO_FILL

    leaks.record(pair, classification)
    inv.invalidate(maker_sub)
    inv.invalidate(taker_sub)

    if classification == Classification.INTERNAL:
        ctx.stats.last_print_ts = _t.time()
        sym = "R" if quote == "ZAR" else "$"
        log.info(
            "%s: ✅ PRINT %s→%s %s@%s x%s (~%s%.2f) | maker=%.0fms taker=%.0fms",
            pair, maker_tag, taker_tag, maker_side, price_s, qty_s,
            sym, float(notional), maker_ms, taker_ms,
        )
    elif classification == Classification.EXTERNAL:
        log.warning(
            "%s: ⚠ EXTERNAL FILL %s %s@%s x%s — taker missed (leak A) | maker=%.0fms taker=%.0fms",
            pair, maker_tag, maker_side, price_s, qty_s, maker_ms, taker_ms,
        )
    elif classification == Classification.NO_FILL:
        log.debug("%s: no-fill (B/C) maker_side=%s @%s", pair, maker_side, price_s)
    else:
        log.debug("%s: inconclusive cycle", pair)
