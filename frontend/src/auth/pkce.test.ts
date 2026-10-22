import { describe, it, expect } from 'vitest';
import {
  base64UrlEncode,
  deriveCodeChallenge,
  generateCodeVerifier,
  generateState,
} from './pkce';

// Sanity tests for the PKCE helpers. The dedicated property test for challenge
// derivation (task 17.6) is separate; these cover the core contract and edge
// cases so the helpers are known-good for the auth client.

const UNRESERVED_RE = /^[A-Za-z0-9\-._~]+$/;
const BASE64URL_RE = /^[A-Za-z0-9\-_]+$/;

describe('base64UrlEncode', () => {
  it('uses the URL-safe alphabet with no padding', () => {
    // 0xFB 0xFF -> standard base64 "+/8=", url-safe no-pad "-_8"
    expect(base64UrlEncode(new Uint8Array([0xfb, 0xff]))).toBe('-_8');
  });

  it('encodes empty input as an empty string', () => {
    expect(base64UrlEncode(new Uint8Array([]))).toBe('');
  });
});

describe('generateCodeVerifier', () => {
  it('returns a verifier within the RFC 7636 length bounds using unreserved chars', () => {
    const verifier = generateCodeVerifier();
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(verifier).toMatch(UNRESERVED_RE);
  });

  it('honors an explicit length within bounds', () => {
    expect(generateCodeVerifier(43)).toHaveLength(43);
    expect(generateCodeVerifier(128)).toHaveLength(128);
  });

  it('rejects lengths outside [43, 128]', () => {
    expect(() => generateCodeVerifier(42)).toThrow(RangeError);
    expect(() => generateCodeVerifier(129)).toThrow(RangeError);
  });

  it('produces distinct verifiers across calls', () => {
    expect(generateCodeVerifier()).not.toBe(generateCodeVerifier());
  });
});

describe('deriveCodeChallenge', () => {
  it('matches the RFC 7636 §B worked example', async () => {
    // Appendix B: verifier -> expected S256 challenge.
    const verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk';
    const expected = 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM';
    await expect(deriveCodeChallenge(verifier)).resolves.toBe(expected);
  });

  it('is deterministic for a given verifier', async () => {
    const verifier = generateCodeVerifier();
    const a = await deriveCodeChallenge(verifier);
    const b = await deriveCodeChallenge(verifier);
    expect(a).toBe(b);
    expect(a).toMatch(BASE64URL_RE);
  });
});

describe('generateState', () => {
  it('returns a non-empty url-safe opaque value that varies per call', () => {
    const state = generateState();
    expect(state).toMatch(BASE64URL_RE);
    expect(state.length).toBeGreaterThan(0);
    expect(generateState()).not.toBe(state);
  });
});
