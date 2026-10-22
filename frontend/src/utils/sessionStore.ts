/**
 * Persistence for the *current* diagnostic Session id (Option A).
 *
 * The Diagnostic_API scopes history to a single Session (`GET
 * /diagnostics/history?sessionId=...`). Keeping the Session id only in React
 * state meant every page load / navigation started a fresh Session, so the
 * history view showed only the symptom just entered. Persisting it to
 * `sessionStorage` keeps every symptom entered during one browser-tab sitting
 * in the SAME Session, so they all appear together in history.
 *
 * `sessionStorage` (not `localStorage`) is deliberate: a "visit" is one tab
 * session. It survives refreshes and in-app navigation but clears when the tab
 * closes, which also avoids reusing a stale id days later after the backend has
 * closed the Session (30-minute idle timeout) and would reject it.
 *
 * Holds no secrets -- only an opaque Session identifier the API returned.
 */

const SESSION_ID_KEY = 'fleetmate.currentSessionId';

function getStorage(): Storage | null {
  try {
    const storage = (globalThis as { sessionStorage?: Storage }).sessionStorage;
    return storage ?? null;
  } catch {
    return null;
  }
}

/** The persisted current Session id, or `undefined` if none is stored. */
export function getCurrentSessionId(): string | undefined {
  const storage = getStorage();
  if (storage === null) {
    return undefined;
  }
  try {
    const value = storage.getItem(SESSION_ID_KEY);
    return value !== null && value.length > 0 ? value : undefined;
  } catch {
    return undefined;
  }
}

/** Persist the current Session id for the rest of this browser-tab sitting. */
export function setCurrentSessionId(sessionId: string): void {
  const storage = getStorage();
  if (storage === null || sessionId.length === 0) {
    return;
  }
  try {
    storage.setItem(SESSION_ID_KEY, sessionId);
  } catch {
    // Non-fatal: the Session id simply won't persist across navigation.
  }
}

/** Forget the current Session id (e.g. on logout, to start fresh next time). */
export function clearCurrentSessionId(): void {
  const storage = getStorage();
  if (storage === null) {
    return;
  }
  try {
    storage.removeItem(SESSION_ID_KEY);
  } catch {
    // Nothing to do.
  }
}