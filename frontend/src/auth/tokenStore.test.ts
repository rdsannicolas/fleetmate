import { describe, it, expect, beforeEach } from 'vitest';
import {
  type AuthTokens,
  setTokens,
  getTokens,
  clearTokens,
  isTokenValid,
  expiresAtFromJwt,
} from './tokenStore';

// Sanity tests for the token store and validity predicate (task 17.2).
// Exhaustive property tests (17.8 token validity, 18.4 clearing invariant) are
// separate optional tasks and are not implemented here.

const sampleTokens: AuthTokens = {
  idToken: 'id-token-abc',
  accessToken: 'access-token-xyz',
  expiresAt: 1_000_000,
};

describe('tokenStore', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('round-trips tokens through session storage', () => {
    expect(getTokens()).toBeNull();
    setTokens(sampleTokens);
    expect(getTokens()).toEqual(sampleTokens);
  });

  it('clearTokens leaves nothing retrievable', () => {
    setTokens(sampleTokens);
    clearTokens();
    expect(getTokens()).toBeNull();
    // Nothing usable remains under the hood either.
    expect(sessionStorage.getItem('fleetmate.authTokens')).toBeNull();
  });

  it('returns null for a malformed stored entry', () => {
    sessionStorage.setItem('fleetmate.authTokens', 'not-json');
    expect(getTokens()).toBeNull();
  });
});

describe('isTokenValid', () => {
  it('is invalid when no tokens are present', () => {
    expect(isTokenValid(null, 0)).toBe(false);
  });

  it('is valid strictly before expiry', () => {
    expect(isTokenValid(sampleTokens, sampleTokens.expiresAt - 1)).toBe(true);
  });

  it('is invalid at expiry (at-or-after is invalid)', () => {
    expect(isTokenValid(sampleTokens, sampleTokens.expiresAt)).toBe(false);
  });

  it('is invalid after expiry', () => {
    expect(isTokenValid(sampleTokens, sampleTokens.expiresAt + 1)).toBe(false);
  });
});

describe('expiresAtFromJwt', () => {
  function makeJwt(payload: Record<string, unknown>): string {
    const toBase64Url = (obj: Record<string, unknown>): string =>
      btoa(JSON.stringify(obj))
        .replace(/\+/g, '-')
        .replace(/\//g, '_')
        .replace(/=+$/, '');
    return `${toBase64Url({ alg: 'none' })}.${toBase64Url(payload)}.sig`;
  }

  it('converts a numeric exp (seconds) to ms since epoch', () => {
    expect(expiresAtFromJwt(makeJwt({ exp: 1700 }))).toBe(1_700_000);
  });

  it('returns null for a token without three segments', () => {
    expect(expiresAtFromJwt('a.b')).toBeNull();
  });

  it('returns null when exp is missing or non-numeric', () => {
    expect(expiresAtFromJwt(makeJwt({ sub: 'user' }))).toBeNull();
    expect(expiresAtFromJwt(makeJwt({ exp: 'soon' }))).toBeNull();
  });
});
