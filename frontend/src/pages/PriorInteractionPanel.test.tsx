import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import PriorInteractionPanel, { type PreviousFn } from './PriorInteractionPanel';
import type { RuntimeConfig } from '../config/runtimeConfig';
import type { InteractionRecord } from '../services/apiClient';

const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.test/v1',
  cognitoDomain: 'https://auth.example.test',
  userPoolId: 'pool',
  appClientId: 'client',
  redirectUri: 'https://app.example.test/callback',
  logoutUri: 'https://app.example.test/',
};

const priorRecord: InteractionRecord = {
  recordId: 'r1',
  sessionId: 's0',
  createdAt: '2024-01-01T00:00:00.000Z',
  symptomText: 'engine knocking',
  probableIssue: 'Worn bearings',
  repairRecommendation: 'Inspect bearings',
  estimatedCost: '₹10,000 - ₹20,000',
  confidenceScore: 0.82,
};

describe('PriorInteractionPanel', () => {
  it('renders the prior symptom and diagnosis fields on PRIOR_RECORD (Req 13.2, 13.7)', async () => {
    const previousFn = vi.fn<PreviousFn>().mockResolvedValue({
      kind: 'PRIOR_RECORD',
      record: priorRecord,
      timestamp: 't',
    });

    render(
      <PriorInteractionPanel config={config} currentSessionId="s1" previousFn={previousFn} />,
    );

    await waitFor(() =>
      expect(screen.getByText(/engine knocking/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Worn bearings/)).toBeInTheDocument();
    expect(screen.getByText(/Inspect bearings/)).toBeInTheDocument();
    // Component renders Math.round(confidenceScore * 100) + '%'.
    expect(screen.getByText(/82%/)).toBeInTheDocument();
  });

  it('renders the no-previous-interaction empty state on NO_PRIOR (Req 13.8)', async () => {
    const previousFn = vi.fn<PreviousFn>().mockResolvedValue({
      kind: 'NO_PRIOR',
      timestamp: 't',
    });

    render(
      <PriorInteractionPanel config={config} currentSessionId="s1" previousFn={previousFn} />,
    );

    await waitFor(() =>
      expect(screen.getByText(/no previous interaction/i)).toBeInTheDocument(),
    );
  });

  it('invokes onUnauthorized to force re-auth on UNAUTHORIZED (Req 13.9)', async () => {
    const previousFn = vi.fn<PreviousFn>().mockResolvedValue({ kind: 'UNAUTHORIZED' });
    const onUnauthorized = vi.fn();

    render(
      <PriorInteractionPanel
        config={config}
        currentSessionId="s1"
        previousFn={previousFn}
        onUnauthorized={onUnauthorized}
      />,
    );

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
  });

  it('shows the error with a Retry control that re-issues the request (Req 13.10)', async () => {
    const previousFn = vi
      .fn<PreviousFn>()
      .mockResolvedValueOnce({
        kind: 'PREVIOUS_ERROR',
        message: 'Could not retrieve your previous interaction.',
        retry: true,
      })
      .mockResolvedValueOnce({
        kind: 'PRIOR_RECORD',
        record: priorRecord,
        timestamp: 't',
      });

    render(
      <PriorInteractionPanel config={config} currentSessionId="s1" previousFn={previousFn} />,
    );

    await waitFor(() =>
      expect(
        screen.getByText(/could not retrieve your previous interaction/i),
      ).toBeInTheDocument(),
    );
    const retry = screen.getByRole('button', { name: /retry/i });
    expect(retry).toBeInTheDocument();

    fireEvent.click(retry);

    await waitFor(() =>
      expect(screen.getByText(/engine knocking/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Worn bearings/)).toBeInTheDocument();
    expect(previousFn).toHaveBeenCalledTimes(2);
  });
});
