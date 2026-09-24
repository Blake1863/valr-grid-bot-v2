#!/usr/bin/env python3
"""Compute sustainable inventory plan across all 3 buckets.

Fee model: 0.02% taker only, 0 maker. Fee drain is negligible.
Target: ~$30/pair base inventory (~$15/sub working buffer).
Quote pool: only needs enough working ZAR/USDT/USDC for the active pairs.
"""

# Approx USD prices (spot)
PX = {
    "BTC": 79000, "ETH": 1720, "XRP": 0.149, "SOL": 71, "AVAX": 0.77,
    "BNB": 53.5, "LINK": 1.49, "XAUT": 3270, "USDC": 1.0, "USDT": 1.0,
    "TRUMP": 7.4, "JUP": 0.42, "HOODX": 64, "SPYX": 600, "VALR10": 8.0,
    "TSLAX": 290, "CRCLX": 110, "COINX": 290, "MSTRX": 290, "BITGOLD": 64,
    "NVDAX": 140, "PUMP": 0.0035, "EURC": 1.08,
}

ZAR_USD = 0.0595  # 1 ZAR in USD (~16.8 ZAR/USD)

# Live balances captured from API
ZAR = {
    "CMSZAR1": {"ZAR": 3816.82, "BTC": 0.001581868647, "ETH": 0.000816640932,
                "XRP": 42.79721243, "SOL": 0.040535792, "AVAX": 3.168695295185,
                "BNB": 0.180349331593, "LINK": 10.0273545, "XAUT": 0.005530145,
                "USDC": 11.8853759935},
    "CMSZAR2": {"ZAR": 3256.95, "BTC": 0.001475230715, "ETH": 0.000816640932,
                "XRP": 36.45561243, "SOL": 0.040535792, "AVAX": 8.582820463333,
                "BNB": 0.172361035021, "LINK": 9.3474865, "XAUT": 0.008330025,
                "USDC": 13.6831961935},
}

# ZAR pairs use these base assets
ZAR_PAIRS = {
    "BTCZAR": "BTC", "ETHZAR": "ETH", "XRPZAR": "XRP", "SOLZAR": "SOL",
    "AVAXZAR": "AVAX", "BNBZAR": "BNB", "LINKZAR": "LINK", "XAUTZAR": "XAUT",
    "USDCZAR": "USDC",
}

TARGET = 30.0  # $/pair total base inventory


def usd(asset, amt):
    return amt * PX.get(asset, 0)


print("=" * 70)
print("ZAR BUCKET")
print("=" * 70)

# Quote pool: ZAR
zar_total = ZAR["CMSZAR1"]["ZAR"] + ZAR["CMSZAR2"]["ZAR"]
zar_total_usd = zar_total * ZAR_USD
print(f"\nZAR quote pool: R{zar_total:,.0f} = ${zar_total_usd:,.0f}")
print("  (shared across all 9 ZAR pairs)")

# Base inventory per pair
print(f"\n{'Pair':<10} {'Base':<6} {'Total units':>14} {'USD val':>9} {'Target':>8} {'Action':>20}")
print("-" * 75)
buys = []
sells = []
for pair, base in ZAR_PAIRS.items():
    units = ZAR["CMSZAR1"].get(base, 0) + ZAR["CMSZAR2"].get(base, 0)
    val = usd(base, units)
    delta = TARGET - val
    if delta > 3:
        action = f"BUY ${delta:.0f}"
        buys.append((pair, base, delta))
    elif delta < -3:
        action = f"SELL ${-delta:.0f}"
        sells.append((pair, base, -delta))
    else:
        action = "ok"
    print(f"{pair:<10} {base:<6} {units:>14.6f} ${val:>7.2f} ${TARGET:>6.0f} {action:>20}")

tot_buy = sum(d for _, _, d in buys)
tot_sell = sum(d for _, _, d in sells)
print(f"\nBase BUY total: ${tot_buy:.0f}  |  Base SELL total: ${tot_sell:.0f}")

