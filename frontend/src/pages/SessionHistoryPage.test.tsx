import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import SessionHistoryPage, {
  sortRecordsAscending,
  type GetHistoryFn,
} from './SessionHistoryPage';
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

function rec(partial: Partial<InteractionRecord> & { recordId: string; createdAt: string }): InteractionRecord {
  return { sessionId: 's1', ...partial };
}

describe('sortRecordsAscending', () => {
  it('orders ascending by createdAt, tie-broken by recordId, without mutating input', () => {
    const input = [
      rec({ recordId: 'b', createdAt: '2024-01-01T00:00:00.000Z' }),
      rec({ recordId: 'a', createdAt: '2024-01-01T00:00:00.000Z' }),
      rec({ recordId: 'c', createdAt: '2023-12-31T23:59:59.000Z' }),
    ];
    const snapshot = [...input];
    const sorted = sortRecordsAscending(input);

    expect(sorted.map((r) => r.recordId)).toEqual(['c', 'a', 'b']);
    expect(input).toEqual(snapshot); // input untouched
  });
});

describe('SessionHistoryPage', () => {
  it('renders records in ascending order on open (Req 9.1, 9.2)', async () => {
    const getHistoryFn: GetHistoryFn = vi.fn().mockResolvedValue({
      kind: 'RECORDS',
      records: [
        rec({ recordId: 'r2', createdAt: '2024-01-02T00:00:00.000Z', symptomText: 'B', probableIssue: 'Issue B' }),
        rec({ recordId: 'r1', createdAt: '2024-01-01T00:00:00.000Z', symptomText: 'A', probableIssue: 'Issue A' }),
      ],
      timestamp: '2024-01-03T00:00:00.000Z',
    });

    render(<SessionHistoryPage sessionId="s1" config={config} getHistoryFn={getHistoryFn} />);

    await waitFor(() => expect(screen.getByText(/Issue A/)).toBeInTheDocument());
    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('Issue A');
    expect(items[1]).toHaveTextContent('Issue B');
  });

  it('shows the empty message when there are no records (Req 9.3)', async () => {
    const getHistoryFn: GetHistoryFn = vi.fn().mockResolvedValue({
      kind: 'EMPTY',
      timestamp: '2024-01-03T00:00:00.000Z',
    });

    render(<SessionHistoryPage sessionId="s1" config={config} getHistoryFn={getHistoryFn} />);

    await waitFor(() =>
      expect(screen.getByText(/no past interactions/i)).toBeInTheDocument(),
    );
  });

  it('shows a not-authorized message and retains records on 403 (Req 9.4)', async () => {
    const getHistoryFn = vi
      .fn<GetHistoryFn>()
      .mockResolvedValueOnce({
        kind: 'RECORDS',
        records: [rec({ recordId: 'r1', createdAt: '2024-01-01T00:00:00.000Z', probableIssue: 'Kept' })],
        nextPageToken: 'tok',
        timestamp: '2024-01-03T00:00:00.000Z',
      })
      .mockResolvedValueOnce({ kind: 'FORBIDDEN' });

    render(<SessionHistoryPage sessionId="s1" config={config} getHistoryFn={getHistoryFn} />);

    await waitFor(() => expect(screen.getByText(/Kept/)).toBeInTheDocument());
    screen.getByRole('button', { name: /load more/i }).click();

    await waitFor(() =>
      expect(screen.getByText(/not authorized to view this session history/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Kept/)).toBeInTheDocument(); // retained
  });

  it('caps retry at 3 attempts on repeated errors (Req 9.5)', async () => {
    const getHistoryFn: GetHistoryFn = vi.fn().mockResolvedValue({ kind: 'TIMEOUT' });

    render(<SessionHistoryPage sessionId="s1" config={config} getHistoryFn={getHistoryFn} />);

    const retry = await screen.findByRole('button', { name: /retry/i });
    retry.click(); // attempt 2
    await waitFor(() => expect(getHistoryFn).toHaveBeenCalledTimes(2));
    screen.getByRole('button', { name: /retry/i }).click(); // attempt 3
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /retry/i })).toBeDisabled(),
    );
    expect(getHistoryFn).toHaveBeenCalledTimes(3);
  });

  it('signals re-auth on UNAUTHORIZED (Req 10.1)', async () => {
    const getHistoryFn: GetHistoryFn = vi.fn().mockResolvedValue({ kind: 'UNAUTHORIZED' });
    const onUnauthorized = vi.fn();

    render(
      <SessionHistoryPage
        sessionId="s1"
        config={config}
        getHistoryFn={getHistoryFn}
        onUnauthorized={onUnauthorized}
      />,
    );

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
  });
});
