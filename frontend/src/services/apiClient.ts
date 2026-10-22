/**
 * Diagnostic_API client for the FleetMate SPA.
 *
 * Builds authenticated requests to the existing Diagnostic_API (deployed under
 * `/v1`) and maps every HTTP outcome to a typed, discriminated union so the UI
 * can `switch` on the result instead of catching exceptions. Nothing is thrown
 * across this boundary and no internal/provider error detail is surfaced.
 *
 * Responsibilities and the acceptance criteria they uphold:
 * - Attach the Access_Token as `Authorization: Bearer <accessToken>` on every
 *   request, read from the token store. If no valid token is held at `now`,
 *   the request is treated as unauthenticated (re-auth required) and the API is
 *   never called (Req 6.3, Req 6.5).
 * - `analyze(...)` → `POST {apiBaseUrl}/diagnostics/analyze` (Req 8.2) with a
 *   30-second request budget (Req 8.8).
 * - `getHistory(...)` → `GET {apiBaseUrl}/diagnostics/history` (Req 9.1) with a
 *   10-second request budget (Req 9.5).
 * - Status → outcome mapping: 200 success; 401 discards tokens synchronously
 *   (≤1s) and signals re-auth (Req 10.1); 403 → forbidden (Req 9.4); 422 →
 *   validation error carrying `{ code, suggestion }` (Req 8.5); 503 → service
 *   unavailable carrying `suggestion` (Req 8.6); abort/network → timeout or
 *   network error.
 *
 * `fetch`, the token source, and the clock are injectable (with production
 * defaults) so tests neither hit the network nor depend on wall-clock time.
 * This module holds no secrets; it only reads the Access_Token issued to the
 * browser and forwards it as a bearer credential.
 */

import { clearTokens, getTokens, isTokenValid, type AuthTokens } from '../auth/tokenStore';
import type { RuntimeConfig } from '../config/runtimeConfig';

/** Per-request time budgets (ms). Overridable per call for testing. */
export const ANALYZE_TIMEOUT_MS = 30_000; // Req 8.8
export const HISTORY_TIMEOUT_MS = 10_000; // Req 9.5
export const PREVIOUS_TIMEOUT_MS = 10_000; // Req 13.10

/** Error codes the client distinguishes, modelled as a string-literal union. */
export type ApiErrorCode =
  | 'UNAUTHORIZED' // 401 (Req 10.1)
  | 'FORBIDDEN' // 403 (Req 9.4)
  | 'VALIDATION_ERROR' // 422 (Req 8.5)
  | 'SERVICE_UNAVAILABLE' // 503 (Req 8.6)
  | 'TIMEOUT' // no response within the request budget
  | 'NETWORK_ERROR'; // fetch rejected, or an unexpected/unparseable response

// ---------------------------------------------------------------------------
// Response body shapes (as returned by the existing Diagnostic_API).
// ---------------------------------------------------------------------------

/** 200 body from `POST /diagnostics/analyze`. */
export interface AnalyzeResponseBody {
  sessionId: string;
  escalated: boolean;
  needsMoreDetail: boolean;
  probableIssue?: string;
  repairRecommendation?: string;
  estimatedCost?: string;
  confidenceScore?: number;
  message?: string;
  timestamp: string;
}

/**
 * A single Interaction_Record as presented to the UI (a FLAT view-model).
 *
 * The API returns each record with the symptom under `symptomDescription` and
 * the diagnosis nested under a `result` object; {@link normalizeHistoryRecord}
 * flattens that wire shape into the fields below so the history page can read
 * them directly.
 */
export interface InteractionRecord {
  recordId: string;
  sessionId: string;
  createdAt: string;
  symptomText?: string;
  probableIssue?: string;
  repairRecommendation?: string;
  estimatedCost?: string;
  confidenceScore?: number;
  escalated?: boolean;
  needsMoreDetail?: boolean;
  message?: string;
}

/** 200 body from `GET /diagnostics/history`. */
export interface HistoryResponseBody {
  records: InteractionRecord[];
  nextPageToken?: string;
  timestamp: string;
}

