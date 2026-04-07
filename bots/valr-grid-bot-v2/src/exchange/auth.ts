/**
 * VALR API Authentication — HMAC-SHA512
 *
 * From docs: https://api-docs.rooibos.dev/guides/authentication.md
 *
 * Signature = HMAC-SHA512(API_SECRET, timestamp + verb + path + body + subaccountId)
 * - timestamp: Unix ms as string
 * - verb: uppercase HTTP method
 * - path: exact API path WITH query string (e.g. /v1/positions/open?currencyPair=SOLUSDTPERP)
 * - body: exact JSON string sent in body, empty string if none
 * - subaccountId: sub-account ID string, empty string if not using subaccounts
 *
 * WebSocket account auth: HMAC-SHA512(API_SECRET, timestamp + "GET" + "/ws/account")
 * (no body, no subaccountId for WS signature)
 */

import crypto from 'crypto';

export interface AuthHeaders {
  'X-VALR-API-KEY': string;
  'X-VALR-SIGNATURE': string;
  'X-VALR-TIMESTAMP': string;
  'X-VALR-SUB-ACCOUNT-ID'?: string;
  [key: string]: string | undefined;
}

export function signRequest(
  apiSecret: string,
  timestamp: number,
  verb: string,
  path: string,
  body: string = '',
  subaccountId: string = ''
): string {
  const mac = crypto.createHmac('sha512', apiSecret);
  mac.update(timestamp.toString());
  mac.update(verb.toUpperCase());
  mac.update(path);
  mac.update(body);
  mac.update(subaccountId);
  return mac.digest('hex');
}

export function buildAuthHeaders(
  apiKey: string,
  apiSecret: string,
  verb: string,
  path: string,
  body: string = '',
  subaccountId: string = ''
): AuthHeaders {
  const timestamp = Date.now();
  const signature = signRequest(apiSecret, timestamp, verb, path, body, subaccountId);

  const headers: AuthHeaders = {
    'X-VALR-API-KEY': apiKey,
    'X-VALR-SIGNATURE': signature,
    'X-VALR-TIMESTAMP': timestamp.toString(),
  };

  if (subaccountId) {
    headers['X-VALR-SUB-ACCOUNT-ID'] = subaccountId;
  }

  return headers;
}

export function buildWsAuthHeaders(
  apiKey: string,
  apiSecret: string
): { 'X-VALR-API-KEY': string; 'X-VALR-SIGNATURE': string; 'X-VALR-TIMESTAMP': string } {
  const timestamp = Date.now();
  // WS: sign timestamp + "GET" + "/ws/account" — no body, no subaccountId
  const signature = signRequest(apiSecret, timestamp, 'GET', '/ws/account');
  return {
    'X-VALR-API-KEY': apiKey,
    'X-VALR-SIGNATURE': signature,
    'X-VALR-TIMESTAMP': timestamp.toString(),
  };
}
