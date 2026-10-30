// Staff console for server mode (#/staff): server sign-in, the server's complaint list, case detail,
// private evidence viewing and case management (status, assignment, work orders, notes, reinspection,
// audit trail). Every evidence view is logged by the server with the staff name. Management controls
// are drawn only from the server's `allowedActions`; the server re-checks every request anyway.
// The session token lives only in memory (staff-session.js). Local mode keeps the existing
// demonstration workspaces; this screen only explains that it needs server mode.

import {esc} from '../services.js';
import {icon, btn, heading, notice, publicTopbar, statusPill, verificationPill, fmtDate, fmtDateTime, coords, toast} from '../ui.js';
import {addCleanup} from '../state.js';
import {navigate} from '../router.js';
import {loadApiConfig} from '../api-config.js';
import {ApiError, createApiClient} from '../api-client.js';
import {staffSession} from '../staff-session.js';
import {mountInbox} from './notifications.js';
import {aiSections, mountAiPanels, overlayDetections} from './staff-ai.js';
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
  if (!staffSession.get()) return renderSignIn(root, http);
  if (id === 'notifications') return renderStaffNotifications(root, http);
  const publicId = decodeURIComponent(id || '').trim().toUpperCase();
  return publicId ? renderDetail(root, http, publicId) : renderList(root, http);
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
      staffSession.set(await http.staffSignIn(String(form.get('username')), String(form.get('password'))));
      listOffset = 0;
      // Notifications opened before signing in (sessions are memory-only) are marked read now.
      for (const nid of takePendingStaffReads()) http.markNotification(staffSession.get().token, nid, true).catch(() => {});
      const target = location.hash.replace(/^#\/staff\/?/, '');
      if (target) return renderStaff(root, target);  // e.g. a notification's complaint or the inbox
      renderList(root, http);
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

async function renderStaffNotifications(root, http) {
  page(root, `<p><a class="inline-link" href="#/staff">${icon('back', 'mini-icon')}<span>All server complaints</span></a></p>
    ${heading({eyebrow: 'CivicLens staff · server', title: 'Staff notifications', lead: 'New complaints and new evidence on the server. Turn notifications on to get them as system notifications on this computer or phone, even when CivicLens is closed.'})}
    <div id="staff-notifications" role="status" aria-live="polite"><p class="micro">Loading…</p></div>`, {signedIn: true});
  return mountInbox($('staff-notifications'), {http, audience: 'staff', token: staffSession.get().token, onUnauthorized: () => {
    staffSession.clear();
    renderSignIn(root, http, 'Your staff session has ended. Sign in again.');
  }});
}

async function renderList(root, http) {
  page(root, `${heading({eyebrow: 'CivicLens staff · server', title: 'Server complaints', lead: 'Complaints stored on the CivicLens server, newest first. Reports kept in local mode on residents’ devices are not here.'})}
    <div id="staff-list" role="status" aria-live="polite"><p class="micro">Loading complaints…</p></div>`, {signedIn: true});
  const slot = $('staff-list');
  try {
    const {items, total} = await http.listStaffComplaints(staffSession.get().token, {limit: PAGE_SIZE, offset: listOffset});
    if (!slot.isConnected) return;
    if (!items.length) {
      slot.innerHTML = notice(listOffset ? 'No more complaints.' : 'No complaints have been stored on the server yet.', 'info', 'alert');
      return;
    }
    const rows = items.map(i => `<tr>
      <td><a class="inline-link code-inline" href="#/staff/${esc(i.id)}">${esc(i.id)}</a></td>
      <td>${esc(i.category)}</td><td>${esc(i.area)}</td>
      <td>${statusPill(i.status)} ${verificationPill(i.verification)}</td>
      <td>${i.created ? fmtDate(i.created) : '—'}</td>
      <td>${i.hasEvidence ? `${icon(i.mediaType === 'video' ? 'video' : 'image', 'mini-icon')} Uploaded` : 'Not uploaded'}</td></tr>`).join('');
    const from = listOffset + 1, to = listOffset + items.length;
    slot.innerHTML = `<div class="table-wrap"><table class="data-table" id="staff-table">
        <thead><tr><th scope="col">Complaint</th><th scope="col">Category</th><th scope="col">Area</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col">Evidence</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="row-actions mt"><span class="micro">${from}–${to} of ${total}</span>
        ${listOffset > 0 ? btn('<span>Previous</span>', 'id="staff-prev"', 'ghost') : ''}${to < total ? btn('<span>Next</span>', 'id="staff-next"', 'secondary') : ''}</div>`;
    $('staff-prev')?.addEventListener('click', () => { listOffset = Math.max(0, listOffset - PAGE_SIZE); renderList(root, http); });
    $('staff-next')?.addEventListener('click', () => { listOffset += PAGE_SIZE; renderList(root, http); });
  } catch (err) {
    if (slot.isConnected) handleError(root, http, err, slot);
  }
}

async function renderDetail(root, http, publicId) {
  if (!SAMPLE.test(publicId)) return navigate('staff');
  page(root, `<p><a class="inline-link" href="#/staff">${icon('back', 'mini-icon')}<span>All server complaints</span></a></p>
    <div id="staff-detail" role="status" aria-live="polite"><p class="micro">Loading ${esc(publicId)}…</p></div>`, {signedIn: true});
  const slot = $('staff-detail');
  let r;
  try {
    r = await http.getStaffComplaint(staffSession.get().token, publicId);
  } catch (err) {
    if (slot.isConnected) handleError(root, http, err, slot);
    return;
  }
  if (!slot.isConnected) return;
  const g = r.gps;
  const timeline = [...r.timeline].reverse().map(t => `<li><div class="tl-head"><strong>${esc(t.status)}</strong><time datetime="${esc(t.date)}">${fmtDateTime(t.date)}</time></div><p>${esc(t.text)}</p></li>`).join('');
  const what = r.mediaType === 'video' ? 'video' : 'photo';
  slot.innerHTML = `<article class="panel" aria-labelledby="staff-case-title">
      <header class="track-head"><div><p class="eyebrow">Server complaint</p><h1 id="staff-case-title" class="code-inline" tabindex="-1">${esc(r.id)}</h1></div>
        <div class="pill-row">${statusPill(r.status)}${verificationPill(r.verification)}</div></header>
      <dl class="meta-list two-col">
        <div><dt>Category</dt><dd>${esc(r.category)}</dd></div>
        <div><dt>Neighbourhood</dt><dd>${esc(r.area)}</dd></div>
        <div><dt>Department</dt><dd>${esc(r.department)}</dd></div>
        <div><dt>Captured</dt><dd>${r.capturedAt ? fmtDateTime(r.capturedAt) : 'Not recorded'}${r.captureSource ? ` · ${esc(r.captureSource)}` : ''}</dd></div>
        <div><dt>Received</dt><dd>${r.uploadedAt ? fmtDateTime(r.uploadedAt) : '—'}</dd></div>
        <div><dt>Location</dt><dd>${coords(g)}${g.accuracy ? ` · ±${Math.round(g.accuracy)} m` : ''}<br><span class="micro">${esc(g.source || g.sourceType || '')}${g.address ? ` · ${esc(g.address)}` : ''}</span></dd></div>
        <div><dt>Description</dt><dd>${r.description ? esc(r.description) : '<span class="micro">None given</span>'}</dd></div>
        <div><dt>Resident’s sharing choice</dt><dd>${r.shareEvidence ? 'Share with community' : 'Private'}</dd></div>
      </dl>
      <h2 class="mt">Case management</h2>
      <div id="staff-manage">${manageSection(r)}</div>
      <h2 class="mt">Evidence</h2>
      <div id="staff-evidence">${r.evidence
        ? `<p class="micro">${esc(r.evidence.contentType)} · ${Math.max(1, Math.round(r.evidence.byteSize / 1024))} KB · uploaded ${fmtDateTime(r.evidence.uploadedAt)} · checksum <span class="code-inline">${esc(r.evidence.sha256.slice(0, 12))}…</span></p>
           ${notice(`Opening the ${what} records your name and the time in the access log.`, 'info', 'eye')}
           <div class="row-actions">${btn(icon('eye') + `<span>View ${what}</span>`, 'id="staff-view-evidence"', 'primary')}</div>
           <div id="staff-evidence-view" class="mt"></div>`
        : notice('No evidence has been uploaded for this complaint.', 'info', 'alert')}</div>
      ${aiSections()}
      <h2 class="mt">Evidence access log</h2>
      <div id="staff-access-log"><p class="micro">${r.evidence ? 'Loading…' : 'No evidence, so no access.'}</p></div>
      <h2 class="mt">Public timeline</h2>
      ${timeline ? `<ol class="timeline">${timeline}</ol>` : '<p class="micro">No updates yet.</p>'}
    </article>`;
  if (r.evidence) {
    $('staff-view-evidence').addEventListener('click', () => viewEvidence(root, http, r));
    loadAccessLog(root, http, r.id);
  }
  wireManagement(root, http, r);
  mountAiPanels(http, r, {
    onError: (err, slot) => handleError(root, http, err, slot),
    viewOriginal: (target, evidenceId) => showEvidenceInto(root, http, target, () => http.fetchStaffEvidence(staffSession.get().token, r.id), evidenceId, 'Original evidence'),
    viewAfter: (target, workOrderId, evidenceId) => showEvidenceInto(root, http, target,
      () => http.fetchWorkOrderEvidence(staffSession.get().token, workOrderId, evidenceId), evidenceId, 'After-repair evidence')
  });
}

/** Before/after viewer for the repair-verification panel. Each view is logged by the server. */
async function showEvidenceInto(root, http, target, load, evidenceId, alt) {
  target.innerHTML = '<p class="micro" role="status">Loading evidence…</p>';
  try {
    const blob = await load();
    if (!target.isConnected) return;
    const url = URL.createObjectURL(blob);
    addCleanup(() => URL.revokeObjectURL(url));
    target.innerHTML = blob.type.startsWith('video/')
      ? `<video class="receipt-media" controls playsinline src="${url}" aria-label="${esc(alt)}"></video>`
      : `<img class="receipt-media" src="${url}" alt="${esc(alt)}">`;
    overlayDetections(target, evidenceId);
    target.insertAdjacentHTML('beforeend', '<p class="micro">Viewing is recorded in the evidence access log.</p>');
  } catch (err) {
    if (target.isConnected) handleError(root, http, err, target);
  }
}

// ---------- Case management ----------

const can = (r, action) => r.allowedActions.some(a => a.action === action);
const field = (id, label, control, hint = '') => `<div class="field"><label for="${id}">${label}</label>${control}${hint ? `<p class="micro">${hint}</p>` : ''}</div>`;

function manageSection(r) {
  const a = r.assignment;
  const transitions = r.allowedActions.filter(x => x.action === 'transition');
  const orders = r.workOrders.map(w => `<tr data-work-order="${esc(w.id)}">
      <td>${esc(w.team)}${w.assignee ? `<br><span class="micro">${esc(w.assignee)}</span>` : ''}${w.description ? `<br><span class="micro">${esc(w.description)}</span>` : ''}</td>
      <td>${esc(w.status)}${w.overdue ? ' <span class="count-badge" title="Past its target date">Overdue</span>' : ''}</td>
      <td>${w.dueAt ? esc(w.dueAt) : '—'}</td>
      <td>${w.evidence.map(e => `<button type="button" class="btn btn-ghost" data-wo-evidence="${esc(e.evidenceId)}">${icon('eye', 'mini-icon')}<span>${e.phase === 'after' ? 'After' : 'Before'}</span></button>`).join(' ') || '—'}</td>
      <td class="row-actions">${w.status === 'Open' ? `<button type="button" class="btn btn-secondary" data-wo-to="In Progress">Start</button>` : ''}
        ${w.status === 'In Progress' ? `<button type="button" class="btn btn-secondary" data-wo-to="Completed">Complete</button>` : ''}
        ${['Open', 'In Progress'].includes(w.status) ? `<button type="button" class="btn btn-ghost" data-wo-to="Cancelled">Cancel</button>
          <label class="btn btn-ghost">${icon('image', 'mini-icon')}<span>After photo</span><input type="file" accept="image/jpeg" data-wo-upload hidden></label>` : ''}</td></tr>`).join('');
  return `<div id="manage-message" role="status" aria-live="polite"></div>
    <dl class="meta-list two-col">
      <div><dt>Department</dt><dd>${a.department ? esc(a.department.name) : '<span class="micro">Not assigned (triage queue)</span>'}</dd></div>
      <div><dt>Assigned to</dt><dd>${a.assignee ? `${esc(a.assignee.displayName)} (${esc(a.assignee.username)})` : '<span class="micro">Nobody yet</span>'}</dd></div>
    </dl>
    ${transitions.length ? `<form id="manage-transition" class="panel mt">
      <h3>Change status</h3>
      ${field('manage-explanation', 'Public explanation', '<textarea id="manage-explanation" maxlength="500" rows="2"></textarea>', 'Shown in public tracking. Required for some changes; never include names or private details.')}
      <div class="row-actions">${transitions.map(t => `<button type="submit" class="btn btn-secondary" data-to="${esc(t.to)}">${esc(t.label || t.to)} → ${esc(t.to)}</button>`).join(' ')}</div>
    </form>` : ''}
    ${can(r, 'assign') ? `<form id="manage-assign" class="panel mt">
      <h3>${a.department ? 'Reassign' : 'Assign'}</h3>
      ${field('manage-department', 'Department', '<select id="manage-department" required><option value="">Loading…</option></select>')}
      ${field('manage-assignee', 'Staff username (optional)', '<input id="manage-assignee" autocomplete="off" spellcheck="false" maxlength="64">', 'Officials must be members of the department.')}
      ${field('manage-reason', 'Reason', `<input id="manage-reason" maxlength="500" ${a.department ? 'required' : ''}>`, a.department ? 'Required when reassigning.' : '')}
      <div class="row-actions"><button type="submit" class="btn btn-primary">Save assignment</button></div>
    </form>` : ''}
    ${can(r, 'request_reinspection') ? `<form id="manage-reinspect" class="panel mt">
      <h3>Request reinspection</h3>
      ${field('manage-reinspect-note', 'Reason (public)', '<textarea id="manage-reinspect-note" maxlength="500" rows="2" required></textarea>')}
      <label class="check"><input type="checkbox" id="manage-reopen"> <span>Also reopen the complaint (back to In Progress)</span></label>
      <div class="row-actions"><button type="submit" class="btn btn-secondary">Request reinspection</button></div>
    </form>` : ''}
    <h3 class="mt">Work orders</h3>
    ${orders ? `<div class="table-wrap"><table class="data-table" id="work-orders"><thead><tr><th scope="col">Team</th><th scope="col">Status</th><th scope="col">Target date</th><th scope="col">Evidence</th><th scope="col">Actions</th></tr></thead><tbody>${orders}</tbody></table></div>
      ${field('manage-wo-note', 'Completion note or cancellation reason', '<input id="manage-wo-note" maxlength="1000">', 'Kept internally with the work order.')}
      <div id="wo-evidence-view" class="mt"></div>` : '<p class="micro">No work orders yet.</p>'}
    ${can(r, 'create_work_order') ? `<form id="manage-work-order" class="panel mt">
      <h3>New work order</h3>
      ${field('manage-team', 'Responsible team', '<input id="manage-team" maxlength="120" required>', 'Shown publicly, e.g. “Ward 4 road crew”.')}
      ${field('manage-due', 'Target date', '<input id="manage-due" type="date">')}
      ${field('manage-description', 'Work description', '<textarea id="manage-description" maxlength="1000" rows="2"></textarea>')}
      <div class="row-actions"><button type="submit" class="btn btn-secondary">Create work order</button></div>
    </form>` : ''}
    <h3 class="mt">Internal notes <span class="micro">(staff only; never shown publicly)</span></h3>
    ${r.notes.length ? `<ol class="timeline">${r.notes.map(n => `<li><div class="tl-head"><strong>${esc(n.author?.displayName || '')}</strong><time datetime="${esc(n.createdAt)}">${fmtDateTime(n.createdAt)}</time></div><p>${esc(n.body)}</p></li>`).join('')}</ol>` : '<p class="micro">No internal notes.</p>'}
    ${can(r, 'add_note') ? `<form id="manage-note" class="panel">
      ${field('manage-note-body', 'Add an internal note', '<textarea id="manage-note-body" maxlength="2000" rows="2" required></textarea>')}
      <div class="row-actions"><button type="submit" class="btn btn-secondary">Save note</button></div></form>` : ''}
    ${can(r, 'add_public_update') ? `<form id="manage-update" class="panel mt">
      <h3>Post a public update</h3>
      ${field('manage-update-text', 'Update (public)', '<textarea id="manage-update-text" maxlength="500" rows="2" required></textarea>')}
      <label class="check"><input type="checkbox" id="manage-update-dept"> <span>This is a department response</span></label>
      <div class="row-actions"><button type="submit" class="btn btn-secondary">Publish update</button></div></form>` : ''}
    ${r.assignmentHistory.length ? `<h3 class="mt">Assignment history</h3><ol class="timeline">${r.assignmentHistory.map(h => `<li><div class="tl-head"><strong>${esc(h.department)}${h.assignee ? ` · ${esc(h.assignee)}` : ''}</strong><time datetime="${esc(h.at)}">${fmtDateTime(h.at)}</time></div><p>${h.reason ? esc(h.reason) : 'No reason given'} · by ${esc(h.by)}</p></li>`).join('')}</ol>` : ''}
    <h3 class="mt">Audit trail</h3>
    <div id="manage-audit"><button type="button" class="btn btn-ghost" id="manage-load-audit">Show audit trail</button> <span class="micro">Supervisors and admins.</span></div>`;
}

function wireManagement(root, http, r) {
  const token = () => staffSession.get()?.token;
  const message = $('manage-message');
  const run = async (button, fn, done) => {
    if (button) button.disabled = true;
    message.innerHTML = '';
    try {
      await fn();
      toast(done);
      renderDetail(root, http, r.id);  // re-read the case from the server
    } catch (err) {
      if (button) button.disabled = false;
      if (message.isConnected) handleError(root, http, err, message);
    }
  };
  const value = id => ($(id)?.value || '').trim();

  $('manage-transition')?.addEventListener('submit', e => {
    e.preventDefault();
    const to = e.submitter?.dataset.to;
    if (to) run(e.submitter, () => http.transitionComplaint(token(), r.id, {to, expectedStatus: r.status, explanation: value('manage-explanation')}), `Status changed to ${to}.`);
  });
  const departments = $('manage-department');
  if (departments) {
    http.listDepartments(token()).then(list => {
      if (!departments.isConnected) return;
      departments.innerHTML = '<option value="">Choose a department</option>' + list.map(d => `<option value="${esc(d.code)}" ${r.assignment.department?.code === d.code ? 'selected' : ''}>${esc(d.name)}</option>`).join('');
    }).catch(err => handleError(root, http, err, message));
  }
  $('manage-assign')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.assignComplaint(token(), r.id, {department: value('manage-department'), assignee: value('manage-assignee').toLowerCase() || null,
      reason: value('manage-reason'), expectedStatus: r.status}), 'Assignment saved.');
  });
  $('manage-reinspect')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.requestReinspection(token(), r.id, {note: value('manage-reinspect-note'), reopen: $('manage-reopen').checked, expectedStatus: r.status}),
      'Reinspection requested.');
  });
  $('manage-work-order')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.createWorkOrder(token(), r.id, {team: value('manage-team'), dueAt: value('manage-due') || null, description: value('manage-description')}),
      'Work order created.');
  });
  $('work-orders')?.addEventListener('click', e => {
    const row = e.target.closest('[data-work-order]');
    const order = row && r.workOrders.find(w => w.id === row.dataset.workOrder);
    if (!order) return;
    const to = e.target.closest('[data-wo-to]')?.dataset.woTo;
    if (to) return run(e.target.closest('button'), () => http.changeWorkOrder(token(), order.id, {to, expectedStatus: order.status, note: value('manage-wo-note')}),
      `Work order ${to === 'In Progress' ? 'started' : to.toLowerCase()}.`);
    const evidenceId = e.target.closest('[data-wo-evidence]')?.dataset.woEvidence;
    if (evidenceId) showWorkOrderEvidence(root, http, order, evidenceId);
  });
  $('work-orders')?.addEventListener('change', e => {
    const input = e.target.closest('[data-wo-upload]');
    const file = input?.files?.[0];
    if (!file) return;
    const id = e.target.closest('[data-work-order]').dataset.workOrder;
    run(null, () => http.uploadRepairEvidence(token(), id, file), 'After-repair photo uploaded.');
  });
  $('manage-note')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.addStaffNote(token(), r.id, value('manage-note-body')), 'Internal note saved.');
  });
  $('manage-update')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.addPublicUpdate(token(), r.id, {text: value('manage-update-text'), kind: $('manage-update-dept').checked ? 'department_response' : 'progress'}),
      'Public update published.');
  });
  $('manage-load-audit')?.addEventListener('click', async () => {
    const slot = $('manage-audit');
    try {
      const entries = await http.getAuditTrail(token(), r.id);
      if (!slot.isConnected) return;
      slot.innerHTML = entries.length
        ? `<ol class="timeline">${entries.map(e => `<li><div class="tl-head"><strong>${esc(e.action.replace(/_/g, ' '))}</strong><time datetime="${esc(e.at)}">${fmtDateTime(e.at)}</time></div><p class="micro">${esc(e.actorType === 'staff' ? `${e.actorName || ''} (${e.actor || ''})` : e.actorType)} · <span class="code-inline">${esc(JSON.stringify(e.details))}</span></p></li>`).join('')}</ol>`
        : '<p class="micro">No audit entries yet.</p>';
    } catch (err) {
      if (slot.isConnected) handleError(root, http, err, slot);
    }
  });
}

