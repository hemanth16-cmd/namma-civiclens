// Application bootstrap: route table, role guards, delegated actions and error recovery.
import {renderCapture, renderReview, renderReceipt, cameraActions, setCameraMode, download} from './views/capture.js';
import {renderEntry, renderTrack, renderLogin} from './views/public.js';
import {renderOverview, renderComplaints, renderClaim, renderNotifications, renderSettings, renderWorkOrders} from './views/workspace.js';
import {renderIssue} from './views/issue.js';
import {renderIntelligence} from './views/intelligence.js';
import {renderMapPage} from './views/map-page.js';
import {renderStaff} from './views/staff.js';
import {mountVideoReview} from './video-review.js';
import {api} from './services.js';
import {state, viewer, setSession, runCleanups, addCleanup} from './state.js';
import {navigate, currentPath, recordRoute, goBack, setRenderer} from './router.js';
import {toast, workspaceShell, notice, esc, btn} from './ui.js';

const app = document.querySelector('#app');
const TITLES = {
  camera: 'Report an issue', review: 'Review evidence', home: 'Choose an option', track: 'Track a problem',
  login: 'Sign in', register: 'Create a demo profile', receipt: 'Receipt', issue: 'Case', staff: 'Staff console'
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
  if (head === 'staff') return renderStaff(root, parts[1]);  // server mode only; authorisation is enforced by the API
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
  const role = viewer().role;

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

render({focus: false});
