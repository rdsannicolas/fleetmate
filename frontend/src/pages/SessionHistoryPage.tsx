/**
 * Session-history view for the FleetMate SPA.
 *
 * Shows a Customer their own Session history (Req 9). On open it requests the
 * first page from `GET /diagnostics/history` using the Customer's Session
 * identifier (Req 9.1), renders the returned Interaction_Records ascending by
 * `createdAt` and tie-broken by `recordId` (Req 9.2), and offers a next-page
 * control whenever the response carries a `nextPageToken`, appending up to 50
 * more records per page while preserving ascending order across pages (Req 9.6).
 *
 * Non-200 outcomes are handled from the typed {@link HistoryOutcome} union:
 * an empty set shows a no-past-interactions message (Req 9.3); a 403 shows a
 * not-authorized message and retains any records already displayed (Req 9.4);
 * a timeout, network, or service error shows a could-not-retrieve message with
 * a Retry control capped at 3 attempts (Req 9.5); a 401 signals that re-auth is
 * required via {@link SessionHistoryPageProps.onUnauthorized} (Req 10.1).
 *
 * The data fetch is injectable (`getHistoryFn`) with the real
 * {@link getHistory} as the default, so tests never touch the network. The
 * ordering logic lives in the pure, exported {@link sortRecordsAscending} so it
 * can be property-tested independently. This component holds no secrets.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getHistory,
  type ApiClientDeps,
  type HistoryOutcome,
  type InteractionRecord,
} from '../services/apiClient';
import type { RuntimeConfig } from '../config/runtimeConfig';

/** Maximum number of history retrieval attempts before the Retry control is disabled (Req 9.5). */
export const MAX_HISTORY_ATTEMPTS = 3;

/** The injectable signature of the history fetch, matching {@link getHistory}. */
export type GetHistoryFn = (
  input: { sessionId: string; pageToken?: string },
  config: RuntimeConfig,
  deps?: ApiClientDeps,
  timeoutMs?: number,
) => Promise<HistoryOutcome>;

/** Props for {@link SessionHistoryPage}. */
export interface SessionHistoryPageProps {
  /** The Session whose history is displayed (Req 9.1). */
  sessionId: string;
  /** Non-secret runtime config used to build the history request. */
  config: RuntimeConfig;
  /** Test seam: history fetch. Defaults to the real {@link getHistory}. */
  getHistoryFn?: GetHistoryFn;
  /** Invoked when the API reports the Customer must re-authenticate (Req 10.1). */
  onUnauthorized?: () => void;
}

/**
 * Pure ordering helper: return a new array of the given records sorted ascending
 * by `createdAt`, breaking ties deterministically by `recordId` (Req 9.2).
 *
 * The input array is never mutated. Comparison uses string ordering, which is
 * correct for the UTC, millisecond-precision ISO timestamps the API emits and
 * for the record identifiers.
 */
export function sortRecordsAscending(
  records: readonly InteractionRecord[],
): InteractionRecord[] {
  return [...records].sort((a, b) => {
    if (a.createdAt < b.createdAt) return -1;
    if (a.createdAt > b.createdAt) return 1;
    if (a.recordId < b.recordId) return -1;
    if (a.recordId > b.recordId) return 1;
    return 0;
  });
}

/** The visible phase of the view, derived from the latest fetch outcome. */
type HistoryPhase = 'LOADING' | 'LOADED' | 'EMPTY' | 'FORBIDDEN' | 'ERROR';

export default function SessionHistoryPage({
  sessionId,
  config,
  getHistoryFn = getHistory,
  onUnauthorized,
}: SessionHistoryPageProps): JSX.Element {
  const [records, setRecords] = useState<InteractionRecord[]>([]);
  const [nextPageToken, setNextPageToken] = useState<string | undefined>(undefined);
  const [phase, setPhase] = useState<HistoryPhase>('LOADING');
  const [attempts, setAttempts] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);

  // Latest records are read inside async callbacks without adding them to the
  // callback's dependencies, so pagination appends to the current list.
  const recordsRef = useRef<InteractionRecord[]>([]);
  recordsRef.current = records;

  /**
   * Fetch one page. `pageToken` undefined loads the first page; otherwise it
   * appends the next page. A 403/error retains whatever is already displayed
   * (Req 9.4, 9.5); an empty first page shows the empty message (Req 9.3).
   */
  const fetchPage = useCallback(
    async (pageToken?: string): Promise<void> => {
      const isInitial = pageToken === undefined;
      setLoading(true);
      if (isInitial) {
        setPhase('LOADING');
      }

      const input = pageToken === undefined ? { sessionId } : { sessionId, pageToken };
      const outcome = await getHistoryFn(input, config);

      switch (outcome.kind) {
        case 'RECORDS': {
          const merged = sortRecordsAscending([...recordsRef.current, ...outcome.records]);
          setRecords(merged);
          setNextPageToken(outcome.nextPageToken);
          setPhase('LOADED');
          break;
        }
        case 'EMPTY': {
          // Only an empty first page means "no past interactions"; a later page
          // simply has nothing more to append and should keep what's shown.
          if (isInitial) {
            setRecords([]);
            setPhase('EMPTY');
          }
          setNextPageToken(undefined);
          break;
        }
        case 'FORBIDDEN': {
          // Retain any previously displayed records (Req 9.4).
          setPhase('FORBIDDEN');
          break;
        }
        case 'UNAUTHORIZED': {
          // Tokens were discarded by the client; signal re-auth (Req 10.1).
          onUnauthorized?.();
          break;
        }
        case 'TIMEOUT':
        case 'NETWORK_ERROR':
        case 'SERVICE_UNAVAILABLE':
        case 'VALIDATION_ERROR': {
          setAttempts((n) => n + 1);
          setPhase('ERROR');
          break;
        }
      }

      setLoading(false);
    },
    [sessionId, config, getHistoryFn, onUnauthorized],
  );

  // Load the first page on open / whenever the session changes (Req 9.1). Reset
  // the view so a new session doesn't show the previous session's records.
  useEffect(() => {
    setRecords([]);
    recordsRef.current = [];
    setNextPageToken(undefined);
    setAttempts(0);
    setPhase('LOADING');
    void fetchPage();
  }, [fetchPage]);

  const retriesExhausted = attempts >= MAX_HISTORY_ATTEMPTS;

  const onRetry = (): void => {
    if (retriesExhausted || loading) return;
    void fetchPage();
  };

  const onLoadMore = (): void => {
    if (nextPageToken === undefined || loading) return;
    void fetchPage(nextPageToken);
  };

  return (
    <main>
      <h1>Session history</h1>

      {loading && (
        <p role="status" aria-live="polite">
          Loading your session history…
        </p>
      )}

      {phase === 'EMPTY' && !loading && (
        <p>No past interactions exist for this session.</p>
      )}

      {phase === 'FORBIDDEN' && (
        <p role="alert">You are not authorized to view this session history.</p>
      )}

      {phase === 'ERROR' && (
        <div role="alert">
          <p>Your session history could not be retrieved.</p>
          <button type="button" onClick={onRetry} disabled={retriesExhausted || loading}>
            Retry
          </button>
          {retriesExhausted && (
            <p>Maximum retry attempts reached. Please try again later.</p>
          )}
        </div>
      )}

      {records.length > 0 && (
        <ul>
          {records.map((record) => (
            <li key={record.recordId}>
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
            </li>
          ))}
        </ul>
      )}

      {nextPageToken !== undefined && (
        <button type="button" onClick={onLoadMore} disabled={loading}>
          Load more
        </button>
      )}
    </main>
  );
}
