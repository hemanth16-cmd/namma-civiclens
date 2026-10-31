// Shared presentation helpers. Every user-supplied string must pass through esc().
import {esc, categories} from './services.js';
import {icon, categoryIcon, resolutionState} from './presentation.js';
import {state, viewer} from './state.js';
import {navigate, currentPath} from './router.js';
import {LOGO_SRC, BRAND_NAME} from './brand.js';
import {staffSession} from './staff-session.js';

export {esc, icon, categoryIcon, resolutionState, categories};

const DATE_ZONE = 'Asia/Kolkata';
export const fmtDate = iso => iso ? new Date(iso).toLocaleDateString('en-IN', {day: 'numeric', month: 'short', year: 'numeric', timeZone: DATE_ZONE}) : 'Not recorded';
export const fmtDateTime = iso => iso ? new Date(iso).toLocaleString('en-IN', {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: DATE_ZONE}) : 'Not recorded';
export const fmtTime = iso => iso ? new Date(iso).toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: DATE_ZONE}) : '—';
export const coords = (gps, digits = 6) => gps ? `${gps.lat.toFixed(digits)}, ${gps.lng.toFixed(digits)}` : 'No coordinates';

export function btn(label, attrs = '', variant = 'secondary', extra = '') {
  return `<button type="button" class="btn btn-${variant} ${extra}" ${attrs}>${label}</button>`;
}
export function link(label, href, variant = 'secondary', extra = '') {
  return `<a class="btn btn-${variant} ${extra}" href="${href}">${label}</a>`;
}

const TONES = {neutral: '', coral: 'pill-coral', green: 'pill-green', red: 'pill-red', navy: 'pill-navy'};
export function pill(text, tone = 'neutral', iconName = '') {
  return `<span class="pill ${TONES[tone] ?? ''}">${iconName ? icon(iconName, 'pill-icon') : ''}<span>${esc(text)}</span></span>`;
}
export function statusPill(status) {
  const map = {
    'Submitted': ['neutral', 'file'], 'Under Review': ['navy', 'eye'], 'Assigned': ['navy', 'user'],
    'In Progress': ['coral', 'wrench'], 'Marked Resolved': ['green', 'check']
  };
  const [tone, ic] = map[status] || ['neutral', 'flag'];
  return pill(status, tone, ic);
}
export function verificationPill(v) {
  const map = {
    'Pending Verification': ['neutral', 'clock'], 'Citizen Confirmed': ['green', 'check'],
    'Disputed': ['red', 'alert'], 'Reinspection Required': ['coral', 'refresh']
  };
  const [tone, ic] = map[v] || ['neutral', 'flag'];
  return pill(v, tone, ic);
}
export function priorityPill(band) {
  const map = {High: ['coral', 'flag'], Medium: ['navy', 'flag'], Low: ['neutral', 'flag']};
  const [tone, ic] = map[band] || ['neutral', 'clock'];
  return pill(band === 'Unassessed' ? 'Not assessed' : `${band} priority`, tone, ic);
}

export function heading({eyebrow = '', title, lead = '', actions = ''}) {
  return `<header class="page-head"><div class="page-head-text">${eyebrow ? `<p class="eyebrow">${esc(eyebrow)}</p>` : ''}<h1 tabindex="-1" id="page-title">${title}</h1>${lead ? `<p class="lead">${lead}</p>` : ''}</div>${actions ? `<div class="page-head-actions">${actions}</div>` : ''}</header>`;
}

export function notice(body, tone = 'info', iconName = 'alert', extra = '') {
  return `<div class="notice notice-${tone} ${extra}" role="note">${icon(iconName, 'notice-icon')}<div>${body}</div></div>`;
}

export function empty(title, body = '', action = '') {
  return `<div class="empty">${icon('search', 'empty-icon')}<h3>${esc(title)}</h3>${body ? `<p>${body}</p>` : ''}${action}</div>`;
}

export function panel(inner, cls = '', attrs = '') {
  return `<section class="panel ${cls}" ${attrs}>${inner}</section>`;
}
export function panelHead(title, right = '', eyebrow = '') {
  return `<div class="panel-head"><div>${eyebrow ? `<p class="eyebrow">${esc(eyebrow)}</p>` : ''}<h2>${title}</h2></div>${right ? `<div class="panel-head-right">${right}</div>` : ''}</div>`;
}

