import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { requireAuth, RequireAuth } from './routeGuard';
import { setTokens, clearTokens, type AuthTokens } from './tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

// Sanity tests for the route guard (task 17.4). The exhaustive property test
// (17.5 — Property 16) is a separate optional task.

const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.com/v1',
  cognitoDomain: 'https://fleetmate.auth.us-east-1.amazoncognito.com',
  userPoolId: 'us-east-1_abc123',
  appClientId: 'client-abc',
  redirectUri: 'https://app.example.com/callback',
  logoutUri: 'https://app.example.com/',
};

const NOW = 1_000_000;

function tokens(expiresAt: number): AuthTokens {
  return { idToken: 'id', accessToken: 'access', expiresAt };
}

beforeEach(() => {
  clearTokens();
});

describe('requireAuth (pure)', () => {
  it('ALLOWs when a valid, unexpired token is held', () => {
    expect(requireAuth(tokens(NOW + 1), NOW)).toBe('ALLOW');
  });

  it('REDIRECTs when no token is held', () => {
    expect(requireAuth(null, NOW)).toBe('REDIRECT_TO_HOSTED_UI');
  });

  it('REDIRECTs when the token has expired (Req 10.5)', () => {
    expect(requireAuth(tokens(NOW - 1), NOW)).toBe('REDIRECT_TO_HOSTED_UI');
  });

  it('REDIRECTs at the exact expiry boundary (expiry is not valid)', () => {
    expect(requireAuth(tokens(NOW), NOW)).toBe('REDIRECT_TO_HOSTED_UI');
  });
});

describe('RequireAuth (component)', () => {
  it('renders children and does not redirect when authenticated', () => {
    setTokens(tokens(Date.now() + 60_000));
    const redirect = vi.fn();

    render(
      <RequireAuth config={config} authDeps={{ redirect }}>
        <p>Protected content</p>
      </RequireAuth>,
    );

    expect(screen.getByText('Protected content')).toBeInTheDocument();
    expect(redirect).not.toHaveBeenCalled();
  });

  it('renders the fallback and triggers login redirect when unauthenticated', async () => {
    const redirect = vi.fn();

    render(
      <RequireAuth
        config={config}
        authDeps={{ redirect }}
        fallback={<p>Redirecting…</p>}
      >
        <p>Protected content</p>
      </RequireAuth>,
    );

    expect(screen.queryByText('Protected content')).not.toBeInTheDocument();
    expect(screen.getByText('Redirecting…')).toBeInTheDocument();

    // beginLogin awaits PKCE derivation before redirecting; let microtasks flush.
    await vi.waitFor(() => {
      expect(redirect).toHaveBeenCalledTimes(1);
    });
    expect(redirect.mock.calls[0]?.[0]).toContain(`${config.cognitoDomain}/oauth2/authorize`);
  });
});
