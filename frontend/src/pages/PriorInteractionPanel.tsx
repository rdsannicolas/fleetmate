/**
 * Prior-interaction panel for the FleetMate SPA.
 *
 * Surfaces the Customer's single most-recent *prior* Interaction — the one from
 * before the current Session (Req 13.2, 13.7). On open it calls
 * `previous(currentSessionId)` using the persisted Current_Session identifier
 * (resolved from {@link getCurrentSessionId}, or an explicit prop override) and
 * renders exactly one record. It is deliberately DISTINCT from
 * {@link SessionHistoryPage}, which shows a paginated list for the current
 * Session; this panel shows at most one prior record.
 *
 * The typed {@link PreviousOutcome} union drives the view:
 *  - `PRIOR_RECORD` renders the prior Symptom_Description and Diagnostic_Result
 *    using the SHARED normalized view-model fields on `record`, laid out the
 *    same way {@link SessionHistoryPage} renders a record (Req 13.2, 13.7).
 *  - `NO_PRIOR` shows a no-previous-interaction empty state (Req 13.8).
 *  - `UNAUTHORIZED` means the client already discarded tokens, so it signals the
 *    shell to force re-auth via {@link PriorInteractionPanelProps.onUnauthorized}
 *    (Req 13.9, mirroring Req 10.1).
 *  - `PREVIOUS_ERROR` shows a could-not-retrieve message with a Retry control
 *    that re-issues the request (Req 13.10).
 *
 * When no current Session id is available there is no current Session to exclude
 * a prior interaction from, so the panel renders the same empty state without
 * issuing a request.
 *
 * The data fetch is injectable (`previousFn`) with the real {@link previous} as
 * the default, so tests never touch the network. This component holds no secrets
 * and carries no business logic beyond view-state mapping — owner-scoping and
 * record selection live server-side.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  previous,
  type ApiClientDeps,
  type InteractionRecord,
  type PreviousOutcome,
} from '../services/apiClient';
import type { RuntimeConfig } from '../config/runtimeConfig';
import { getCurrentSessionId } from '../utils/sessionStore';

/** Maximum number of prior-interaction retrieval attempts before Retry is disabled (Req 13.10). */
export const MAX_PREVIOUS_ATTEMPTS = 3;

/** The injectable signature of the prior-interaction fetch, matching {@link previous}. */
export type PreviousFn = (
  currentSessionId: string,
  config: RuntimeConfig,
  deps?: ApiClientDeps,
  timeoutMs?: number,
) => Promise<PreviousOutcome>;

/** Props for {@link PriorInteractionPanel}. */
export interface PriorInteractionPanelProps {
  /** Non-secret runtime config used to build the prior-interaction request. */
  config: RuntimeConfig;
  /**
   * Optional override for the current Session id. When provided it is used
   * directly; otherwise the panel falls back to {@link getCurrentSessionId}.
   */
  currentSessionId?: string;
  /** Test seam: prior-interaction fetch. Defaults to the real {@link previous}. */
  previousFn?: PreviousFn;
  /** Invoked when the API reports the Customer must re-authenticate (Req 13.9). */
  onUnauthorized?: () => void;
}

/** The visible phase of the panel, derived from the latest fetch outcome. */
type PriorPhase = 'LOADING' | 'RECORD' | 'NONE' | 'ERROR';

export default function PriorInteractionPanel({
  config,
  currentSessionId,
  previousFn = previous,
  onUnauthorized,
}: PriorInteractionPanelProps): JSX.Element {
  const [record, setRecord] = useState<InteractionRecord | undefined>(undefined);
  const [phase, setPhase] = useState<PriorPhase>('LOADING');
  const [errorMessage, setErrorMessage] = useState<string | undefined>(undefined);
  const [attempts, setAttempts] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);

  // Resolve the Session id once per render: explicit prop wins, else the
  // persisted current Session id (undefined when none is stored).
  const resolvedSessionId = currentSessionId ?? getCurrentSessionId();

  /**
   * Fetch the single most-recent prior interaction. With no resolved Session id
   * there is nothing to exclude a prior interaction from, so we show the same
   * empty state and never call `previous`.
   */
  const fetchPrevious = useCallback(async (): Promise<void> => {
    if (resolvedSessionId === undefined) {
      setRecord(undefined);
      setErrorMessage(undefined);
      setPhase('NONE');
      return;
    }

    setLoading(true);
    setPhase('LOADING');

    const outcome = await previousFn(resolvedSessionId, config);

    switch (outcome.kind) {
      case 'PRIOR_RECORD': {
        setRecord(outcome.record);
        setErrorMessage(undefined);
        setPhase('RECORD');
        break;
      }
      case 'NO_PRIOR': {
        setRecord(undefined);
        setErrorMessage(undefined);
        setPhase('NONE');
        break;
      }
      case 'UNAUTHORIZED': {
        // Tokens were discarded by the client; signal re-auth (Req 13.9).
        onUnauthorized?.();
        break;
      }
      case 'PREVIOUS_ERROR': {
        setAttempts((n) => n + 1);
        setErrorMessage(outcome.message);
        setPhase('ERROR');
        break;
      }
    }

    setLoading(false);
  }, [resolvedSessionId, config, previousFn, onUnauthorized]);

  // Load on open and whenever the resolved Session id (or fetch) changes.
  useEffect(() => {
    setRecord(undefined);
    setErrorMessage(undefined);
    setAttempts(0);
    setPhase('LOADING');
    void fetchPrevious();
  }, [fetchPrevious]);

  const retriesExhausted = attempts >= MAX_PREVIOUS_ATTEMPTS;

  const onRetry = (): void => {
    if (retriesExhausted || loading) return;
    void fetchPrevious();
  };

  return (
    <section aria-label="Previous interaction">
      <h2>Previous interaction</h2>

      {loading && (
        <p role="status" aria-live="polite">
          Loading your previous interaction…
        </p>
      )}

      {phase === 'NONE' && !loading && <p>No previous interaction found.</p>}

      {phase === 'ERROR' && !loading && (
        <div role="alert">
          <p>{errorMessage ?? 'Could not retrieve your previous interaction.'}</p>
          <button type="button" onClick={onRetry} disabled={retriesExhausted || loading}>
            Retry
          </button>
          {retriesExhausted && (
            <p>Maximum retry attempts reached. Please try again later.</p>
          )}
        </div>
      )}

      {phase === 'RECORD' && !loading && record !== undefined && (
        <div>
          <p>
            <strong>Symptom:</strong> {record.symptomText ?? '—'}
          </p>
          {record.probableIssue !== undefined ? (
            <>
              <p>
                <strong>Probable issue:</strong> {record.probableIssue}
              </p>
              <p>
                <strong>Repair recommendation:</strong>{' '}
                {record.repairRecommendation ?? '—'}
              </p>
              {record.estimatedCost !== undefined && (
                <p>
                  <strong>Estimated cost:</strong> {record.estimatedCost}
                </p>
              )}
              {record.confidenceScore !== undefined && (
                <p>
                  <strong>Confidence:</strong>{' '}
                  {Math.round(record.confidenceScore * 100)}%
                </p>
              )}
            </>
          ) : (
            <p>
              <strong>Diagnosis:</strong>{' '}
              {record.message ?? 'More detail was requested to complete this diagnosis.'}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