/**
 * 200 body from `GET /diagnostics/previous`. The `record` is the
 * Most_Recent_Prior_Interaction in the same wire shape used by history, or
 * `null` when the Customer owns no Prior_Interaction (Req 13.2, 13.5).
 */
export interface PreviousResponseBody {
  record: InteractionRecord | null;
  timestamp: string;
}

// ---------------------------------------------------------------------------
// Inputs.
// ---------------------------------------------------------------------------

/** Partial vehicle context; only provided fields are sent. */
export interface VehicleInput {
  make?: string;
  model?: string;
  year?: number;
}

/** Input to {@link analyze}. */
export interface AnalyzeInput {
  symptomText: string;
  sessionId?: string;
  vehicle?: VehicleInput;
}

/** Input to {@link getHistory}. */
export interface HistoryInput {
  sessionId: string;
  pageToken?: string;
}

// ---------------------------------------------------------------------------
// Outcomes (discriminated unions keyed on `kind`).
// ---------------------------------------------------------------------------

/** A diagnosis with a probable issue and repair recommendation (Req 8.3). */
export interface DiagnosisOutcome {
  kind: 'DIAGNOSIS';
  sessionId: string;
  probableIssue: string;
  repairRecommendation: string;
  estimatedCost?: string;
  confidenceScore?: number;
  escalated: boolean;
  timestamp: string;
}

/** The API needs more symptom detail before diagnosing (Req 8.4). */
export interface NeedsMoreDetailOutcome {
  kind: 'NEEDS_MORE_DETAIL';
  sessionId: string;
  message: string;
  timestamp: string;
}

/** 422: the submission was rejected; carries the returned suggestion (Req 8.5). */
export interface ValidationErrorOutcome {
  kind: 'VALIDATION_ERROR';
  code: string;
  suggestion: string;
}

/** 503: the diagnosis service is temporarily unavailable (Req 8.6). */
export interface ServiceUnavailableOutcome {
  kind: 'SERVICE_UNAVAILABLE';
  suggestion: string;
}

/** 403: the caller is not authorized for this resource (Req 9.4). */
export interface ForbiddenOutcome {
  kind: 'FORBIDDEN';
}

/** 401: tokens have been discarded; the caller must re-authenticate (Req 10.1). */
export interface UnauthorizedOutcome {
  kind: 'UNAUTHORIZED';
}

/** The request exceeded its time budget before a response arrived. */
export interface TimeoutOutcome {
  kind: 'TIMEOUT';
}

/** The request failed at the transport layer, or returned an unusable response. */
export interface NetworkErrorOutcome {
  kind: 'NETWORK_ERROR';
}

/** 200: a page of Interaction_Records (Req 9.2, 9.6). */
export interface RecordsOutcome {
  kind: 'RECORDS';
  records: InteractionRecord[];
  nextPageToken?: string;
  timestamp: string;
}

/** 200: the Session exists but has no Interaction_Records (Req 9.3). */
export interface EmptyHistoryOutcome {
  kind: 'EMPTY';
  timestamp: string;
}

/** 200: the Most_Recent_Prior_Interaction, normalized to the flat view-model (Req 13.2, 13.7). */
export interface PriorRecord {
  kind: 'PRIOR_RECORD';
  record: InteractionRecord;
  timestamp: string;
}

/** 200: the Customer owns no Prior_Interaction (Req 13.8). */
export interface NoPrior {
  kind: 'NO_PRIOR';
  timestamp: string;
}

/**
 * The prior-interaction request could not be completed (service/network/timeout
 * or an unexpected status). A single retryable error so the panel can offer a
 * retry control (Req 13.10). `message` is safe to display and never leaks
 * internal/provider detail.
 */
export interface PreviousError {
  kind: 'PREVIOUS_ERROR';
  message: string;
  retry: true;
}

/** Discriminated outcome of {@link analyze}. */
export type AnalyzeOutcome =
  | DiagnosisOutcome
  | NeedsMoreDetailOutcome
  | ValidationErrorOutcome
  | ServiceUnavailableOutcome
  | UnauthorizedOutcome
  | ForbiddenOutcome
  | TimeoutOutcome
  | NetworkErrorOutcome;

