/**
 * Minimal zero-dependency hash router for the FleetMate SPA.
 *
 * The SPA is served from a private S3 bucket behind CloudFront. CloudFront maps
 * 403/404 responses back to `/index.html`, so any deep path already resolves to
 * the entry document (Req 11.3). Using the URL fragment (`#/...`) for in-app
 * routing means navigation never issues a server request at all: every route is
 * resolved purely in the browser from `window.location.hash`. That keeps
 * client-side routing working regardless of how the static host treats
 * unknown paths, and avoids any server-route configuration beyond the SPA
 * fallback the infrastructure already provides.
 *
 * This module holds no secrets and performs no auth or API work. It only parses
 * and observes the location fragment and exposes a tiny navigation helper.
 */

import { useEffect, useState } from 'react';

/** The in-app routes the SPA understands. */
export type Route =
  | { readonly name: 'home' }
  | { readonly name: 'callback' }
  | { readonly name: 'history' };

/**
 * Parse a location fragment (e.g. `#/callback?code=…`) into a {@link Route}.
 *
 * The fragment's own query string (anything after `?`) is ignored here; the
 * callback page reads the OAuth parameters from `window.location.search`, which
 * is where the Hosted UI places them. Unknown fragments fall back to `home`,
 * matching the SPA entry-document fallback (Req 11.3).
 */
export function parseRoute(hash: string): Route {
  // Strip a leading '#', then a leading '/', then drop any fragment query.
  const withoutHash = hash.startsWith('#') ? hash.slice(1) : hash;
  const path = withoutHash.split('?')[0] ?? '';
  const normalized = path.replace(/^\/+/, '').replace(/\/+$/, '').toLowerCase();

  switch (normalized) {
    case 'callback':
      return { name: 'callback' };
    case 'history':
      return { name: 'history' };
    case '':
    case 'home':
    default:
      return { name: 'home' };
  }
}

/** Build the fragment for a route, suitable for an anchor `href`. */
export function routeToHash(route: Route): string {
  switch (route.name) {
    case 'callback':
      return '#/callback';
    case 'history':
      return '#/history';
    case 'home':
    default:
      return '#/';
  }
}

/**
 * Navigate to a route by setting the location fragment. Assigning `hash` does
 * not reload the page and fires a `hashchange` event, which {@link useHashRoute}
 * listens for.
 */
export function navigate(route: Route): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.location.hash = routeToHash(route);
}

/**
 * React hook: the current {@link Route}, kept in sync with the location
 * fragment. Re-renders the subscriber whenever the fragment changes.
 */
export function useHashRoute(): Route {
  const read = (): Route =>
    parseRoute(typeof window !== 'undefined' ? window.location.hash : '');

  const [route, setRoute] = useState<Route>(read);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }
    const onHashChange = (): void => {
      setRoute(read());
    };
    window.addEventListener('hashchange', onHashChange);
    // Resync once on mount in case the fragment changed before we subscribed.
    onHashChange();
    return () => {
      window.removeEventListener('hashchange', onHashChange);
    };
  }, []);

  return route;
}
