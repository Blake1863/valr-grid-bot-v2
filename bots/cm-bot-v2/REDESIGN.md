# CM Bot v2 - Account Rotation Redesign

## Current Issues

1. **Fixed rotation ignores inventory** - Rotates every 3 cycles regardless of actual USDT balances
2. **No balance checking** - Attempts orders even when accounts are underfunded
3. **Inventory drift** - No tracking of net position per account
4. **LINKZAR failures** - CM1 consistently fails due to insufficient balance

## Current Account Balances
- **CM1**: 2.39 USDT available (5.84 total)
- **CM2**: 2.22 USDT available (2.86 total)

Both accounts are severely underfunded for sustained wash trading.

---

## Proposed Redesign

### 1. Balance-Aware Role Assignment

Instead of fixed rotation, assign roles based on **relative inventory**:

```
if cm1_usdt > cm2_usdt:
    CM1 = SELLER (generates USDT)
    CM2 = BUYER (consumes USDT)
else:
    CM1 = BUYER
    CM2 = SELLER
```

**Rationale**: The account with more USDT should sell (converting asset → USDT), while the account with less should buy (converting USDT → asset). This naturally rebalances over time.

### 2. Minimum Balance Thresholds

Before each cycle, check:
```
MIN_BALANCE_USDT = 10.0  # Minimum USDT to continue trading
MIN_ORDER_VALUE = 5.0    # Minimum order size (VALR requirement)

if cm1_usdt < MIN_BALANCE_USDT or cm2_usdt < MIN_BALANCE_USDT:
    PAUSE TRADING
    ALERT: "Account underfunded - transfer USDT required"
```

### 3. Position Tracking

Track net position per account:
```json
{
  "CM1": {
    "usdt_start": 100.0,
    "usdt_current": 95.5,
    "net_bought": 0.05,  // SOL bought - SOL sold
    "cycles_as_maker": 10,
    "cycles_as_taker": 10
  },
  "CM2": {
    "usdt_start": 100.0,
    "usdt_current": 104.5,
    "net_bought": -0.05,
    "cycles_as_maker": 10,
    "cycles_as_taker": 10
  }
}
```

### 4. Dynamic Rebalancing

When imbalance exceeds threshold:
```
IMBALANCE_THRESHOLD = 0.3  # 30% difference

imbalance = abs(cm1_usdt - cm2_usdt) / (cm1_usdt + cm2_usdt)

if imbalance > IMBALANCE_THRESHOLD:
    # Force role assignment to rebalance
    if cm1_usdt > cm2_usdt:
        CM1 = SELLER (reduce USDT)
        CM2 = BUYER (increase USDT)
    else:
        CM1 = BUYER
        CM2 = SELLER
```

### 5. Internal Transfer Support

VALR allows internal transfers between subaccounts. Add rebalancing function:
```python
def rebalance_accounts():
    cm1_balance = get_balance(CM1)
    cm2_balance = get_balance(CM2)
    total = cm1_balance + cm2_balance
    target = total / 2
    
    if cm1_balance > target * 1.2:  # 20% above target
        transfer_amount = cm1_balance - target
        internal_transfer(CM1 → CM2, transfer_amount)
    elif cm2_balance > target * 1.2:
        transfer_amount = cm2_balance - target
        internal_transfer(CM2 → CM1, transfer_amount)
```

---

## Implementation Priority

### Phase 1: Quick Fixes (Do Now)
1. ✅ Add balance checking before orders
2. ✅ Dynamic role assignment based on USDT balance
3. ✅ Pause trading when accounts underfunded

### Phase 2: Position Tracking (Next)
1. Track net position per account
2. Log inventory drift over time
3. Alert when drift exceeds threshold

### Phase 3: Auto-Rebalancing (Future)
1. Implement internal transfer logic
2. Automatic rebalancing every N cycles
3. Manual override via config

---

## Immediate Action Required

**Fund both accounts with at least 50 USDT each** for sustainable trading:
- Current: ~2.4 USDT each (barely enough for 1-2 orders)
- Recommended: 50 USDT each (enough for ~10 orders per account)
- Optimal: 100+ USDT each (sustained trading without frequent rebalancing)

**Transfer USDT to subaccounts:**
1. Log into VALR web interface
2. Navigate to Subaccounts
3. Transfer ~50 USDT to CM1
4. Transfer ~50 USDT to CM2

---

## Code Changes Required

### `state.rs` - Add Balance Tracking
```rust
struct AccountState {
    usdt_balance: f64,
    last_balance_check: chrono::DateTime<Utc>,
    net_bought: f64,  // Positive = net buyer, Negative = net seller
    cycles_as_maker: u64,
    cycles_as_taker: u64,
}
```

### `main.rs` - Balance Check Before Cycle
```rust
// Before each cycle
let cm1_balance = state.cm1_client.get_usdt_balance().await?;
let cm2_balance = state.cm2_client.get_usdt_balance().await?;

if cm1_balance < MIN_BALANCE || cm2_balance < MIN_BALANCE {
    eprintln!("[WARN] Insufficient balance - pausing trading");
    tokio::time::sleep(Duration::from_secs(60)).await;
    continue;
}

// Determine roles based on balance
let (maker_client, taker_client, maker_account, taker_account) = 
    if cm1_balance > cm2_balance {
        (&state.cm1_client, &state.cm2_client, "CM1", "CM2")
    } else {
        (&state.cm2_client, &state.cm1_client, "CM2", "CM1")
    };
```

---

## Expected Outcomes

After implementing this redesign:
1. ✅ No more "Insufficient Balance" errors (pauses instead)
2. ✅ Natural inventory rebalancing through role assignment
3. ✅ Better capital efficiency (trades when accounts are balanced)
4. ✅ Clear alerts when funding needed
5. ✅ Position tracking for audit/compliance