async function showWorkOrderEvidence(root, http, order, evidenceId) {
  const view = $('wo-evidence-view');
  view.innerHTML = '<p class="micro" role="status">Loading evidence…</p>';
  try {
    const blob = await http.fetchWorkOrderEvidence(staffSession.get().token, order.id, evidenceId);
    if (!view.isConnected) return;
    const url = URL.createObjectURL(blob);
    addCleanup(() => URL.revokeObjectURL(url));
    view.innerHTML = `<img class="receipt-media" src="${url}" alt="Work order evidence for ${esc(order.team)}"><p class="micro">Viewing is recorded in the evidence access log.</p>`;
    overlayDetections(view, evidenceId);
  } catch (err) {
    if (view.isConnected) handleError(root, http, err, view);
  }
}

async function viewEvidence(root, http, r) {
  const view = $('staff-evidence-view');
  const button = $('staff-view-evidence');
  button.disabled = true;
  view.innerHTML = '<p class="micro" role="status">Loading evidence…</p>';
  try {
    const blob = await http.fetchStaffEvidence(staffSession.get().token, r.id);
    if (!view.isConnected) return;
    const url = URL.createObjectURL(blob);
    addCleanup(() => URL.revokeObjectURL(url));
    view.innerHTML = blob.type.startsWith('video/')
      ? `<video class="receipt-media" controls playsinline src="${url}" aria-label="Evidence video for ${esc(r.id)}"></video>`
      : `<img class="receipt-media" src="${url}" alt="Evidence photo for ${esc(r.id)}">`;
    overlayDetections(view, r.evidence.evidenceId);
    view.insertAdjacentHTML('beforeend', '<p class="micro">Shown from the server for this session only. It is not saved in this browser.</p>');
    loadAccessLog(root, http, r.id);
  } catch (err) {
    button.disabled = false;
    if (view.isConnected) handleError(root, http, err, view);
  }
}

