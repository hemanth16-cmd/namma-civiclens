// Application bootstrap: route table, role guards, delegated actions and error recovery.
import {renderCapture, renderReview, renderReceipt, cameraActions, setCameraMode, download} from './views/capture.js';
import {renderEntry, renderTrack, renderLogin} from './views/public.js';
import {renderOverview, renderComplaints, renderClaim, renderNotifications, renderSettings, renderWorkOrders} from './views/workspace.js';
import {renderIssue} from './views/issue.js';
import {renderIntelligence} from './views/intelligence.js';
import {renderMapPage} from './views/map-page.js';
import {renderStaff} from './views/staff.js';
import {renderMyReports} from './views/my-reports.js';
import {reportMode} from './report-gateway.js';
import {renderNotifications as renderServerNotifications} from './views/notifications.js';
import {openNotification, refreshPush, takeLaunchNotification, inbox} from './push-client.js';
import {staffSession} from './staff-session.js';
import {residentSession} from './resident-session.js';
import {loadApiConfig} from './api-config.js';
import {createApiClient} from './api-client.js';
import {mountVideoReview} from './video-review.js';
import {api} from './services.js';
import {state, viewer, setSession, runCleanups, addCleanup} from './state.js';
import {navigate, currentPath, recordRoute, goBack, setRenderer, serverModeRoute, serverWorkspaceAccess} from './router.js';
import {toast, workspaceShell, notice, esc, btn} from './ui.js';

const app = document.querySelector('#app');

// An identity restored from sessionStorage is only a display hint. The old "this device" pseudo-resident
// and a server resident whose in-memory token is gone (reload, new tab) are dropped here. A stale staff
// identity is kept until the router sees it, so a reloaded case page sends staff back to sign-in.
if (state.session?.device || (state.session?.server && state.session.role === 'resident' && !residentSession.get())) setSession(null);
const TITLES = {
  camera: 'Report an issue', review: 'Review evidence', home: 'Choose an option', track: 'Track a problem',
  login: 'Sign in', register: 'Create a demo profile', receipt: 'Receipt', issue: 'Case', staff: 'Staff console',
  notifications: 'Notifications', 'my-reports': 'Reports on This Device'
};

function fallbackPath(name) {
  return {home: 'home', camera: 'camera', official: 'official', resident: 'resident'}[name] || 'camera';
}

async function renderVideoDesk(root) {
  const records = await api.list(viewer());
  root.innerHTML = workspaceShell({title: 'Video review', body: `
    <header class="page-head"><div class="page-head-text"><p class="eyebrow">Admin · private footage</p><h1 tabindex="-1" id="page-title">Video review</h1><p class="lead">Look closer, then record what happened. Footage stays private, and every observation is written by a person.</p></div></header>
    <div id="video-review-root"></div>`});
  const host = root.querySelector('#video-review-root');
  const cleanup = await mountVideoReview({root: host, actor: viewer(), records, toast});
  addCleanup(cleanup);
}

async function dispatch(path, parts, root) {
  const [head, second] = parts;
  if (path === 'camera' || path === '') return renderCapture(root);
  if (head === 'review') return renderReview(root);
  if (head === 'receipt') return renderReceipt(root, parts[1]);
  if (head === 'home' || head === 'entry') return renderEntry(root);
  if (head === 'track') return renderTrack(root, parts.slice(1).join('/'));
  if (head === 'login') return renderLogin(root, second === 'official' ? 'official' : 'resident');
  if (head === 'register') return renderLogin(root, 'resident', true);
  if (head === 'issue') return renderIssue(root, parts[1]);
  if (head === 'my-reports') return renderMyReports(root);  // server mode only
  if (head === 'staff') return renderStaff(root, parts[1]);  // server mode only; authorisation is enforced by the API
  if (head === 'notifications') return renderServerNotifications(root, parts[1], parts[2]);  // server mode only
  if (head === 'official' || head === 'resident') {
    if (!second) return renderOverview(root);
    switch (second) {
      case 'complaints': return renderComplaints(root, head === 'official' ? 'all' : 'mine');
      case 'community': return renderComplaints(root, 'community');
      case 'map': return renderMapPage(root);
      case 'claim': return renderClaim(root);
      case 'notifications': return renderNotifications(root);
      case 'settings': return renderSettings(root);
      case 'workorders': return renderWorkOrders(root);
      case 'intelligence': return renderIntelligence(root);
      case 'video-review': return renderVideoDesk(root);
      default: return renderOverview(root);
    }
  }
  return navigate('camera');
}

