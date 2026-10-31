// The resident's server session, held in memory only, like the staff session (staff-session.js).
// It is never written to localStorage, sessionStorage, IndexedDB, cookies, the URL or a global, so a
// reload or closing the tab signs the resident out. Only the server decides who a resident is: this
// module just keeps the bearer token the server issued at sign-in.

let current = null;

export const residentSession = {
  /** The active session, or null when absent or past its expiry time. */
  get(now = Date.now()) {
    if (current && current.expiresAt && Date.parse(current.expiresAt) <= now) current = null;
    return current;
  },
  set(session) {
    current = session && typeof session.token === 'string'
      ? {token: session.token, expiresAt: session.expiresAt ?? null, resident: session.resident}
      : null;
  },
  clear() {
    current = null;
  }
};
