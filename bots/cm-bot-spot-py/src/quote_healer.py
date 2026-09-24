"""Quote healer — self-healing quote buffer on subaccounts.

When a sub runs low on quote (working buffer drained by external fills),
the healer market-sells the most valuable base asset on that sub to
rebuild the quote buffer. No MAIN involvement, no transfers needed.

Root cause: external fills drain quote from the sub, bot can't place
orders because it has no quote to pay with. Selling base on the same
sub converts accumulated base back into working quote.

Design:
  - Triggered by the orchestrator when preflight fails with "insufficient <quote>"
  - Only heals the sub that's missing quote
  - Sells just enough base to reach the target buffer (not all of it)
  - Picks the highest-value base asset first (by notional value)
  - Logs and alerts on action
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional

from .inventory import InventoryClient
from .pair_meta import format_price, format_qty


@dataclass
class QuoteHealerConfig:
    """Per-bucket quote buffer targets."""
    # Minimum quote buffer to maintain per sub (in quote currency units)
    # ZAR ≈ R2500, USDT ≈ $50, USDC ≈ $3
    target_buffer: Dict[str, Decimal] = field(default_factory=lambda: {
        "ZAR": Decimal("2500"),
        "USDT": Decimal("50"),
        "USDC": Decimal("3"),
        "BTC": Decimal("0.001"),
    })
    # Only sell if quote is below this fraction of target (avoid flapping)
    heal_threshold_fraction: float = 0.60
    # Keep this fraction of base after healing (never sell everything)
    keep_base_fraction: Decimal = Decimal("0.50")
    # Minimum value of a sell (in USD-ref) to bother
    min_sell_usd: float = 0.50
    # Cooldown: don't heal same sub+quote more than once per N seconds
    cooldown_seconds: int = 300  # 5 min
    # Base healing: target base float per sub in USD ($35/pair → $17.50/sub)
    base_target_usd: float = 17.5
    # Base healing: never spend quote below this fraction of the quote target
    quote_reserve_fraction: Decimal = Decimal("0.5")


@dataclass
class HealResult:
    ok: bool
    sub_id: str = ""
    sub_name: str = ""
    pair: str = ""
    base: str = ""
    quote: str = ""
    sold_amount: Decimal = Decimal("0")
    quote_raised: Decimal = Decimal("0")
    error: str = ""


class QuoteHealer:
    """Self-healing quote buffer for CMS subaccounts."""

    def __init__(
        self,
        inv: InventoryClient,
        rest,
        *,
        config: Optional[QuoteHealerConfig] = None,
        quote_to_usd: Dict[str, float] | None = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.inv = inv
        self.rest = rest
        self.cfg = config or QuoteHealerConfig()
        self.quote_to_usd = quote_to_usd or {}
        self.log = logger or logging.getLogger("quote_healer")
        self._cooldowns: Dict[str, float] = {}  # f"{sub_id}_{quote}" -> timestamp

    def _cooldown_key(self, sub_id: str, quote: str) -> str:
        return f"{sub_id}_{quote}"

    def _is_cooldown(self, sub_id: str, quote: str) -> bool:
        key = self._cooldown_key(sub_id, quote)
        last = self._cooldowns.get(key, 0)
        return (time.time() - last) < self.cfg.cooldown_seconds

    def _mark_cooldown(self, sub_id: str, quote: str) -> None:
        key = self._cooldown_key(sub_id, quote)
        self._cooldowns[key] = time.time()

    def _target(self, quote: str) -> Decimal:
        return self.cfg.target_buffer.get(quote, Decimal("1"))

    def _usd_value(self, amount: Decimal, currency: str) -> float:
        rate = self.quote_to_usd.get(currency, 1.0)
        return float(amount) * rate

    async def needs_healing(
        self, sub_id: str, quote: str
    ) -> tuple[bool, Decimal, Decimal]:
        """Check if a sub needs quote healing.

        Returns: (needs_heal, have_quote, need_quote)
        """
        target = self._target(quote)
        threshold = target * Decimal(str(self.cfg.heal_threshold_fraction))

        have = await self.inv.get_balance(sub_id, quote, force=True)

        if have >= threshold:
            return False, have, Decimal(0)

        need = target - have
        return True, have, need

    async def heal(
        self,
        sub_id: str,
        sub_name: str,
        quote: str,
        available_bases: list[str],
        *,
        prices: Optional[Dict[str, Decimal]] = None,
    ) -> HealResult:
        """Sell base on the sub to rebuild quote buffer.

        Args:
            sub_id: subaccount ID
            sub_name: human-readable sub name
            quote: quote currency that's low (e.g., "ZAR", "USDT")
            available_bases: list of base currencies on this sub (e.g., ["BTC", "ETH"])
            prices: optional cache of {pair: price} to avoid re-fetching
        """
        # Check cooldown
        if self._is_cooldown(sub_id, quote):
            return HealResult(
                ok=False, error="cooldown — healed recently"
            )

        needs, have, need = await self.needs_healing(sub_id, quote)
        if not needs:
            return HealResult(ok=True, sub_id=sub_id, quote=quote)

        self.log.info(
            "HEAL %s: %s buffer low — have %s, need %s (target %s)",
            sub_name, quote, have, need, self._target(quote),
        )

        # Find the best base to sell (highest notional value)
        best_result: Optional[HealResult] = None
        best_usd = 0.0

        for base in available_bases:
            pair = f"{base}{quote}"
            try:
                bal = await self.inv.get_balance(sub_id, base)
                if bal <= 0:
                    continue

                # Get price
                if prices and pair in prices:
                    price = prices[pair]
                else:
                    try:
                        data = await self.rest.public_marketsummary(pair)
                    except Exception:
                        continue  # pair doesn't exist or API hiccup
                    lp = data.get("lastTradedPrice") or data.get("markPrice")
                    if not lp:
                        continue
                    price = Decimal(str(lp))

                # How much to sell
                keep = bal * self.cfg.keep_base_fraction
                sellable = bal - keep
                if sellable <= 0:
                    continue

                # Only sell what we need
                sell_for_quote = sellable * price
                if sell_for_quote >= need:
                    sell_amount = need / price
                    # Round down slightly to avoid over-selling
                    sell_amount = sell_amount * Decimal("0.98")
                else:
                    sell_amount = sellable

                actual_quote = sell_amount * price

                usd_val = self._usd_value(actual_quote, quote)
                if usd_val < self.cfg.min_sell_usd:
                    continue

                if usd_val <= best_usd:
                    continue

                best_usd = usd_val
                best_result = HealResult(
                    ok=True,
                    sub_id=sub_id,
                    sub_name=sub_name,
                    pair=pair,
                    base=base,
                    quote=quote,
                    sold_amount=sell_amount,
                    quote_raised=actual_quote,
                )
            except Exception as e:
                self.log.debug("HEAL %s: error evaluating %s: %s", sub_name, base, e)
                continue

        if best_result is None:
            self.log.warning(
                "HEAL %s: no suitable base to sell (need %s %s)",
                sub_name, need, quote,
            )
            return HealResult(
                ok=False, sub_id=sub_id, quote=quote,
                error=f"no base available to sell for {quote}",
            )

        # Execute the sell
        r = best_result
        self.log.info(
            "HEAL %s: selling %s %s on %s → ~%s %s",
            sub_name, r.sold_amount, r.base, r.pair, r.quote_raised, quote,
        )

        try:
            # Fixed-point formatting — Decimal str() can emit scientific notation
            # (e.g. 2.3E-9) which VALR rejects as "invalid base amount".
            amt_str = f"{r.sold_amount:.10f}".rstrip("0").rstrip(".")
            st, data = await self.rest.market_order(
                sub_id,
                pair=r.pair,
                side="SELL",
                base_amount=amt_str,
            )
            if st in (200, 201, 202):
                self.log.info(
                    "HEAL %s: ✅ sold %s %s → raised ~%s %s",
                    sub_name, r.sold_amount, r.base, r.quote_raised, quote,
                )
                self._mark_cooldown(sub_id, quote)
                # Invalidate balance caches
                self.inv.invalidate(sub_id)
                return r
            else:
                err_msg = f"market sell failed: {st} {data}"
                self.log.warning("HEAL %s: %s", sub_name, err_msg)
                return HealResult(
                    ok=False, sub_id=sub_id, quote=quote, error=err_msg,
                )
        except Exception as e:
            err_msg = f"market sell error: {e}"
            self.log.warning("HEAL %s: %s", sub_name, err_msg)
            return HealResult(
                ok=False, sub_id=sub_id, quote=quote, error=err_msg,
            )

    async def heal_base(
        self,
        sub_id: str,
        sub_name: str,
        base: str,
        quote: str,
        *,
        quote_to_usd: float = 1.0,
    ) -> HealResult:
        """Buy BASE using surplus QUOTE on the same sub (reverse healing).

        Used when a pair is starved of base but the sub has plentiful quote.
        Never dips below quote_reserve_fraction of the quote buffer target.
        """
        cd_key = f"base_{base}"
        if self._is_cooldown(sub_id, cd_key):
            return HealResult(ok=False, error="cooldown — healed recently")

        pair = f"{base}{quote}"
        try:
            quote_bal = await self.inv.get_balance(sub_id, quote, force=True)
            base_bal = await self.inv.get_balance(sub_id, base)

            # Quote we may spend: everything above the protected reserve
            reserve = self._target(quote) * self.cfg.quote_reserve_fraction
            spendable = quote_bal - reserve
            if spendable <= 0:
                return HealResult(
                    ok=False, sub_id=sub_id, quote=quote,
                    error=f"no surplus {quote} (bal={quote_bal}, reserve={reserve})",
                )

            # How much base do we want? Top up to base_target_usd per sub.
            try:
                data = await self.rest.public_marketsummary(pair)
            except Exception as e:
                return HealResult(ok=False, error=f"no price for {pair}: {e}")
            lp = data.get("lastTradedPrice") or data.get("markPrice")
            if not lp:
                return HealResult(ok=False, error=f"no price for {pair}")
            price = Decimal(str(lp))

            # Fetch pair minimums to avoid "Minimum order size not met" errors.
            min_quote_amount = Decimal("0")
            min_base_amount = Decimal("0")
            try:
                pairs_data = await self.rest.public_pairs()
                for pd in pairs_data:
                    if pd.get("symbol") == pair:
                        min_quote_amount = Decimal(str(pd.get("minQuoteAmount", "0")))
                        min_base_amount = Decimal(str(pd.get("minBaseAmount", "0")))
                        break
            except Exception:
                self.log.debug("HEAL-BASE %s: could not fetch pair minimums for %s", sub_name, pair)

            usd_rate = Decimal(str(max(quote_to_usd, 1e-9)))
            base_usd_now = float(base_bal * price * usd_rate)
            need_usd = self.cfg.base_target_usd - base_usd_now
            if need_usd < self.cfg.min_sell_usd:
                return HealResult(ok=True, sub_id=sub_id)  # already at target

            spend_quote = min(
                Decimal(str(need_usd)) / usd_rate,
                spendable,
            )

            # Enforce VALR minimum order size: bump spend_quote to min_quote_amount
            # if the calculated amount would result in an order below the minimum.
            if min_quote_amount > 0 and spend_quote < min_quote_amount:
                if spendable >= min_quote_amount:
                    self.log.info(
                        "HEAL-BASE %s: bumping spend %s → %s to meet pair minimum",
                        sub_name, spend_quote, min_quote_amount,
                    )
                    spend_quote = min_quote_amount
                else:
                    return HealResult(
                        ok=False, sub_id=sub_id, quote=quote,
                        error=f"surplus {quote} ({spendable}) below pair minimum {min_quote_amount} for {pair}",
                    )

            # Verify the base amount we'd receive also meets min_base_amount
            implied_base = spend_quote / price
            if min_base_amount > 0 and implied_base < min_base_amount:
                base_needed = min_base_amount
                quote_for_base = base_needed * price
                if spendable >= quote_for_base:
                    self.log.info(
                        "HEAL-BASE %s: bumping spend %s → %s to meet base minimum",
                        sub_name, spend_quote, quote_for_base,
                    )
                    spend_quote = quote_for_base
                else:
                    return HealResult(
                        ok=False, sub_id=sub_id, quote=quote,
                        error=f"surplus {quote} ({spendable}) insufficient for base minimum {min_base_amount} {base}",
                    )

            spend_usd = float(spend_quote * usd_rate)
            if spend_usd < self.cfg.min_sell_usd:
                return HealResult(
                    ok=False, sub_id=sub_id,
                    error=f"surplus {quote} too small to buy {base} (${spend_usd:.2f})",
                )

            amt_str = f"{spend_quote:.6f}".rstrip("0").rstrip(".")
            self.log.info(
                "HEAL-BASE %s: buying %s with %s %s on %s (base_usd_now=%.2f target=%.2f)",
                sub_name, base, amt_str, quote, pair, base_usd_now, self.cfg.base_target_usd,
            )
            st, data = await self.rest.market_order(
                sub_id, pair=pair, side="BUY", quote_amount=amt_str,
            )
            if st in (200, 201, 202):
                self.log.info(
                    "HEAL-BASE %s: ✅ bought %s with %s %s", sub_name, base, amt_str, quote,
                )
                self._mark_cooldown(sub_id, cd_key)
                self.inv.invalidate(sub_id)
                return HealResult(
                    ok=True, sub_id=sub_id, sub_name=sub_name, pair=pair,
                    base=base, quote=quote, quote_raised=spend_quote,
                )
            err_msg = f"market buy failed: {st} {data}"
            self.log.warning("HEAL-BASE %s: %s", sub_name, err_msg)
            return HealResult(ok=False, sub_id=sub_id, error=err_msg)
        except Exception as e:
            err_msg = f"heal_base error: {e}"
            self.log.warning("HEAL-BASE %s: %s", sub_name, err_msg)
            return HealResult(ok=False, sub_id=sub_id, error=err_msg)
