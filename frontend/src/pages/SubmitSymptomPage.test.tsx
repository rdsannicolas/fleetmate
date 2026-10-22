import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SubmitSymptomPage, {
  isSubmittable,
  MAX_SYMPTOM_LENGTH,
  type AnalyzeFn,
} from './SubmitSymptomPage';
import type { RuntimeConfig } from '../config/runtimeConfig';
import type { AnalyzeOutcome } from '../services/apiClient';

// Minimal non-secret config; the analyze function is injected so no field is
// actually dereferenced by the network layer in these tests.
const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.test/v1',
  cognitoDomain: 'https://auth.example.test',
  userPoolId: 'pool',
  appClientId: 'client',
  redirectUri: 'https://app.example.test/callback',
  logoutUri: 'https://app.example.test/logout',
};

describe('isSubmittable (Req 8.1)', () => {
  it('rejects empty and whitespace-only input', () => {
    expect(isSubmittable('')).toBe(false);
    expect(isSubmittable('   \n\t')).toBe(false);
  });

  it('accepts 1 to MAX characters with non-whitespace content', () => {
    expect(isSubmittable('a')).toBe(true);
    expect(isSubmittable('x'.repeat(MAX_SYMPTOM_LENGTH))).toBe(true);
  });

  it('rejects input longer than the maximum', () => {
    expect(isSubmittable('x'.repeat(MAX_SYMPTOM_LENGTH + 1))).toBe(false);
  });
});

describe('SubmitSymptomPage', () => {
  it('keeps submit disabled until the input is valid (Req 8.1)', async () => {
    const user = userEvent.setup();
    const analyzeFn: AnalyzeFn = vi.fn();
    render(<SubmitSymptomPage config={config} analyzeFn={analyzeFn} />);

    const submit = screen.getByRole('button', { name: /submit/i });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/symptom description/i), 'engine stalls');
    expect(submit).toBeEnabled();
  });

  it('submits and surfaces a diagnosis outcome, carrying the sessionId (Req 8.2)', async () => {
    const user = userEvent.setup();
    const outcome: AnalyzeOutcome = {
      kind: 'DIAGNOSIS',
      sessionId: 'session-123',
      probableIssue: 'Weak Battery',
      repairRecommendation: 'Replace battery',
      escalated: false,
      timestamp: '2024-01-01T00:00:00.000Z',
    };
    const analyzeFn = vi.fn<AnalyzeFn>().mockResolvedValue(outcome);
    const onResult = vi.fn();
    render(
      <SubmitSymptomPage config={config} analyzeFn={analyzeFn} onResult={onResult} />,
    );

    await user.type(screen.getByLabelText(/symptom description/i), 'engine stalls');
    await user.click(screen.getByRole('button', { name: /submit/i }));

    await waitFor(() => expect(onResult).toHaveBeenCalledWith(outcome));
    expect(screen.getByText(/weak battery/i)).toBeInTheDocument();

    // Follow-up submission reuses the returned sessionId (Req 8.2).
    await user.type(screen.getByLabelText(/symptom description/i), ' and clicks');
    await user.click(screen.getByRole('button', { name: /submit/i }));
    await waitFor(() => expect(analyzeFn).toHaveBeenCalledTimes(2));
    expect(analyzeFn.mock.calls[1]?.[0].sessionId).toBe('session-123');
  });

  it('shows the timeout message and retains text on TIMEOUT (Req 8.8)', async () => {
    const user = userEvent.setup();
    const analyzeFn = vi.fn<AnalyzeFn>().mockResolvedValue({ kind: 'TIMEOUT' });
    render(<SubmitSymptomPage config={config} analyzeFn={analyzeFn} />);

    const textarea = screen.getByLabelText(/symptom description/i);
    await user.type(textarea, 'intermittent fault');
    await user.click(screen.getByRole('button', { name: /submit/i }));

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument();
    expect(textarea).toHaveValue('intermittent fault');
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('signals re-auth on UNAUTHORIZED (Req 10.1)', async () => {
    const user = userEvent.setup();
    const analyzeFn = vi.fn<AnalyzeFn>().mockResolvedValue({ kind: 'UNAUTHORIZED' });
    const onUnauthorized = vi.fn();
    render(
      <SubmitSymptomPage
        config={config}
        analyzeFn={analyzeFn}
        onUnauthorized={onUnauthorized}
      />,
    );

    await user.type(screen.getByLabelText(/symptom description/i), 'no start');
    await user.click(screen.getByRole('button', { name: /submit/i }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
  });
});
