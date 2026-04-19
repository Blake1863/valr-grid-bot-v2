import { z } from 'zod';
import Decimal from 'decimal.js';

export const BotConfigSchema = z.object({
  // === Identity ===
  pair: z.string(),                    // e.g., "SOLUSDTPERP"
  subaccountId: z.string().optional(), // VALR subaccount ID
  
  // === Grid Range (REQUIRED) ===
  lowerBound: z.string(),              // Lower price bound (string for precision)
  upperBound: z.string(),              // Upper price bound
  
  // === Grid Construction ===
  gridCount: z.number().int().positive(),  // Number of intervals (NOT levels)
  gridMode: z.enum(['arithmetic', 'geometric']).default('arithmetic'),
  
  // === Trading Mode ===
  mode: z.enum(['neutral', 'long', 'short']).default('neutral'),
  referencePrice: z.string().optional(), // Base price for neutral mode (defaults to mid-range or current)
  
  // === Capital & Leverage ===
  leverage: z.number().positive(),           // Target leverage
  capitalAllocationPercent: z.number().min(1).max(100).default(90),
  dynamicSizing: z.boolean().default(false), // Auto-adjust quantity with balance
  quantityPerLevel: z.string().optional(),   // Fixed quantity per level (if not dynamic)
  
  // === Order Behavior ===
  postOnly: z.boolean().default(true),
  allowMargin: z.boolean().default(false),
  
  // === Risk Management ===
  stopLossMode: z.enum(['percent', 'fixed']).default('percent'),
  stopLossValue: z.string(),             // SL distance (percent or fixed)
  tpMode: z.enum(['disabled', 'fixed', 'percent']).default('disabled'),
  tpFixedValue: z.string().optional(),
  tpPercentValue: z.string().optional(),
  
  // === Price Source ===
  triggerType: z.enum(['MARK_PRICE', 'LAST_PRICE']).default('MARK_PRICE'),
  referencePriceSource: z.enum(['mark_price', 'last_price', 'index_price']).default('mark_price'),
  
  // === Timing & Health ===
  staleDataTimeoutMs: z.number().positive().default(30000),
  healthCheckIntervalMs: z.number().positive().default(5000),
  reconciliationIntervalSecs: z.number().positive().default(60),
  cooldownAfterStopSecs: z.number().nonnegative().default(300),
  
  // === Safety ===
  dryRun: z.boolean().default(false),
  maxActiveGridOrders: z.number().positive().default(30),
  
  // === VALR-specific ===
  wsStaleTimeoutSecs: z.number().positive().default(30),
});

export type BotConfig = z.infer<typeof BotConfigSchema>;

/**
 * Validate and normalize config.
 */
export function validateConfig(raw: unknown): BotConfig {
  const config = BotConfigSchema.parse(raw);
  
  // Validate bounds
  const lower = new Decimal(config.lowerBound);
  const upper = new Decimal(config.upperBound);
  if (lower.greaterThanOrEqualTo(upper)) {
    throw new Error(`lowerBound (${config.lowerBound}) must be less than upperBound (${config.upperBound})`);
  }
  
  return config;
}