export function logo(href = '#/camera') {
  return `<a class="logo" href="${href}" aria-label="${BRAND_NAME} home"><span class="logo-mark" aria-hidden="true"><img src="${LOGO_SRC}" alt="" width="34" height="34" decoding="async"></span><span class="logo-text"><strong>${BRAND_NAME}</strong><small>Accountable civic action</small></span></a>`;
}

export function demoBadge() {
  return `<span class="demo-badge" title="Browser-local prototype. Data stays on this device."><span class="dot" aria-hidden="true"></span>Local demo</span>`;
}

export function backButton(fallback = 'home') {
  return `<button type="button" class="btn btn-ghost btn-back" data-action="back" data-fallback="${fallback}" aria-label="Back to previous screen">${icon('back')}<span>Back</span></button>`;
}

export function toast(message, tone = 'info') {
  const el = document.querySelector('#toast');
  if (!el) return;
  el.dataset.tone = tone;
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), 4500);
}

/** Accessible modal built on <dialog>. Escape and the close button are both wired. */
export function openDialog({title, body, actions = '', size = 'md', onClose} = {}) {
  const dialog = document.createElement('dialog');
  dialog.className = `dialog dialog-${size}`;
  dialog.setAttribute('aria-labelledby', 'dialog-title');
  dialog.innerHTML = `<div class="dialog-body"><div class="dialog-head"><h2 id="dialog-title">${title}</h2><button type="button" class="btn btn-ghost btn-icon" data-dialog-close aria-label="Close">${icon('x')}</button></div>${body}${actions ? `<div class="dialog-actions">${actions}</div>` : ''}</div>`;
  document.body.appendChild(dialog);
  const close = value => { if (dialog.open) dialog.close(value); };
  dialog.querySelectorAll('[data-dialog-close]').forEach(el => { el.onclick = () => close('cancel'); });
  dialog.addEventListener('close', () => { onClose?.(dialog.returnValue); dialog.remove(); });
  dialog.showModal();
  return {dialog, close};
}

export function confirmDialog({title, message, confirmLabel = 'Confirm'}) {
  return new Promise(resolve => {
    const {dialog, close} = openDialog({
      title, size: 'sm',
      body: `<p class="dialog-message">${esc(message)}</p>`,
      actions: `${btn('Cancel', 'data-dialog-cancel', 'secondary')}${btn(esc(confirmLabel), 'data-dialog-ok', 'primary')}`,
      onClose: value => resolve(value === 'ok')
    });
    dialog.querySelector('[data-dialog-cancel]').onclick = () => close('cancel');
    dialog.querySelector('[data-dialog-ok]').onclick = () => close('ok');
  });
}

export function publicTopbar({back = true, fallback = 'home', right = ''} = {}) {
  return `<header class="topbar"><div class="topbar-left">${back ? backButton(fallback) : ''}${logo()}</div><nav class="topbar-right" aria-label="Public navigation">${right || `<a class="btn btn-ghost" href="#/camera">${icon('camera')}<span>Report</span></a>`}</nav></header>`;
}

