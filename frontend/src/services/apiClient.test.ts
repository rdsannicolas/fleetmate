import { describe, it, expect, vi } from 'vitest';
import {
  type AnalyzeOutcome,
  type ApiClientDeps,
  type HistoryOutcome,
  type PreviousOutcome,
  analyze,
  getHistory,
  previous,
} from './apiClient';
import type { AuthTokens } from '../auth/tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

// Sanity tests for the API client (task 18.1). The dedicated property tests
// (18.2 bearer-to-correct-endpoint, 18.3 401 discards + re-auth, 18.4 clearing
// invariant) are separate optional tasks.

const config: RuntimeConfig = {
  apiBaseUrl: 'https://api.example.com/v1',
  cognitoDomain: 'https://fleetmate.auth.us-east-1.amazoncognito.com',
  userPoolId: 'us-east-1_abc123',
  appClientId: 'client-abc',
  redirectUri: 'https://app.example.com/callback',
  logoutUri: 'https://app.example.com/',
};

const NOW = 1_000_000;
const validTokens: AuthTokens = {
  idToken: 'id-token',
  accessToken: 'access-abc',
  expiresAt: NOW + 60_000,
};

/** Build a JSON Response stub with the given status and body. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    status,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

/** Deps with a valid token and a controllable fetch/clear. */
function depsWith(
  fetchImpl: typeof fetch,
  overrides: Partial<ApiClientDeps> = {},
): ApiClientDeps {
  return {
    fetchImpl,
    getTokensImpl: () => validTokens,
    clearTokensImpl: overrides.clearTokensImpl ?? vi.fn(),
    now: () => NOW,
  };
}

describe('analyze', () => {
  it('attaches the bearer token and posts to /diagnostics/analyze', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, {
        sessionId: 's1',
        escalated: false,
        needsMoreDetail: false,
        probableIssue: 'Weak Battery',
        repairRecommendation: 'Replace battery',
        estimatedCost: '₹4,000 - ₹8,000',
        confidenceScore: 0.89,
        timestamp: '2024-01-01T00:00:00.000Z',
      }),
    );

    const result = await analyze({ symptomText: 'car wont start' }, config, depsWith(fetchImpl as unknown as typeof fetch));

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example.com/v1/diagnostics/analyze');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer id-token');

    expect(result.kind).toBe('DIAGNOSIS');
    if (result.kind === 'DIAGNOSIS') {
      expect(result.probableIssue).toBe('Weak Battery');
      expect(result.confidenceScore).toBe(0.89);
    }
  });

  it('maps needs-more-detail 200 responses', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, {
        sessionId: 's1',
        escalated: false,
        needsMoreDetail: true,
        message: 'Please describe the noise',
        timestamp: '2024-01-01T00:00:00.000Z',
      }),
    );
    const result = await analyze({ symptomText: 'noise' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result.kind).toBe('NEEDS_MORE_DETAIL');
  });

  it('maps 422 to a validation error with code + suggestion', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(422, { code: 'TOO_SHORT', suggestion: 'Add more detail', timestamp: 't' }),
    );
    const result = await analyze({ symptomText: 'x' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<AnalyzeOutcome>({
      kind: 'VALIDATION_ERROR',
      code: 'TOO_SHORT',
      suggestion: 'Add more detail',
    });
  });

  it('maps 503 to service unavailable carrying the suggestion', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(503, { suggestion: 'Try again shortly', timestamp: 't' }),
    );
    const result = await analyze({ symptomText: 'x' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<AnalyzeOutcome>({ kind: 'SERVICE_UNAVAILABLE', suggestion: 'Try again shortly' });
  });

  it('on 401 discards tokens and returns UNAUTHORIZED', async () => {
    const clearTokensImpl = vi.fn();
    const fetchImpl = vi.fn(async () => jsonResponse(401, {}));
    const result = await analyze(
      { symptomText: 'x' },
      config,
      depsWith(fetchImpl as unknown as typeof fetch, { clearTokensImpl }),
    );
    expect(result).toEqual<AnalyzeOutcome>({ kind: 'UNAUTHORIZED' });
    expect(clearTokensImpl).toHaveBeenCalledTimes(1);
  });

  it('does not call the API when no valid token is held, and clears tokens', async () => {
    const clearTokensImpl = vi.fn();
    const fetchImpl = vi.fn();
    const result = await analyze({ symptomText: 'x' }, config, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      getTokensImpl: () => null,
      clearTokensImpl,
      now: () => NOW,
    });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(result).toEqual<AnalyzeOutcome>({ kind: 'UNAUTHORIZED' });
    expect(clearTokensImpl).toHaveBeenCalledTimes(1);
  });

  it('treats an expired token as unauthenticated without calling the API', async () => {
    const fetchImpl = vi.fn();
    const result = await analyze({ symptomText: 'x' }, config, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      getTokensImpl: () => ({ ...validTokens, expiresAt: NOW }),
      clearTokensImpl: vi.fn(),
      now: () => NOW,
    });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(result.kind).toBe('UNAUTHORIZED');
  });

  it('maps an aborted request to TIMEOUT', async () => {
    const fetchImpl = vi.fn((_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          const err = new Error('aborted');
          err.name = 'AbortError';
          reject(err);
        });
      }),
    );
    vi.useFakeTimers();
    try {
      const pending = analyze(
        { symptomText: 'x' },
        config,
        depsWith(fetchImpl as unknown as typeof fetch),
        50,
      );
      await vi.advanceTimersByTimeAsync(50);
      const result = await pending;
      expect(result).toEqual<AnalyzeOutcome>({ kind: 'TIMEOUT' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('maps a fetch rejection to NETWORK_ERROR', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('network down');
    });
    const result = await analyze({ symptomText: 'x' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<AnalyzeOutcome>({ kind: 'NETWORK_ERROR' });
  });
});

