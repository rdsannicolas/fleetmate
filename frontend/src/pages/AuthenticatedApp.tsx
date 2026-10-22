/**
 * The authenticated work area for the FleetMate SPA.
 *
 * Composes the already-built feature pages into the signed-in experience:
 * - {@link SubmitSymptomPage} drives the submit lifecycle and reports each
 *   completed outcome through `onResult` (Req 8.1, 8.2).
 * - The latest DIAGNOSIS / NEEDS_MORE_DETAIL outcome flows into
 *   {@link DiagnosisResultView} for rich rendering (Req 8.3, 8.4).
 * - A visible Logout control calls {@link logout}, which discards tokens and
 *   redirects to the Hosted UI logout endpoint (Req 10.2, 10.3).
 * - A link navigates to the session-history route for the current Session,
 *   passing the sessionId captured from the latest analyze result (Req 9.1).
 * - {@link PriorInteractionPanel} is composed in as its own distinct section to
 *   surface the single most-recent prior Interaction for the Customer, separate
 *   from the current Session's history (Req 13.7).
 *
 * An `onUnauthorized` signal (401 / token discarded) is bubbled up so the shell
 * can return the Customer to the unauthenticated view and force re-auth
 * (Req 6.5, 10.1).
 *
 * This component owns no auth policy and no data fetching of its own — it wires
 * together modules built in earlier tasks. It holds no secrets.
 */

import { useState } from 'react';
import DiagnosisResultView from './DiagnosisResultView';
import PriorInteractionPanel from './PriorInteractionPanel';
import SubmitSymptomPage from './SubmitSymptomPage';
import { logout, type AuthClientDeps } from '../auth/authClient';
import type { AnalyzeOutcome } from '../services/apiClient';
import type { RuntimeConfig } from '../config/runtimeConfig';
import { navigate } from '../utils/hashRouter';
import { clearCurrentSessionId } from '../utils/sessionStore';

/** Props for {@link AuthenticatedApp}. */
export interface AuthenticatedAppProps {
  /** Non-secret runtime config threaded into the pages and auth client. */
  config: RuntimeConfig;
  /** The Session currently in progress, captured from the latest result. */
  sessionId?: string;
  /** Report the Session id up to the shell so the history route can use it. */
  onSessionId?: (sessionId: string) => void;
  /**
   * Invoked when the API reports the Customer must re-authenticate (401 /
   * token discarded) so the shell can redirect to the Hosted UI (Req 10.1).
   */
  onUnauthorized?: () => void;
  /** Test seam forwarded to {@link logout} (redirect injection). */
  authDeps?: AuthClientDeps;
}

/**
 * Is the outcome one that carries a sessionId worth remembering? Only a
 * DIAGNOSIS or NEEDS_MORE_DETAIL response establishes/continues a Session.
 */
function outcomeSessionId(outcome: AnalyzeOutcome): string | undefined {
  if (outcome.kind === 'DIAGNOSIS' || outcome.kind === 'NEEDS_MORE_DETAIL') {
    return outcome.sessionId;
  }
  return undefined;
}

export default function AuthenticatedApp({
  config,
  sessionId,
  onSessionId,
  onUnauthorized,
  authDeps,
}: AuthenticatedAppProps): JSX.Element {
  // The latest renderable outcome (a diagnosis or a needs-more-detail result).
  const [result, setResult] = useState<
    Extract<AnalyzeOutcome, { kind: 'DIAGNOSIS' | 'NEEDS_MORE_DETAIL' }> | undefined
  >(undefined);

  const handleResult = (outcome: AnalyzeOutcome): void => {
    const nextSessionId = outcomeSessionId(outcome);
    if (nextSessionId !== undefined) {
      onSessionId?.(nextSessionId);
    }
    if (outcome.kind === 'DIAGNOSIS' || outcome.kind === 'NEEDS_MORE_DETAIL') {
      setResult(outcome);
    }
  };

  const handleLogout = (): void => {
    // Forget the current Session so the next sign-in starts fresh, then discard
    // tokens and redirect to the Hosted UI logout endpoint.
    clearCurrentSessionId();
    logout(config, authDeps);
  };

  const handleViewHistory = (): void => {
    navigate({ name: 'history' });
  };

  return (
    <main>
      <header>
        <h1>FleetMate</h1>
        <nav aria-label="Account">
          <button type="button" onClick={handleLogout}>
            Log out
          </button>
        </nav>
      </header>

      <SubmitSymptomPage
        config={config}
        onResult={handleResult}
        {...(sessionId !== undefined ? { initialSessionId: sessionId } : {})}
        {...(onUnauthorized !== undefined ? { onUnauthorized } : {})}
      />

      {result !== undefined && <DiagnosisResultView result={result} />}

      <section aria-labelledby="history-link-heading">
        <h2 id="history-link-heading">Past interactions</h2>
        {sessionId !== undefined ? (
          <button type="button" onClick={handleViewHistory}>
            View session history
          </button>
        ) : (
          <p>Submit a symptom to start a session and view its history.</p>
        )}
      </section>

      <PriorInteractionPanel
        config={config}
        {...(sessionId !== undefined ? { currentSessionId: sessionId } : {})}
        {...(onUnauthorized !== undefined ? { onUnauthorized } : {})}
      />
    </main>
  );
}
