/**
 * Auth client: drives the Cognito Hosted UI OAuth 2.0 authorization-code flow
 * with PKCE (RFC 7636) for the FleetMate SPA.
 *
 * Responsibilities:
 * - `beginLogin()` — mint a PKCE verifier + CSRF state, persist them transiently
 *   (sessionStorage), and redirect the browser to the Hosted UI authorize
 *   endpoint using the code flow + PKCE (Req 6.1).
 * - `handleCallback()` — the callback handler. Validates the Hosted UI result,
 *   exchanges the authorization code for tokens within a 10-second budget
 *   (Req 6.2), and returns a typed `AuthResult` instead of throwing across the
 *   boundary so the callback page can render the right message (Req 6.4).
 *   Fail-closed: every failure path leaves no stored token (Req 6.7).
 * - `logout()` — discard tokens and redirect to the Hosted UI logout endpoint
 *   (Req 10.2).
 *
 * The app client is a PUBLIC OAuth client with NO client secret; nothing in this
 * module is secret. The HTTP exchange and the browser redirect are injectable so
 * tests neither hit the network nor navigate.
 */

import { deriveCodeChallenge, generateCodeVerifier, generateState } from './pkce';
import type { RuntimeConfig } from '../config/runtimeConfig';
import {
  type AuthTokens,
  clearTokens,
  expiresAtFromJwt,
  getTokens,
  isTokenValid,
  setTokens,
} from './tokenStore';

/** sessionStorage keys for the transient PKCE verifier and CSRF state. */
const VERIFIER_KEY = 'fleetmate.pkceVerifier';
const STATE_KEY = 'fleetmate.oauthState';

/** OAuth scopes requested from the Hosted UI. */
const SCOPES = 'openid email profile';

/** Hard budget for the token exchange request (Req 6.2 / 6.7). */
const EXCHANGE_TIMEOUT_MS = 10_000;

/** Why a login attempt did not complete, so the callback page can react. */
export type AuthFailureReason =
  | 'AUTH_ERROR' // Hosted UI returned ?error (Req 6.4)
  | 'STATE_MISMATCH' // returned state did not match the stored state (CSRF)
  | 'MISSING_CODE' // no error, but no authorization code either
  | 'EXCHANGE_FAILED' // token endpoint rejected / returned a bad response (Req 6.7)
  | 'TIMEOUT'; // exchange exceeded the 10-second budget (Req 6.2 / 6.7)

/**
 * Typed outcome of {@link handleCallback}. Success means a full token set is now
 * stored; any failure means no token remains and the page should offer a retry.
 */
export type AuthResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: AuthFailureReason };

/** Parameters parsed from the Hosted UI callback redirect query string. */
export interface CallbackParams {
  code?: string;
  state?: string;
  error?: string;
}

/**
 * Seams for testing: a `fetch` implementation and a browser-redirect function.
 * Both default to the real browser primitives in production.
 */
export interface AuthClientDeps {
  /** Fetch used for the token exchange. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  /** Navigate the browser to `url`. Defaults to assigning `window.location`. */
  redirect?: (url: string) => void;
}

/** Shape of the subset of the token endpoint JSON response we consume. */
interface TokenResponse {
  id_token?: unknown;
  access_token?: unknown;
  expires_in?: unknown;
}

function defaultRedirect(url: string): void {
  // Assigning `href` performs a full navigation to the Hosted UI.
  window.location.assign(url);
}

function resolveDeps(deps?: AuthClientDeps): Required<AuthClientDeps> {
  return {
    fetchImpl: deps?.fetchImpl ?? fetch.bind(globalThis),
    redirect: deps?.redirect ?? defaultRedirect,
  };
}

/** Best-effort session-scoped storage, or `null` when unavailable. */
function getStorage(): Storage | null {
  try {
    const storage = (globalThis as { sessionStorage?: Storage }).sessionStorage;
    return storage ?? null;
  } catch {
    return null;
  }
}

/** Remove the transient PKCE verifier and CSRF state. */
function clearTransient(): void {
  const storage = getStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.removeItem(VERIFIER_KEY);
    storage.removeItem(STATE_KEY);
  } catch {
    // Nothing more to do; values become unreadable regardless.
  }
}

/**
 * Build the Hosted UI authorize URL for the authorization-code flow with PKCE.
 * Exposed for reuse/testing; `beginLogin` calls it internally.
 */
export function buildLoginUrl(
  config: RuntimeConfig,
  state: string,
  challenge: string,
): string {
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.appClientId,
    redirect_uri: config.redirectUri,
    scope: SCOPES,
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  return `${config.cognitoDomain}/oauth2/authorize?${params.toString()}`;
}

/** Build the Hosted UI logout URL (Req 10.2). */
export function buildLogoutUrl(config: RuntimeConfig): string {
  const params = new URLSearchParams({
    client_id: config.appClientId,
    logout_uri: config.logoutUri,
  });
  return `${config.cognitoDomain}/logout?${params.toString()}`;
}

/**
 * Start the login flow (Req 6.1): generate a PKCE verifier + CSRF state, persist
 * them transiently so the callback can retrieve them, then redirect to the
 * Hosted UI authorize endpoint.
 */
export async function beginLogin(
  config: RuntimeConfig,
  deps?: AuthClientDeps,
): Promise<void> {
  const { redirect } = resolveDeps(deps);

  const verifier = generateCodeVerifier();
  const state = generateState();
  const challenge = await deriveCodeChallenge(verifier);

  const storage = getStorage();
  if (storage !== null) {
    try {
      storage.setItem(VERIFIER_KEY, verifier);
      storage.setItem(STATE_KEY, state);
    } catch {
      // If we cannot persist the verifier/state, the callback cannot validate
      // state or exchange the code; proceeding would always fail closed.
    }
  }

  redirect(buildLoginUrl(config, state, challenge));
}

