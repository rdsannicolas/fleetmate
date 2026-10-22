/**
 * Application shell for the FleetMate SPA (task 26.1).
 *
 * Responsibilities:
 * - Load the non-secret runtime config exactly once at startup via
 *   {@link loadRuntimeConfig}, showing a brief loading state while it resolves
 *   and a clear error state (with retry) if it fails (Req 12.1/12.2 wiring).
 * - Resolve the current view from the location fragment with the minimal hash
 *   router, so client-side routing works on top of the CloudFront SPA fallback
 *   (Req 11.3).
 * - Route to the pages built in earlier tasks and apply the auth guard:
 *     `#/`         → LoginLandingPage when unauthenticated; the protected
 *                    AuthenticatedApp (submit + diagnosis + history link) when
 *                    authenticated, wrapped in {@link RequireAuth} (Req 6.1, 6.5).
 *     `#/callback` → CallbackPage (Hosted UI redirect target); on success it
 *                    navigates to the authenticated home view.
 *     `#/history`  → the protected SessionHistoryPage for the current Session.
 *
 * The shell holds no secrets and no auth policy — it composes the config loader,
 * the route guard, the auth client, and the feature pages so nothing is
 * orphaned.
 */

import { useEffect, useState } from 'react';
import { loadRuntimeConfig, type RuntimeConfig } from './config/runtimeConfig';
import { RequireAuth } from './auth/routeGuard';
import { beginLogin, isAuthenticated } from './auth/authClient';
import LoginLandingPage from './pages/LoginLandingPage';
import CallbackPage from './pages/CallbackPage';
import SessionHistoryPage from './pages/SessionHistoryPage';
import AuthenticatedApp from './pages/AuthenticatedApp';
import { navigate, useHashRoute } from './utils/hashRouter';
import { getCurrentSessionId, setCurrentSessionId } from './utils/sessionStore';

/** Loading phase for the one-time runtime-config fetch. */
type ConfigState =
  | { status: 'loading' }
  | { status: 'ready'; config: RuntimeConfig }
  | { status: 'error'; message: string };

/** Minimal chrome shared by the pre-config loading/error screens. */
function Shell({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <main>
      <h1>FleetMate</h1>
      {children}
    </main>
  );
}

/**
 * Trigger re-authentication: discard happens in the API/token layer; here we
 * send the browser to the Hosted UI login. Used as the `onUnauthorized` signal
 * from the protected pages (Req 6.5, 10.1).
 */
function redirectToLogin(config: RuntimeConfig): void {
  void beginLogin(config);
}

function hasOAuthCallbackParams(): boolean {
  if (typeof window === 'undefined') {
    return false;
  }
  const params = new URLSearchParams(window.location.search);
  return params.has('code') || params.has('error');
}

function clearOAuthQueryFromUrl(): void {
  if (typeof window === 'undefined' || typeof window.history === 'undefined') {
    return;
  }
  const url = new URL(window.location.href);
  url.search = '';
  window.history.replaceState(window.history.state, '', url.toString());
}

export default function App(): JSX.Element {
  const [configState, setConfigState] = useState<ConfigState>({ status: 'loading' });
  const [loadToken, setLoadToken] = useState(0);
  const route = useHashRoute();
  // The Session in progress, lifted here so it survives navigation between the
  // authenticated home view and the history route (Req 9.1).
  // Seed from the persisted current Session (Option A) so the symptoms entered
  // this visit stay grouped in one Session across refreshes and navigation.
  const [sessionId, setSessionId] = useState<string | undefined>(getCurrentSessionId());

  // Load the runtime config once at startup (and again if the user retries).
  useEffect(() => {
    let cancelled = false;
    setConfigState({ status: 'loading' });

    void (async (): Promise<void> => {
      try {
        const config = await loadRuntimeConfig();
        if (!cancelled) {
          setConfigState({ status: 'ready', config });
        }
      } catch {
        if (!cancelled) {
          setConfigState({
            status: 'error',
            message:
              'We could not load the application configuration. Please try again.',
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [loadToken]);

  if (configState.status === 'loading') {
    return (
      <Shell>
        <p role="status" aria-live="polite">
          Loading…
        </p>
      </Shell>
    );
  }

  if (configState.status === 'error') {
    return (
      <Shell>
        <p role="alert">{configState.message}</p>
        <button type="button" onClick={() => setLoadToken((n) => n + 1)}>
          Retry
        </button>
      </Shell>
    );
  }

  const { config } = configState;
  const onUnauthorized = (): void => redirectToLogin(config);

  // Persist the current Session id as it changes (Option A) so it survives
  // refreshes and navigation for the rest of this browser-tab sitting, keeping
  // every symptom entered this visit in the same Session's history.
  const handleSessionId = (nextSessionId: string): void => {
    setCurrentSessionId(nextSessionId);
    setSessionId(nextSessionId);
  };

  // The Hosted UI redirect target. Cognito callback URLs cannot carry a URL
  // fragment, so after login the browser lands on the bare CloudFront origin
  // with the authorization `code`/`state` (or an `error`) in the QUERY
  // string, not on the `#/callback` hash route. Detect that case by the query
  // params and run the callback exchange regardless of the hash; otherwise the
  // code is never exchanged and the Customer stays on the landing page.
  if (route.name === 'callback' || hasOAuthCallbackParams()) {
    return (
      <CallbackPage
        config={config}
        onSuccess={() => {
          // Strip the ?code/?state from the address bar (so a refresh doesn't
          // retry a consumed code) and show the authenticated home view.
          clearOAuthQueryFromUrl();
          navigate({ name: 'home' });
        }}
      />
    );
  }

  // The session-history route is protected: an unauthenticated visitor is
  // redirected to the Hosted UI by the guard (Req 6.5).
  if (route.name === 'history') {
    return (
      <RequireAuth config={config} fallback={<RedirectingNotice />}>
        {sessionId !== undefined ? (
          <SessionHistoryPage
            sessionId={sessionId}
            config={config}
            onUnauthorized={onUnauthorized}
          />
        ) : (
          <NoActiveSession />
        )}
      </RequireAuth>
    );
  }

  // Home: unauthenticated Customers see the landing page; authenticated
  // Customers see the protected work area, guarded so a token expiry forces
  // re-auth (Req 6.1, 6.5).
  if (!isAuthenticated()) {
    return <LoginLandingPage config={config} />;
  }

  return (
    <RequireAuth config={config} fallback={<RedirectingNotice />}>
      <AuthenticatedApp
        config={config}
        {...(sessionId !== undefined ? { sessionId } : {})}
        onSessionId={handleSessionId}
        onUnauthorized={onUnauthorized}
      />
    </RequireAuth>
  );
}

/** Shown briefly while the guard redirects an unauthenticated visitor. */
function RedirectingNotice(): JSX.Element {
  return (
    <Shell>
      <p role="status">Redirecting to sign-in…</p>
    </Shell>
  );
}

/** Shown on the history route before any Session has been started. */
function NoActiveSession(): JSX.Element {
  return (
    <main>
      <h1>Session history</h1>
      <p>No active session yet. Submit a symptom first to start one.</p>
      <button type="button" onClick={() => navigate({ name: 'home' })}>
        Back to submit
      </button>
    </main>
  );
}
