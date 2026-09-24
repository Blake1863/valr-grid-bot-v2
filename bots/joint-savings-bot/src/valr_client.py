"""VALR REST API client for the joint savings bot."""

import hmac
import hashlib
import time
import json
import urllib.request
import urllib.parse
import urllib.error
import logging
from typing import Optional

log = logging.getLogger("joint-savings.valr")


class ValrClient:
    """Minimal VALR REST client for spot trading."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.valr.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def _sign(self, verb: str, path: str, body: str) -> dict:
        ts = str(int(time.time() * 1000))
        payload = ts + verb + path + body
        sig = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha512).hexdigest()
        return {
            "X-VALR-API-KEY": self.api_key,
            "X-VALR-SIGNATURE": sig,
            "X-VALR-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _request(self, verb: str, path: str, body: Optional[dict] = None, query: Optional[dict] = None) -> dict:
        full_path = path
        if query:
            qs = urllib.parse.urlencode(query)
            full_path = f"{path}?{qs}"

        body_str = json.dumps(body) if body else ""
        headers = self._sign(verb, full_path, body_str)

        data = body_str.encode() if body else None
        req = urllib.request.Request(f"{self.base_url}{full_path}", data=data, headers=headers, method=verb)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            log.error("VALR %s %s → %d: %s", verb, full_path, e.code, raw[:500])
            raise

    # ── Balances ──

    def get_balances(self) -> list:
        """Return list of all balance objects."""
        return self._request("GET", "/v1/account/balances")

    def get_zar_available(self) -> float:
        """Get available ZAR balance."""
        balances = self.get_balances()
        for b in balances:
            if b.get("currency") == "ZAR":
                return float(b.get("available", "0"))
        return 0.0

    def get_sol_available(self) -> float:
        """Get available SOL balance."""
        balances = self.get_balances()
        for b in balances:
            if b.get("currency") == "SOL":
                return float(b.get("available", "0"))
        return 0.0

    # ── Staking ──

    def get_staking_balances(self, earn_type: str = "STAKE") -> list:
        """Get staking balances for the given earn type."""
        return self._request("GET", "/v1/staking/balances", query={"earnType": earn_type})

    def get_staked_balance(self, currency: str = "SOL", earn_type: str = "STAKE") -> float:
        """Get the staked balance for a specific currency."""
        balances = self.get_staking_balances(earn_type)
        for b in balances:
            if b.get("currencySymbol") == currency:
                return float(b.get("amount", "0"))
        return 0.0

    def stake(self, currency: str, amount: str, earn_type: str = "STAKE") -> dict:
        """Lock currency into staking. Returns 200 on success."""
        body = {
            "currencySymbol": currency,
            "amount": amount,
            "earnType": earn_type,
        }
        return self._request("POST", "/v1/staking/stake", body=body)

    def get_staking_rewards(self, currency: str = "SOL", earn_type: str = "STAKE", limit: int = 20) -> list:
        """Get recent staking rewards."""
        return self._request("GET", "/v1/staking/rewards", query={
            "currencySymbol": currency,
            "earnType": earn_type,
            "limit": str(limit),
        })

    def get_staking_history(self, currency: str = "SOL", earn_type: str = "STAKE", limit: int = 20) -> list:
        """Get recent staking history."""
        return self._request("GET", "/v1/staking/history", query={
            "currencySymbol": currency,
            "earnType": earn_type,
            "limit": str(limit),
        })

    # ── Transaction History ──

    def get_fiat_deposits(self, limit: int = 50, after_event_at: Optional[str] = None) -> list:
        """Get FIAT_DEPOSIT transactions in ZAR, newest first."""
        query = {
            "currency": "ZAR",
            "transactionTypes": "FIAT_DEPOSIT",
            "limit": str(limit),
        }
        return self._request("GET", "/v1/account/transactionhistory", query=query)

    # ── Market Data ──

    def get_market_summary(self, pair: str) -> dict:
        """Get public market summary for a pair."""
        return self._request("GET", f"/v1/public/{pair}/marketsummary")

    # ── Orders ──

    def place_market_buy(self, pair: str, quote_amount: str, customer_order_id: Optional[str] = None) -> dict:
        """Place a market BUY order using quote amount. Returns 202 Accepted response."""
        body = {
            "side": "BUY",
            "quoteAmount": quote_amount,
            "pair": pair,
            "allowMargin": False,
        }
        if customer_order_id:
            body["customerOrderId"] = customer_order_id
        return self._request("POST", "/v1/orders/market", body=body)

    def get_order_by_id(self, order_id: str) -> dict:
        """Get order status by orderId."""
        return self._request("GET", f"/v1/orders/{order_id}")
