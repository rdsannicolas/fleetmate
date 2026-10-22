/**
 * Submit-a-symptom page for the FleetMate SPA (Requirement 8).
 *
 * Renders a controlled form that lets an authenticated Customer type a
 * Symptom_Description and submit it to the Diagnostic_API via the injected
 * {@link analyze} client. This component owns only the submission interaction;
 * rich rendering of a diagnosis is the responsibility of the DiagnosisResultView
 * (task 21.2). The latest {@link AnalyzeOutcome} is surfaced to the parent
 * through `onResult` so that view can render it, and a minimal inline summary is
 * shown here as a placeholder.
 *
 * Acceptance criteria upheld:
 * - Req 8.1: input gating — the submit control is disabled while the text is
 *   empty, whitespace-only, or longer than 2000 characters, and a live
 *   remaining-character count is shown. The pure predicate {@link isSubmittable}
 *   is exported so the property test (task 21.3) can target it directly.
 * - Req 8.2: a valid submission calls `analyze({ symptomText, sessionId? })`,
 *   carrying the sessionId from the previous DIAGNOSIS / NEEDS_MORE_DETAIL
 *   response so follow-ups stay in the same Session.
 * - Req 8.4: NEEDS_MORE_DETAIL shows the message and keeps the text for editing.
 * - Req 8.5: VALIDATION_ERROR shows the suggestion and retains the text.
 * - Req 8.6: SERVICE_UNAVAILABLE shows a temporarily-unavailable message and a
 *   retry control.
 * - Req 8.7: while a request is in flight an in-progress indicator is shown and
 *   duplicate submission of the same request is prevented.
 * - Req 8.8: the apiClient enforces a 30s budget and returns TIMEOUT; on TIMEOUT
 *   the indicator stops, a did-not-complete message and retry are shown, and the
 *   entered text is retained.
 * - Req 10.1: UNAUTHORIZED is signalled to the parent via `onUnauthorized` so it
 *   can drive re-authentication.
 *
 * This module holds no secrets.
 */

import { useCallback, useState, type ReactElement } from 'react';
import {
  analyze as defaultAnalyze,
  type AnalyzeInput,
  type AnalyzeOutcome,
} from '../services/apiClient';
import type { RuntimeConfig } from '../config/runtimeConfig';

/** Maximum Symptom_Description length accepted by the input (Req 8.1). */
export const MAX_SYMPTOM_LENGTH = 2000;

/** Minimum non-whitespace length accepted by the input (Req 8.1). */
export const MIN_SYMPTOM_LENGTH = 1;

/**
 * Pure input-validity predicate (Req 8.1).
 *
 * A Symptom_Description is submittable only when, after trimming surrounding
 * whitespace, it has at least one character, and when its raw length does not
 * exceed {@link MAX_SYMPTOM_LENGTH}. The raw (untrimmed) length is what is
 * compared against the maximum so the gate matches what the Customer sees in the
 * character counter.
 *
 * Exported so the property test (task 21.3) can exercise it over many inputs.
 */
export function isSubmittable(text: string): boolean {
  const trimmedLength = text.trim().length;
  return trimmedLength >= MIN_SYMPTOM_LENGTH && text.length <= MAX_SYMPTOM_LENGTH;
}

/** The async shape of the injectable analyze function. */
export type AnalyzeFn = (
  input: AnalyzeInput,
  config: RuntimeConfig,
) => Promise<AnalyzeOutcome>;

/** Props for {@link SubmitSymptomPage}. */
export interface SubmitSymptomPageProps {
  /** Non-secret runtime configuration passed through to the API client. */
  config: RuntimeConfig;
  /**
   * Injectable analyze implementation. Defaults to the real API client so tests
   * can supply a stub and avoid the network.
   */
  analyzeFn?: AnalyzeFn;
  /**
   * Notified with the latest outcome after each completed submission, so a
   * parent (e.g. the DiagnosisResultView, task 21.2) can render the result.
   */
  onResult?: (outcome: AnalyzeOutcome) => void;
  /**
   * Notified when the API reports the Customer is no longer authenticated
   * (401 / token discarded) so the parent can trigger re-authentication
   * (Req 10.1).
   */
  onUnauthorized?: () => void;
  /**
   * The Session id to continue, if one is already in progress for this visit.
   * Seeds the page so every symptom entered in a sitting stays in the SAME
   * Session (Option A); the page keeps it current as new results arrive.
   */
  initialSessionId?: string;
}

/** UI phase of the submission interaction. */
type Phase = 'idle' | 'submitting';