/**
 * Handle the Hosted UI callback. Returns a typed {@link AuthResult}; never throws
 * across the boundary. Fail-closed: any non-success path clears both the
 * transient PKCE material and any token so nothing partial remains (Req 6.7).
 */
export async function handleCallback(
  params: CallbackParams,
  config: RuntimeConfig,
  deps?: AuthClientDeps,
): Promise<AuthResult> {
  const { fetchImpl } = resolveDeps(deps);

  // Idempotency guard: an OAuth authorization code is single-use, and this
  // handler can be invoked more than once for the same redirect (a React
  // re-render, a hashchange, or Strict Mode double-invoke). If a valid token
  // set is already stored, the exchange already succeeded on a prior call --
  // return success WITHOUT re-validating state or re-exchanging the (now
  // consumed) code. Without this guard a replayed call would find the transient
  // PKCE material already cleared, treat it as a CSRF STATE_MISMATCH, and clear
  // the good tokens the first call just stored -- bouncing the Customer back to
  // the landing page despite a successful login.
  if (isTokenValid(getTokens(), Date.now())) {
    clearTransient();
    return { ok: true };
  }

  // Hosted UI reported an authentication error (Req 6.4): no token stored.
  if (params.error !== undefined && params.error !== '') {
    clearTransient();
    clearTokens();
    return { ok: false, reason: 'AUTH_ERROR' };
  }

  const storage = getStorage();
  const storedState = storage?.getItem(STATE_KEY) ?? null;
  const storedVerifier = storage?.getItem(VERIFIER_KEY) ?? null;

  // CSRF: the returned state must match the state we persisted at beginLogin.
  // Only the transient PKCE material is cleared here -- never an existing token
  // set (the idempotency guard above already handled the already-authenticated
  // case, so reaching here with no valid token means there is nothing to wipe).
  if (
    storedState === null ||
    params.state === undefined ||
    params.state !== storedState
  ) {
    clearTransient();
    return { ok: false, reason: 'STATE_MISMATCH' };
  }

  // We must have both an authorization code and the matching PKCE verifier.
  if (
    params.code === undefined ||
    params.code === '' ||
    storedVerifier === null
  ) {
    clearTransient();
    return { ok: false, reason: 'MISSING_CODE' };
  }

  // Exchange the code for tokens within a 10-second budget (Req 6.2).
  let timedOut = false;
  const controller = new AbortController();
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, EXCHANGE_TIMEOUT_MS);

  try {
    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: config.appClientId,
      code: params.code,
      redirect_uri: config.redirectUri,
      code_verifier: storedVerifier,
    });

    const response = await fetchImpl(`${config.cognitoDomain}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
      signal: controller.signal,
    });

    if (!response.ok) {
      clearTransient();
      clearTokens();
      return { ok: false, reason: 'EXCHANGE_FAILED' };
    }

    const json = (await response.json()) as TokenResponse;
    const tokens = toAuthTokens(json);
    if (tokens === null) {
      // Malformed token response — treat as a failed exchange, store nothing.
      clearTransient();
      clearTokens();
      return { ok: false, reason: 'EXCHANGE_FAILED' };
    }

    // Success: persist the full token set, then drop the transient material.
    setTokens(tokens);
    clearTransient();
    return { ok: true };
  } catch (error) {
    // AbortError from our own timeout vs. any other network/parse failure.
    clearTransient();
    clearTokens();
    if (timedOut || (error instanceof Error && error.name === 'AbortError')) {
      return { ok: false, reason: 'TIMEOUT' };
    }
    return { ok: false, reason: 'EXCHANGE_FAILED' };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Convert a token endpoint response into {@link AuthTokens}, computing `expiresAt`
 * from `expires_in` (seconds from now) when present, otherwise from the ID
 * token's `exp` claim. Returns `null` for a malformed/incomplete response.
 */
function toAuthTokens(json: TokenResponse, now: number = Date.now()): AuthTokens | null {
  const idToken = typeof json.id_token === 'string' ? json.id_token : null;
  const accessToken = typeof json.access_token === 'string' ? json.access_token : null;
  if (idToken === null || accessToken === null) {
    return null;
  }

  let expiresAt: number | null = null;
  if (typeof json.expires_in === 'number' && Number.isFinite(json.expires_in)) {
    expiresAt = now + json.expires_in * 1000;
  } else {
    expiresAt = expiresAtFromJwt(idToken);
  }
  if (expiresAt === null || !Number.isFinite(expiresAt)) {
    return null;
  }

  return { idToken, accessToken, expiresAt };
}

/**
 * Log out (Req 10.2): discard stored tokens, then redirect to the Hosted UI
 * logout endpoint. Tokens are cleared before navigation so none remain even if
 * the redirect is intercepted.
 */
export function logout(config: RuntimeConfig, deps?: AuthClientDeps): void {
  const { redirect } = resolveDeps(deps);
  clearTokens();
  clearTransient();
  redirect(buildLogoutUrl(config));
}

/** Convenience: the currently stored tokens, or `null`. Delegates to the store. */
export function getCurrentTokens(): AuthTokens | null {
  return getTokens();
}

/** Convenience: whether a valid (unexpired) token set is held at `now`. */
export function isAuthenticated(now: number = Date.now()): boolean {
  return isTokenValid(getTokens(), now);
}
