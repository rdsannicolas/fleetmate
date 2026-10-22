/**
 * Presentational renderer for a successful Diagnostic_Result (Req 8.3).
 *
 * Given an already-fetched {@link DiagnosisOutcome}, this component displays the
 * Probable_Issue, the Repair_Recommendation, the estimated cost, and the
 * confidence score. When the AI_Engine escalated the diagnosis, a clear
 * "needs human review" notice is shown.
 *
 * It is purely presentational: no data fetching, no timers, no side effects. The
 * SubmitSymptomPage (task 21.1) owns the request lifecycle and the validation
 * (422), service-unavailable (503), timeout, and error states. As a convenience
 * this view can also render a needs-more-detail message when a
 * {@link NeedsMoreDetailOutcome} is passed instead of a diagnosis.
 */
import type {
  DiagnosisOutcome,
  NeedsMoreDetailOutcome,
} from '../services/apiClient';

/**
 * Props for {@link DiagnosisResultView}.
 *
 * Accepts the full {@link DiagnosisOutcome} discriminated union member so the
 * caller can forward the API outcome directly. A {@link NeedsMoreDetailOutcome}
 * may be passed instead; the component discriminates on `kind`.
 */
export interface DiagnosisResultViewProps {
  result: DiagnosisOutcome | NeedsMoreDetailOutcome;
}

/** Placeholder shown when an optional field is absent from the diagnosis. */
const NOT_AVAILABLE = 'Not available';

/**
 * Format a confidence score for display. The API models confidence as a 0–1
 * value, so render it as a whole-number percentage (e.g. `0.89` → `89%`).
 * Values outside the 0–1 range are clamped so the display stays sensible.
 */
export function formatConfidence(score: number | undefined): string {
  if (score === undefined || !Number.isFinite(score)) {
    return NOT_AVAILABLE;
  }
  const clamped = Math.min(1, Math.max(0, score));
  return `${Math.round(clamped * 100)}%`;
}

export default function DiagnosisResultView({
  result,
}: DiagnosisResultViewProps): JSX.Element {
  if (result.kind === 'NEEDS_MORE_DETAIL') {
    return (
      <section aria-labelledby="needs-more-detail-heading">
        <h2 id="needs-more-detail-heading">More detail needed</h2>
        <p role="status">{result.message}</p>
      </section>
    );
  }

  const { probableIssue, repairRecommendation, estimatedCost, confidenceScore, escalated } =
    result;

  return (
    <section aria-labelledby="diagnosis-result-heading">
      <h2 id="diagnosis-result-heading">Diagnosis</h2>

      {escalated ? (
        <p role="alert" data-testid="escalation-notice">
          This diagnosis has been escalated and needs human review before you act on it.
        </p>
      ) : null}

      <dl>
        <div>
          <dt>Probable issue</dt>
          <dd>{probableIssue}</dd>
        </div>

        <div>
          <dt>Repair recommendation</dt>
          <dd>{repairRecommendation}</dd>
        </div>

        <div>
          <dt>Estimated cost</dt>
          <dd>{estimatedCost ?? NOT_AVAILABLE}</dd>
        </div>

        <div>
          <dt>Confidence score</dt>
          <dd>{formatConfidence(confidenceScore)}</dd>
        </div>
      </dl>
    </section>
  );
}
