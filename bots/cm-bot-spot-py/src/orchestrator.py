"""Orchestrator — wires everything together, runs the per-pair tasks.

Hard safety check at startup: refuses to enable any pair that's also
enabled in the legacy `cm-bot-spot/config.json` or `cm-bot-spot-illiquid/config.json`.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .alerter import TelegramAlerter
from .config import Config, enabled_pairs
from .cycle import Accounts, CycleContext, execute as run_cycle
from .inventory import InventoryClient, Rebalancer, extract_failed_currency
from .leak_monitor import LeakMonitor
from .pair_meta import PairMeta, parse_all
from .quote_healer import QuoteHealer, QuoteHealerConfig
from .valr_account_ws import AccountWS
from .valr_rest import ValrRest
from .valr_ws import PriceFeed

WORKSPACE = Path(__file__).resolve().parents[3]


def _check_collision(enabled: List[str], log: logging.Logger) -> None:
    """Refuse to start if any enabled pair is also enabled in a legacy bot config."""
    legacy_paths = [
        WORKSPACE / "bots" / "cm-bot-spot" / "config.json",
        WORKSPACE / "bots" / "cm-bot-spot-illiquid" / "config.json",
    ]
    collisions: list[tuple[str, str]] = []
    for p in legacy_paths:
        if not p.exists():
            continue
        try:
            raw = _json.loads(p.read_text())
            legacy_pairs = raw.get("pairs", {})
            for sym, pc in legacy_pairs.items():
                if sym in enabled and pc.get("enabled"):
                    collisions.append((sym, str(p)))
        except Exception as e:
            log.warning("could not parse legacy config %s: %s", p, e)
    if collisions:
        msg = "REFUSING TO START — pair collision with legacy bots:\n" + "\n".join(
            f"  {s} also enabled in {pth}" for s, pth in collisions
        )
        raise RuntimeError(msg)


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        creds: dict,
        log: logging.Logger,
        *,
        dry_run: bool = False,
    ):
        self.cfg = cfg
        self.creds = creds
        self.log = log
        self.dry_run = dry_run

        self.rest: Optional[ValrRest] = None
        self.feed: Optional[PriceFeed] = None
        self.cms1_ws: Optional[AccountWS] = None
        self.cms2_ws: Optional[AccountWS] = None
        self.account_a_id = cfg.global_.account_a_id or creds["CM1_SUBACCOUNT_ID"]
        self.account_b_id = cfg.global_.account_b_id or creds["CM2_SUBACCOUNT_ID"]
        self.account_a_name = cfg.global_.account_a_name or "CMS1"
        self.account_b_name = cfg.global_.account_b_name or "CMS2"
        self.inv: Optional[InventoryClient] = None
        self.rebalancer: Optional[Rebalancer] = None
        self.healer: Optional[QuoteHealer] = None
        self.leaks = LeakMonitor(
            external_alert_threshold=cfg.global_.external_fill_alert_threshold,
            alert_cooldown_seconds=cfg.global_.alert_cooldown_seconds,
            min_samples_for_alert=cfg.global_.leak_min_samples,
        )
        self.alerter: Optional[TelegramAlerter] = None
        self.metas: Dict[str, PairMeta] = {}
        self.contexts: Dict[str, CycleContext] = {}
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._rate_sem = asyncio.Semaphore(cfg.global_.rate_limit_per_sec)

    async def setup(self) -> List[str]:
        """Set up clients, fetch metadata, return list of enabled pairs."""
        enabled = enabled_pairs(self.cfg)
        if not enabled:
            raise RuntimeError("No pairs enabled in config")
        if self.account_a_id == self.account_b_id:
            raise RuntimeError("account_a_id and account_b_id must be different")

        if not self.dry_run:
            _check_collision(enabled, self.log)
        else:
            # Still warn so operator knows, but allow boot
            try:
                _check_collision(enabled, self.log)
            except RuntimeError as e:
                self.log.warning("DRY-RUN: collision detected (allowed because no orders will be placed):\n%s", e)

        # REST client
        self.rest = ValrRest(self.creds["MAIN_API_KEY"], self.creds["MAIN_API_SECRET"])
        await self.rest.__aenter__()

        # Pair metadata
        all_pairs = await self.rest.public_pairs()
        self.metas = parse_all(all_pairs)
        missing = [p for p in enabled if p not in self.metas]
        if missing:
            raise RuntimeError(f"Pairs not found in VALR public/pairs: {missing}")

        # Inventory client
        self.inv = InventoryClient(self.rest)

        # Rebalancer — transfers between subs when one holds > threshold
        self.rebalancer = Rebalancer(
            self.inv, self.rest,
            sub_a_id=self.account_a_id,
            sub_b_id=self.account_b_id,
            sub_a_name=self.account_a_name,
            sub_b_name=self.account_b_name,
            threshold_pct=self.cfg.global_.rebalance_threshold_pct,
            min_transfer_value_usd=self.cfg.global_.min_transfer_value_usd,
            logger=self.log,
        )

        # Quote healer — sells base on subs to rebuild quote buffers
        self.healer = QuoteHealer(
            self.inv, self.rest,
            config=QuoteHealerConfig(),
            logger=self.log,
        )

        # Price feed (public WS)
        self.feed = PriceFeed(logger=self.log)
        await self.feed.start(enabled)

        # Account WSs (one per subaccount)
        if self.cfg.global_.use_account_ws and not self.dry_run:
            self.cms1_ws = AccountWS(
                self.creds["MAIN_API_KEY"], self.creds["MAIN_API_SECRET"],
                self.account_a_id, name=self.account_a_name, logger=self.log,
            )
            self.cms2_ws = AccountWS(
                self.creds["MAIN_API_KEY"], self.creds["MAIN_API_SECRET"],
                self.account_b_id, name=self.account_b_name, logger=self.log,
            )
            await self.cms1_ws.start()
            await self.cms2_ws.start()
            try:
                await asyncio.wait_for(
                    asyncio.gather(self.cms1_ws.wait_ready(), self.cms2_ws.wait_ready()),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                self.log.warning("account WS not ready in 20s — falling back to REST for placement")

        # Alerter
        self.alerter = TelegramAlerter(
            chat_id=self.cfg.global_.telegram_chat_id, logger=self.log
        )
        await self.alerter.__aenter__()

        # Build per-pair contexts
        for sym in enabled:
            self.contexts[sym] = CycleContext(
                pair=sym,
                meta=self.metas[sym],
                pcfg=self.cfg.for_pair(sym),
            )

        # Pre-cycle cleanup: cancel all open orders on both subaccounts
        if self.cfg.global_.cancel_all_on_startup and not self.dry_run:
            self.log.info(
                "startup: cancelling all open orders on %s+%s",
                self.account_a_name,
                self.account_b_name,
            )
            try:
                n1 = await self.rest.cancel_all_on_subaccount(self.account_a_id)
                n2 = await self.rest.cancel_all_on_subaccount(self.account_b_id)
                self.log.info(
                    "startup: cancelled %d on %s, %d on %s",
                    n1,
                    self.account_a_name,
                    n2,
                    self.account_b_name,
                )
            except Exception as e:
                self.log.warning("startup cancel-all failed: %s", e)

        # Wait briefly for the price feed to populate
        self.log.info("waiting for price feed to populate (10s)...")
        for _ in range(20):
            primed = sum(1 for s in enabled if self.feed and self.feed.get(s) is not None)
            if primed >= len(enabled) // 2 + 1:
                break
            await asyncio.sleep(0.5)
        primed = sum(1 for s in enabled if self.feed and self.feed.get(s) is not None)
        self.log.info("price feed primed: %d/%d pairs", primed, len(enabled))
        return enabled

    async def run(self, enabled: List[str]) -> None:
        """Spawn per-pair tasks + summary task and wait."""
        accts = Accounts(
            cms1_id=self.account_a_id,
            cms2_id=self.account_b_id,
            cms1_label=self.account_a_name,
            cms2_label=self.account_b_name,
            cms1_ws=self.cms1_ws,
            cms2_ws=self.cms2_ws,
            rest=self.rest,
        )
        # Stagger pair start times
        for idx, sym in enumerate(enabled):
            delay = idx * self.cfg.global_.stagger_seconds
            self._tasks.append(asyncio.create_task(
                self._pair_loop(sym, accts, delay),
                name=f"pair-{sym}",
            ))
        self._tasks.append(asyncio.create_task(self._summary_loop(), name="summary"))
        self._tasks.append(asyncio.create_task(self._alert_loop(), name="alerts"))

        await self._stop.wait()

        # Cleanly cancel
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stop.set()
        if self.feed is not None:
            await self.feed.stop()
        if self.cms1_ws is not None:
            await self.cms1_ws.stop()
        if self.cms2_ws is not None:
            await self.cms2_ws.stop()
        if self.alerter is not None:
            await self.alerter.__aexit__(None, None, None)
        if self.rest is not None:
            await self.rest.__aexit__(None, None, None)

    async def _pair_loop(self, sym: str, accts: Accounts, initial_delay: float) -> None:
        self.log.info("%s: pair task starting (initial_delay=%.1fs)", sym, initial_delay)
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        ctx = self.contexts[sym]
        cycle_n = 0
        while not self._stop.is_set():
            cycle_n += 1
            self.log.info("%s: cycle #%d starting", sym, cycle_n)
            try:
                async with self._rate_sem:
                    await run_cycle(ctx, self.feed, self.inv, accts, self.leaks, self.log,
                                    dry_run=self.dry_run)
                self.log.info("%s: cycle #%d completed", sym, cycle_n)

                # Periodic rebalance check (every N cycles)
                if cycle_n % self.cfg.global_.rebalance_interval_cycles == 0:
                    await self._try_rebalance(sym)

                # Reactive rebalance on preflight failure
                if ctx.stats.last_error and "insufficient" in ctx.stats.last_error:
                    await self._try_rebalance_on_failure(sym, ctx.stats.last_error)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.exception("%s: unhandled cycle error: %s", sym, e)
                ctx.stats.last_error = f"exception: {e}"
                # Per-pair exponential-ish backoff
                ctx.backoff_until = time.time() + 5.0
            # Sleep until next cycle (or backoff)
            now = time.time()
            sleep_s = max(0.0, ctx.backoff_until - now) if ctx.backoff_until > now else ctx.pcfg.cycle_interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass

    async def _summary_loop(self) -> None:
        interval = self.cfg.global_.summary_interval_seconds
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            self._emit_summary()

    def _emit_summary(self) -> None:
        self.log.info("----- summary -----")
        total_prints = 0
        for sym, ctx in self.contexts.items():
            s = ctx.stats
            stats = self.leaks.get(sym).stats()
            if s.last_print_ts > 0:
                last_print_age = f"{int(time.time() - s.last_print_ts)}s"
            else:
                last_print_age = "never"
            self.log.info(
                "  %s: prints=%d ext=%d skip_bal=%d skip_spread=%d fail=%d incl=%d "
                "internal_ratio=%.2f window=%d last_print=%s last_err=%s",
                sym, s.prints, s.taker_failed_after_maker_filled, s.skipped_balance,
                s.skipped_spread, s.maker_failed, s.inconclusive,
                stats["internal_ratio"], stats["total"], last_print_age, (s.last_error or "-")[:80],
            )
            total_prints += s.prints
        self.log.info("  TOTAL prints all pairs: %d", total_prints)
        self.log.info("-------------------")

    async def _try_rebalance(self, sym: str) -> None:
        """Periodic rebalance: check base+quote for the pair and transfer if imbalanced."""
        if self.dry_run or self.rebalancer is None:
            return
        pcfg = self.cfg.for_pair(sym)
        self.rebalancer.quote_to_usd = Decimal(str(pcfg.quote_to_usd))
        meta = self.contexts[sym].meta
        base = meta.base
        quote = meta.quote
        actions = await self.rebalancer.rebalance_pair(sym, base, quote)
        for action in actions:
            await self.rebalancer.execute_transfer(action)

    async def _try_rebalance_on_failure(self, sym: str, reason: str) -> None:
        """Reactive rebalance/heal: on preflight failure, try to transfer the missing currency
        OR sell base on the sub to rebuild the quote buffer."""
        if self.dry_run:
            return
        currency = extract_failed_currency(reason)
        if currency is None:
            return

        base, quote = self.contexts[sym].meta.base, self.contexts[sym].meta.quote

        # If the missing currency is the QUOTE, try healing first (sell base on sub)
        if currency == quote and self.healer is not None:
            # Determine which sub is missing quote and try to heal it
            await self.inv.get_balance(self.account_a_id, currency, force=True)
            await self.inv.get_balance(self.account_b_id, currency, force=True)
            bal_a = await self.inv.get_balance(self.account_a_id, currency)
            bal_b = await self.inv.get_balance(self.account_b_id, currency)

            # Gather available bases on each sub
            await self.inv.get_balance_forced(self.account_a_id)
            await self.inv.get_balance_forced(self.account_b_id)

            for sub_id, sub_name in [(self.account_a_id, self.account_a_name),
                                     (self.account_b_id, self.account_b_name)]:
                bal = await self.inv.get_balance(sub_id, currency)
                target = self.healer._target(currency)
                if bal >= target * Decimal("0.6"):
                    continue  # This sub is fine

                # Get all bases this sub holds
                cached = self.inv._cache.get(sub_id)
                if not cached:
                    continue
                bases_on_sub = [c for c, v in cached.balances.items() if v > 0 and c != currency]
                if not bases_on_sub:
                    continue

                result = await self.healer.heal(
                    sub_id, sub_name, currency, bases_on_sub,
                )
                if result.ok and result.sold_amount > 0:
                    # Success is logged inside the healer — no Telegram alert.
                    return  # Healed, no need to transfer
                if not result.ok and result.error and "cooldown" not in result.error:
                    # Heal FAILED — this needs eyes.
                    if self.alerter:
                        await self.alerter.send(
                            f"⚠️ Quote healer FAILED on {sub_name} ({sym}): "
                            f"{result.error}"
                        )

        # If healing didn't work (or it's a base shortage), fall back to transfer rebalance
        if self.rebalancer is None:
            return
        # Force-refresh both subs to get accurate balances
        await self.inv.get_balance(self.account_a_id, currency, force=True)
        await self.inv.get_balance(self.account_b_id, currency, force=True)
        bal_a = await self.inv.get_balance(self.account_a_id, currency)
        bal_b = await self.inv.get_balance(self.account_b_id, currency)
        combined = bal_a + bal_b
        pcfg = self.cfg.for_pair(sym)
        can_transfer = (
            combined > 0
            and max(bal_a, bal_b) / combined >= self.cfg.global_.rebalance_threshold_pct
        )
        if can_transfer:
            self.rebalancer.quote_to_usd = Decimal(str(pcfg.quote_to_usd))
            actions = await self.rebalancer.rebalance_currency(sym, currency)
            for action in actions:
                await self.rebalancer.execute_transfer(action)
            return

        # Transfer can't fix it — overall shortage on both subs.
        # If the missing currency is the BASE, buy it with surplus quote
        # on the starved sub(s) (reverse healing, stays within subs).
        if currency == base and self.healer is not None:
            for sub_id, sub_name in [(self.account_a_id, self.account_a_name),
                                     (self.account_b_id, self.account_b_name)]:
                result = await self.healer.heal_base(
                    sub_id, sub_name, base, quote,
                    quote_to_usd=pcfg.quote_to_usd,
                )
                # Success is logged inside the healer — no Telegram alert.
                if not result.ok and result.error and "cooldown" not in result.error \
                        and "no surplus" not in result.error \
                        and "too small" not in result.error \
                        and "below pair minimum" not in result.error:
                    # Heal FAILED for a real reason (API error, no price) — alert.
                    if self.alerter:
                        await self.alerter.send(
                            f"⚠️ Base healer FAILED on {sub_name} ({sym} {base}): "
                            f"{result.error}"
                        )

    async def _alert_loop(self) -> None:
        # Check every 30s whether any pair is breaching the leak threshold.
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
                return
            except asyncio.TimeoutError:
                pass
            for sym in list(self.contexts.keys()):
                breach = self.leaks.should_alert(sym)
                if breach is None:
                    continue
                msg = (
                    f"⚠️ valr-cm-spot leak alert: {sym}\n"
                    f"external_ratio={breach['external_ratio']:.1%} "
                    f"internal={breach['internal']} external={breach['external']} "
                    f"window={breach['total']}\n"
                    f"Cycle paused on {sym} for cooldown."
                )
                self.log.warning(msg)
                if self.alerter is not None:
                    await self.alerter.send(msg)
                # Cooldown: extend the pair's backoff
                ctx = self.contexts.get(sym)
                if ctx is not None:
                    ctx.backoff_until = time.time() + 600  # 10 min pair cooldown
