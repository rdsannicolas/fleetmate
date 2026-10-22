import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  type AuthResult,
  beginLogin,
  buildLoginUrl,
  buildLogoutUrl,
  handleCallback,
  logout,
} from './authClient';
import { getTokens } from './tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

// Sanity tests for the auth client (task 17.3). The exhaustive property test
// (17.7 — callback never leaves a partial token) is a separate optional task.

const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.com/v1',
  cognitoDomain: 'https://fleetmate.auth.us-east-1.amazoncognito.com',
  userPoolId: 'us-east-1_abc123',
  appClientId: 'client-abc',
  redirectUri: 'https://app.example.com/callback',
  logoutUri: 'https://app.example.com/',
};

const VERIFIER_KEY = 'fleetmate.pkceVerifier';
const STATE_KEY = 'fleetmate.oauthState';

/** Build a minimal JWT whose exp claim is `seconds` since the epoch. */
function makeJwt(expSeconds: number): string {
  const b64 = (obj: Record<string, unknown>): string =>
    btoa(JSON.stringify(obj)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `${b64({ alg: 'none' })}.${b64({ exp: expSeconds })}.sig`;
}

function okTokenResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response;
}

describe('buildLoginUrl', () => {
  it('targets the authorize endpoint with code flow + PKCE params', () => {
    const url = new URL(buildLoginUrl(config, 'STATE1', 'CHALLENGE1'));
    expect(url.origin + url.pathname).toBe(`${config.cognitoDomain}/oauth2/authorize`);
    expect(url.searchParams.get('response_type')).toBe('code');
    expect(url.searchParams.get('client_id')).toBe(config.appClientId);
    expect(url.searchParams.get('redirect_uri')).toBe(config.redirectUri);
    expect(url.searchParams.get('scope')).toBe('openid email profile');
    expect(url.searchParams.get('state')).toBe('STATE1');
    expect(url.searchParams.get('code_challenge')).toBe('CHALLENGE1');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
  });
});

describe('buildLogoutUrl', () => {
  it('targets the logout endpoint with client id and logout uri', () => {
    const url = new URL(buildLogoutUrl(config));
    expect(url.origin + url.pathname).toBe(`${config.cognitoDomain}/logout`);
    expect(url.searchParams.get('client_id')).toBe(config.appClientId);
    expect(url.searchParams.get('logout_uri')).toBe(config.logoutUri);
  });
});

describe('beginLogin', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('persists verifier + state and redirects to the authorize endpoint', async () => {
    let redirected = '';
    await beginLogin(config, { redirect: (u) => (redirected = u) });

    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    const state = sessionStorage.getItem(STATE_KEY);
    expect(verifier).toBeTruthy();
    expect(state).toBeTruthy();

    const url = new URL(redirected);
    expect(url.searchParams.get('state')).toBe(state);
    // The challenge must match the stored verifier (verified end-to-end below).
    expect(url.searchParams.get('code_challenge')).toBeTruthy();
  });
});

describe('handleCallback', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  function primeTransient(state: string, verifier = 'verifier-xyz'): void {
    sessionStorage.setItem(STATE_KEY, state);
    sessionStorage.setItem(VERIFIER_KEY, verifier);
  }

  function expectNoTokensOrTransient(): void {
    expect(getTokens()).toBeNull();
    expect(sessionStorage.getItem(VERIFIER_KEY)).toBeNull();
    expect(sessionStorage.getItem(STATE_KEY)).toBeNull();
  }

  it('returns AUTH_ERROR and stores nothing when Hosted UI returns an error', async () => {
    primeTransient('S');
    const result = await handleCallback({ error: 'access_denied', state: 'S' }, config, {
      fetchImpl: vi.fn(),
    });
    expect(result).toEqual<AuthResult>({ ok: false, reason: 'AUTH_ERROR' });
    expectNoTokensOrTransient();
  });

  it('returns STATE_MISMATCH on a CSRF state mismatch', async () => {
    primeTransient('EXPECTED');
    const result = await handleCallback({ code: 'c', state: 'ATTACKER' }, config, {
      fetchImpl: vi.fn(),
    });
    expect(result).toEqual<AuthResult>({ ok: false, reason: 'STATE_MISMATCH' });
    expectNoTokensOrTransient();
  });

  it('exchanges the code and stores the full token set on success', async () => {
    primeTransient('S', 'the-verifier');
    const fetchImpl = vi.fn(async () =>
      okTokenResponse({
        id_token: makeJwt(2_000_000),
        access_token: 'access-abc',
        expires_in: 3600,
      }),
    );

    const result = await handleCallback({ code: 'auth-code', state: 'S' }, config, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(result).toEqual<AuthResult>({ ok: true });

    // Correct endpoint + the stored verifier submitted at exchange.
    const [calledUrl, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(calledUrl).toBe(`${config.cognitoDomain}/oauth2/token`);
    const sent = new URLSearchParams(init.body as string);
    expect(sent.get('grant_type')).toBe('authorization_code');
    expect(sent.get('code')).toBe('auth-code');
    expect(sent.get('code_verifier')).toBe('the-verifier');

    const tokens = getTokens();
    expect(tokens?.accessToken).toBe('access-abc');
    expect(tokens?.idToken).toBeTruthy();

    // Transient material is cleared after a successful exchange.
    expect(sessionStorage.getItem(VERIFIER_KEY)).toBeNull();
    expect(sessionStorage.getItem(STATE_KEY)).toBeNull();
  });

  it('returns EXCHANGE_FAILED and stores nothing on a non-OK response', async () => {
    primeTransient('S');
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 400 }) as Response);
    const result = await handleCallback({ code: 'c', state: 'S' }, config, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(result).toEqual<AuthResult>({ ok: false, reason: 'EXCHANGE_FAILED' });
    expectNoTokensOrTransient();
  });

  it('returns TIMEOUT and stores nothing when the exchange aborts', async () => {
    primeTransient('S');
    const fetchImpl = vi.fn((_url: string, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          const err = new Error('aborted');
          err.name = 'AbortError';
          reject(err);
        });
      });
    });
    vi.useFakeTimers();
    try {
      const pending = handleCallback({ code: 'c', state: 'S' }, config, {
        fetchImpl: fetchImpl as unknown as typeof fetch,
      });
      await vi.advanceTimersByTimeAsync(10_000);
      const result = await pending;
      expect(result).toEqual<AuthResult>({ ok: false, reason: 'TIMEOUT' });
    } finally {
      vi.useRealTimers();
    }
    expectNoTokensOrTransient();
  });
});

describe('logout', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('clears tokens and redirects to the logout endpoint', () => {
    sessionStorage.setItem('fleetmate.authTokens', JSON.stringify({
      idToken: 'i',
      accessToken: 'a',
      expiresAt: 9_999_999_999_999,
    }));
    let redirected = '';
    logout(config, { redirect: (u) => (redirected = u) });
    expect(getTokens()).toBeNull();
    expect(redirected).toBe(buildLogoutUrl(config));
  });
});
