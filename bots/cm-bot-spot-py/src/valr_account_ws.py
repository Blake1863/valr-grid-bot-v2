"""Account WebSocket client for low-latency order placement.

One WS connection per subaccount. Authenticated via handshake headers
including `X-VALR-SUB-ACCOUNT-ID` (with subaccount_id appended to the
signed payload, matching the REST signing convention).

Supports:
  - PLACE_LIMIT_ORDER request -> PLACE_LIMIT_WS_RESPONSE reply
  - Reconnect with backoff
  - clientMsgId correlation via futures
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as _json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

WS_URL = "wss://api.valr.com/ws/account"

# Reasonable default reply timeout for an order placement RPC.
PLACE_TIMEOUT_S = 5.0


def _sign_ws(secret: str, ts_ms: int, subaccount_id: str) -> str:
    msg = f"{ts_ms}GET/ws/account{subaccount_id}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()


class AccountWS:
    """One WS connection per subaccount.

    Usage:
        ws = AccountWS(api_key, secret, subaccount_id, name="CMS1", logger=...)
        await ws.start()
        ok, data = await ws.place_limit(...)
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        subaccount_id: str,
        *,
        name: str = "WS",
        logger: Optional[logging.Logger] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.subaccount_id = subaccount_id
        self.name = name
        self.log = logger or logging.getLogger(name)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._authenticated = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the connect/read loop in background."""
        self._stop.clear()
        self._reader_task = asyncio.create_task(self._run(), name=f"valr-account-ws-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=3)
            except Exception:
                pass

    async def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._connect_and_read()
                # Clean close: short pause then reconnect
                if not self._stop.is_set():
                    self.log.warning("[%s] WS closed cleanly, reconnecting in 10s", self.name)
                    await asyncio.sleep(10)
                    backoff = 5.0
            except Exception as e:
                self.log.warning("[%s] WS error: %s — reconnecting in %.0fs", self.name, e, backoff)
                # Fail any pending RPCs
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError(f"WS error: {e}"))
                self._pending.clear()
                self._connected.clear()
                self._authenticated.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    async def _connect_and_read(self) -> None:
        ts = int(time.time() * 1000)
        sig = _sign_ws(self.api_secret, ts, self.subaccount_id)
        headers = [
            ("X-VALR-API-KEY", self.api_key),
            ("X-VALR-SIGNATURE", sig),
            ("X-VALR-TIMESTAMP", str(ts)),
        ]
        if self.subaccount_id:
            headers.append(("X-VALR-SUB-ACCOUNT-ID", self.subaccount_id))
        # websockets >=12 uses additional_headers; older accepts extra_headers
        try:
            ws = await websockets.connect(
                WS_URL, additional_headers=headers, open_timeout=10, ping_interval=20, ping_timeout=20
            )
        except TypeError:
            ws = await websockets.connect(
                WS_URL, extra_headers=headers, open_timeout=10, ping_interval=20, ping_timeout=20
            )
        self._ws = ws
        self._connected.set()
        self.log.info("[%s] WS connected", self.name)

        # Wait for AUTHENTICATED message (auto-pushed when handshake auth ok)
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=10.0)
            try:
                obj = _json.loads(first)
                if obj.get("type") == "AUTHENTICATED":
                    self._authenticated.set()
                    self.log.info("[%s] WS authenticated", self.name)
                else:
                    self.log.warning("[%s] WS first msg not AUTHENTICATED: %s", self.name, str(obj)[:200])
            except _json.JSONDecodeError:
                self.log.warning("[%s] WS first msg non-JSON: %s", self.name, first[:200])
        except asyncio.TimeoutError:
            raise RuntimeError("AUTHENTICATED not received within 10s")

        async for raw in ws:
            if self._stop.is_set():
                break
            try:
                obj = _json.loads(raw)
            except _json.JSONDecodeError:
                continue
            await self._handle(obj)

    async def _handle(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type", "")
        cmid = msg.get("clientMsgId")
        if not cmid:
            data = msg.get("data")
            if isinstance(data, dict):
                cmid = data.get("clientMsgId")
        if cmid and cmid in self._pending:
            fut = self._pending.pop(cmid)
            if not fut.done():
                payload = msg.get("data")
                if not isinstance(payload, dict):
                    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
                fut.set_result((msg_type, payload))
        # Other message types (BALANCE_UPDATE, OPEN_ORDERS_UPDATE, NEW_ACCOUNT_TRADE
        # etc.) are intentionally ignored here; we use REST for balances and
        # order-history detail for cycle classification.

    async def wait_ready(self, timeout: float = 15.0) -> None:
        await asyncio.wait_for(self._authenticated.wait(), timeout=timeout)

    async def place_limit(
        self,
        *,
        pair: str,
        side: str,
        price: str,
        quantity: str,
        post_only: bool,
        customer_order_id: str,
        time_in_force: str = "GTC",
        timeout: float = PLACE_TIMEOUT_S,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Returns (success, info_dict).

        success=True iff PLACE_LIMIT_WS_RESPONSE arrived with success/order id.
        info_dict carries the response payload (or error details).
        """
        if self._ws is None or not self._authenticated.is_set():
            return (False, {"error": "WS not ready"})

        cmid = uuid.uuid4().hex[:24]
        msg = {
            "type": "PLACE_LIMIT_ORDER",
            "clientMsgId": cmid,
            "payload": {
                "pair": pair,
                "side": side.upper(),
                "price": price,
                "quantity": quantity,
                "postOnly": bool(post_only),
                "customerOrderId": customer_order_id,
                "timeInForce": time_in_force,
            },
        }

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cmid] = fut
        try:
            await self._ws.send(_json.dumps(msg))
        except Exception as e:
            self._pending.pop(cmid, None)
            return (False, {"error": f"send failed: {e}"})

        try:
            msg_type, data = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmid, None)
            return (False, {"error": "timeout waiting for PLACE_LIMIT_WS_RESPONSE"})
        except Exception as e:
            self._pending.pop(cmid, None)
            return (False, {"error": f"reply error: {e}"})

        # Common shapes: success at data["success"] or top-level message_type INVALID_*
        if msg_type == "INVALID_PLACE_LIMIT_REQUEST":
            return (False, {"error": "invalid", "data": data})
        success = bool(data.get("success", True))
        order_id = data.get("orderId") or data.get("id") or ""
        return (success, {"orderId": order_id, "msg_type": msg_type, "data": data})
