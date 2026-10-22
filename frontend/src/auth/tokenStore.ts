/**
 * Token store and validity predicate for the Cognito Hosted UI auth flow.
 *
 * Policy (Requirement 10):
 * - Tokens are retained only for the authenticated browser session and are
 *   discarded when the session ends. We therefore persist to `sessionStorage`
 *   (cleared by the browser at session end) rather than `localStorage` (Req 10.4).
 * - Validity is decided solely by expiry versus the current time: a token is
 *   valid iff `now` is strictly before `expiresAt` (Req 10.5).
 * - `clearTokens()` must leave the store empty even if the underlying discard
 *   reports failure, so no subsequent request can carry a token (Req 10.6).
 *
 * This module holds no secrets. It only stores tokens the Hosted UI issued to
 * the browser. Consumed by the auth client (17.3), route guard (17.4), and API
 * client (18.1).
 */

/**
 * The tokens held for an authenticated browser session.
 *
 * `expiresAt` is **milliseconds since the Unix epoch** so it compares directly
 * with `Date.now()`. Convert a JWT `exp` claim (seconds) with `exp * 1000`
 * (see {@link expiresAtFromJwt}).
 */
export interface AuthTokens {
  idToken: string;
  accessToken: string;
  /** Absolute expiry as milliseconds since the Unix epoch (comparable to Date.now()). */
  expiresAt: number;
}

/** Single key under which the serialized token set is stored. */
const STORAGE_KEY = 'fleetmate.authTokens';

/**
 * Resolve the session-scoped storage, or `null` when it is unavailable
 * (e.g. SSR, or a browser that blocks storage access). Callers degrade to a
 * no-token state rather than throwing.
 */
function getStorage(): Storage | null {
  try {
    if (typeof globalThis === 'undefined') {
      return null;
    }
    const storage = (globalThis as { sessionStorage?: Storage }).sessionStorage;
    return storage ?? null;
  } catch {
    // Accessing sessionStorage can throw in some privacy modes.
    return null;
  }
}

/** Narrow unknown parsed JSON to a well-formed {@link AuthTokens}. */
function isAuthTokens(value: unknown): value is AuthTokens {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.idToken === 'string' &&
    typeof candidate.accessToken === 'string' &&
    typeof candidate.expiresAt === 'number' &&
    Number.isFinite(candidate.expiresAt)
  );
}

/**
 * Persist the token set for the current browser session, replacing any
 * previously stored set. Silently no-ops if storage is unavailable.
 */
export function setTokens(tokens: AuthTokens): void {
  const storage = getStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(tokens));
  } catch {
    // Quota or access failures must not crash the caller.
  }
}

/**
 * Read the stored token set, or `null` when nothing valid is stored. A
 * malformed or unparseable entry yields `null` (defensive against tampering).
 */
export function getTokens(): AuthTokens | null {
  const storage = getStorage();
  if (storage === null) {
    return null;
  }
  let raw: string | null;
  try {
    raw = storage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    return isAuthTokens(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Remove every stored token so nothing remains. This upholds the token-clearing
 * invariant (Req 10.6): even if the underlying `removeItem` reports failure, the
 * store is left empty — we also best-effort overwrite the key before removing so
 * a subsequent read can never return a usable token.
 */
export function clearTokens(): void {
  const storage = getStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // If removal fails, fall through to the overwrite below so that any
    // subsequent getTokens() cannot surface a usable token.
  }
  // Best-effort: ensure the key cannot be read back as valid tokens even if
  // the removeItem above failed. A second removeItem then tries once more.
  try {
    storage.setItem(STORAGE_KEY, '');
    storage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing more we can do; getTokens() will reject the empty/invalid value.
  }
}

/**
 * Pure validity predicate: tokens are valid iff they exist AND `now` is strictly
 * before `expiresAt`. Expiry at-or-after `now` is treated as invalid (Req 10.5).
 *
 * `now` is injected (not read from `Date.now()` internally) so the predicate is
 * deterministically testable.
 */
export function isTokenValid(tokens: AuthTokens | null, now: number): boolean {
  if (tokens === null) {
    return false;
  }
  return now < tokens.expiresAt;
}

/**
 * Defensively parse a JWT's `exp` claim and return the expiry as milliseconds
 * since the epoch, or `null` for any malformed input. Does not verify the
 * signature — the token is trusted because it was issued to this browser by the
 * Hosted UI; this only extracts the expiry for client-side scheduling.
 */
export function expiresAtFromJwt(jwt: string): number | null {
  if (typeof jwt !== 'string') {
    return null;
  }
  const parts = jwt.split('.');
  if (parts.length !== 3) {
    return null;
  }
  const payload = decodeBase64Url(parts[1] ?? '');
  if (payload === null) {
    return null;
  }
  let claims: unknown;
  try {
    claims = JSON.parse(payload);
  } catch {
    return null;
  }
  if (typeof claims !== 'object' || claims === null) {
    return null;
  }
  const exp = (claims as Record<string, unknown>).exp;
  if (typeof exp !== 'number' || !Number.isFinite(exp)) {
    return null;
  }
  // `exp` is seconds since the epoch; convert to ms to match AuthTokens.expiresAt.
  return exp * 1000;
}

/** Base64url-decode to a UTF-8 string, returning `null` on malformed input. */
function decodeBase64Url(segment: string): string | null {
  if (segment.length === 0) {
    return null;
  }
  // Restore standard base64 alphabet and padding.
  const base64 = segment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=');
  try {
    if (typeof atob === 'function') {
      const binary = atob(padded);
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      return new TextDecoder().decode(bytes);
    }
    // Node fallback (test environments without atob).
    const bufferCtor = (globalThis as { Buffer?: { from(s: string, enc: string): { toString(enc: string): string } } }).Buffer;
    if (bufferCtor) {
      return bufferCtor.from(padded, 'base64').toString('utf-8');
    }
    return null;
  } catch {
    return null;
  }
}
