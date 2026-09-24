"""Joint Savings Bot — detects ZAR deposits, auto-buys SOL, and auto-stakes."""

import json
import time
import uuid
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .valr_client import ValrClient

log = logging.getLogger("joint-savings")


class BotState:
    """Persistent bot state stored as JSON."""

    def __init__(self, path: str):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        defaults = {
            "seen_deposit_ids": [],
            "last_poll_at": None,
            "last_zar_balance": None,
            "last_sol_balance": None,
            "last_reward_event_id": None,
            "total_bought_sol": "0",
            "total_staked_sol": "0",
            "total_spent_zar": "0",
            "total_rewards_sol": "0",
            "purchases": [],
            "stakes": [],
            "rewards": [],
        }
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                existing = json.load(f)
            # Merge in any new fields missing from old state
            for k, v in defaults.items():
                if k not in existing:
                    existing[k] = v
            self.data = existing
        else:
            self.data = defaults
            self.save()

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    @property
    def seen_deposit_ids(self) -> set:
        return set(self.data.get("seen_deposit_ids", []))

    @property
    def last_zar_balance(self) -> Optional[float]:
        return self.data.get("last_zar_balance")

    @last_zar_balance.setter
    def last_zar_balance(self, v: float):
        self.data["last_zar_balance"] = v

    @property
    def last_sol_balance(self) -> Optional[float]:
        return self.data.get("last_sol_balance")

    @last_sol_balance.setter
    def last_sol_balance(self, v: float):
        self.data["last_sol_balance"] = v

    @property
    def last_reward_event_id(self) -> Optional[str]:
        return self.data.get("last_reward_event_id")

    @last_reward_event_id.setter
    def last_reward_event_id(self, v: Optional[str]):
        self.data["last_reward_event_id"] = v

    def record_purchase(self, order_id: str, zar_spent: float, sol_bought: float, price: float):
        self.data["purchases"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "orderId": order_id,
            "zarSpent": str(zar_spent),
            "solBought": str(sol_bought),
            "avgPrice": str(round(price, 4)),
        })
        self.data["total_spent_zar"] = str(float(self.data.get("total_spent_zar", "0")) + zar_spent)
        self.data["total_bought_sol"] = str(float(self.data.get("total_bought_sol", "0")) + sol_bought)
        self.save()

    def record_stake(self, sol_amount: float):
        self.data["stakes"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "solStaked": str(sol_amount),
        })
        self.data["total_staked_sol"] = str(float(self.data.get("total_staked_sol", "0")) + sol_amount)
        self.save()

    def record_reward(self, sol_amount: float):
        self.data["rewards"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "solRewarded": str(sol_amount),
        })
        self.data["total_rewards_sol"] = str(float(self.data.get("total_rewards_sol", "0")) + sol_amount)
        self.save()

    def mark_deposits_seen(self, ids: list):
        existing = set(self.data.get("seen_deposit_ids", []))
        new_ids = [i for i in ids if i not in existing]
        existing.update(new_ids)
        if len(existing) > 500:
            sorted_ids = sorted(existing)
            existing = set(sorted_ids[-500:])
        self.data["seen_deposit_ids"] = sorted(existing)
        self.save()


