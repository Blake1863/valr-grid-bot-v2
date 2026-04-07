# Qwen Model Cost Comparison — Cache vs No Cache

## Pricing (per 1M tokens, <256k tier)

| Model | Input | Output | Cache Create | Cache Hit | Savings |
|-------|-------|--------|--------------|-----------|---------|
| **qwen3.5-plus** | $0.40 | $2.40 | $0.50 | **$0.04** | **90%** |
| **qwen3-max** | $1.20 | $6.00 | $1.50 | **$0.12** | **90%** |

**Note:** qwen3.5-plus = explicit cache only (manual), qwen3-max = implicit cache (automatic)

---

## Cost Scenarios

### Scenario A: Single Request (No Cache)

| Model | Context | Input | Output | **Total** |
|-------|---------|-------|--------|-----------|
| qwen3.5-plus | 50k | $0.02 | $0.024 | **$0.044** |
| qwen3-max | 50k | $0.06 | $0.06 | **$0.12** |

**Winner:** qwen3.5-plus (**3x cheaper** for one-off requests)

---

### Scenario B: 10 Requests, Same Context (50k context, 1k output each)

#### qwen3.5-plus (Explicit Cache)
| Request | Input | Output | Cache | **Total** |
|---------|-------|--------|-------|-----------|
| 1st | $0.02 | $0.024 | $0.025 (create) | $0.069 |
| 2nd-10th | $0.002 × 9 | $0.024 × 9 | $0 | $0.234 |
| **Total** | | | | **$0.303** |

#### qwen3-max (Implicit Cache — Automatic)
| Request | Input | Output | Cache | **Total** |
|---------|-------|--------|-------|-----------|
| 1st | $0.06 | $0.06 | $0.075 (create) | $0.195 |
| 2nd-10th | $0.006 × 9 | $0.06 × 9 | $0 | $0.594 |
| **Total** | | | | **$0.789** |

**Winner:** qwen3.5-plus (**2.6x cheaper** with explicit cache)

---

### Scenario C: 100 Requests, Same Context (50k context, 1k output each)

#### qwen3.5-plus (Explicit Cache)
| Component | Cost |
|-----------|------|
| 1st request (cache create) | $0.069 |
| 99 requests (cache hits) | $0.020 × 99 = $1.98 |
| Output (100 × 1k) | $0.024 × 100 = $2.40 |
| **Total** | **$4.449** |

#### qwen3-max (Implicit Cache)
| Component | Cost |
|-----------|------|
| 1st request (cache create) | $0.195 |
| 99 requests (cache hits) | $0.060 × 99 = $5.94 |
| Output (100 × 1k) | $0.06 × 100 = $6.00 |
| **Total** | **$18.135** |

**Winner:** qwen3.5-plus (**4x cheaper** at scale with cache)

---

### Scenario D: Every Request Different (No Cache Benefit)

| Model | 100 Requests (50k ctx, 1k out each) | **Total** |
|-------|-------------------------------------|-----------|
| qwen3.5-plus | ($0.02 + $0.024) × 100 | **$4.40** |
| qwen3-max | ($0.06 + $0.06) × 100 | **$12.00** |

**Winner:** qwen3.5-plus (**2.7x cheaper** without cache)

---

## Break-Even Analysis

**qwen3-max is NEVER cheaper** — qwen3.5-plus dominates in all scenarios:

| Scenario | qwen3.5-plus | qwen3-max | Difference |
|----------|--------------|-----------|------------|
| Single request | $0.044 | $0.12 | **63% savings** |
| 10 requests (cached) | $0.303 | $0.789 | **62% savings** |
| 100 requests (cached) | $4.45 | $18.14 | **75% savings** |
| 100 requests (no cache) | $4.40 | $12.00 | **63% savings** |

---

## Key Insights

1. **qwen3.5-plus is always cheaper** — Base prices are 3x lower than qwen3-max
2. **Cache benefit is proportional** — 90% off sounds great, but 90% of a small number (qwen3.5-plus) vs 90% of a large number (qwen3-max) still favors the cheaper base model
3. **Explicit vs Implicit doesn't matter for cost** — Both give 90% cache hit discount
4. **Only reason to use qwen3-max:** If it has better quality/reasoning for complex tasks

---

## Recommendation

**Stick with qwen3.5-plus** — it's 2.5-4x cheaper in all scenarios.

**To maximize savings:**
1. Implement explicit caching in OpenClaw (cache workspace files: MEMORY.md, SOUL.md, TOOLS.md, USER.md, AGENTS.md)
2. These files are ~10-20k tokens sent with every request
3. With 90% cache hit discount: **~50-70% total cost reduction**

**Estimated monthly savings:**
- If you spend $10/month now: **$5-7/month saved**
- If you spend $100/month: **$50-70/month saved**

---

## Next Steps

1. **Verify OpenClaw explicit cache support** — Check if cache headers can be sent
2. **If not supported:** Either wait for update or manually reduce context
3. **Alternative:** Use qwen3.5-plus without cache (still 3x cheaper than qwen3-max)
