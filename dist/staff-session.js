// The staff console's server session, held in memory only. It is never written to localStorage,
// sessionStorage, IndexedDB, cookies, the URL or a global, so a reload or closing the tab signs the
// browser out. Separate from the demonstration sign-in in state.js, which local mode keeps using.

let current = null;

export const staffSession = {
  /** The active session, or null when absent or past its expiry time. */
  get(now = Date.now()) {
    if (current && current.expiresAt && Date.parse(current.expiresAt) <= now) current = null;
    return current;
  },
  set(session) {
    current = session && typeof session.token === 'string' ? {token: session.token, expiresAt: session.expiresAt ?? null, staff: session.staff} : null;
  },
  clear() {
    current = null;
  }
};