class JointSavingsBot:
    """Polls for ZAR deposits, buys SOL, and auto-stakes everything."""

    def __init__(self, config: dict, state_path: str):
        self.config = config
        self.pair = config["pair"]
        self.buy_asset = config.get("buy_asset", "SOL")
        self.quote_currency = config.get("quote_currency", "ZAR")
        self.min_buy = config.get("min_buy_amount_zar", 10.0)
        self.min_stake = config.get("min_stake_amount", 0.001)
        self.client = ValrClient(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            base_url=config.get("base_url", "https://api.valr.com"),
        )
        self.state = BotState(state_path)

    def _check_deposits(self) -> list:
        """Check for new FIAT_DEPOSIT transactions. Returns list of new deposit amounts."""
        deposits = self.client.get_fiat_deposits(limit=50)
        new_deposits = []
        for d in deposits:
            tid = d.get("id")
            if tid and tid not in self.state.seen_deposit_ids:
                credit_val = d.get("creditValue", "0")
                credit_cur = d.get("creditCurrency", "")
                event_at = d.get("eventAt", "")
                log.info(
                    "📥 New deposit detected: %s %s (tx %s, at %s)",
                    credit_val, credit_cur, tid[:8], event_at
                )
                new_deposits.append(float(credit_val))

        all_ids = [d.get("id") for d in deposits if d.get("id")]
        if all_ids:
            self.state.mark_deposits_seen(all_ids)

        return new_deposits

    def _check_balance_increase(self) -> bool:
        """Check if ZAR balance increased since last poll."""
        current = self.client.get_zar_available()
        last = self.state.last_zar_balance
        self.state.last_zar_balance = current
        self.state.save()

        if last is not None and current > last:
            diff = current - last
            log.info("💰 ZAR balance increased: R%.2f → R%.2f (+R%.2f)", last, current, diff)
            return True
        return False

    def _buy_sol(self, zar_amount: float) -> bool:
        """Buy SOL with the full ZAR balance via market order, then stake it."""
        if zar_amount < self.min_buy:
            log.info("⏭️ ZAR balance R%.2f below minimum R%.2f, skipping", zar_amount, self.min_buy)
            return False

        cid = f"js-buy-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
        log.info("🛒 Placing market BUY: %s SOL with R%.2f (cid=%s)", self.pair, zar_amount, cid)

        try:
            sol_before = self.client.get_sol_available()

            resp = self.client.place_market_buy(
                pair=self.pair,
                quote_amount=str(zar_amount),
                customer_order_id=cid,
            )

            order_id = resp.get("orderId", "unknown")
            log.info("✅ Order submitted: %s (202 Accepted)", order_id)

            # Wait for execution
            time.sleep(3)

            sol_after = self.client.get_sol_available()
            sol_bought = max(0, sol_after - sol_before)

            if sol_bought > 0:
                avg_price = zar_amount / sol_bought if sol_bought > 0 else 0
                log.info(
                    "🎉 Bought %.6f SOL for R%.2f (avg R%.2f/SOL)",
                    sol_bought, zar_amount, avg_price
                )
                self.state.record_purchase(order_id, zar_amount, sol_bought, avg_price)

                # Immediately stake the bought SOL
                self._stake_sol(sol_bought)
            else:
                log.warning("⚠️ Order submitted but SOL balance didn't change. Order %s — check status.", order_id)

            return True

        except Exception as e:
            log.error("❌ Market buy failed: %s", e)
            return False

    def _stake_sol(self, sol_amount: float) -> bool:
        """Stake a given amount of SOL."""
        if sol_amount < self.min_stake:
            log.info("⏭️ SOL amount %.6f below minimum %.6f, skipping stake", sol_amount, self.min_stake)
            return False

        # Truncate to 8 decimals (VALR staking max precision)
        import math
        truncated = math.floor(sol_amount * 1e8) / 1e8
        if truncated <= 0:
            log.info("⏭️ SOL amount %.8f truncated to zero, skipping stake", sol_amount)
            return False

        try:
            log.info("🔒 Staking %.8f SOL", truncated)
            resp = self.client.stake(
                currency=self.buy_asset,
                amount=f"{truncated:.8f}",
                earn_type="STAKE",
            )
            log.info("✅ Staked %.6f SOL successfully", sol_amount)
            self.state.record_stake(sol_amount)
            return True
        except Exception as e:
            log.error("❌ Staking failed: %s", e)
            return False

    def _check_staking_rewards(self) -> Optional[float]:
        """Check for new staking rewards. Returns reward amount if found."""
        try:
            rewards = self.client.get_staking_rewards(
                currency=self.buy_asset,
                earn_type="STAKE",
                limit=10,
            )

            # Find rewards newer than last seen
            last_id = self.state.last_reward_event_id
            new_reward_total = 0.0
            for r in rewards:
                eid = r.get("eventId", r.get("id"))
                if eid == last_id:
                    break
                if eid:
                    amount = float(r.get("rewardAmount", r.get("amount", "0")))
                    if amount > 0:
                        log.info("⭐ New staking reward: %.8f %s", amount, self.buy_asset)
                        new_reward_total += amount

            # Update last seen ID to the most recent
            if rewards:
                latest = rewards[0].get("eventId", rewards[0].get("id"))
                if latest:
                    self.state.last_reward_event_id = latest
                    self.state.save()

            return new_reward_total if new_reward_total > 0 else None

        except Exception as e:
            log.warning("⚠️ Failed to check staking rewards: %s", e)
            return None

    def _check_sol_balance_increase(self) -> Optional[float]:
        """Check if available SOL balance increased (from rewards or otherwise)."""
        current = self.client.get_sol_available()
        last = self.state.last_sol_balance
        self.state.last_sol_balance = current
        self.state.save()

        if last is not None and current > last:
            diff = current - last
            log.info("💎 Available SOL increased: %.6f → %.6f (+%.6f)", last, current, diff)
            return diff
        return None

    def run_once(self) -> bool:
        """One poll cycle. Returns True if action was taken."""
        action_taken = False

        # 1. Check for new ZAR deposits
        deposit_amounts = self._check_deposits()
        balance_increased = self._check_balance_increase()

        # 2. If new ZAR funds detected, buy SOL and stake it
        if deposit_amounts or balance_increased:
            zar = self.client.get_zar_available()
            if zar >= self.min_buy:
                if self._buy_sol(zar):
                    action_taken = True
            else:
                log.info("⏭️ Available ZAR R%.2f below minimum, not buying", zar)

        # 3. Check for staking rewards (via API)
        reward_amount = self._check_staking_rewards()
        if reward_amount and reward_amount >= self.min_stake:
            log.info("🔒 Auto-staking %.6f SOL from rewards", reward_amount)
            if self._stake_sol(reward_amount):
                self.state.record_reward(reward_amount)
                action_taken = True

        # 4. Also check for any available SOL balance increase (catches rewards not yet in API)
        sol_increase = self._check_sol_balance_increase()
        if sol_increase and sol_increase >= self.min_stake:
            log.info("🔒 Auto-staking %.6f SOL from available balance", sol_increase)
            if self._stake_sol(sol_increase):
                self.state.record_reward(sol_increase)
                action_taken = True

        return action_taken
