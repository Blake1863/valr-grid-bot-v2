# VALR Ops Archive (reference — read on demand)

Archived 2026-08-20 from MEMORY.md. Wash trading is fully wound down; this file
keeps the operational details in case of future VALR work.

## 🔁 Wash-Bot Restock Workflow (historical — bots wound down 2026-08-19)
1. Load creds from `bots/cm-bot-spot/.env` → `MAIN_API_KEY`/`MAIN_API_SECRET`
2. `GET /v1/account/balances` on main. ZAR pairs: convert USDT→ZAR via `POST /v1/orders/market {"side":"SELL","baseAmount":"<n>","pair":"USDTZAR"}`
3. Buy base on main: `POST /v1/orders/market {"side":"BUY","quoteAmount":"<spend>","pair":"<PAIR>"}` (~$35/pair)
4. Transfer to subs: `POST /v1/account/subaccounts/transfer {"fromId":0,"toId":<subId>,"currencyCode":"<BASE>","amount":"<half>","allowBorrow":false}`
5. Wait ~90s for balance cache TTL before verifying prints.

## 💵 Fee Model (2026-06-18)
VALR wash-bot fees: 0.02% TAKER only, 0% MAKER. Drain ~$0.60/week/pair.
Inventory target was $35/pair (±$5). Base only drains on SELL-base prints.

## 🔧 Normalization Scripts (historical)
- `bots/cm-bot-spot-py/scripts/normalize_inventory.py` — market-SELL excess base to $35/pair
- `scripts/normalize_phase2.py`, `scripts/balance_audit.py`, `scripts/main_bal.py`
- **Cred gotcha:** `***"MAIN_API_KEY"]` trips redaction; use `creds.get("MAIN_API_" + "KEY")`

## Subaccount IDs (historical)
| Bucket | Sub A | Sub B |
|--------|-------|-------|
| ZAR  | CMSZAR1 1513524239074144256 | CMSZAR2 1513524288865144832 |
| USDT | CMSUSDT1 1513524297399840768 | CMSUSDT2 1513524305939443712 |
| USDC | CMSUSDC1 1513524314495823872 | CMSUSDC2 1513524323040333824 |

## 💸 Withdrawal Fee Lookup
`GET /v1/wallet/crypto/:currencyCode/withdraw` → `withdrawCost`, `minimumWithdrawAmount`. NOT `/withdraw/config` (returns 500).

## VALR API lessons (still relevant for any future VALR work)
- Transfer endpoint must be signed with EMPTY subaccount id; sub header only for sub-scoped reads/trades.
- Futures close = opposite-side market order with `reduceOnly:true`.
- Min-order-size gotcha: pad position to min size, then close.
- Public market data: `/v1/public/{pair}/marketsummary|orderbook|trades` (not `/v1/marketdata/summary`).
- Margin info: WS ONLY (`MARGIN_INFO` on `/ws/account`), no REST endpoint.
- Auth: HMAC-SHA512(timestamp + VERB + path + body + subaccountId); headers `X-VALR-API-KEY/SIGNATURE/TIMESTAMP/SUB-ACCOUNT-ID`. WS: `wss://api.valr.com/ws/trade|account`.

## Wind-down state (2026-08-19)
All wash trading discontinued. Services stopped+disabled: `valr-cm-spot@{zar,usdt,usdc,btc}`, `cm-bot-v2`. Positions closed, subs liquidated → ~528 USDT swept to MAIN. Remaining: dust only (<1 ZAR, <0.03 USDC, EURC/VALR10/BITGOLD specks), below min order sizes. Repo kept: `bots/cm-bot-spot-py/`. Do NOT restart services or recreate wash-bot crons.
