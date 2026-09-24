from decimal import Decimal

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pair_meta as pm


def test_split_pair():
    assert pm.split_pair("BTCZAR") == ("BTC", "ZAR")
    assert pm.split_pair("XAUTUSDT") == ("XAUT", "USDT")
    assert pm.split_pair("EURCUSDC") == ("EURC", "USDC")
    assert pm.split_pair("SOLUSDTPERP") == ("SOL", "USDTPERP")


def test_parse_basic():
    raw = {
        "symbol": "BTCZAR",
        "baseCurrency": "BTC",
        "quoteCurrency": "ZAR",
        "tickSize": "1",
        "baseDecimalPlaces": "8",
        "minBaseAmount": "0.0001",
        "minQuoteAmount": "10",
    }
    m = pm.parse(raw)
    assert m.symbol == "BTCZAR"
    assert m.base == "BTC"
    assert m.quote == "ZAR"
    assert m.tick_size == Decimal("1")
    assert m.tick_decimals == 0
    assert m.base_decimals == 8


def test_round_to_tick():
    tick = Decimal("0.01")
    assert pm.round_to_tick(Decimal("84.605"), tick) == Decimal("84.60") or pm.round_to_tick(Decimal("84.605"), tick) == Decimal("84.61")
    assert pm.round_to_tick(Decimal("84.60"), tick) == Decimal("84.60")


def test_format_price_strips_trailing():
    raw = {"symbol": "BTCZAR", "tickSize": "0.01", "baseDecimalPlaces": "8", "minBaseAmount": "0.0001", "minQuoteAmount": "10"}
    m = pm.parse(raw)
    s = pm.format_price(Decimal("84.6"), m)
    assert s == "84.60"


def test_round_up_to_step():
    # Exact value passes through
    assert pm.round_up_to_step(Decimal("0.10"), 2) == Decimal("0.10")
    # Round up
    assert pm.round_up_to_step(Decimal("0.101"), 2) == Decimal("0.11")
    assert pm.round_up_to_step(Decimal("0.001"), 2) == Decimal("0.01")
