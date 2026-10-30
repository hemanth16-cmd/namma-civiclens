// Resident and admin workspace screens. Administrative controls are rendered only for officials.
import {api, categories, statuses, verificationStates, esc, assessPriority} from '../services.js';
import {icon, categoryIcon, resolutionState, workspaceShell, heading, btn, link, statusPill, verificationPill, priorityPill, notice, empty, panel, panelHead, toast, fmtDate, fmtDateTime, confirmDialog} from '../ui.js';
import {state, viewer, setSession, readPrefs, writePrefs, readLastRead, writeLastRead, addCleanup} from '../state.js';
import {navigate} from '../router.js';
import {filterRecords, sortRecords, readFilters} from '../filters.js';
import {mountCityMap} from './map.js';
import {workspaceRecords, usesServerRecords, dailyActivity} from '../server-records.js';
import {staffSession} from '../staff-session.js';
import {endStaffSession} from './staff.js';
import {ApiError} from '../api-client.js';

const isAdmin = () => viewer().role === 'official';

export async function loadRecords() {
  try {
    state.records = await workspaceRecords(viewer());
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {  // server mode: the staff session ended
      staffSession.clear();
      setSession(null);
      navigate('login/official');
      throw new Error('Your staff session has ended. Sign in again.');
    }
    throw err;
  }
  return state.records;
}

function mount(root, title, body) {
  root.innerHTML = workspaceShell({title, body});
  wireWorkspace(root);
}

export function wireWorkspace(root) {
  root.querySelector('[data-action="logout"]').onclick = async () => {
    const server = state.session?.server === true;
    if (server) await endStaffSession();  // revokes the server session and staff push on this device
    setSession(null);
    toast(server ? 'Signed out of CivicLens staff.' : 'You have signed out of this demonstration workspace.');
    navigate('home');
  };
}

// ---------- Shared pieces ----------
export function issueRow(r) {
  const state_ = resolutionState(r);
  const meta = [r.area, r.id].map(esc).join(' · ');
  return `<li class="issue-row">
    <span class="issue-symbol ${state_.className}" title="${esc(state_.label)}" aria-label="${esc(r.category)}, ${esc(state_.label)}">${categoryIcon(r.category)}</span>
    <div class="issue-row-body">
      <h3 class="issue-title"><a href="#/issue/${esc(r.id)}">${esc(r.title)}</a></h3>
      <p class="issue-meta">${meta}</p>
      <p class="issue-tags">${statusPill(r.status)} ${verificationPill(r.verification)}${r.priority ? priorityPill(r.priority.band) : ''}</p>
    </div>
    <span class="issue-count" title="Community reviews">${icon('message', 'mini-icon')}<span>${r.reviewCount ?? 0}</span><span class="sr-only"> community reviews</span></span>
  </li>`;
}

function stat(label, value, hint, tone = '') {
  return `<div class="stat ${tone}"><p class="stat-label">${esc(label)}</p><p class="stat-value">${String(value).padStart(2, '0')}</p><p class="stat-hint">${esc(hint)}</p></div>`;
}

function filterBar(id, {withStatus = true, withCategory = true, withVerification = false, withSort = true, extra = ''} = {}) {
  return `<form class="filter-bar" id="${id}" role="search" aria-label="Filter reports">
    <div class="field field-search"><label for="${id}-q">Search</label><input id="${id}-q" name="q" type="search" placeholder="Issue, neighbourhood or ID"></div>
    ${withStatus ? `<div class="field"><label for="${id}-status">Lifecycle status</label><select id="${id}-status" name="status"><option value="">All statuses</option>${statuses.map(s => `<option>${s}</option>`).join('')}</select></div>` : ''}
    ${withVerification ? `<div class="field"><label for="${id}-verification">Verification</label><select id="${id}-verification" name="verification"><option value="">Any verification</option>${verificationStates.map(s => `<option>${s}</option>`).join('')}</select></div>` : ''}
    ${withCategory ? `<div class="field"><label for="${id}-category">Category</label><select id="${id}-category" name="category"><option value="">All categories</option>${categories.map(c => `<option>${esc(c)}</option>`).join('')}</select></div>` : ''}
    ${withSort ? `<div class="field"><label for="${id}-sort">Sort</label><select id="${id}-sort" name="sort"><option value="newest">Newest first</option><option value="oldest">Oldest first</option>${isAdmin() ? '<option value="priority">Priority score</option>' : ''}</select></div>` : ''}
    ${extra}
  </form>`;
}