# How much ZAR quote is actually needed?
# Each pair needs ~$15/sub working quote too, but ZAR is shared.
# Working ZAR needed = ~$30/pair worth for pairs that print BUY-side = ~$270 total
# Actually quote per sub just needs a few prints buffer. ~$200 total is plenty.
needed_zar_usd = 9 * 15  # ~$15/pair working quote buffer, very generous
print(f"\nZAR needed (working buffer ~$15/pair × 9): ${needed_zar_usd:.0f}")
print(f"ZAR held: ${zar_total_usd:.0f}")
print(f"EXCESS ZAR: ${zar_total_usd - needed_zar_usd:.0f}  <-- can withdraw to main")
print(f"  Note: also need ZAR to fund the ${tot_buy:.0f} of base buys")

# USDT bucket
print()
print("=" * 70)
print("USDT BUCKET")
print("=" * 70)
USDT = {
    "CMSUSDT1": {"USDT": 51.84, "TRUMP": 27.97272297, "JUP": 251.585924,
                 "HOODX": 1.0005735, "SPYX": 0.2979668, "VALR10": 0.600168835,
                 "TSLAX": 0.0984636, "CRCLX": 0.4397565, "COINX": 0.1002205,
                 "XAUT": 0.00169202, "MSTRX": 0.40353229, "BITGOLD": 0.39331515,
                 "NVDAX": 0.1620665, "PUMP": 21127.6045},
    "CMSUSDT2": {"USDT": 64.57, "JUP": 206.63016, "MSTRX": 0.53353029,
                 "HOODX": 1.2495463, "TSLAX": 0.1024624, "CRCLX": 0.5137541,
                 "COINX": 0.1402205, "XAUT": 0.00209198, "BITGOLD": 0.51930735,
                 "VALR10": 0.770094835, "NVDAX": 0.2000697, "PUMP": 23025.2169,
                 "TRUMP": 18.91340497, "SPYX": 0.2639816},
}
USDT_PAIRS = {
    "XAUTUSDT": "XAUT", "SPYXUSDT": "SPYX", "NVDAXUSDT": "NVDAX",
    "COINXUSDT": "COINX", "TRUMPUSDT": "TRUMP", "MSTRXUSDT": "MSTRX",
    "HOODXUSDT": "HOODX", "TSLAXUSDT": "TSLAX", "CRCLXUSDT": "CRCLX",
    "BITGOLDUSDT": "BITGOLD", "VALR10USDT": "VALR10", "JUPUSDT": "JUP",
    "PUMPUSDT": "PUMP",
}
usdt_total = USDT["CMSUSDT1"]["USDT"] + USDT["CMSUSDT2"]["USDT"]
print(f"\nUSDT quote pool: ${usdt_total:,.2f} (shared across {len(USDT_PAIRS)} pairs)")
print(f"\n{'Pair':<13} {'Base':<8} {'Total units':>14} {'USD val':>9} {'Action':>16}")
print("-" * 70)
ut_buy = ut_sell = 0
for pair, base in USDT_PAIRS.items():
    units = USDT["CMSUSDT1"].get(base, 0) + USDT["CMSUSDT2"].get(base, 0)
    val = usd(base, units)
    delta = TARGET - val
    if delta > 3:
        action = f"BUY ${delta:.0f}"; ut_buy += delta
    elif delta < -3:
        action = f"SELL ${-delta:.0f}"; ut_sell += -delta
    else:
        action = "ok"
    print(f"{pair:<13} {base:<8} {units:>14.4f} ${val:>7.2f} {action:>16}")
print(f"\nBase BUY total: ${ut_buy:.0f}  |  Base SELL total: ${ut_sell:.0f}")
needed_usdt = len(USDT_PAIRS) * 8
print(f"USDT needed (working buffer ~$8/pair): ${needed_usdt:.0f}")
print(f"USDT held: ${usdt_total:.0f}  | status: {'OK' if usdt_total < needed_usdt*1.5 else 'EXCESS'}")

# USDC bucket
print()
print("=" * 70)
print("USDC BUCKET")
print("=" * 70)
usdc_total = 16.4976404425746 + 13.94083536672346
print(f"\nUSDC quote pool: ${usdc_total:,.2f}")
print("Only pair: EURCUSDC (currently DEAD — 0 EURC, never printed)")
print("Needs: ~$15 EURC base bought + split to both subs")
print(f"USDC held: ${usdc_total:.2f} — enough to fund EURC buy + working buffer")
