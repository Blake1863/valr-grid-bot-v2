#!/usr/bin/env python3
"""Inventory plan v2 — live prices. Fee model: 0.02% taker only."""
import asyncio, aiohttp, time, hmac, hashlib, sys
sys.path.insert(0, "src")
from creds import load
creds = load()
KEY = creds["MAIN_API_KEY"]; SEC = creds["MAIN_API_SECRET"]

SUBS = {
    "CMSZAR1":"1513524239074144256","CMSZAR2":"1513524288865144832",
    "CMSUSDT1":"1513524297399840768","CMSUSDT2":"1513524305939443712",
    "CMSUSDC1":"1513524314495823872","CMSUSDC2":"1513524323040333824",
}
ZAR_PAIRS={"BTCZAR":"BTC","ETHZAR":"ETH","XRPZAR":"XRP","SOLZAR":"SOL",
    "AVAXZAR":"AVAX","BNBZAR":"BNB","LINKZAR":"LINK","XAUTZAR":"XAUT","USDCZAR":"USDC"}
USDT_PAIRS={"XAUTUSDT":"XAUT","SPYXUSDT":"SPYX","NVDAXUSDT":"NVDAX","COINXUSDT":"COINX",
    "TRUMPUSDT":"TRUMP","MSTRXUSDT":"MSTRX","HOODXUSDT":"HOODX","TSLAXUSDT":"TSLAX",
    "CRCLXUSDT":"CRCLX","BITGOLDUSDT":"BITGOLD","VALR10USDT":"VALR10","JUPUSDT":"JUP","PUMPUSDT":"PUMP"}
TARGET=30.0

async def bal(s, sid):
    ts=str(int(time.time()*1000)); path="/v1/account/balances"
    sig=hmac.new(SEC.encode(),(ts+"GET"+path+""+sid).encode(),hashlib.sha512).hexdigest()
    h={"X-VALR-API-KEY":KEY,"X-VALR-SIGNATURE":sig,"X-VALR-TIMESTAMP":ts,"X-VALR-SUB-ACCOUNT-ID":sid}
    async with s.get(f"https://api.valr.com{path}",headers=h) as r: return await r.json()

async def px(s, pair):
    async with s.get(f"https://api.valr.com/v1/public/{pair}/marketsummary") as r:
        d=await r.json(); return float(d.get("lastTradedPrice",0) or 0)

async def main():
    async with aiohttp.ClientSession() as s:
        B={}
        for n,sid in SUBS.items():
            d=await bal(s,sid)
            B[n]={it["currency"]:float(it.get("available","0") or 0) for it in d} if isinstance(d,list) else {}
            await asyncio.sleep(0.12)
        # ZAR price (USDTZAR proxy ~ 1/zar_usd). Use USDCZAR for ZAR->USD.
        zar_usd = 1.0/ (await px(s,"USDCZAR"))  # USDC~$1, so USDCZAR = ZAR per USDC
        print(f"ZAR/USD: 1 ZAR = ${zar_usd:.5f}\n")

        # ---- ZAR bucket ----
        print("="*72); print("ZAR BUCKET"); print("="*72)
        zar_pool=(B["CMSZAR1"].get("ZAR",0)+B["CMSZAR2"].get("ZAR",0))
        print(f"ZAR quote pool: R{zar_pool:,.0f} = ${zar_pool*zar_usd:,.0f}\n")
        print(f"{'Pair':<11}{'Base':<7}{'Units':>14}{'USD':>9}{'Action':>16}")
        print("-"*60)
        zb=zs=0
        for pair,base in ZAR_PAIRS.items():
            units=B["CMSZAR1"].get(base,0)+B["CMSZAR2"].get(base,0)
            p=await px(s,pair); await asyncio.sleep(0.08)
            # convert pair price (ZAR) to USD
            val=units*p*zar_usd
            d=TARGET-val
            a=f"BUY ${d:.0f}" if d>3 else (f"SELL ${-d:.0f}" if d<-3 else "ok")
            if d>3: zb+=d
            elif d<-3: zs+=-d
            print(f"{pair:<11}{base:<7}{units:>14.6f}${val:>7.2f}{a:>16}")
        print(f"\nBUY ${zb:.0f} | SELL ${zs:.0f}")
        need=9*15
        print(f"ZAR working need ~${need} | held ${zar_pool*zar_usd:.0f} | "
              f"excess ${zar_pool*zar_usd-need:.0f} (after funding ${zb:.0f} buys)")

        # ---- USDT bucket ----
        print("\n"+"="*72); print("USDT BUCKET"); print("="*72)
        upool=B["CMSUSDT1"].get("USDT",0)+B["CMSUSDT2"].get("USDT",0)
        print(f"USDT quote pool: ${upool:,.2f}\n")
        print(f"{'Pair':<13}{'Base':<8}{'Units':>14}{'USD':>9}{'Action':>16}")
        print("-"*64)
        ub=us=0
        for pair,base in USDT_PAIRS.items():
            units=B["CMSUSDT1"].get(base,0)+B["CMSUSDT2"].get(base,0)
            p=await px(s,pair); await asyncio.sleep(0.08)
            val=units*p
            d=TARGET-val
            a=f"BUY ${d:.0f}" if d>3 else (f"SELL ${-d:.0f}" if d<-3 else "ok")
            if d>3: ub+=d
            elif d<-3: us+=-d
            print(f"{pair:<13}{base:<8}{units:>14.4f}${val:>7.2f}{a:>16}")
        print(f"\nBUY ${ub:.0f} | SELL ${us:.0f}")
        need=13*8
        print(f"USDT working need ~${need} | held ${upool:.0f}")

        # ---- USDC bucket ----
        print("\n"+"="*72); print("USDC BUCKET"); print("="*72)
        cpool=B["CMSUSDC1"].get("USDC",0)+B["CMSUSDC2"].get("USDC",0)
        eurc=B["CMSUSDC1"].get("EURC",0)+B["CMSUSDC2"].get("EURC",0)
        ep=await px(s,"EURCUSDC")
        print(f"USDC quote pool: ${cpool:,.2f}")
        print(f"EURC base: {eurc} (=${eurc*ep:.2f}) | EURCUSDC price {ep}")
        print(f"EURCUSDC DEAD. Needs ~$15 EURC bought + split. USDC held ${cpool:.0f} covers it.")

asyncio.run(main())
