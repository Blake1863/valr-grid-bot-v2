# cm-bot-spot-py — Build Brief

**Objective:** Replace the existing Rust `cm-bot-spot` + Python `cm-bot-spot-illiquid` + Python `cm-bot-spot-monitor` (three services, two languages) with a single, clean Python service `cm-bot-spot-py` that does both liquid and illiquid wash trading between subaccounts CMS1 and CMS2 on VALR.

**Operator:** Blake (single user). VALR South Africa. Trades primarily ZAR-quoted and USDT-quoted spot pairs. Goal of the bots: maintain trading volume / chart presence on listed pairs (legitimate market-making activity by the listing party). NOT a profit strategy — every cycle pays maker rebate / taker fee, so the system runs at a small bleed by design. **Optimization target: minimize that bleed by maximizing internal fills (CMS1 ↔ CMS2 self-matches) and avoiding leakage to external takers.**

---

## 1. First-principles core

The whole system reduces to one repeating cycle, per pair:

1. CMS_maker places a `postOnly` limit order **inside** the current orderbook spread.
2. CMS_taker fires an IOC limit at exactly that same price, intended to consume CMS_maker's resting order.
3. If they self-match → chart prints, fees are maker rebate + taker fee, inventory swaps CMS_maker → CMS_taker.
4. If an external taker hits CMS_maker first → chart still prints (maker filled externally), but inventory has now leaked to the external party. The waiting CMS_taker IOC finds nothing at that price and cancels with no fill (good — no double-spend, no unintended position).
5. If an external maker improves the price between our maker placement and our taker firing → our maker is no longer at top-of-book on the side that matters; CMS_taker IOC may match the external maker, leaking quote currency to them (bad).

**Leak modes to engineer against:**