// ---------- Overview ----------
export async function renderOverview(root) {
  const v = viewer();
  const admin = isAdmin();
  const records = await loadRecords();
  const server = admin && await usesServerRecords();
  if (admin) {
    const open = records.filter(r => r.status !== 'Marked Resolved');
    const needsReview = records.filter(r => ['Submitted', 'Under Review'].includes(r.status));
    const disputed = records.filter(r => ['Disputed', 'Reinspection Required'].includes(r.verification));
    const resolved = records.filter(r => r.status === 'Marked Resolved');
    const ranked = sortRecords(records.map(r => ({...r, priority: r.priority ?? assessPriority(r)})), 'priority').slice(0, 6);
    mount(root, 'Overview', `
      ${heading({eyebrow: 'City operations', title: 'Operational overview', lead: 'Cases needing review, disputed repairs and the next actions for your teams.', actions: link(icon('pin') + '<span>Open city map</span>', '#/official/map', 'secondary')})}
      ${server
        ? notice(`Showing <strong>${records.length}</strong> ${records.length === 1 ? 'complaint' : 'complaints'} stored on the CivicLens server. Status changes, assignment and work orders arrive with server-side complaint management; until then cases are read-only here. Open a case to view its evidence.`, 'info', 'alert')
        : notice('Sample incidents and the activity series are illustrative. Browser-local reports appear alongside them. No municipal system is connected.', 'info', 'alert')}
      <div class="stats" role="list">
        ${stat('Total cases', records.length, 'Across the workspace')}
        ${stat('Awaiting first review', needsReview.length, 'Submitted or under review', 'stat-navy')}
        ${stat('Disputed or recheck', disputed.length, 'Needs a second look', 'stat-red')}
        ${stat('Marked resolved', resolved.length, `${open.length} still open`, 'stat-green')}
      </div>
      <div class="split">
        ${panel(`${panelHead('Needs attention', link('All cases', '#/official/complaints', 'ghost'), 'Ranked by explainable priority')}
          <ul class="issue-list">${ranked.map(issueRow).join('') || '<li class="empty-inline">No cases yet.</li>'}</ul>
          <p class="micro">Scores are illustrative baselines from documented inputs. They support, not replace, emergency procedures.</p>`, 'panel-main')}
        <div class="stack">
          ${panel(`${panelHead('City map', link('Open map', '#/official/map', 'ghost'))}<div class="map-compact" id="overview-map"></div>`)}
          ${panel(server ? `${panelHead('Reporting activity', '<span class="pill">Server data</span>')}${activityChart(records)}` : `${panelHead('Reporting activity', badgeSample())}${sampleChart()}`)}
        </div>
      </div>`);
  } else {
    const mine = records.filter(r => r.mine);
    const open = mine.filter(r => r.status !== 'Marked Resolved').length;
    const resolved = mine.filter(r => r.status === 'Marked Resolved').length;
    const challenged = mine.filter(r => ['Disputed', 'Reinspection Required'].includes(r.verification)).length;
    mount(root, 'Overview', `
      ${heading({eyebrow: 'Your neighbourhood', title: `Hello, ${esc(v.name || 'neighbour')}`, lead: 'Your reports, what has changed, and where your neighbours are reporting.', actions: link(icon('camera') + '<span>Report an issue</span>', '#/camera', 'primary')})}
      <div class="stats" role="list">
        ${stat('My reports', mine.length, mine.length ? 'Claimed or submitted by you' : 'Claim a report to add it here')}
        ${stat('Open', open, 'Waiting for civic action', 'stat-navy')}
        ${stat('Marked resolved', resolved, 'Verification tracked separately', 'stat-green')}
        ${stat('Disputed or recheck', challenged, 'Needs a second look', 'stat-red')}
      </div>
      <div class="split">
        ${panel(`${panelHead('My recent reports', link('All my complaints', '#/resident/complaints', 'ghost'))}
          ${mine.length ? `<ul class="issue-list">${mine.slice(0, 5).map(issueRow).join('')}</ul>` : empty('No reports linked to your profile yet', 'Report an issue from the camera, or claim an anonymous report with its public ID and private claim code.', `<div class="row-actions">${link('Claim a complaint', '#/resident/claim', 'secondary')}${link('Report an issue', '#/camera', 'primary')}</div>`)}`, 'panel-main')}
        <div class="stack">
          ${panel(`${panelHead('Neighbourhood map', link('Open map', '#/resident/map', 'ghost'))}<div class="map-compact" id="overview-map"></div>`)}
          ${panel(`<h2>Close the loop</h2><p>When a repair is marked resolved, you can confirm it, report that it is still present, or say you cannot confirm. Those answers are kept beside the official status.</p><a class="inline-link" href="#/resident/community">Browse community issues ${icon('forward')}</a>`)}
        </div>
      </div>`);
  }
  const container = root.querySelector('#overview-map');
  if (container) {
    const map = await mountCityMap(container, {records, compact: true});
    addCleanup(() => map.destroy());
  }
}

