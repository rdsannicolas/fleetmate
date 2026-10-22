/**
 * Unauthenticated landing view for the FleetMate SPA.
 *
 * This is what a Customer sees before logging in and, by the same token, what
 * they return to after logging out (Req 10.3). It shows a brief product intro
 * and a single "Log in / Sign up" control that hands off to the Cognito Hosted
 * UI via {@link beginLogin} (Req 6.1). Self-service sign-up and email
 * verification are handled entirely by the Hosted UI (Req 7), so one control
 * covers both returning and new Customers.
 *
 * The component is intentionally thin: it renders copy and delegates the login
 * redirect to the already-built auth client. It holds no secrets and no routing.
 */

import { beginLogin, type AuthClientDeps } from '../auth/authClient';
import type { RuntimeConfig } from '../config/runtimeConfig';

/** Props for {@link LoginLandingPage}. */
export interface LoginLandingPageProps {
  /** Non-secret runtime config used to build the Hosted UI login redirect. */
  config: RuntimeConfig;
  /** Test seam forwarded to {@link beginLogin} (fetch/redirect injection). */
  authDeps?: AuthClientDeps;
}

/**
 * The landing / logout-return view. A click on the single control starts the
 * Hosted UI authorization-code flow; new Customers register on that hosted page.
 */
export default function LoginLandingPage({
  config,
  authDeps,
}: LoginLandingPageProps): JSX.Element {
  const onLogin = (): void => {
    // beginLogin awaits PKCE derivation before redirecting; the click handler
    // cannot be async, so kick it off and let it settle. Failures here are
    // non-fatal — the Customer simply stays on the landing view.
    void beginLogin(config, authDeps);
  };

  return (
    <main>
      <h1>FleetMate</h1>
      <p>AI-powered vehicle diagnostic assistant.</p>
      <p>
        Describe your vehicle symptoms and get a probable issue, a repair
        recommendation, and an estimated cost.
      </p>
      <button type="button" onClick={onLogin}>
        Log in / Sign up
      </button>
      <p>New here? You can create an account on the secure sign-in page.</p>
    </main>
  );
}