/** Discriminated outcome of {@link getHistory}. */
export type HistoryOutcome =
  | RecordsOutcome
  | EmptyHistoryOutcome
  | ValidationErrorOutcome
  | ServiceUnavailableOutcome
  | UnauthorizedOutcome
  | ForbiddenOutcome
  | TimeoutOutcome
  | NetworkErrorOutcome;

/**
 * Discriminated outcome of {@link previous}. Deliberately narrow: a prior
 * record, no prior record, re-auth required, or a single retryable error. Every
 * service/network/timeout failure folds into {@link PreviousError} (Req 13.10).
 */
export type PreviousOutcome =
  | PriorRecord
  | NoPrior
  | UnauthorizedOutcome
  | PreviousError;

// ---------------------------------------------------------------------------
// Dependency injection seams.
// ---------------------------------------------------------------------------

/**
 * Testing seams for the API client. All default to the real browser/token-store
 * primitives in production.
 */
export interface ApiClientDeps {
  /** Fetch implementation. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  /** Reads the currently stored tokens. Defaults to the token store. */
  getTokensImpl?: () => AuthTokens | null;
  /** Discards stored tokens (used on 401). Defaults to the token store. */
  clearTokensImpl?: () => void;
  /** Clock used to evaluate token validity. Defaults to `Date.now`. */
  now?: () => number;
}

interface ResolvedDeps {
  fetchImpl: typeof fetch;
  getTokensImpl: () => AuthTokens | null;
  clearTokensImpl: () => void;
  now: () => number;
}

function resolveDeps(deps?: ApiClientDeps): ResolvedDeps {
  return {
    fetchImpl: deps?.fetchImpl ?? fetch.bind(globalThis),
    getTokensImpl: deps?.getTokensImpl ?? getTokens,
    clearTokensImpl: deps?.clearTokensImpl ?? clearTokens,
    now: deps?.now ?? Date.now,
  };
}

// ---------------------------------------------------------------------------
// Request helpers.
// ---------------------------------------------------------------------------

/** Join a base URL and a path, tolerating a trailing slash on the base. */
function joinUrl(baseUrl: string, path: string): string {
  const trimmedBase = baseUrl.replace(/\/+$/, '');
  const trimmedPath = path.replace(/^\/+/, '');
  return `${trimmedBase}/${trimmedPath}`;
}

/** Low-level HTTP result the mappers consume. Never thrown. */
type RawResult =
  | { kind: 'RESPONSE'; status: number; body: unknown }
  | { kind: 'UNAUTHENTICATED' } // no valid token held; API was not called
  | { kind: 'TIMEOUT' }
  | { kind: 'NETWORK_ERROR' };

interface RequestOptions {
  method: 'GET' | 'POST';
  url: string;
  body?: unknown;
  timeoutMs: number;
}

/**
 * Perform a single authenticated request. Reads the Access_Token from the store;
 * if no valid token is held at `now`, returns `UNAUTHENTICATED` without touching
 * the network (Req 6.3/6.5). Otherwise attaches the bearer credential, enforces
 * the per-request timeout with an AbortController, and classifies the result.
 */
async function request(
  options: RequestOptions,
  deps: ResolvedDeps,
): Promise<RawResult> {
  const tokens = deps.getTokensImpl();
  if (!isTokenValid(tokens, deps.now()) || tokens === null) {
    return { kind: 'UNAUTHENTICATED' };
  }

  let timedOut = false;
  const controller = new AbortController();
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs);

  const headers: Record<string, string> = {
    Authorization: `Bearer ${tokens.idToken}`,
    Accept: 'application/json',
  };
  const init: RequestInit = {
    method: options.method,
    headers,
    signal: controller.signal,
  };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }

  try {
    const response = await deps.fetchImpl(options.url, init);
    const body = await readBody(response);
    return { kind: 'RESPONSE', status: response.status, body };
  } catch (error) {
    if (timedOut || (error instanceof Error && error.name === 'AbortError')) {
      return { kind: 'TIMEOUT' };
    }
    return { kind: 'NETWORK_ERROR' };
  } finally {
    clearTimeout(timer);
  }
}