function badgeSample() {
  return '<span class="pill">Illustrative sample</span>';
}
/** Complaints received per day over the last 12 days, from server records (measured, not illustrative). */
function activityChart(records) {
  const days = dailyActivity(records);
  const peak = Math.max(1, ...days.map(d => d.count));
  const total = days.reduce((a, d) => a + d.count, 0);
  return `<figure class="chart" aria-label="Complaints received per day over the last 12 days: ${total} in total"><div class="chart-bars">${days.map(d => `<div class="chart-bar" style="height:${Math.max(4, Math.round(d.count / peak * 100))}%" title="${esc(d.day)}: ${d.count}"><span>${esc(d.day.slice(8))}</span></div>`).join('')}</div><figcaption class="micro">${total} ${total === 1 ? 'complaint' : 'complaints'} received in the last 12 days (UTC dates), from the CivicLens server.</figcaption></figure>`;
}

function sampleChart() {
  const bars = [40, 65, 51, 84, 70, 92, 64, 78, 58, 86, 100, 73];
  const labels = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12'];
  return `<figure class="chart" aria-label="Illustrative reporting activity over 12 days"><div class="chart-bars">${bars.map((b, i) => `<div class="chart-bar" style="height:${b}%"><span>${labels[i]}</span></div>`).join('')}</div><figcaption class="micro">Sample series for layout only. Not measured activity. Last 12 days.</figcaption></figure>`;
}

// ---------- Complaint lists ----------
export async function renderComplaints(root, mode) {
  const records = await loadRecords();
  const admin = isAdmin();
  const base = admin ? records : mode === 'community' ? records : records.filter(r => r.mine);
  const title = admin ? 'Incident management' : mode === 'community' ? 'Community issues' : 'My complaints';
  const lead = admin
    ? 'Every case, its lifecycle status and its verification state. Filter, sort and open a case to act on it.'
    : mode === 'community'
      ? 'Neighbourhood reports you can read and review. Owner details, claim codes and private evidence are never shown here.'
      : 'Reports linked to your resident profile. Community issues are listed separately.';
  const actions = admin ? '' : mode === 'community' ? link('Claim a report', '#/resident/claim', 'secondary') : link(icon('camera') + '<span>Report an issue</span>', '#/camera', 'primary');
  mount(root, title, `
    ${heading({eyebrow: admin ? 'Operations' : 'Your workspace', title, lead, actions})}
    <section class="panel panel-flush">
      ${filterBar('complaint-filter', {withVerification: admin, withCategory: true, extra: mode === 'community' ? '<div class="field"><label for="complaint-scope">Show</label><select id="complaint-scope" name="scope"><option value="">All community issues</option><option value="following">Issues I follow</option></select></div>' : ''})}
      <p class="result-count" id="result-count" role="status" aria-live="polite"></p>
      <div id="complaint-results"></div>
    </section>`);
  const results = root.querySelector('#complaint-results');
  const count = root.querySelector('#result-count');
  const form = root.querySelector('#complaint-filter');
  const paint = () => {
    const f = readFilters(form);
    const scoped = f.scope === 'following' ? base.filter(r => r.following) : base;
    const list = sortRecords(filterRecords(scoped, f), f.sort);
    count.textContent = `${list.length} ${list.length === 1 ? 'case' : 'cases'} shown`;
    if (!list.length) {
      results.innerHTML = empty('No matching cases', mode === 'mine' && !admin ? 'Try a different filter, or claim a report with its public ID and private claim code.' : 'Clear the filters to see every case.', mode === 'mine' && !admin ? `<div class="row-actions">${link('Claim a complaint', '#/resident/claim', 'secondary')}</div>` : '');
      return;
    }
    if (admin || mode === 'mine') {
      results.innerHTML = `<div class="table-wrap"><table class="data-table">
        <caption class="sr-only">${admin ? 'All cases' : 'My complaints'}, ${list.length} shown</caption>
        <thead><tr><th scope="col">Complaint</th><th scope="col">Area and category</th>${admin ? '<th scope="col">Priority</th>' : ''}<th scope="col">Lifecycle</th><th scope="col">Verification</th></tr></thead>
        <tbody>${list.map(r => `<tr>
          <td><span class="cell-title"><span class="issue-symbol-sm ${resolutionState(r).className}" aria-hidden="true">${categoryIcon(r.category)}</span><span><a href="#/issue/${esc(r.id)}">${esc(r.title)}</a><small class="code-inline">${esc(r.id)}</small></span></span></td>
          <td>${esc(r.area)}<small>${esc(r.category)}</small></td>
          ${admin ? `<td>${r.priority ? priorityPill(r.priority.band) : ''}<small>Score ${r.priority?.score ?? '—'}</small></td>` : ''}
          <td>${statusPill(r.status)}</td>
          <td>${verificationPill(r.verification)}</td>
        </tr>`).join('')}</tbody></table></div>`;
    } else {
      results.innerHTML = `<ul class="issue-list issue-list-wide">${list.map(issueRow).join('')}</ul>`;
    }
  };
  form.addEventListener('input', paint);
  form.addEventListener('change', paint);
  form.addEventListener('submit', e => e.preventDefault());
  paint();
}