- **Leak A — external taker hits our maker:** unavoidable in narrow spreads; mitigate by placing maker as deep into the spread as still safely above/below the opposite side, AND by minimizing time between maker-placed and taker-fired (target <100 ms).
- **Leak B — external maker improves price between our two orders:** mitigate by re-reading orderbook just before firing taker, and by using IOC limit at *exactly maker's price* (not market) — if best opposite is now better than our maker's price, our IOC just won't fill, and we cancel the maker. No leak.
- **Leak C — maker rejected (postOnly violation, would-have-crossed):** retry with more conservative price, don't fire taker.
- **Leak D — taker fills against an external maker before reaching ours:** avoid by IOC-limit at exact maker price. VALR matches by price-time priority, so an IOC at that price either hits our maker (best at that price, since postOnly only places when there's a gap) or doesn't fill at all.

**Strategy summary:** treat the maker placement as a price-pinning move that creates a unique fillable level inside the spread, then race to consume our own level before anyone else does. The key invariant is **IOC limit at the maker's exact price, never at market, and never aggressive past it**.

---

## 2. Existing context to learn from (read these in this order)

1. `bots/cm-bot-spot-illiquid/bot.py` — already implements the postOnly maker → IOC taker pattern correctly for illiquid pairs. **This is the closest existing model to the new architecture.** Read its docstring carefully.
2. `bots/cm-bot-spot-illiquid/config.json` — small, clear config schema for per-pair tuning.
3. `bots/cm-bot-spot-src/src/main.rs` — the current Rust bot. Lots of strategy detail in here:
   - 6-cycle rotation (CMS1 sells 3 cycles, then CMS2 sells 3 cycles — needs reconsidering, see §4)
   - `random_maker.rs` — randomised maker selection with consecutive-cap and 10-cycle balance bias. **Worth porting in spirit.**
   - `rebalance.rs` — periodic asset rebalancer (transfers when one account holds >60% of combined). **Definitely port.**
   - `liquidator.rs` — liquidation utility, used by monitor.py only on extreme balance drift. Out of scope for the merged bot, but understand it.
4. `bots/cm-bot-spot/monitor.py` — the auto-replenish service. It watches for `Insufficient Balance` failures and triggers `quote_replenish.py` / `spot_rebalance_manual.py`. **Most of its logic should fold into the new bot's inline rebalancer.** External rebalance scripts can be retained as one-shot utilities.
5. `bots/cm-bot-spot/config.json` — the full pair list (35 pairs, 15 currently enabled).
6. `bots/cm-bot-spot-src/src/ws_client.rs` — VALR account-WS message handling, `PLACE_LIMIT_WS` for low-latency order placement.
7. `bots/cm-bot-spot-src/src/price_feed.rs` — VALR public-WS orderbook subscription pattern.
8. `skills/valr-exchange/SKILL.md` and `skills/valr-exchange/references/valr-llms-full.txt` — **authoritative endpoint docs. Read first before any API work. Never invent endpoints.**

---

## 3. Architecture (target)

Single service: `valr-spot-bot.service` (rename when unifying to make it clearly the new system).

```
cm-bot-spot-py/
  bot.py                  # entrypoint
  src/
    config.py             # config schema, env loading
    valr_rest.py          # signed REST client (HMAC-SHA512, sub-account header support)
    valr_ws.py            # account WS (CMS1 + CMS2) + public trade WS (orderbook)
    pair_meta.py          # tickSize / minBaseAmount / minQuoteAmount / decimals cache
    strategy.py           # the maker→taker cycle, per-pair
    inventory.py          # balance tracking + intra-bot rebalancer (folds in monitor.py)
    maker_selector.py     # randomised maker side/account selection (port of random_maker.rs)
    quoting.py            # "where to place the maker inside the spread"
    metrics.py            # cycle counters, internal-vs-external fill ratio, leak detection
    logger.py             # structured logging, INFO + DEBUG levels, gated debug
  config.json             # merged config (all pairs, per-pair tuning)
  state.json              # persistent cycle state
  logs/
    bot.log
  README.md
  systemd/valr-spot-bot.service
```

**Single config file**, single state file, single log file, single systemd unit.

**Concurrency model:** asyncio. One task per pair (or one per pair-group), shared WS connections, shared rate-limit budget. Avoid threading.

**Dependencies:** stdlib + `aiohttp` + `websockets` + (optional) `pydantic` for config. **No heavy frameworks.** Match the spirit of the illiquid bot — small, readable, debuggable.

---

## 4. Strategy decisions (please make these explicit in design doc)

1. **Maker-side selection.** Current Rust bot uses random-with-balance-bias and a 6-cycle phase rotation. Pick one model:
   - Pure per-pair random with balance bias (illiquid bot does this).
   - Phase-based rotation (current Rust).
   - **Recommended:** random with balance bias *and* per-asset inventory feedback (avoid making the maker side that's already low on the base asset). Document chosen model.
2. **Maker price placement inside the spread.** Options:
   - `(bid + ask) / 2` rounded to tick — simple, but may collide with mid-resters.
   - `bid + tick` if maker BUY, `ask - tick` if maker SELL — pegs to one side, less collision but external takers more likely to hit.
   - Spread-aware: if spread > N ticks, place 1 tick inside our side; if spread = 1 tick (no room for postOnly), **skip the cycle**. Document chosen model and justify.
3. **Quantity selection.** Current Rust uses `qty_range_min_multiplier` × min_qty. Illiquid bot uses USD-value range. **Recommended:** USD-value range per pair (`print_value_usd_min/max`), enforce both base min_qty and quote min_value floors.
4. **Internal vs external fill detection.** After maker fills, we know it was "internal" if the taker IOC also filled in the same cycle, "external" otherwise. **Track this ratio per pair as the primary health metric. Alert if external-fill ratio exceeds, e.g., 20% for a liquid pair over a 100-cycle window.**
5. **Inventory floor.** Don't fire a cycle if the maker account doesn't have ≥ N × intended_qty of the base asset (or quote, depending on side). The monitor.py service already has this logic; bring it inline.
6. **Auto-rebalance.** Run every M cycles. Threshold and minimum transfer size from current `rebalance.rs`. **Important:** prefer transferring the asset the cycle just imbalanced, not a full sweep, to keep API load low.
7. **Cycle cadence per pair.** Currently 15s global. Should be per-pair (illiquid pairs slower, liquid pairs faster). Document recommended defaults.
8. **Failure handling.** If maker fails (postOnly rejected, balance shortage, rate limit), exponential back-off for that pair only, don't cascade. If WS disconnects, reconnect cleanly and resync state from REST.

---

## 5. Migration plan

Build it in three phases:

**Phase 1 — Design doc.** `cm-bot-spot-py/DESIGN.md`. Cover §3 architecture, §4 strategy decisions with rationale, the leak-mode mitigations from §1, and an explicit list of what's intentionally NOT being ported from the Rust bot. **Stop after Phase 1 and let the operator review before coding.**

**Phase 2 — Implementation.** Single service, all pairs disabled in config initially. Quality gates:
- `python -m pytest` passes (write tests for: pair_meta parsing, signing, quoting decisions, maker-selector, inventory floor checks)
- `python -m pyflakes src/` clean
- Type hints throughout; `mypy` clean optional
- Dry-run mode that prints intended cycles without firing orders

**Phase 3 — Rollout.**
1. Enable on 1 pair (suggest XRPZAR — high liquidity, low USD-per-cycle so failure is cheap) in dry-run for 30 min.
2. Switch that pair to live; disable same pair on Rust bot.
3. Compare fill rates and external-leak ratio for 24h.
4. Gradually migrate remaining pairs.
5. Once all pairs migrated, stop+disable: `cm-bot-spot.service`, `cm-bot-spot-illiquid.service`, `cm-bot-spot-monitor.service`. Archive `cm-bot-spot-src/` and the Rust binary.

---

## 6. Hard constraints / gotchas (read carefully)

- **Never invent VALR endpoints.** Use the local mirror at `skills/valr-exchange/references/valr-llms-full.txt`. The MEMORY.md "🛑 CORE RULE" applies.
- **Sub-account auth.** When using `X-VALR-SUB-ACCOUNT-ID` header, the subaccountId MUST also be in the signature payload, else `-11252 invalid signature`.
- **Credentials.** Read from `bots/cm-bot-spot/.env` (already used by both existing bots). Required keys: `MAIN_API_KEY`, `MAIN_API_SECRET`, `CM1_SUBACCOUNT_ID`, `CM2_SUBACCOUNT_ID`. Or read via `python3 /home/admin/.openclaw/secrets/secrets.py get <NAME>` (the Rust bot does this; both work).
- **postOnly rejection** is normal, not an error — handle it as a "skip this cycle, retry next interval".
- **Trailing zeros.** VALR strips trailing zeros from price strings in API responses (`"84.60"` → `"84.6"`). Compare prices numerically, not as strings. (This bit the v3 grid bot in production; see MEMORY.md.)
- **VALR sometimes returns HTML 400 on transient errors** even for accepted orders. If a placement returns HTML, verify via `GET /v1/orders/history/detail/customerorderid/{cid}` before deciding it failed.
- **Rate limits.** VALR is generous (~150 req/sec across REST + WS) but bursts can 429. Coalesce orderbook reads, prefer WS over REST polling.
- **customerOrderId reservation.** Once submitted, a cid is reserved for ~24h even on failure. Never reuse cids.
- **Logging discipline.** The Rust bot's chattiness was the operator's chief complaint. INFO = one line per cycle outcome. DEBUG = everything else, behind `BOT_DEBUG=1` env. Logrotate-compatible; no per-message JSON dumps in INFO.
- **Don't break the existing bots while building.** Keep them running. The new service must be standalone and only enabled at cutover time.

---

## 7. Deliverables

- Phase 1: `cm-bot-spot-py/DESIGN.md` — checked in, returned for review.
- Phase 2: working code in `cm-bot-spot-py/`, tests passing, dry-run output sample.
- Phase 3: rollout commands + checklist in `cm-bot-spot-py/ROLLOUT.md`.

**STOP after Phase 1 design doc and report back.** Do not write code or change any running service yet.

When you finish Phase 1, summarize:
- Strategy choices made (§4) and why
- Any open questions for the operator
- Files written and their purpose

---

Workspace root: `/home/admin/.openclaw/workspace`. Work inside `bots/cm-bot-spot-py/`.
