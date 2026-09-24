"""Signed REST client for VALR.

HMAC-SHA512 over: timestamp + VERB + path + body + subaccountId.
Header X-VALR-SUB-ACCOUNT-ID set when subaccount_id provided; subaccount_id
must also be in the signature payload (else -11252 invalid signature).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as _json
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

API_BASE = "https://api.valr.com"


def _ts_ms() -> int:
    return int(time.time() * 1000)


def sign(secret: str, method: str, path: str, body: str, subaccount_id: str = "") -> Tuple[int, str]:
    ts = _ts_ms()
    msg = f"{ts}{method.upper()}{path}{body}{subaccount_id}"
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha512)
    return ts, mac.hexdigest()


class ValrRest:
    def __init__(self, api_key: str, api_secret: str, *, timeout: float = 15.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "ValrRest":
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        subaccount_id: str = "",
        *,
        signed: bool = True,
    ) -> Tuple[int, Any]:
        body_str = _json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{API_BASE}{path}"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if signed:
            ts, sig = sign(self.api_secret, method, path, body_str, subaccount_id)
            headers["X-VALR-API-KEY"] = self.api_key
            headers["X-VALR-SIGNATURE"] = sig
            headers["X-VALR-TIMESTAMP"] = str(ts)
            if subaccount_id:
                headers["X-VALR-SUB-ACCOUNT-ID"] = subaccount_id
        async with self.session.request(method, url, data=body_str if body else None, headers=headers) as r:
            text = await r.text()
            try:
                data = _json.loads(text) if text else None
            except _json.JSONDecodeError:
                data = {"_raw": text[:400], "_error": "non-json-response"}
            return r.status, data

    # ----- Public endpoints (no auth) -----

    async def public_pairs(self) -> list:
        st, data = await self.request("GET", "/v1/public/pairs", signed=False)
        if st != 200 or not isinstance(data, list):
            raise RuntimeError(f"public_pairs failed: {st} {data}")
        return data

    async def public_orderbook(self, pair: str) -> dict:
        st, data = await self.request("GET", f"/v1/public/{pair}/orderbook", signed=False)
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError(f"orderbook {pair} failed: {st} {data}")
        return data

    async def public_marketsummary(self, pair: str) -> dict:
        st, data = await self.request("GET", f"/v1/public/{pair}/marketsummary", signed=False)
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError(f"marketsummary {pair} failed: {st} {data}")
        return data

    # ----- Authed endpoints -----

    async def balances(self, subaccount_id: str) -> list:
        st, data = await self.request("GET", "/v1/account/balances", subaccount_id=subaccount_id)
        if st != 200 or not isinstance(data, list):
            raise RuntimeError(f"balances({subaccount_id}) failed: {st} {data}")
        return data

    async def cancel_all_on_subaccount(self, subaccount_id: str) -> int:
        """DELETE /v1/orders — cancels all open orders on the (sub)account."""
        st, data = await self.request("DELETE", "/v1/orders", subaccount_id=subaccount_id)
        if st in (200, 202):
            if isinstance(data, list):
                return len(data)
            return 0
        return 0  # Best effort; non-fatal

    async def cancel_order(self, subaccount_id: str, *, pair: str,
                           order_id: Optional[str] = None,
                           customer_order_id: Optional[str] = None) -> Tuple[int, Any]:
        body: Dict[str, Any] = {"pair": pair}
        if order_id:
            body["orderId"] = order_id
        if customer_order_id:
            body["customerOrderId"] = customer_order_id
        return await self.request("DELETE", "/v1/orders/order", body, subaccount_id)

    async def order_history_by_cid(self, subaccount_id: str, customer_order_id: str) -> Tuple[int, Any]:
        path = f"/v1/orders/history/detail/customerorderid/{customer_order_id}"
        return await self.request("GET", path, subaccount_id=subaccount_id)

    async def place_limit_rest(self, subaccount_id: str, *, pair: str, side: str,
                               price: str, quantity: str, post_only: bool,
                               customer_order_id: str, time_in_force: str = "GTC") -> Tuple[int, Any]:
        body = {
            "pair": pair,
            "side": side.upper(),
            "price": price,
            "quantity": quantity,
            "postOnly": bool(post_only),
            "customerOrderId": customer_order_id,
            "timeInForce": time_in_force,
        }
        return await self.request("POST", "/v1/orders/limit", body, subaccount_id)

    async def market_order(self, subaccount_id: str, *, pair: str, side: str,
                           base_amount: Optional[str] = None,
                           quote_amount: Optional[str] = None) -> Tuple[int, Any]:
        """POST /v1/orders/market — market order on a (sub)account.
        Exactly one of base_amount / quote_amount must be provided."""
        body: Dict[str, Any] = {"pair": pair, "side": side.upper()}
        if base_amount is not None:
            body["baseAmount"] = base_amount
        if quote_amount is not None:
            body["quoteAmount"] = quote_amount
        return await self.request("POST", "/v1/orders/market", body, subaccount_id)

    async def subaccount_transfer(self, *, from_id: int | str, to_id: int | str,
                                  currency: str, amount: str) -> Tuple[int, Any]:
        # Per VALR docs (postV1AccountSubaccountsTransfer): fromId/toId are int64,
        # 0 = primary account. Path is singular 'transfer'.
        # When from_id is empty string (primary), the signature must NOT include
        # the subaccount ID, but the body needs 0.
        body = {
            "fromId": 0 if from_id == "" else int(from_id),
            "toId": int(to_id),
            "currencyCode": currency,
            "amount": amount,
            "allowBorrow": False,
        }
        return await self.request("POST", "/v1/account/subaccounts/transfer", body)
