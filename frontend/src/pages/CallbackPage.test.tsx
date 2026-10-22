import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CallbackPage from './CallbackPage';
import { getTokens, clearTokens } from '../auth/tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

// Sanity RTL tests for the callback page (task 20.1). They drive the real
// auth client via an injected fetch so no network is hit; the dedicated logout
// test (20.2) is a separate optional task.

const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.com/v1',
  cognitoDomain: 'https://fleetmate.auth.us-east-1.amazoncognito.com',
  userPoolId: 'us-east-1_abc123',
  appClientId: 'client-abc',
  redirectUri: 'https://app.example.com/callback',
  logoutUri: 'https://app.example.com/',
};

// A valid, far-future ID token so the exchange response is accepted.
const ID_TOKEN =
  'eyJhbGciOiJub25lIn0.' +
  btoa(JSON.stringify({ exp: 4_102_444_800 })).replace(/=+$/, '') +
  '.sig';

/** Seed the transient PKCE verifier + CSRF state as beginLogin would have. */
function seedTransient(state: string): void {
  sessionStorage.setItem('fleetmate.pkceVerifier', 'test-verifier');
  sessionStorage.setItem('fleetmate.oauthState', state);
}

function okTokenResponse(): Response {
  return {
    ok: true,
    json: async () => ({
      id_token: ID_TOKEN,
      access_token: 'access-token',
      expires_in: 3600,
    }),
  } as unknown as Response;
}

beforeEach(() => {
  clearTokens();
  sessionStorage.clear();
});

describe('CallbackPage', () => {
  it('exchanges the code and signals success', async () => {
    seedTransient('xyz');
    const onSuccess = vi.fn();
    const fetchImpl = vi.fn(async () => okTokenResponse());

    render(
      <CallbackPage
        config={config}
        search="?code=auth-code&state=xyz"
        onSuccess={onSuccess}
        authDeps={{ fetchImpl }}
      />,
    );

    expect(await screen.findByText('Signing you in…')).toBeInTheDocument();
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(getTokens()).not.toBeNull();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('shows a login-did-not-complete message with retry on Hosted UI error', async () => {
    const redirect = vi.fn();

    render(
      <CallbackPage
        config={config}
        search="?error=access_denied"
        authDeps={{ redirect }}
      />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/login did not complete/i);
    expect(getTokens()).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /retry login/i }));
    await vi.waitFor(() => {
      expect(redirect).toHaveBeenCalledTimes(1);
    });
    expect(redirect.mock.calls[0]?.[0]).toContain(
      `${config.cognitoDomain}/oauth2/authorize`,
    );
  });
});