// ---------- Claim ----------
export function renderClaim(root) {
  mount(root, 'Claim a complaint', `
    ${heading({eyebrow: 'Anonymous report, resident follow-through', title: 'Claim a complaint', lead: 'Link a report to your resident profile. You need both its public ID and its private claim code. Both are on the receipt.'})}
    <div class="narrow-panel">
      <section class="panel">
        <form id="claim-form" class="stack">
          <div class="field"><label for="claim-id">Public complaint ID</label><input id="claim-id" name="id" required placeholder="NCL-2026-XXXXXX" autocomplete="off" spellcheck="false"><small class="micro">Shown on the receipt and in public tracking.</small></div>
          <div class="field"><label for="claim-code">Private claim code</label><input id="claim-code" name="code" type="password" required autocomplete="off" placeholder="Your private claim code"><small class="micro">Keep it private. Do not share it publicly.</small></div>
          <div id="claim-error" role="alert"></div>
          <button class="btn btn-primary btn-lg" type="submit">${icon('lock')}<span>Claim report</span></button>
        </form>
        ${notice('Sample report: public ID <span class="code-inline">NCL-2026-DEMO01</span> with claim code <span class="code-inline">DEMO-CLAIM-01</span>. Demonstration data only.', 'info', 'alert', 'mt')}
      </section>
    </div>`);
  root.querySelector('#claim-form').onsubmit = async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const submit = e.target.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const claimed = await api.claim(String(fd.get('id')).trim().toUpperCase(), String(fd.get('code')).trim(), viewer().email);
      toast(`${claimed.id} is now linked to your profile.`, 'success');
      navigate('resident/complaints');
    } catch (err) {
      submit.disabled = false;
      document.getElementById('claim-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };
}

// ---------- Notifications ----------
export async function renderNotifications(root) {
  const prefs = readPrefs();
  const lastRead = readLastRead();
  const events = await api.notificationsFor(viewer(), prefs, lastRead);
  const unread = events.filter(e => e.unread).length;
  mount(root, 'Notifications', `
    ${heading({eyebrow: 'Stay in the loop', title: 'Notifications', lead: unread ? `${unread} new ${unread === 1 ? 'update' : 'updates'} since you last checked.` : 'You are up to date.', actions: btn(icon('check') + '<span>Mark all as read</span>', 'data-action="mark-read"', 'secondary', events.length ? '' : 'disabled')})}
    ${notice(`Showing ${prefs.updates !== false ? 'status updates' : 'no status updates'} and ${prefs.verification !== false ? 'verification changes' : 'no verification changes'}. Change these in <a class="inline-link" href="#/${isAdmin() ? 'official' : 'resident'}/settings">Preferences</a>. Push delivery is not connected in this demo.`, 'info', 'bell')}
    <section class="panel panel-flush">
      ${events.length ? `<ul class="notification-list">${events.map(e => `<li class="notification ${e.unread ? 'is-unread' : ''}">
        <span class="notification-icon ${e.kind === 'response' ? 'tone-navy' : e.kind === 'verification' ? 'tone-coral' : ''}">${icon(e.kind === 'update' ? 'flag' : e.kind === 'response' ? 'message' : 'refresh')}</span>
        <div><p class="notification-title">${e.unread ? '<span class="pill pill-coral">New</span> ' : ''}${esc(e.text)}</p><p class="micro">${esc(e.title)} · <a class="inline-link" href="#/issue/${esc(e.caseId)}">${esc(e.caseId)}</a></p></div>
        <time datetime="${esc(e.date)}">${fmtDateTime(e.date)}</time>
      </li>`).join('')}</ul>` : empty('No notifications match your preferences', 'Follow an issue or report one to receive updates here.')}
    </section>`);
  root.querySelector('[data-action="mark-read"]')?.addEventListener('click', () => {
    writeLastRead(new Date().toISOString());
    toast('All notifications are marked as read.', 'success');
    renderNotifications(root);
  });
}

// ---------- Preferences ----------
export function renderSettings(root) {
  const prefs = readPrefs();
  const admin = isAdmin();
  mount(root, 'Preferences', `
    ${heading({eyebrow: 'Your workspace', title: 'Preferences', lead: 'Choose what this browser shows you. Preferences are stored on this device for your account.'})}
    <div class="split">
      <section class="panel">
        ${panelHead('Notifications')}
        <form id="prefs-form" class="stack">
          <label class="switch-row"><span><strong>Status updates</strong><small class="micro">Lifecycle changes and public responses on your cases${admin ? ' and all cases' : ' or cases you follow'}.</small></span><input type="checkbox" name="updates" ${prefs.updates ? 'checked' : ''}></label>
          <label class="switch-row"><span><strong>Verification changes</strong><small class="micro">Citizen confirmations, disputes and reinspection requests.</small></span><input type="checkbox" name="verification" ${prefs.verification ? 'checked' : ''}></label>
          <label class="switch-row is-disabled"><span><strong>Hotspot alerts</strong><small class="micro">Unavailable: forecasting is illustrative and no validated model is connected.</small></span><input type="checkbox" name="hotspots" disabled></label>
        </form>
        <p class="micro mt" id="prefs-status" role="status"></p>
      </section>
      <section class="panel">
        ${panelHead('Demonstration data')}
        <p>Reset clears reports, evidence files, video reviews and profile claims from this browser, then restores the sample workspace. It does not affect other devices.</p>
        ${btn(icon('refresh') + '<span>Reset sample workspace</span>', 'data-action="reset"', 'secondary')}
        ${notice('Local data is visible to anyone who can use this browser. Production data would be governed by server-side access control and retention rules.', 'warn', 'lock', 'mt')}
      </section>
    </div>`);
  root.querySelector('#prefs-form').addEventListener('change', e => {
    const next = {...readPrefs(), [e.target.name]: e.target.checked};
    const ok = writePrefs(next);
    document.getElementById('prefs-status').textContent = ok ? 'Preference saved on this device.' : 'Preference could not be saved: browser storage is blocked.';
  });
  root.querySelector('[data-action="reset"]').onclick = async () => {
    const yes = await confirmDialog({title: 'Reset the demonstration workspace?', message: 'This removes every locally created report, evidence file and video review in this browser. It cannot be undone.', confirmLabel: 'Reset workspace'});
    if (!yes) return;
    try {
      await api.reset();
      toast('Sample workspace restored.', 'success');
      navigate(isAdmin() ? 'official' : 'resident');
    } catch (err) {
      toast('Reset did not finish: ' + (err.message || 'storage was unavailable.'), 'error');
    }
  };
}

// ---------- Work orders (admin) ----------
export async function renderWorkOrders(root) {
  const records = await loadRecords();
  if (await usesServerRecords()) return renderServerWorkOrders(root, records);
  const cases = await Promise.all(records.map(r => api.detail(r.id, viewer())));
  const orders = cases.flatMap(c => (c?.workOrders || []).map(w => ({...w, caseId: c.id, title: c.title, status_: c.status})));
  const today = new Date().toISOString().slice(0, 10);
  const awaiting = records.filter(r => ['Submitted', 'Under Review'].includes(r.status));
  const rows = orders.sort((a, b) => (b.createdAt > a.createdAt ? 1 : -1)).map(w => {
    const overdue = w.due && w.due < today && w.status !== 'Closed';
    return `<tr><td><a href="#/issue/${esc(w.caseId)}">${esc(w.title)}</a><small class="code-inline">${esc(w.caseId)}</small></td><td>${esc(w.team)}</td><td>${w.due ? fmtDate(w.due) : 'Not set'}${overdue ? ' <span class="pill pill-red">Overdue</span>' : ''}</td><td>${esc(w.instructions || '—')}</td><td>${fmtDate(w.createdAt)}</td></tr>`;
  }).join('');
  mount(root, 'Work orders', `
    ${heading({eyebrow: 'From insight to action', title: 'Work orders', lead: 'Assign a responsible team and a target date. The assignment is recorded in the public timeline.'})}
    <div class="split">
      <section class="panel" aria-labelledby="wo-form-title">
        ${panelHead('Create a work order')}
        <form id="work-form" class="stack">
          <div class="field"><label for="wo-case">Case</label><select id="wo-case" name="id" required>${records.map(r => `<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.title)}</option>`).join('')}</select></div>
          <div class="field"><label for="wo-team">Responsible team</label><input id="wo-team" name="team" required maxlength="120" placeholder="Ward maintenance team"></div>
          <div class="field"><label for="wo-due">Target date</label><input id="wo-due" name="due" type="date" min="${today}"></div>
          <div class="field"><label for="wo-instructions">Instructions for the team</label><textarea id="wo-instructions" name="instructions" rows="3" maxlength="1000" placeholder="Scope, safety precautions and how to confirm completion."></textarea></div>
          <div id="work-error" role="alert"></div>
          <button class="btn btn-primary" type="submit">${icon('plus')}<span>Create work order</span></button>
        </form>
      </section>
      <div class="stack">
        ${panel(`${panelHead('Awaiting assignment', '', 'Submitted or under review')}
          ${awaiting.length ? `<ul class="issue-list">${awaiting.slice(0, 6).map(issueRow).join('')}</ul>` : '<p class="micro">Nothing is waiting for assignment.</p>'}`)}
      </div>
    </div>
    <section class="panel panel-flush mt">
      ${panelHead('Open and closed work orders')}
      ${orders.length ? `<div class="table-wrap"><table class="data-table"><caption class="sr-only">Work orders</caption><thead><tr><th scope="col">Case</th><th scope="col">Team</th><th scope="col">Target date</th><th scope="col">Instructions</th><th scope="col">Created</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty('No work orders yet', 'Create one above. Assigning a team also moves the case to Assigned.')}
    </section>`);
  root.querySelector('#work-form').onsubmit = async e => {
    e.preventDefault();
    const f = readFilters(e.target);
    try {
      await api.addWorkOrder(viewer(), f.id, {team: f.team, due: f.due, instructions: f.instructions});
      toast('Work order created and case assigned.', 'success');
      renderWorkOrders(root);
    } catch (err) {
      document.getElementById('work-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };
}

// Server mode: work orders do not exist on the server yet (they arrive with complaint management), so
// nothing is created in the browser for a server complaint. The queue of cases awaiting assignment is real.
function renderServerWorkOrders(root, records) {
  const awaiting = records.filter(r => ['Submitted', 'Under Review'].includes(r.status));
  mount(root, 'Work orders', `
    ${heading({eyebrow: 'From insight to action', title: 'Work orders', lead: 'Assign a responsible team and a target date, and follow each order to completion.'})}
    ${notice('<strong>Not available on the server yet.</strong> Work orders, assignment and status changes are part of server-side complaint management, which is still being built. Nothing is created in this browser for a server complaint, so no change here could be lost or contradict the server.', 'info', 'alert')}
    <div class="split mt">
      ${panel(`${panelHead('Awaiting assignment', '', 'Submitted or under review on the server')}
        ${awaiting.length ? `<ul class="issue-list">${awaiting.slice(0, 12).map(issueRow).join('')}</ul>` : '<p class="micro">Nothing is waiting for assignment.</p>'}`, 'panel-main')}
      ${panel(`${panelHead('Open and closed work orders')}${empty('No server work orders yet', 'They will appear here once complaint management is deployed on the server.')}`)}
    </div>`);
}

export {isAdmin, fmtDate, btn, link, panel, panelHead, notice, empty, icon, statusPill, verificationPill, priorityPill};
