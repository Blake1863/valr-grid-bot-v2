/**
 * Auth tests — verify against the known test vectors from VALR docs
 * https://api-docs.rooibos.dev/guides/authentication.md
 */

import { describe, it, expect } from 'vitest';
import { signRequest, buildAuthHeaders, buildWsAuthHeaders } from '../src/exchange/auth.js';

describe('signRequest', () => {
  // Test vectors from VALR docs
  it('produces correct GET signature (doc test vector)', () => {
    const secret = '4961b74efac86b25cce8fbe4c9811c4c7a787b7a5996660afcc2e287ad864363';
    const timestamp = 1558014486185;
    const result = signRequest(secret, timestamp, 'GET', '/v1/account/balances', '', '');
    expect(result).toBe(
      '9d52c181ed69460b49307b7891f04658e938b21181173844b5018b2fe783a6d4c62b8e67a03de4d099e7437ebfabe12c56233b73c6a0cc0f7ae87e05f6289928'
    );
  });

  it('produces correct POST signature (doc test vector)', () => {
    const secret = '4961b74efac86b25cce8fbe4c9811c4c7a787b7a5996660afcc2e287ad864363';
    const timestamp = 1558017528946;
    const body = '{"customerOrderId":"ORDER-000001","pair":"BTCUSDC","side":"BUY","quoteAmount":"80000"}';
    const result = signRequest(secret, timestamp, 'POST', '/v1/orders/market', body, '');
    expect(result).toBe(
      '09f536e3dfdad58443f16010a97a0a21ad27486b7b8d6d4103170d885410ed77f037f1fa628474190d4f5c08ca12c1acc850901f1c2e75c6d906ec3b32b008d0'
    );
  });

  it('includes subaccountId in signature when provided', () => {
    const secret = 'test-secret';
    const ts = 1234567890000;
    const withSub = signRequest(secret, ts, 'GET', '/v1/positions/open', '', 'subaccount-123');
    const withoutSub = signRequest(secret, ts, 'GET', '/v1/positions/open', '', '');
    expect(withSub).not.toBe(withoutSub);
  });

  it('verb is uppercased', () => {
    const secret = 'test-secret';
    const ts = 1234567890000;
    const lower = signRequest(secret, ts, 'get', '/v1/test');
    const upper = signRequest(secret, ts, 'GET', '/v1/test');
    expect(lower).toBe(upper);
  });
});

describe('buildAuthHeaders', () => {
  it('includes all required headers', () => {
    const headers = buildAuthHeaders('key', 'secret', 'GET', '/v1/test');
    expect(headers['X-VALR-API-KEY']).toBe('key');
    expect(headers['X-VALR-SIGNATURE']).toBeTruthy();
    expect(headers['X-VALR-TIMESTAMP']).toBeTruthy();
    expect(headers['X-VALR-SUB-ACCOUNT-ID']).toBeUndefined();
  });

  it('includes sub-account header when subaccountId provided', () => {
    const headers = buildAuthHeaders('key', 'secret', 'GET', '/v1/test', '', 'subaccount-123');
    expect(headers['X-VALR-SUB-ACCOUNT-ID']).toBe('subaccount-123');
  });
});

describe('buildWsAuthHeaders', () => {
  it('signs /ws/account path with GET verb', () => {
    const headers = buildWsAuthHeaders('key', 'secret');
    expect(headers['X-VALR-API-KEY']).toBe('key');
    expect(headers['X-VALR-SIGNATURE']).toBeTruthy();
    expect(headers['X-VALR-TIMESTAMP']).toBeTruthy();
    // Verify the signature is what we'd expect for GET /ws/account
    const ts = parseInt(headers['X-VALR-TIMESTAMP'], 10);
    const expected = signRequest('secret', ts, 'GET', '/ws/account', '', '');
    expect(headers['X-VALR-SIGNATURE']).toBe(expected);
  });
});
