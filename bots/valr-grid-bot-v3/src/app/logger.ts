/**
 * Logger — Pino-based structured logging
 */

import { createWriteStream, mkdirSync, existsSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import pino from 'pino';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Ensure logs directory exists
const logsDir = path.join(__dirname, '../../logs');
if (!existsSync(logsDir)) {
  mkdirSync(logsDir, { recursive: true });
}

// Create streams
const stdoutStream = pino.default.destination({ fd: 1 });
const fileStream = pino.default.destination(path.join(logsDir, 'bot.log'));

// Create logger
const logger = pino.default(
  {
    level: process.env.LOG_LEVEL || 'info',
    timestamp: pino.default.stdTimeFunctions.isoTime,
  },
  pino.default.multistream([
    { level: 'info', stream: stdoutStream },
    { level: 'info', stream: fileStream },
  ])
);

export function createLogger(module: string) {
  return logger.child({ module });
}
