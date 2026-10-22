/**
 * OAuth redirect target for the FleetMate SPA (Req 6.2, 6.4, 6.7).
 *
 * The Hosted UI redirects back here with a `?code`/`?state` (success) or a
 * `?error` (failure) query string. On mount this page parses those params and
 * hands them to {@link handleCallback}, which validates PKCE/state and exchanges
 * the code for tokens within a bounded budget, returning a typed
 * {@link AuthResult}. The page renders one of three states:
 *
 * - in flight → "Completing sign-in…"
 * - `{ ok: true }` → success; fires {@link CallbackPageProps.onSuccess} so the
 *   app can navigate to the protected submit-symptom view (routing is wired in
 *   a later task), and shows a brief redirecting message in the meantime.
 * - `{ ok: false, reason }` → a friendly "login did not complete" message plus a
 *   "Retry login" control that restarts the Hosted UI flow (Req 6.4, 6.7).
 *
 * The component is thin: it orchestrates the already-built auth client and
 * renders state. It holds no secrets. The query-param source is injected so
 * tests can supply params without touching `window.location`.
 */

import { useEffect, useRef, useState } from 'react';
import {
  beginLogin,
  handleCallback,
  type AuthClientDeps,
  type AuthFailureReason,
  type CallbackParams,
} from '../auth/authClient';
import type { RuntimeConfig } from '../config/runtimeConfig';

/** Props for {@link CallbackPage}. */
export interface CallbackPageProps {
  /** Non-secret runtime config used for the token exchange and retry redirect. */
  config: RuntimeConfig;
  /**
   * The query string to parse (including a leading `?`). Injected so tests can
   * supply params; defaults to the live `window.location.search`.
   */
  search?: string;
  /**
   * Called once the code exchange succeeds, so the app can navigate to the
   * protected view. Optional because routing is wired in a later task; when
   * omitted the page simply shows a success message.
   */
  onSuccess?: () => void;
  /** Test seam forwarded to the auth client (fetch/redirect injection). */
  authDeps?: AuthClientDeps;
}

/** Local render phase for the callback flow. */
type Phase =
  | { status: 'pending' }
  | { status: 'success' }
  | { status: 'failure'; reason: AuthFailureReason };

/** Parse `code`, `state`, and `error` out of a callback query string. */
function parseCallbackParams(search: string): CallbackParams {
  const query = new URLSearchParams(search);
  const params: CallbackParams = {};
  const code = query.get('code');
  const state = query.get('state');
  const error = query.get('error');
  if (code !== null) {
    params.code = code;
  }
  if (state !== null) {
    params.state = state;
  }
  if (error !== null) {
    params.error = error;
  }
  return params;
}

/**
 * Map a failure reason to Customer-facing copy. Never leaks internal reason
 * codes, provider errors, or stack details (Req 6.4, 6.7).
 */
function messageForReason(reason: AuthFailureReason): string {
  switch (reason) {
    case 'TIMEOUT':
      return 'Login timed out before it could complete. Please try again.';
    case 'AUTH_ERROR':
    case 'STATE_MISMATCH':
    case 'MISSING_CODE':
    case 'EXCHANGE_FAILED':
    default:
      return 'Login did not complete. Please try again.';
  }
}

/**
 * The callback page. Runs the exchange once on mount and renders the resulting
 * state. The exhaustive-dependency effect intentionally runs a single time for
 * a given `search`/`config`; React 18 Strict Mode double-invokes it in dev, but
 * {@link handleCallback} is fail-closed and idempotent on a consumed code.
 */
export default function CallbackPage({
  config,
  search = typeof window !== 'undefined' ? window.location.search : '',
  onSuccess,
  authDeps,
}: CallbackPageProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>({ status: 'pending' });
  // The authorization code is single-use. Guard against running the exchange
  // more than once (React 18 Strict Mode double-invokes effects in dev, and a
  // re-render could otherwise re-enter) so a consumed code is never resubmitted.
  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) {
      return;
    }
    startedRef.current = true;
    let cancelled = false;

    const run = async (): Promise<void> => {
      const params = parseCallbackParams(search);
      const result = await handleCallback(params, config, authDeps);
      if (cancelled) {
        return;
      }
      if (result.ok) {
        setPhase({ status: 'success' });
        onSuccess?.();
      } else {
        setPhase({ status: 'failure', reason: result.reason });
      }
    };

    void run();

    return () => {
      cancelled = true;
    };
    // `onSuccess` is deliberately excluded: the exchange must run once per
    // callback, not re-run if the parent passes a new callback identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, config, authDeps]);

  const onRetry = (): void => {
    void beginLogin(config, authDeps);
  };

  if (phase.status === 'pending') {
    return (
      <main>
        <h1>FleetMate</h1>
        <p role="status">Completing sign-in…</p>
      </main>
    );
  }

  if (phase.status === 'success') {
    return (
      <main>
        <h1>FleetMate</h1>
        <p role="status">Signing you in…</p>
      </main>
    );
  }

  return (
    <main>
      <h1>FleetMate</h1>
      <p role="alert">{messageForReason(phase.reason)}</p>
      <button type="button" onClick={onRetry}>
        Retry login
      </button>
    </main>
  );
}