describe('getHistory', () => {
  it('requests /diagnostics/history with sessionId and bearer token', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, {
        records: [{ recordId: 'r1', sessionId: 's1', createdAt: '2024-01-01T00:00:00.000Z' }],
        nextPageToken: 'tok2',
        timestamp: 't',
      }),
    );
    const result = await getHistory({ sessionId: 's1' }, config, depsWith(fetchImpl as unknown as typeof fetch));

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example.com/v1/diagnostics/history?sessionId=s1');
    expect(init.method).toBe('GET');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer id-token');

    expect(result.kind).toBe('RECORDS');
    if (result.kind === 'RECORDS') {
      expect(result.records).toHaveLength(1);
      expect(result.nextPageToken).toBe('tok2');
    }
  });

  it('maps an empty record set to EMPTY', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(200, { records: [], timestamp: 't' }));
    const result = await getHistory({ sessionId: 's1' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result.kind).toBe('EMPTY');
  });

  it('maps 403 to FORBIDDEN', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(403, {}));
    const result = await getHistory({ sessionId: 's1' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<HistoryOutcome>({ kind: 'FORBIDDEN' });
  });

  it('includes pageToken in the query when provided', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(200, { records: [], timestamp: 't' }));
    await getHistory({ sessionId: 's1', pageToken: 'tok2' }, config, depsWith(fetchImpl as unknown as typeof fetch));
    const [url] = fetchImpl.mock.calls[0] as unknown as [string];
    expect(url).toContain('sessionId=s1');
    expect(url).toContain('pageToken=tok2');
  });
});

describe('previous', () => {
  it('requests /diagnostics/previous with currentSessionId and bearer token, returning a normalized PRIOR_RECORD', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, {
        record: {
          recordId: 'r1',
          sessionId: 's0',
          createdAt: '2024-01-01T00:00:00.000Z',
          symptomDescription: 'engine knocking',
          result: {
            probableIssue: 'Worn bearings',
            repairRecommendation: 'Inspect and replace bearings',
            estimatedCost: '₹10,000 - ₹20,000',
            confidenceScore: 0.82,
          },
        },
        timestamp: 't',
      }),
    );

    const result = await previous('s1', config, depsWith(fetchImpl as unknown as typeof fetch));

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.example.com/v1/diagnostics/previous?currentSessionId=s1');
    expect(init.method).toBe('GET');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer id-token');

    expect(result.kind).toBe('PRIOR_RECORD');
    if (result.kind === 'PRIOR_RECORD') {
      // Flattened the same way history records are normalized.
      expect(result.record.symptomText).toBe('engine knocking');
      expect(result.record.probableIssue).toBe('Worn bearings');
      expect(result.record.repairRecommendation).toBe('Inspect and replace bearings');
      expect(result.record.estimatedCost).toBe('₹10,000 - ₹20,000');
      expect(result.record.confidenceScore).toBe(0.82);
      expect(result.timestamp).toBe('t');
    }
  });

  it('maps a null record to NO_PRIOR', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(200, { record: null, timestamp: 't' }));
    const result = await previous('s1', config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<PreviousOutcome>({ kind: 'NO_PRIOR', timestamp: 't' });
  });

  it('on 401 discards tokens and returns UNAUTHORIZED', async () => {
    const clearTokensImpl = vi.fn();
    const fetchImpl = vi.fn(async () => jsonResponse(401, {}));
    const result = await previous(
      's1',
      config,
      depsWith(fetchImpl as unknown as typeof fetch, { clearTokensImpl }),
    );
    expect(result).toEqual<PreviousOutcome>({ kind: 'UNAUTHORIZED' });
    expect(clearTokensImpl).toHaveBeenCalledTimes(1);
  });

  it('maps a 503 to a retryable PREVIOUS_ERROR carrying the suggestion', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(503, { suggestion: 'Try again shortly', timestamp: 't' }),
    );
    const result = await previous('s1', config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result).toEqual<PreviousOutcome>({
      kind: 'PREVIOUS_ERROR',
      message: 'Try again shortly',
      retry: true,
    });
  });

  it('maps a fetch rejection to a retryable PREVIOUS_ERROR', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('network down');
    });
    const result = await previous('s1', config, depsWith(fetchImpl as unknown as typeof fetch));
    expect(result.kind).toBe('PREVIOUS_ERROR');
    if (result.kind === 'PREVIOUS_ERROR') {
      expect(result.retry).toBe(true);
      expect(result.message.length).toBeGreaterThan(0);
    }
  });

  it('maps a timeout (aborted request) to a retryable PREVIOUS_ERROR', async () => {
    const fetchImpl = vi.fn((_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          const err = new Error('aborted');
          err.name = 'AbortError';
          reject(err);
        });
      }),
    );
    vi.useFakeTimers();
    try {
      const pending = previous('s1', config, depsWith(fetchImpl as unknown as typeof fetch), 50);
      await vi.advanceTimersByTimeAsync(50);
      const result = await pending;
      expect(result).toEqual<PreviousOutcome>({
        kind: 'PREVIOUS_ERROR',
        message: 'Could not retrieve your previous interaction.',
        retry: true,
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