/** Sidebar item. aria-current marks the active destination for screen readers. */
function navItem(href, iconName, label, active) {
  const path = href.replace(/^#\//, '');
  const current = currentPath();
  const isActive = current === path || (path.includes('/') && path !== 'official' && path !== 'resident' && current.startsWith(path + '/'));
  return `<a class="nav-item ${isActive ? 'active' : ''}" href="${href}" ${isActive ? 'aria-current="page"' : ''}>${icon(iconName)}<span>${label}</span></a>`;
}

export function workspaceShell({title, body}) {
  const v = viewer();
  const official = v.role === 'official';
  const staffRole = staffSession.get()?.staff?.role;
  const isSupervisorOrAdmin = ['supervisor', 'admin'].includes(staffRole);
  const base = official ? '#/official' : '#/resident';
  const items = official
    ? [[base, 'dashboard', 'Overview'], [base + '/complaints', 'list', 'Incident management'], [base + '/map', 'pin', 'City map'],
       [base + '/workorders', 'wrench', 'Work orders'], [base + '/video-review', 'video', 'Video review'], [base + '/intelligence', 'scale', 'Intelligence studio'],
       // Supervisors and admins on a server session; the API enforces the role on every request.
       ...(isSupervisorOrAdmin ? [[base + '/supervisor', 'layers', 'Supervisor wards']] : [])]
    : [[base, 'dashboard', 'Overview'], [base + '/complaints', 'list', 'My complaints'], [base + '/community', 'message', 'Community issues'],
       [base + '/map', 'pin', 'City map'], [base + '/claim', 'lock', 'Claim a complaint'], [base + '/notifications', 'bell', 'Notifications']];
  const initials = (v.name || (official ? 'Civic official' : 'Resident')).split(/\s+/).map(s => s[0]).join('').slice(0, 2).toUpperCase();
  const roleLabel = staffRole === 'supervisor' ? 'Supervisor operations' : staffRole === 'admin' ? 'City operations · Admin' : official ? 'City operations' : 'Your neighbourhood';
  const workspaceTitle = staffRole === 'supervisor' ? 'Supervisor workspace' : official ? 'Admin workspace' : 'User workspace';
  return `<div class="app-shell" id="workspace">
    <a class="skip-link" href="#main">Skip to content</a>
    <aside class="sidebar" id="sidebar" aria-label="Workspace navigation">
      <div class="sidebar-head">${logo(base)}<button type="button" class="btn btn-ghost btn-icon sidebar-close" data-action="close-menu" aria-label="Close navigation">${icon('x')}</button></div>
      <p class="sidebar-role">${roleLabel}</p>
      <nav class="nav-group" aria-label="Main">${items.map(([h, i, l]) => navItem(h, i, l, currentPath())).join('')}</nav>
      <div class="nav-group nav-foot">
        ${navItem(base + '/settings', 'settings', 'Preferences', currentPath())}
        <a class="nav-item" href="#/home">${icon('home')}<span>Public entry</span></a>
      </div>
      <div class="sidebar-foot">
        <p class="sidebar-note">${state.session?.server && state.session?.role === 'resident' ? 'Resident account. Your complaints come from the CivicLens server, which checks every request against your resident session.' : state.session?.server ? (staffRole === 'supervisor' ? 'Supervisor workspace. Complaints come from the CivicLens server, which checks role and geographic scope.' : 'Server workspace. Complaints come from the CivicLens server, which checks every request against your staff session.') : 'Local prototype. Records stay in this browser. Access rules are demonstration guards.'}</p>
        <button type="button" class="btn btn-ghost btn-block" data-action="logout">${icon('logout')}<span>Sign out</span></button>
      </div>
    </aside>
    <div class="sidebar-scrim" data-action="close-menu" aria-hidden="true"></div>
    <div class="workspace-main">
      <header class="workspace-bar">
        <div class="workspace-bar-left">
          <button type="button" class="btn btn-ghost btn-icon menu-toggle" data-action="open-menu" aria-controls="sidebar" aria-expanded="false" aria-label="Open navigation">${icon('list')}</button>
          ${backButton(official ? 'official' : 'resident')}
          <p class="breadcrumb"><span>${workspaceTitle}</span><span aria-hidden="true">/</span><span class="breadcrumb-current">${esc(title)}</span></p>
        </div>
        <div class="workspace-bar-right">
          <a class="icon-link" href="${base}/notifications" aria-label="Notifications">${icon('bell')}</a>
          <span class="avatar" title="${esc(v.name || '')}" aria-hidden="true">${esc(initials)}</span>
        </div>
      </header>
      <main id="main" class="content" tabindex="-1">${body}</main>
    </div>
  </div>`;
}

export function currentViewerLabel() {
  const v = viewer();
  return v.role === 'official' ? 'Admin' : v.role === 'resident' ? 'Resident' : 'Public';
}

export function mapCredit() {
  return 'Map data © OpenStreetMap contributors · vector tiles by OpenFreeMap';
}

export function openLightbox(src, alt) {
  openDialog({
    title: 'Enlarged evidence', size: 'lg',
    body: `<figure class="lightbox"><img src="${esc(src)}" alt="${esc(alt)}"></figure>`
  });
}

export {navigate, state};