async function loadAccessLog(root, http, id) {
  const slot = $('staff-access-log');
  try {
    const entries = await http.getEvidenceAccessLog(staffSession.get().token, id);
    if (!slot?.isConnected) return;
    slot.innerHTML = entries.length
      ? `<ol class="timeline">${entries.map(e => `<li><div class="tl-head"><strong>${esc(e.displayName)} (${esc(e.username)})</strong><time datetime="${esc(e.accessedAt)}">${fmtDateTime(e.accessedAt)}</time></div><p>${esc(e.action === 'view' ? 'Viewed the evidence' : e.action)}</p></li>`).join('')}</ol>`
      : '<p class="micro">Nobody has viewed this evidence yet.</p>';
  } catch (err) {
    if (slot?.isConnected) handleError(root, http, err, slot);
  }
}

async function signOut(root) {
  const session = staffSession.get();
  staffSession.clear();
  const http = await client();
  if (session && http) {
    // A signed-out browser (for example a shared computer) must stop receiving staff notifications.
    if (registeredDevice('staff')) {
      try { await disablePush({http, token: session.token, audience: 'staff'}); } catch { /* deleted at a later failed delivery at worst */ }
    }
    try { await http.staffSignOut(session.token); } catch { /* the in-memory session is gone either way */ }
  }
  toast('Signed out of the staff console.', 'success');
  if (http) renderSignIn(root, http);
}
