/**
 * PKCE (RFC 7636) helpers for the OAuth 2.0 authorization-code flow against the
 * Cognito Hosted UI.
 *
 * Responsibilities:
 * - Generate a high-entropy `code_verifier` and the opaque `state` value using
 *   the browser Web Crypto RNG (`crypto.getRandomValues`).
 * - Derive the `code_challenge` as `base64url(SHA-256(verifier))` with
 *   `code_challenge_method = S256` (`crypto.subtle.digest`).
 *
 * These helpers are intentionally free of any OAuth/URL/storage concerns — the
 * auth client (task 17.3) consumes them. Derivation is deterministic for a
 * given verifier; generation draws from the crypto RNG.
 */

/** Unreserved character set permitted in a PKCE code verifier (RFC 7636 §4.1). */
const UNRESERVED_CHARS =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';

/** Default verifier length. RFC 7636 allows 43–128 characters. */
const DEFAULT_VERIFIER_LENGTH = 64;

/** Byte length of the random state value before base64url encoding. */
const STATE_BYTE_LENGTH = 32;

/**
 * Encode bytes as a base64url string (URL-safe alphabet, no padding) per
 * RFC 4648 §5.
 */
export function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Generate a high-entropy, cryptographically-random PKCE `code_verifier`.
 *
 * The result is `length` characters drawn uniformly from the unreserved set
 * `[A-Za-z0-9-._~]`. `length` must be within the RFC 7636 bound of 43–128.
 */
export function generateCodeVerifier(length: number = DEFAULT_VERIFIER_LENGTH): string {
  if (!Number.isInteger(length) || length < 43 || length > 128) {
    throw new RangeError('PKCE code verifier length must be an integer in [43, 128].');
  }

  const randomBytes = new Uint8Array(length);
  crypto.getRandomValues(randomBytes);

  const charsetSize = UNRESERVED_CHARS.length;
  let verifier = '';
  for (const byte of randomBytes) {
    verifier += UNRESERVED_CHARS[byte % charsetSize];
  }
  return verifier;
}

/**
 * Derive the PKCE `code_challenge` for a verifier: `base64url(SHA-256(verifier))`
 * (`code_challenge_method = S256`). Deterministic for a given verifier.
 */
export async function deriveCodeChallenge(verifier: string): Promise<string> {
  const verifierBytes = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest('SHA-256', verifierBytes);
  return base64UrlEncode(new Uint8Array(digest));
}

/**
 * Generate a random, opaque `state` value for CSRF protection on the
 * authorization request. Returned as a base64url string.
 */
export function generateState(): string {
  const randomBytes = new Uint8Array(STATE_BYTE_LENGTH);
  crypto.getRandomValues(randomBytes);
  return base64UrlEncode(randomBytes);
}

/** Alias matching the design vocabulary (`generateVerifier`). */
export const generateVerifier = generateCodeVerifier;

/** Alias matching the design vocabulary (`deriveChallenge`). */
export const deriveChallenge = deriveCodeChallenge;
