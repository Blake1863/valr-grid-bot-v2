"""Balance tracking + inventory floor preflight + intra-bot rebalancer.

Folds in the role of the standalone monitor.py service:
  - Per-pair preflight (§4.5): hard skip if either side is short.
  - Intra-bot rebalance (§4.6): transfers asset between subaccounts when
    one holds > threshold_pct of combined. Runs periodically every N cycles
    and reactively on preflight failure.
  - Failure-driven replenish: triggered upstream by the cycle layer when
    skipped_balance counters cross a threshold.

Balance fetches are cached briefly (default 30s) to avoid REST hammering;
WS-driven balance updates would be ideal but introduce more failure modes,
so we keep a REST cache and refresh on miss.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple


@dataclass
class _BalanceCacheEntry:
    balances: Dict[str, Decimal]  # currency -> available
    ts: float


class InventoryClient:
    def __init__(self, rest, ttl_seconds: float = 30.0):
        self._rest = rest
        self._ttl = ttl_seconds
        self._cache: Dict[str, _BalanceCacheEntry] = {}

    async def get_balance(self, subaccount_id: str, currency: str, *, force: bool = False) -> Decimal:
        now = time.time()
        entry = self._cache.get(subaccount_id)
        if force or entry is None or (now - entry.ts) > self._ttl:
            try:
                rows = await self._rest.balances(subaccount_id)
            except Exception:
                if entry is not None:
                    return entry.balances.get(currency, Decimal(0))
                return Decimal(0)
            bals: Dict[str, Decimal] = {}
            for row in rows:
                cur = row.get("currency")
                if cur is None:
                    continue
                try:
                    bals[cur] = Decimal(str(row.get("available", "0")))
                except Exception:
                    bals[cur] = Decimal(0)
            self._cache[subaccount_id] = _BalanceCacheEntry(bals, now)
            entry = self._cache[subaccount_id]
        return entry.balances.get(currency, Decimal(0))

    def invalidate(self, subaccount_id: Optional[str] = None) -> None:
        if subaccount_id is None:
            self._cache.clear()
        else:
            self._cache.pop(subaccount_id, None)

    async def get_balance_forced(self, subaccount_id: str) -> Dict[str, Decimal]:
        """Force-refresh balances for a subaccount (bypass cache)."""
        rows = await self._rest.balances(subaccount_id)
        bals: Dict[str, Decimal] = {}
        for row in rows:
            cur = row.get("currency")
            if cur is None:
                continue
            try:
                bals[cur] = Decimal(str(row.get("available", "0")))
            except Exception:
                bals[cur] = Decimal(0)
        now = time.time()
        self._cache[subaccount_id] = _BalanceCacheEntry(bals, now)
        return bals


@dataclass
class TransferAction:
    currency: str
    from_sub: str
    to_sub: str
    amount: Decimal


class Rebalancer:
    """Intra-bot inventory rebalancer (§4.6).

    When one sub holds > threshold_pct of the combined inventory for a given
    currency, transfer enough to bring both subs to ~50/50.

    External fills create base↔quote conversion on a single sub, causing
    exactly this kind of imbalance. The rebalancer fixes it by moving the
    currency from the rich sub to the poor sub.
    """

    def __init__(
        self,
        inv: InventoryClient,
        rest,
        *,
        sub_a_id: str,
        sub_b_id: str,
        sub_a_name: str,
        sub_b_name: str,
        threshold_pct: float = 0.60,
        min_transfer_value_usd: float = 1.0,
        quote_to_usd: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.inv = inv
        self.rest = rest
        self.sub_a_id = sub_a_id
        self.sub_b_id = sub_b_id
        self.sub_a_name = sub_a_name
        self.sub_b_name = sub_b_name
        self.threshold = Decimal(str(threshold_pct))
        self.min_transfer_usd = Decimal(str(min_transfer_value_usd))
        self.quote_to_usd = Decimal(str(quote_to_usd))
        self.log = logger or logging.getLogger("rebalancer")

    async def _get_currencies(
        self, pair: str, base: str, quote: str
    ) -> List[str]:
        """Return unique currencies to check for a pair (base + quote)."""
        seen = set()
        result = []
        for cur in (base, quote):
            if cur not in seen:
                seen.add(cur)
                result.append(cur)
        return result

    async def _needs_rebalance(
        self, bal_a: Decimal, bal_b: Decimal
    ) -> Tuple[bool, Decimal]:
        """Check if rebalance is needed. Returns (needs_transfer, transfer_amount)."""
        combined = bal_a + bal_b
        if combined <= 0:
            return False, Decimal(0)

        # Target: 50/50 split
        target = combined / 2

        # Check if either sub exceeds threshold
        ratio_a = bal_a / combined
        if ratio_a > self.threshold:
            # A has too much, transfer from A to B
            transfer = bal_a - target
        elif ratio_a < (1 - self.threshold):
            # B has too much, transfer from B to A
            transfer = target - bal_a
        else:
            return False, Decimal(0)

        if transfer <= 0:
            return False, Decimal(0)

        return True, transfer

    async def rebalance_pair(
        self, pair: str, base: str, quote: str
    ) -> List[TransferAction]:
        """Rebalance both base and quote for a single pair."""
        actions: List[TransferAction] = []
        for currency in (base, quote):
            xfers = await self._rebalance_currency(pair, currency)
            actions.extend(xfers)
        return actions

    async def rebalance_currency(
        self, pair: str, currency: str
    ) -> List[TransferAction]:
        """Rebalance a single currency between the two subs."""
        return await self._rebalance_currency(pair, currency)

    async def _rebalance_currency(
        self, pair: str, currency: str
    ) -> List[TransferAction]:
        """Internal: check and create transfer actions for one currency."""
        bal_a = await self.inv.get_balance(self.sub_a_id, currency, force=False)
        bal_b = await self.inv.get_balance(self.sub_b_id, currency, force=False)

        needs_xfer, transfer_amt = await self._needs_rebalance(bal_a, bal_b)
        if not needs_xfer:
            return []

        # Determine direction
        if bal_a > bal_b:
            from_sub, to_sub = self.sub_a_id, self.sub_b_id
            from_name, to_name = self.sub_a_name, self.sub_b_name
        else:
            from_sub, to_sub = self.sub_b_id, self.sub_a_id
            from_name, to_name = self.sub_b_name, self.sub_a_name

        self.log.info(
            "REBALANCE %s %s: %s (%s) → %s (%s) | amount=%s (ratio=%.1f%%)",
            pair, currency,
            from_name, from_sub[-6:] if len(from_sub) > 6 else from_sub,
            to_name, to_sub[-6:] if len(to_sub) > 6 else to_sub,
            transfer_amt,
            float(max(bal_a, bal_b) / (bal_a + bal_b) * 100) if (bal_a + bal_b) > 0 else 0,
        )
        return [TransferAction(
            currency=currency,
            from_sub=from_sub,
            to_sub=to_sub,
            amount=transfer_amt,
        )]

    async def execute_transfer(self, action: TransferAction) -> bool:
        """Execute a single transfer via VALR REST API."""
        try:
            st, data = await self.rest.subaccount_transfer(
                from_id=action.from_sub,
                to_id=action.to_sub,
                currency=action.currency,
                amount=str(action.amount),
            )
            if st in (200, 202):
                self.log.info(
                    "TRANSFER OK: %s %s → %s | amt=%s",
                    action.currency,
                    action.from_sub[-6:] if len(action.from_sub) > 6 else action.from_sub,
                    action.to_sub[-6:] if len(action.to_sub) > 6 else action.to_sub,
                    action.amount,
                )
                # Invalidate balance cache so next cycle sees updated balances
                self.inv.invalidate(action.from_sub)
                self.inv.invalidate(action.to_sub)
                return True
            else:
                self.log.warning(
                    "TRANSFER FAILED: %s %s → %s | status=%s | %s",
                    action.currency,
                    action.from_sub[-6:] if len(action.from_sub) > 6 else action.from_sub,
                    action.to_sub[-6:] if len(action.to_sub) > 6 else action.to_sub,
                    st,
                    data,
                )
                return False
        except Exception as e:
            self.log.warning(
                "TRANSFER ERROR: %s %s → %s | %s",
                action.currency,
                action.from_sub[-6:] if len(action.from_sub) > 6 else action.from_sub,
                action.to_sub[-6:] if len(action.to_sub) > 6 else action.to_sub,
                e,
            )
            return False


@dataclass
class PreflightResult:
    ok: bool
    reason: str = ""


async def preflight(
    inv: InventoryClient,
    *,
    maker_sub: str,
    taker_sub: str,
    maker_side: str,
    base: str,
    quote: str,
    qty: Decimal,
    notional: Decimal,
    fee_margin: Decimal = Decimal("1.02"),
) -> PreflightResult:
    """Hard skip if either account short on the asset they need."""
    need_base = qty * fee_margin
    need_quote = notional * fee_margin
    if maker_side == "SELL":
        m_bal = await inv.get_balance(maker_sub, base)
        t_bal = await inv.get_balance(taker_sub, quote)
        if m_bal < need_base:
            return PreflightResult(False, f"maker insufficient {base}: {m_bal} < {need_base}")
        if t_bal < need_quote:
            return PreflightResult(False, f"taker insufficient {quote}: {t_bal} < {need_quote}")
    else:
        m_bal = await inv.get_balance(maker_sub, quote)
        t_bal = await inv.get_balance(taker_sub, base)
        if m_bal < need_quote:
            return PreflightResult(False, f"maker insufficient {quote}: {m_bal} < {need_quote}")
        if t_bal < need_base:
            return PreflightResult(False, f"taker insufficient {base}: {t_bal} < {need_base}")
    return PreflightResult(True, "")


def extract_failed_currency(reason: str) -> Optional[str]:
    """Parse the currency from a preflight failure reason.

    Examples:
        'maker insufficient BTC: 0.001 < 0.005' → 'BTC'
        'taker insufficient ZAR: 50.0 < 55.0' → 'ZAR'
    """
    # Pattern: "... insufficient <CURRENCY>: ..."
    idx = reason.find("insufficient ")
    if idx < 0:
        return None
    rest = reason[idx + len("insufficient "):]
    # Currency is the first word before a space or colon
    parts = rest.split()
    if parts:
        return parts[0].rstrip(":")
    return None
