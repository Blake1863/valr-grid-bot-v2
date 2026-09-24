import asyncio
from decimal import Decimal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.inventory import InventoryClient, preflight


class FakeRest:
    def __init__(self, balances_by_sub):
        self._balances = balances_by_sub

    async def balances(self, sub):
        # return list-of-dicts in VALR shape
        out = []
        for cur, avail in self._balances.get(sub, {}).items():
            out.append({"currency": cur, "available": str(avail)})
        return out


def test_preflight_ok_sell():
    rest = FakeRest({
        "M": {"BTC": "0.01"},
        "T": {"ZAR": "10000"},
    })
    inv = InventoryClient(rest, ttl_seconds=60)

    async def go():
        r = await preflight(
            inv, maker_sub="M", taker_sub="T",
            maker_side="SELL", base="BTC", quote="ZAR",
            qty=Decimal("0.001"), notional=Decimal("100"),
        )
        assert r.ok, r.reason

    asyncio.run(go())


def test_preflight_short_maker_sell():
    rest = FakeRest({
        "M": {"BTC": "0.0001"},  # not enough
        "T": {"ZAR": "10000"},
    })
    inv = InventoryClient(rest)

    async def go():
        r = await preflight(
            inv, maker_sub="M", taker_sub="T",
            maker_side="SELL", base="BTC", quote="ZAR",
            qty=Decimal("0.001"), notional=Decimal("100"),
        )
        assert not r.ok
        assert "BTC" in r.reason

    asyncio.run(go())


def test_preflight_buy_taker_short_base():
    rest = FakeRest({
        "M": {"USDT": "1000"},
        "T": {"SOL": "0.1"},  # not enough
    })
    inv = InventoryClient(rest)

    async def go():
        r = await preflight(
            inv, maker_sub="M", taker_sub="T",
            maker_side="BUY", base="SOL", quote="USDT",
            qty=Decimal("1.0"), notional=Decimal("100"),
        )
        assert not r.ok

    asyncio.run(go())
