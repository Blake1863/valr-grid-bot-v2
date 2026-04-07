import pino from 'pino';

const isPretty = process.env.LOG_PRETTY === '1';

const rootLogger = pino({
  level: process.env.LOG_LEVEL ?? 'info',
  ...(isPretty
    ? { transport: { target: 'pino-pretty', options: { colorize: true } } }
    : {}),
});

export function createLogger(name: string) {
  return rootLogger.child({ module: name });
}

export default rootLogger;
