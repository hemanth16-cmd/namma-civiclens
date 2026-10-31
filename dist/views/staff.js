// Staff console for server mode (#/staff): server sign-in, the server's complaint list, case detail,
// private evidence viewing and case management (status, assignment, work orders, notes, reinspection,
// audit trail). Every evidence view is logged by the server with the staff name. Management controls
// are drawn only from the server's `allowedActions`; the server re-checks every request anyway.
// The session token lives only in memory (staff-session.js). Local mode keeps the existing
// demonstration workspaces; this screen only explains that it needs server mode.

import {esc} from '../services.js';
import {icon, btn, heading, notice, publicTopbar, statusPill, verificationPill, fmtDate, fmtDateTime, coords, toast} from '../ui.js';
import {addCleanup, state, setSession} from '../state.js';
import {navigate} from '../router.js';
import {loadApiConfig} from '../api-config.js';
import {ApiError, createApiClient} from '../api-client.js';
import {staffSession} from '../staff-session.js';
import {residentSession} from '../resident-session.js';
import {mountInbox} from './notifications.js';
import {disablePush, registeredDevice, takePendingStaffReads} from '../push-client.js';

const PAGE_SIZE = 25;
const SAMPLE = /^NCL-\d{4}-[A-Z0-9]{6}$/;
let listOffset = 0;

const $ = id => document.getElementById(id);

function page(root, body, {signedIn = false} = {}) {
  const session = staffSession.get();
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home', right: signedIn && session ? `<a class="btn btn-ghost" href="#/staff/notifications" id="staff-notifications-link">${icon('bell')}<span>Notifications</span><span id="staff-unread" class="count-badge" hidden></span></a> <span class="micro">${esc(session.staff.displayName)}</span> ${btn(icon('lock') + '<span>Sign out</span>', 'id="staff-sign-out"', 'ghost')}` : ''})}
    <main id="main" class="narrow" tabindex="-1" data-staff-console="server">${body}</main>
  </div>`;
  $('staff-sign-out')?.addEventListener('click', () => signOut(root));
  if (signedIn && session) showUnread(session.token);
}

async function showUnread(token) {
  try {
    const count = await (await client())?.unreadCount(token);
    const badge = $('staff-unread');
    if (badge && count) {
      badge.hidden = false;
      badge.textContent = String(count);
      badge.setAttribute('aria-label', `${count} unread`);
    }
  } catch { /* the badge is optional */ }
}

async function client() {
  const config = await loadApiConfig();
  return config.mode === 'backend' ? createApiClient({baseUrl: config.baseUrl}) : null;
}

/** Routes: #/staff (list), #/staff/notifications and #/staff/<publicId> (detail). */
export async function renderStaff(root, id = '') {
  const http = await client();
  if (!http) {
    page(root, `${heading({eyebrow: 'CivicLens staff', title: 'Staff console'})}
      ${notice('The staff console works only in <strong>server mode</strong> (set <code>"mode": "backend"</code> in <code>api-config.json</code>). In local mode, use the demonstration <a class="inline-link" href="#/login/official">admin workspace</a>.', 'info', 'alert')}`);
    return;
  }
  const session = staffSession.get();
  if (!session) return renderSignIn(root, http);
  if (session.staff?.role === 'supervisor' && !id) return navigate('official/supervisor');
  return navigate(dashboardRoute(id, session.staff?.role));
}

/** Where a staff-console link leads once signed in: the dashboard keeps the original design. */
export function dashboardRoute(id = '', role = '') {
  if (id === 'notifications') return 'official/notifications';
  if (id === 'supervisor') return 'official/supervisor';
  if (role === 'supervisor' && !id) return 'official/supervisor';
  const publicId = decodeURIComponent(id || '').trim().toUpperCase();
  return SAMPLE.test(publicId) ? 'issue/' + publicId : 'official/complaints';
}

function renderSignIn(root, http, message = '') {
  page(root, `${heading({eyebrow: 'CivicLens staff · server', title: 'Staff sign-in', lead: 'Sign in with the staff account created for you on the CivicLens server.'})}
    <form id="staff-sign-in" class="panel stack" autocomplete="on">
      <div class="field"><label for="staff-username">Username</label><input id="staff-username" name="username" autocomplete="username" required maxlength="64" spellcheck="false"></div>
      <div class="field"><label for="staff-password">Password</label><input id="staff-password" name="password" type="password" autocomplete="current-password" required maxlength="256"></div>
      <div id="staff-sign-in-error" role="alert">${message ? notice(esc(message), 'warn', 'alert') : ''}</div>
      <button class="btn btn-primary btn-block" type="submit">Sign in ${icon('forward')}</button>
    </form>
    ${notice('Your session is kept only in this tab’s memory: reloading or closing the page signs you out. Staff accounts are created by an administrator with <code>python -m app.staff create</code>.', 'info', 'lock', 'mt')}`);
  $('staff-sign-in').addEventListener('submit', async e => {
    e.preventDefault();
    const form = new FormData(e.target);
    const button = e.target.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const session = await http.staffSignIn(String(form.get('username')), String(form.get('password')));
      staffSession.set(session);
      residentSession.clear();  // one identity per tab
      listOffset = 0;
      // Notifications opened before signing in (sessions are memory-only) are marked read now.
      for (const nid of takePendingStaffReads()) http.markNotification(staffSession.get().token, nid, true).catch(() => {});
      setSession({role: 'official', name: session.staff.displayName, email: session.staff.username, server: true});
      const rawId = location.hash.replace(/^#\/staff\/?/, '');
      const targetRoute = session.staff?.role === 'supervisor' && !rawId
        ? 'official/supervisor'
        : dashboardRoute(rawId, session.staff?.role);
      navigate(targetRoute);
    } catch (err) {
      button.disabled = false;
      $('staff-password').value = '';
      $('staff-sign-in-error').innerHTML = notice(esc(err instanceof ApiError ? err.message : 'Sign-in failed.'), 'error', 'alert');
    }
  });
}

/** Any 401 ends the session in this tab and returns to the sign-in form. */
function handleError(root, http, err, slot) {
  if (err instanceof ApiError && err.status === 401) {
    staffSession.clear();
    return renderSignIn(root, http, 'Your staff session has ended. Sign in again.');
  }
  slot.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'The request failed.'), 'error', 'alert');
}

/** Ends the staff session everywhere it is used (console and admin workspace). Never throws. */
export async function endStaffSession() {
  const session = staffSession.get();
  staffSession.clear();
  if (state.session?.server) setSession(null);  // the workspace identity came from this staff session
  const http = await client();
  if (session && http) {
    // A signed-out browser (for example a shared computer) must stop receiving staff notifications.
    if (registeredDevice('staff')) {
      try { await disablePush({http, token: session.token, audience: 'staff'}); } catch { /* deleted at a later failed delivery at worst */ }
    }
    try { await http.staffSignOut(session.token); } catch { /* the in-memory session is gone either way */ }
  }
  return http;
}

async function signOut(root) {
  const http = await endStaffSession();
  toast('Signed out of the staff console.', 'success');
  if (http) renderSignIn(root, http);
}
