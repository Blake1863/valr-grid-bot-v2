import { readFileSync } from 'fs';
import { resolve } from 'path';
import { ConfigSchema, type BotConfig } from './schema.js';
import { createLogger } from '../app/logger.js';

const log = createLogger('config');

export function loadConfig(configPath?: string): BotConfig {
  const path = configPath ?? resolve(process.cwd(), 'config.json');

  let raw: string;
  try {
    raw = readFileSync(path, 'utf-8');
  } catch (err) {
    throw new Error(`Cannot read config at ${path}: ${err}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new Error(`Invalid JSON in config at ${path}: ${err}`);
  }

  const result = ConfigSchema.safeParse(parsed);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  ${i.path.join('.')}: ${i.message}`)
      .join('\n');
    throw new Error(`Config validation failed:\n${issues}`);
  }

  const config = result.data;

  // Cross-field validation
  if (config.tpMode === 'fixed' && !config.tpFixedValue) {
    throw new Error('tpFixedValue is required when tpMode = "fixed"');
  }
  if (config.referencePriceSource === 'manual' && !config.manualReferencePrice) {
    throw new Error('manualReferencePrice is required when referencePriceSource = "manual"');
  }

  log.info({ pair: config.pair, mode: config.mode, dryRun: config.dryRun }, 'Config loaded');
  return config;
}
