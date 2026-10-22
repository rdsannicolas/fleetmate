/**
 * Runtime configuration loader.
 *
 * The SPA is built once and deployed to dev/qa/prod unchanged. Environment
 * specific, non-secret values live in `public/config.json`, which is fetched at
 * runtime so the same artifact can target different backends. No secrets are
 * ever embedded in the client bundle or in `config.json`.
 *
 * Consumed later by the auth client and API client. This module intentionally
 * does no auth or API work — it only loads and validates configuration.
 */

/** Non-secret runtime configuration served from `/config.json`. */
export interface RuntimeConfig {
  /** Diagnostic_API base URL, e.g. `https://<api-id>.execute-api.us-east-1.amazonaws.com/v1`. */
  apiBaseUrl: string;
  /** Cognito Hosted UI domain, e.g. `https://<prefix>.auth.us-east-1.amazoncognito.com`. */
  cognitoDomain: string;
  /** Cognito User Pool id. */
  userPoolId: string;
  /** Cognito User Pool App Client id (public, non-secret). */
  appClientId: string;
  /** OAuth callback URL registered with the App Client. */
  redirectUri: string;
  /** Hosted UI logout redirect URL registered with the App Client. */
  logoutUri: string;
}

/** The required fields, used for presence/non-empty validation. */
const REQUIRED_FIELDS: readonly (keyof RuntimeConfig)[] = [
  'apiBaseUrl',
  'cognitoDomain',
  'userPoolId',
  'appClientId',
  'redirectUri',
  'logoutUri',
];

const CONFIG_URL = '/config.json';

/** Cached promise so repeated callers share a single fetch. */
let cachedConfig: Promise<RuntimeConfig> | undefined;

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function parseConfig(raw: unknown): RuntimeConfig {
  if (raw === null || typeof raw !== 'object') {
    throw new Error('Runtime config is missing or is not a JSON object');
  }

  const source = raw as Record<string, unknown>;
  const missing = REQUIRED_FIELDS.filter((field) => !isNonEmptyString(source[field]));

  if (missing.length > 0) {
    throw new Error(
      `Runtime config is invalid: missing or empty field(s): ${missing.join(', ')}`,
    );
  }

  return {
    apiBaseUrl: source.apiBaseUrl as string,
    cognitoDomain: source.cognitoDomain as string,
    userPoolId: source.userPoolId as string,
    appClientId: source.appClientId as string,
    redirectUri: source.redirectUri as string,
    logoutUri: source.logoutUri as string,
  };
}

async function fetchConfig(): Promise<RuntimeConfig> {
  let response: Response;
  try {
    response = await fetch(CONFIG_URL, { cache: 'no-store' });
  } catch (cause) {
    throw new Error(`Failed to load runtime config from ${CONFIG_URL}`, { cause });
  }

  if (!response.ok) {
    throw new Error(
      `Failed to load runtime config from ${CONFIG_URL}: HTTP ${response.status}`,
    );
  }

  let raw: unknown;
  try {
    raw = await response.json();
  } catch (cause) {
    throw new Error(`Runtime config at ${CONFIG_URL} is not valid JSON`, { cause });
  }

  return parseConfig(raw);
}

/**
 * Load and validate the runtime configuration from `/config.json`.
 *
 * The result is cached after the first successful load so repeated calls do not
 * refetch. A failed load is not cached, so a later call can retry.
 *
 * @throws Error if the config is missing, unreachable, not valid JSON, or if any
 *   required field is absent or empty.
 */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cachedConfig === undefined) {
    const pending = fetchConfig();
    cachedConfig = pending;
    try {
      return await pending;
    } catch (error) {
      // Don't cache failures; allow a subsequent call to retry.
      cachedConfig = undefined;
      throw error;
    }
  }
  return cachedConfig;
}

/** Reset the cached config. Intended for tests. */
export function resetRuntimeConfigCache(): void {
  cachedConfig = undefined;
}
