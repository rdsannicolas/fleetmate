import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import type {
  DiagnosisOutcome,
  NeedsMoreDetailOutcome,
} from '../services/apiClient';
import DiagnosisResultView, { formatConfidence } from './DiagnosisResultView';

interface DiagnosisOverrides {
  probableIssue?: string;
  repairRecommendation?: string;
  estimatedCost?: string | null;
  confidenceScore?: number | null;
  escalated?: boolean;
}

function diagnosis(overrides: DiagnosisOverrides = {}): DiagnosisOutcome {
  const base: DiagnosisOutcome = {
    kind: 'DIAGNOSIS',
    sessionId: 'session-1',
    probableIssue: overrides.probableIssue ?? 'Weak Battery',
    repairRecommendation: overrides.repairRecommendation ?? 'Replace battery',
    escalated: overrides.escalated ?? false,
    timestamp: '2024-01-01T00:00:00.000Z',
  };
  // `null` means "explicitly absent"; undefined means "use the default".
  const cost = overrides.estimatedCost === undefined ? '₹4,000 - ₹8,000' : overrides.estimatedCost;
  if (cost !== null) {
    base.estimatedCost = cost;
  }
  const confidence = overrides.confidenceScore === undefined ? 0.89 : overrides.confidenceScore;
  if (confidence !== null) {
    base.confidenceScore = confidence;
  }
  return base;
}

describe('DiagnosisResultView', () => {
  it('renders the probable issue, repair recommendation, cost and confidence (Req 8.3)', () => {
    render(<DiagnosisResultView result={diagnosis()} />);

    expect(screen.getByRole('heading', { name: /diagnosis/i })).toBeInTheDocument();
    expect(screen.getByText('Weak Battery')).toBeInTheDocument();
    expect(screen.getByText('Replace battery')).toBeInTheDocument();
    expect(screen.getByText('₹4,000 - ₹8,000')).toBeInTheDocument();
    expect(screen.getByText('89%')).toBeInTheDocument();
  });

  it('shows a placeholder when optional cost and confidence are absent', () => {
    render(
      <DiagnosisResultView
        result={diagnosis({ estimatedCost: null, confidenceScore: null })}
      />,
    );

    expect(screen.getAllByText('Not available')).toHaveLength(2);
  });

  it('shows an escalation notice when the diagnosis is escalated', () => {
    render(<DiagnosisResultView result={diagnosis({ escalated: true })} />);

    expect(screen.getByTestId('escalation-notice')).toHaveTextContent(/human review/i);
  });

  it('does not show an escalation notice for a non-escalated diagnosis', () => {
    render(<DiagnosisResultView result={diagnosis({ escalated: false })} />);

    expect(screen.queryByTestId('escalation-notice')).not.toBeInTheDocument();
  });

  it('renders the needs-more-detail message when passed that outcome (Req 8.4)', () => {
    const outcome: NeedsMoreDetailOutcome = {
      kind: 'NEEDS_MORE_DETAIL',
      sessionId: 'session-1',
      message: 'Please describe when the noise occurs.',
      timestamp: '2024-01-01T00:00:00.000Z',
    };

    render(<DiagnosisResultView result={outcome} />);

    expect(screen.getByText('Please describe when the noise occurs.')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /^diagnosis$/i })).not.toBeInTheDocument();
  });

  it('formats confidence as a clamped whole-number percentage', () => {
    expect(formatConfidence(0.89)).toBe('89%');
    expect(formatConfidence(0)).toBe('0%');
    expect(formatConfidence(1)).toBe('100%');
    expect(formatConfidence(1.5)).toBe('100%');
    expect(formatConfidence(-0.2)).toBe('0%');
    expect(formatConfidence(undefined)).toBe('Not available');
  });
});
