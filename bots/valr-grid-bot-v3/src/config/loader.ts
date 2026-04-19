/**
 * Config Loader
 * 
 * Loads bot configuration from JSON file.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { validateConfig, type BotConfig } from './schema.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Load configuration from configs/bot-config.json.
 */
export function loadConfig(): BotConfig {
  const configPath = path.join(__dirname, '../../configs/bot-config.json');
  
  if (!fs.existsSync(configPath)) {
    throw new Error(`Config file not found: ${configPath}`);
  }

  const raw = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  return validateConfig(raw);
}

/**
 * Load configuration from arbitrary path.
 */
export function loadConfigFromPath(configPath: string): BotConfig {
  if (!fs.existsSync(configPath)) {
    throw new Error(`Config file not found: ${configPath}`);
  }

  const raw = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  return validateConfig(raw);
}