export default function SubmitSymptomPage({
  config,
  analyzeFn = defaultAnalyze,
  onResult,
  onUnauthorized,
  initialSessionId,
}: SubmitSymptomPageProps): ReactElement {
  const [symptomText, setSymptomText] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  // Seed from the persisted current Session so submissions this visit continue
  // the same Session (Option A) rather than starting a new one each page load.
  const [sessionId, setSessionId] = useState<string | undefined>(initialSessionId);
  const [outcome, setOutcome] = useState<AnalyzeOutcome | undefined>(undefined);

  const submitting = phase === 'submitting';
  const remaining = MAX_SYMPTOM_LENGTH - symptomText.length;
  // Prevent duplicate submission while a request is in flight (Req 8.7).
  const canSubmit = isSubmittable(symptomText) && !submitting;

  const runAnalyze = useCallback(async (): Promise<void> => {
    // Guard against duplicate submission of the same request (Req 8.7).
    if (!isSubmittable(symptomText) || submitting) {
      return;
    }

    setPhase('submitting');

    const input: AnalyzeInput = { symptomText };
    if (sessionId !== undefined) {
      input.sessionId = sessionId;
    }

    const result = await analyzeFn(input, config);

    // Carry the sessionId forward so follow-up submissions stay in the same
    // Session (Req 8.2 / 8.4).
    if (result.kind === 'DIAGNOSIS' || result.kind === 'NEEDS_MORE_DETAIL') {
      setSessionId(result.sessionId);
    }

    setOutcome(result);
    // TIMEOUT and all terminal outcomes return us to idle, which stops the
    // in-progress indicator (Req 8.8) and re-enables the submit control. The
    // symptom text is never cleared, so it is retained for retry/editing
    // (Req 8.4, 8.5, 8.8).
    setPhase('idle');

    onResult?.(result);

    if (result.kind === 'UNAUTHORIZED') {
      onUnauthorized?.();
    }
  }, [analyzeFn, config, onResult, onUnauthorized, sessionId, submitting, symptomText]);

  const handleSubmit = useCallback(
    (event: React.FormEvent<HTMLFormElement>): void => {
      event.preventDefault();
      void runAnalyze();
    },
    [runAnalyze],
  );

  return (
    <section aria-labelledby="submit-symptom-heading">
      <h2 id="submit-symptom-heading">Describe the vehicle symptom</h2>

      <form onSubmit={handleSubmit}>
        <label htmlFor="symptom-text">Symptom description</label>
        <textarea
          id="symptom-text"
          name="symptomText"
          value={symptomText}
          disabled={submitting}
          aria-describedby="symptom-remaining"
          onChange={(event) => setSymptomText(event.target.value)}
          rows={5}
        />

        <p id="symptom-remaining" aria-live="polite">
          {remaining >= 0
            ? `${remaining} characters remaining`
            : `${Math.abs(remaining)} characters over the ${MAX_SYMPTOM_LENGTH} limit`}
        </p>

        <button type="submit" disabled={!canSubmit}>
          {submitting ? 'Analyzing…' : 'Submit'}
        </button>
      </form>

      {submitting && (
        <p role="status" aria-live="polite">
          Analyzing the symptom…
        </p>
      )}

      {!submitting && outcome !== undefined && (
        <OutcomeSummary outcome={outcome} onRetry={() => void runAnalyze()} />
      )}
    </section>
  );
}

/** Props for the inline placeholder summary. */
interface OutcomeSummaryProps {
  outcome: AnalyzeOutcome;
  onRetry: () => void;
}

/**
 * Minimal inline summary of the latest outcome. Rich rendering of a diagnosis is
 * deferred to the DiagnosisResultView (task 21.2); this keeps the page useful on
 * its own and exercises the per-outcome messaging each criterion requires.
 */
function OutcomeSummary({ outcome, onRetry }: OutcomeSummaryProps): ReactElement {
  switch (outcome.kind) {
    case 'DIAGNOSIS':
      return (
        <div role="region" aria-label="Diagnosis">
          <p>Probable issue: {outcome.probableIssue}</p>
          <p>Repair recommendation: {outcome.repairRecommendation}</p>
          {outcome.estimatedCost !== undefined && (
            <p>Estimated cost: {outcome.estimatedCost}</p>
          )}
          {outcome.confidenceScore !== undefined && (
            <p>Confidence score: {outcome.confidenceScore}</p>
          )}
        </div>
      );

    case 'NEEDS_MORE_DETAIL':
      // Req 8.4: show the message; the text remains in the textarea for editing.
      return (
        <p role="alert">
          {outcome.message.length > 0
            ? outcome.message
            : 'More detail is needed to diagnose this symptom. Please add to your description.'}
        </p>
      );

    case 'VALIDATION_ERROR':
      // Req 8.5: show the returned suggestion; the text is retained.
      return (
        <p role="alert">
          {outcome.suggestion.length > 0
            ? outcome.suggestion
            : 'The symptom description could not be accepted. Please revise it and try again.'}
        </p>
      );

    case 'SERVICE_UNAVAILABLE':
      // Req 8.6: temporarily-unavailable message plus a retry control.
      return (
        <div role="alert">
          <p>
            {outcome.suggestion.length > 0
              ? outcome.suggestion
              : 'The diagnosis service is temporarily unavailable. Please try again shortly.'}
          </p>
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        </div>
      );

    case 'TIMEOUT':
      // Req 8.8: did-not-complete message, retry, and the text is retained.
      return (
        <div role="alert">
          <p>The request did not complete. Please try again.</p>
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        </div>
      );

    case 'NETWORK_ERROR':
      return (
        <div role="alert">
          <p>We could not reach the diagnosis service. Please try again.</p>
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        </div>
      );

    case 'UNAUTHORIZED':
      // Req 10.1: the parent is notified via onUnauthorized; show a brief notice.
      return <p role="alert">Your session has expired. Please sign in again.</p>;

    case 'FORBIDDEN':
      return (
        <p role="alert">You are not authorized to perform this diagnosis.</p>
      );
  }
}
