"""Public trade WebSocket — orderbook subscriptions, mid/bid/ask cache.

One shared connection. We use AGGREGATED_ORDERBOOK_UPDATE for top-of-book
which is sufficient for our quoting needs.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from decimal import Decimal
from typing import Dict, Iterable, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

WS_URL = "wss://api.valr.com/ws/trade"


class PriceFeed:
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.log = logger or logging.getLogger("valr-trade-ws")
        # pair -> (bid, ask, last_update_ts)
        self._book: Dict[str, Tuple[Decimal, Decimal, float]] = {}
        self._symbols: list[str] = []
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self, symbols: Iterable[str]) -> None:
        self._symbols = list(symbols)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="valr-trade-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except Exception:
                pass

    def get(self, pair: str) -> Optional[Tuple[Decimal, Decimal, float]]:
        return self._book.get(pair)

    async def _run(self) -> None:
        backoff = 3.0
        while not self._stop.is_set():
            try:
                await self._connect_and_read()
                if not self._stop.is_set():
                    self.log.warning("trade WS closed, reconnecting in 5s")
                    await asyncio.sleep(5)
                    backoff = 3.0
            except Exception as e:
                self.log.warning("trade WS error: %s — reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect_and_read(self) -> None:
        async with websockets.connect(WS_URL, open_timeout=10, ping_interval=20, ping_timeout=20) as ws:
            sub = {
                "type": "SUBSCRIBE",
                "subscriptions": [
                    {"event": "AGGREGATED_ORDERBOOK_UPDATE", "pairs": self._symbols},
                    {"event": "MARKET_SUMMARY_UPDATE", "pairs": self._symbols},
                ],
            }
            await ws.send(_json.dumps(sub))
            async for raw in ws:
                if self._stop.is_set():
                    break
                try:
                    obj = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "AGGREGATED_ORDERBOOK_UPDATE":
                    pair = obj.get("currencyPairSymbol")
                    data = obj.get("data") or {}
                    bids = data.get("Bids") or data.get("bids") or []
                    asks = data.get("Asks") or data.get("asks") or []
                    if pair and bids and asks:
                        try:
                            bid = Decimal(str(bids[0]["price"]))
                            ask = Decimal(str(asks[0]["price"]))
                        except Exception:
                            continue
                        import time as _t
                        self._book[pair] = (bid, ask, _t.time())