/** Best-effort JSON parse; a missing/invalid body yields `null` (never throws). */
async function readBody(response: Response): Promise<unknown> {
  try {
    const text = await response.text();
    if (text.length === 0) {
      return null;
    }
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function asRecord(body: unknown): Record<string, unknown> {
  return typeof body === 'object' && body !== null
    ? (body as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * On a 401, discard the stored tokens synchronously so no later request carries
 * a token (Req 10.1 / 10.6). `clearTokens` is best-effort and never throws, so
 * this returns well within the 1-second budget.
 */
function discardOnUnauthorized(deps: ResolvedDeps): UnauthorizedOutcome {
  deps.clearTokensImpl();
  return { kind: 'UNAUTHORIZED' };
}

/** Map a 422 body to a validation outcome, defaulting missing fields safely. */
function toValidationError(body: unknown): ValidationErrorOutcome {
  const record = asRecord(body);
  return {
    kind: 'VALIDATION_ERROR',
    code: asString(record.code) ?? 'VALIDATION_ERROR',
    suggestion: asString(record.suggestion) ?? '',
  };
}

/** Map a 503 body to a service-unavailable outcome. */
function toServiceUnavailable(body: unknown): ServiceUnavailableOutcome {
  const record = asRecord(body);
  return { kind: 'SERVICE_UNAVAILABLE', suggestion: asString(record.suggestion) ?? '' };
}

// ---------------------------------------------------------------------------
// Public API.
// ---------------------------------------------------------------------------

/**
 * Submit a Symptom_Description for diagnosis (Req 8.2). Returns a typed
 * {@link AnalyzeOutcome}; never throws across the boundary.
 */
export async function analyze(
  input: AnalyzeInput,
  config: RuntimeConfig,
  deps?: ApiClientDeps,
  timeoutMs: number = ANALYZE_TIMEOUT_MS,
): Promise<AnalyzeOutcome> {
  const resolved = resolveDeps(deps);

  const body: Record<string, unknown> = { symptomText: input.symptomText };
  if (input.sessionId !== undefined) {
    body.sessionId = input.sessionId;
  }
  if (input.vehicle !== undefined) {
    body.vehicle = input.vehicle;
  }

  const result = await request(
    {
      method: 'POST',
      url: joinUrl(config.apiBaseUrl, '/diagnostics/analyze'),
      body,
      timeoutMs,
    },
    resolved,
  );

  switch (result.kind) {
    case 'UNAUTHENTICATED':
      // No valid token was held, so the API was not called. Treat exactly like
      // a 401: discard anything stored and signal re-auth (Req 6.3, 10.1).
      return discardOnUnauthorized(resolved);
    case 'TIMEOUT':
      return { kind: 'TIMEOUT' };
    case 'NETWORK_ERROR':
      return { kind: 'NETWORK_ERROR' };
    case 'RESPONSE':
      return mapAnalyzeResponse(result.status, result.body, resolved);
  }
}

function mapAnalyzeResponse(
  status: number,
  body: unknown,
  deps: ResolvedDeps,
): AnalyzeOutcome {
  if (status === 401) {
    return discardOnUnauthorized(deps);
  }
  if (status === 403) {
    return { kind: 'FORBIDDEN' };
  }
  if (status === 422) {
    return toValidationError(body);
  }
  if (status === 503) {
    return toServiceUnavailable(body);
  }
  if (status === 200) {
    const record = asRecord(body);
    const sessionId = asString(record.sessionId) ?? '';
    const timestamp = asString(record.timestamp) ?? '';
    const probableIssue = asString(record.probableIssue);
    const repairRecommendation = asString(record.repairRecommendation);

    // Needs-more-detail takes precedence: no probable issue is expected yet.
    if (record.needsMoreDetail === true || probableIssue === undefined || repairRecommendation === undefined) {
      return {
        kind: 'NEEDS_MORE_DETAIL',
        sessionId,
        message: asString(record.message) ?? '',
        timestamp,
      };
    }

    const diagnosis: DiagnosisOutcome = {
      kind: 'DIAGNOSIS',
      sessionId,
      probableIssue,
      repairRecommendation,
      escalated: record.escalated === true,
      timestamp,
    };
    const estimatedCost = asString(record.estimatedCost);
    if (estimatedCost !== undefined) {
      diagnosis.estimatedCost = estimatedCost;
    }
    const confidenceScore = asNumber(record.confidenceScore);
    if (confidenceScore !== undefined) {
      diagnosis.confidenceScore = confidenceScore;
    }
    return diagnosis;
  }
  // Any other status (unexpected) is treated as a transport-level failure so the
  // UI can offer a retry rather than crashing on an unmodeled status.
  return { kind: 'NETWORK_ERROR' };
}

/**
 * Retrieve a page of Session history (Req 9.1). Returns a typed
 * {@link HistoryOutcome}; never throws across the boundary.
 */
export async function getHistory(
  input: HistoryInput,
  config: RuntimeConfig,
  deps?: ApiClientDeps,
  timeoutMs: number = HISTORY_TIMEOUT_MS,
): Promise<HistoryOutcome> {
  const resolved = resolveDeps(deps);

  const query = new URLSearchParams({ sessionId: input.sessionId });
  if (input.pageToken !== undefined) {
    query.set('pageToken', input.pageToken);
  }

  const result = await request(
    {
      method: 'GET',
      url: `${joinUrl(config.apiBaseUrl, '/diagnostics/history')}?${query.toString()}`,
      timeoutMs,
    },
    resolved,
  );

  switch (result.kind) {
    case 'UNAUTHENTICATED':
      return discardOnUnauthorized(resolved);
    case 'TIMEOUT':
      return { kind: 'TIMEOUT' };
    case 'NETWORK_ERROR':
      return { kind: 'NETWORK_ERROR' };
    case 'RESPONSE':
      return mapHistoryResponse(result.status, result.body, resolved);
  }
}

/**
 * Flatten a raw history record from the API wire shape into the UI's
 * {@link InteractionRecord}. The API returns the symptom under
 * `symptomDescription` and the diagnosis nested under a `result` object
 * ({ probableIssue?, repairRecommendation?, estimatedCost?, confidenceScore?,
 * needsMoreDetail?, message? }); the history page reads flat fields, so map them
 * here in one place. Missing fields are left `undefined` (rendered as a dash).
 */
function normalizeHistoryRecord(raw: unknown): InteractionRecord {
  const r = asRecord(raw);
  const result = asRecord(r.result);

  const record: InteractionRecord = {
    recordId: asString(r.recordId) ?? '',
    sessionId: asString(r.sessionId) ?? '',
    createdAt: asString(r.createdAt) ?? '',
  };

  // Symptom: the API field is `symptomDescription`; tolerate `symptomText` too.
  const symptom = asString(r.symptomDescription) ?? asString(r.symptomText);
  if (symptom !== undefined) record.symptomText = symptom;

  // Diagnosis fields live under `result` (fall back to top-level, defensively).
  const probableIssue = asString(result.probableIssue) ?? asString(r.probableIssue);
  if (probableIssue !== undefined) record.probableIssue = probableIssue;

  const repair = asString(result.repairRecommendation) ?? asString(r.repairRecommendation);
  if (repair !== undefined) record.repairRecommendation = repair;

  const cost = asString(result.estimatedCost) ?? asString(r.estimatedCost);
  if (cost !== undefined) record.estimatedCost = cost;

  const confidence = asNumber(result.confidenceScore) ?? asNumber(r.confidenceScore);
  if (confidence !== undefined) record.confidenceScore = confidence;

  if (result.needsMoreDetail === true || r.needsMoreDetail === true) {
    record.needsMoreDetail = true;
  }
  const message = asString(result.message) ?? asString(r.message);
  if (message !== undefined) record.message = message;

  return record;
}

function mapHistoryResponse(
  status: number,
  body: unknown,
  deps: ResolvedDeps,
): HistoryOutcome {
  if (status === 401) {
    return discardOnUnauthorized(deps);
  }
  if (status === 403) {
    return { kind: 'FORBIDDEN' };
  }
  if (status === 422) {
    return toValidationError(body);
  }
  if (status === 503) {
    return toServiceUnavailable(body);
  }
  if (status === 200) {
    const record = asRecord(body);
    const timestamp = asString(record.timestamp) ?? '';
    const records = Array.isArray(record.records)
      ? record.records.map(normalizeHistoryRecord)
      : [];

    if (records.length === 0) {
      return { kind: 'EMPTY', timestamp };
    }

    const outcome: RecordsOutcome = { kind: 'RECORDS', records, timestamp };
    const nextPageToken = asString(record.nextPageToken);
    if (nextPageToken !== undefined && nextPageToken.length > 0) {
      outcome.nextPageToken = nextPageToken;
    }
    return outcome;
  }
  return { kind: 'NETWORK_ERROR' };
}

/** Generic, safe copy shown when the prior interaction cannot be retrieved. */
const PREVIOUS_ERROR_MESSAGE = 'Could not retrieve your previous interaction.';

/**
 * Retrieve the Customer's Most_Recent_Prior_Interaction (Req 13.7). Returns a
 * typed {@link PreviousOutcome}; never throws across the boundary. Mirrors
 * {@link getHistory}: it reuses the shared {@link request} helper (so it inherits
 * the correct ID-token bearer credential and timeout handling) and a dedicated
 * response mapper. The `currentSessionId` is sent as a query parameter so the
 * API can exclude the Current_Session when resolving the prior interaction.
 */
export async function previous(
  currentSessionId: string,
  config: RuntimeConfig,
  deps?: ApiClientDeps,
  timeoutMs: number = PREVIOUS_TIMEOUT_MS,
): Promise<PreviousOutcome> {
  const resolved = resolveDeps(deps);

  const query = new URLSearchParams({ currentSessionId });

  const result = await request(
    {
      method: 'GET',
      url: `${joinUrl(config.apiBaseUrl, '/diagnostics/previous')}?${query.toString()}`,
      timeoutMs,
    },
    resolved,
  );

  switch (result.kind) {
    case 'UNAUTHENTICATED':
      // No valid token held; treat exactly like a 401 (Req 13.9).
      return discardOnUnauthorized(resolved);
    case 'TIMEOUT':
      return { kind: 'PREVIOUS_ERROR', message: PREVIOUS_ERROR_MESSAGE, retry: true };
    case 'NETWORK_ERROR':
      return { kind: 'PREVIOUS_ERROR', message: PREVIOUS_ERROR_MESSAGE, retry: true };
    case 'RESPONSE':
      return mapPreviousResponse(result.status, result.body, resolved);
  }
}

function mapPreviousResponse(
  status: number,
  body: unknown,
  deps: ResolvedDeps,
): PreviousOutcome {
  if (status === 401) {
    // Discard tokens and signal re-auth (Req 13.9).
    return discardOnUnauthorized(deps);
  }
  if (status === 200) {
    const response = asRecord(body);
    const timestamp = asString(response.timestamp) ?? '';
    const raw = response.record;

    // A non-null object record → normalize into the shared flat view-model so
    // the panel and history render identical shapes (Req 13.2). A null/absent
    // record means the Customer has no Prior_Interaction (Req 13.8).
    if (typeof raw === 'object' && raw !== null) {
      return {
        kind: 'PRIOR_RECORD',
        record: normalizeHistoryRecord(raw),
        timestamp,
      };
    }
    return { kind: 'NO_PRIOR', timestamp };
  }

  // 503, 403, and any unexpected status fold into a single retryable error so
  // the panel can offer a retry rather than crash (Req 13.10). Prefer the
  // service's `suggestion` when present, else a generic safe message.
  const message = asString(asRecord(body).suggestion) ?? PREVIOUS_ERROR_MESSAGE;
  return { kind: 'PREVIOUS_ERROR', message, retry: true };
}
