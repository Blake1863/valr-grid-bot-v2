import { z } from 'zod';

export const ConfigSchema = z.object({
  pair: z.string().min(1),
  subaccountId: z.string().optional().default(''),

  mode: z.enum(['long_only', 'short_only', 'neutral']),

  levels: z.number().int().min(1).max(100),
  gridRangePercent: z.number().min(0.1).max(100), // Total range % (e.g., 10 = ±5% from ref)
  spacingMode: z.enum(['absolute', 'percent']).default('percent'),
  spacingValue: z.string().optional(), // Computed from gridRangePercent if not provided

  quantityPerLevel: z.string().min(1), // Decimal string — used when dynamicSizing is false

  dynamicSizing: z.boolean().default(false), // Calculate qty from balance when true
  targetLeverage: z.number().int().min(1).max(100).optional(), // Effective leverage for dynamic sizing
  capitalAllocationPercent: z.number().min(1).max(100).default(90), // % of balance to allocate

  stopLossMode: z.enum(['absolute', 'percent']),
  stopLossValue: z.string().min(1), // Decimal string

  tpMode: z.enum(['one_level', 'fixed', 'disabled']),
  tpFixedValue: z.string().optional(), // Required when tpMode = 'fixed'

  triggerType: z.enum(['MARK_PRICE', 'LAST_TRADED']),
  referencePriceSource: z.enum(['mark_price', 'mid_price', 'last_traded', 'manual']),
  manualReferencePrice: z.string().optional(), // Required when referencePriceSource = 'manual'

  leverage: z.number().int().min(1).max(100).optional(),
  postOnly: z.boolean().default(true),
  allowMargin: z.boolean().default(true),

  cooldownAfterStopSecs: z.number().int().min(0).default(300),
  dryRun: z.boolean().default(false),

  reconcileIntervalSecs: z.number().int().min(10).default(60),
  maxActiveGridOrders: z.number().int().min(1).max(200).default(10),
  wsStaleTimeoutSecs: z.number().int().min(10).default(30),
});

export type BotConfig = z.infer<typeof ConfigSchema>;