let renderSeq = 0;
async function render({focus = true} = {}) {
  const seq = ++renderSeq;
  runCleanups();
  const path = currentPath();
  const parts = path.split('/').filter(Boolean);
  const head = parts[0] || 'camera';
  let role = viewer().role;

  // Server mode: the workspaces must never show browser-local sample data in place of server complaints.
  // The admin workspace needs a server staff session and the resident workspace a server resident
  // session (both kept in memory only). A passerby never gets either just by opening a URL.
  if (['official', 'resident', 'issue'].includes(head) && await reportMode() === 'backend') {
    if (seq !== renderSeq) return;
    const staff = staffSession.get();
    const access = serverWorkspaceAccess(head, {staff, resident: residentSession.get(), session: state.session});
    if (access.clear) setSession(null);
    if (access.redirect) return navigate(access.redirect);
    if (access.identity) { setSession(access.identity); role = access.identity.role; }
    const target = serverModeRoute(parts, {staff: !!staff});
    if (target) return navigate(target);
  }

  // Role guards. Frontend checks are demonstration controls, not production authorisation.
  if (head === 'official' && role !== 'official') return navigate('login/official');
  if (head === 'resident' && role !== 'resident') return navigate('login/resident');
  if (head === 'issue' && role === 'public') return navigate('login/resident');
  if (head === 'review' && !state.capture) return navigate('camera');
  if (head === 'receipt' && !parts[1]) return navigate('home');

  recordRoute(path);
  document.title = `${TITLES[head] || 'Workspace'} · Namma CivicLens`;
  try {
    await dispatch(path, parts, app);
    if (seq !== renderSeq) return;
  } catch (err) {
    if (seq !== renderSeq) return;
    console.error(err);
    app.innerHTML = `<div class="page"><main id="main" class="narrow" tabindex="-1">${notice(`<strong>This screen could not load.</strong> ${esc(err?.message || 'An unexpected error occurred.')} Your saved reports are unaffected.`, 'error', 'alert')}<p class="row-actions">${btn('Try again', 'data-action="retry"', 'primary')}${btn('Go to the camera', 'data-action="go-camera"', 'secondary')}</p></main></div>`;
    return;
  }
  if (focus) {
    document.getElementById('page-title')?.focus({preventScroll: true});
    window.scrollTo(0, 0);
  }
}

// Views call rerender() after changes: keep scroll and focus where the user is.
setRenderer(() => render({focus: false}));

// ---------- Delegated actions ----------
function setMenu(open) {
  const shell = document.querySelector('.app-shell');
  if (!shell) return;
  shell.classList.toggle('menu-open', open);
  document.querySelector('[data-action="open-menu"]')?.setAttribute('aria-expanded', String(open));
  if (open) document.querySelector('#sidebar a, #sidebar button')?.focus();
}

const CAMERA = {
  'enable-camera': () => cameraActions.enable(),
  'resume-preview': () => cameraActions.resume(),
  'switch-camera': () => cameraActions.switchCamera(),
  'refresh-location': () => cameraActions.locate(),
  'lookup-live': () => cameraActions.lookupLive(),
  'lookup-street': () => cameraActions.lookupLive(),
  'gallery': () => cameraActions.openGallery(),
  'stop-recording': () => cameraActions.stopRecording(),
  'retake': () => cameraActions.retake(),
  'save-original': () => cameraActions.saveOriginal()
};

document.addEventListener('click', async e => {
  const el = e.target.closest('[data-action],[data-mode],[data-copy]');
  if (!el) return;
  try {
    if (el.dataset.mode) return setCameraMode(el.dataset.mode);
    if (el.dataset.copy) {
      await navigator.clipboard.writeText(el.dataset.copy);
      return toast('Copied to clipboard.', 'success');
    }
    const action = el.dataset.action;
    if (CAMERA[action]) return CAMERA[action]();
    switch (action) {
      case 'back': return goBack(fallbackPath(el.dataset.fallback));
      case 'open-menu': return setMenu(true);
      case 'close-menu': return setMenu(false);
      case 'logout':
        setSession(null);
        toast('Signed out of this demonstration workspace.');
        return navigate('home');
      case 'retry': return render();
      case 'go-camera': return navigate('camera');
      default: return undefined;
    }
  } catch (err) {
    toast(err?.message || 'Something went wrong. Try again.', 'error');
  }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.querySelector('.app-shell.menu-open')) setMenu(false);
});

window.addEventListener('hashchange', () => render({focus: true}));
window.addEventListener('pagehide', () => runCleanups());
window.addEventListener('unhandledrejection', e => {
  e.preventDefault();
  toast(e.reason?.message || 'Something went wrong. Your saved data is unchanged.', 'error');
});

// ---------- Web Push: notification clicks and service worker messages (server mode) ----------
async function notificationClient() {
  const config = await loadApiConfig();
  return config.mode === 'backend' ? createApiClient({baseUrl: config.baseUrl}) : null;
}

function notificationOpened(n) {
  notificationClient()
    .then(http => openNotification(n, {http, staffToken: () => staffSession.get()?.token ?? null, go: navigate}))
    .then(() => window.dispatchEvent(new CustomEvent('civiclens:notifications-changed')))
    .catch(() => { /* best-effort: the notification stays unread in history */ });
}

// A click on a notification opened this window: the route is already in the hash.
const launched = takeLaunchNotification();
if (launched) notificationOpened(launched);

navigator.serviceWorker?.addEventListener('message', e => {
  const data = e.data && typeof e.data === 'object' ? e.data : {};
  if (data.type === 'civiclens:notification-click') notificationOpened({id: data.id, audience: data.audience, url: data.url});
  else if (data.type === 'civiclens:notification-received') window.dispatchEvent(new CustomEvent('civiclens:notifications-changed'));
  else if (data.type === 'civiclens:subscription-changed') {
    notificationClient().then(http => {
      if (!http) return;
      const citizen = inbox.get();
      if (citizen) refreshPush({http, token: citizen, audience: 'citizen'}).catch(() => {});
      const staff = staffSession.get()?.token;
      if (staff) refreshPush({http, token: staff, audience: 'staff'}).catch(() => {});
    });
  }
});

render({focus: false});
