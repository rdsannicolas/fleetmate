/**
 * Route guard for the FleetMate SPA.
 *
 * Restricts the protected Customer views (symptom submission, session history)
 * to authenticated Customers (Req 6.5) and treats an expired token as
 * unauthenticated so it forces re-authentication through the Hosted UI (Req 10.5).
 *
 * The decision is split into two parts:
 * - {@link requireAuth} — a pure, deterministic function (time injected) that
 *   maps a token set + `now` to an access decision. All policy lives here so it
 *   is property-testable (task 17.5) without a DOM or a clock.
 * - {@link RequireAuth} — a thin React wrapper that reads the current tokens and
 *   `Date.now()`, defers to {@link requireAuth}, and either renders its children
 *   or kicks off the Hosted UI login redirect. It holds no policy of its own.
 *
 * This module holds no secrets. It only reads tokens the Hosted UI issued to the
 * browser and the non-secret runtime config.
 */

import { useEffect, type ReactNode } from 'react';
import { beginLogin, type AuthClientDeps } from './authClient';
import { getTokens, isTokenValid, type AuthTokens } from './tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

/**
 * Access decision for a protected view.
 *
 * - `ALLOW` — a valid (unexpired) token set is held; render the view.
 * - `REDIRECT_TO_HOSTED_UI` — no token, or the token has expired; the Customer
 *   must (re-)authenticate via the Hosted UI.
 */
export type AccessDecision = 'ALLOW' | 'REDIRECT_TO_HOSTED_UI';

/**
 * Pure guard decision (Req 6.5, 10.5). Returns `ALLOW` iff
 * {@link isTokenValid} is true for `tokens` at `now`; otherwise
 * `REDIRECT_TO_HOSTED_UI`. An absent or expired token both resolve to a
 * redirect, so an expired token forces re-authentication (Req 10.5).
 *
 * `now` is injected rather than read from `Date.now()` so the function is
 * deterministic and property-testable.
 */
export function requireAuth(tokens: AuthTokens | null, now: number): AccessDecision {
  return isTokenValid(tokens, now) ? 'ALLOW' : 'REDIRECT_TO_HOSTED_UI';
}

/** Props for {@link RequireAuth}. */
export interface RequireAuthProps {
  /** Non-secret runtime config used to build the Hosted UI login redirect. */
  config: RuntimeConfig;
  /** The protected view to render when access is allowed. */
  children: ReactNode;
  /**
   * What to render while the browser is being redirected to the Hosted UI.
   * Defaults to nothing; pages may pass a minimal "Redirecting…" placeholder.
   */
  fallback?: ReactNode;
  /** Test seam forwarded to {@link beginLogin} (fetch/redirect injection). */
  authDeps?: AuthClientDeps;
}

/**
 * Thin React guard. On render it evaluates {@link requireAuth} against the
 * currently stored tokens and the current wall-clock time. When access is
 * allowed it renders `children`; otherwise it triggers the Hosted UI login
 * redirect (Req 6.1) and renders `fallback` (nothing by default).
 *
 * The redirect is fired from an effect so it is a side effect of commit, not of
 * render, keeping render pure and avoiding duplicate redirects under React 18
 * Strict Mode double-invocation of render.
 */
export function RequireAuth({
  config,
  children,
  fallback = null,
  authDeps,
}: RequireAuthProps): ReactNode {
  const decision = requireAuth(getTokens(), Date.now());

  useEffect(() => {
    if (decision === 'REDIRECT_TO_HOSTED_UI') {
      // beginLogin returns a promise (it awaits PKCE derivation) but we cannot
      // await inside an effect; failures here are non-fatal for the guard.
      void beginLogin(config, authDeps);
    }
  }, [decision, config, authDeps]);

  return decision === 'ALLOW' ? children : fallback;
}
