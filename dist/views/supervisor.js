// Supervisor workspace (server mode): the city → zone → ward dashboard and multi-ward allocation, in the
// existing workspace design. Every number comes from GET /staff/supervisor/dashboard (real complaint rows);
// the server checks role and geographic scope on every request, so this page only shows what it returns.
// Wards are never inferred here: complaints without a verified ward are listed for manual ward setting.
import {esc, categories} from '../services.js';
import {icon, workspaceShell, heading, btn, link, statusPill, notice, empty, panel, panelHead, toast, fmtDate} from '../ui.js';
import {setSession} from '../state.js';
import {navigate} from '../router.js';
import {ApiError} from '../api-client.js';
import {staffSession} from '../staff-session.js';
import {serverClient, usesServerRecords} from '../server-records.js';
import {wireWorkspace} from './workspace.js';

const UNRESOLVED = ['Submitted', 'Under Review', 'Assigned', 'In Progress'];
const PRIORITIES = ['low', 'normal', 'high', 'urgent'];
const SELECTED_KEY = 'ncl-supervisor-wards';

/** True when the signed-in server staff member is a supervisor or admin (the server re-checks). */
export function isSupervisor(staff = staffSession.get()?.staff) {
  return ['supervisor', 'admin'].includes(staff?.role);
}

function mount(root, title, body) {
  root.innerHTML = workspaceShell({title, body});
  wireWorkspace(root);
}

function stat(label, value, hint, tone = '') {
  return `<div class="stat ${tone}" role="listitem"><p class="stat-label">${esc(label)}</p><p class="stat-value">${esc(value)}</p><p class="stat-hint">${esc(hint)}</p></div>`;
}

function countCells(c) {
  return `<td>${c.total}</td><td>${c.open}</td><td>${c.resolved}</td><td>${c.pendingVerification}</td><td>${c.overdue ? `<span class="pill pill-red">${c.overdue}</span>` : '0'}</td>`;
}

const COUNT_HEAD = '<th scope="col">Total</th><th scope="col">Open</th><th scope="col">Resolved</th><th scope="col">Pending verification</th><th scope="col">Overdue</th>';

function failed(root, title, err) {
  if (err instanceof ApiError && err.status === 401) {
    staffSession.clear();
    setSession(null);
    return navigate('login/official');
  }
  mount(root, title, `${heading({eyebrow: 'Supervisor operations', title})}${notice(esc(err instanceof ApiError ? err.message : 'The supervisor data could not be loaded.'), 'error', 'alert')}`);
}

function readSelected() {
  try { return JSON.parse(sessionStorage.getItem(SELECTED_KEY) || '[]').filter(Number.isInteger); } catch { return []; }
}

function writeSelected(numbers) {
  try { sessionStorage.setItem(SELECTED_KEY, JSON.stringify(numbers)); } catch { /* selection is a convenience only */ }
}

/** #/official/supervisor[/zone/N | /ward/N | /allocate | /unverified] */
export async function renderSupervisor(root, parts = []) {
  if (!(await usesServerRecords()) || !isSupervisor()) {
    return mount(root, 'Supervisor', `${heading({eyebrow: 'Supervisor operations', title: 'Supervisor workspace'})}
      ${notice('Supervisor operations need a CivicLens server session with the supervisor or admin role.', 'info', 'alert')}`);
  }
  const [view, value] = parts;
  if (view === 'allocate') return renderAllocate(root);
  if (view === 'unverified') return renderAllocate(root, {unverified: true});
  return renderDashboard(root, view === 'zone' ? Number(value) : null, view === 'ward' ? Number(value) : null);
}

async function renderDashboard(root, zone, ward) {
  const http = await serverClient();
  const token = staffSession.get()?.token;
  let d;
  let list = null;
  try {
    d = await http.supervisorDashboard(token, {zone, ward});
    if (ward) list = await http.supervisorComplaints(token, {wards: [ward], limit: 100});
  } catch (err) {
    return failed(root, 'Supervisor dashboard', err);
  }
  const t = d.totals;
  const wardInfo = ward ? d.byWard[0] : null;
  const crumbs = [`<a href="#/official/supervisor">${d.cityWide ? 'Bengaluru' : 'My scope'}</a>`];
  if (zone || wardInfo) {
    const z = zone ? d.byZone[0] : wardInfo.zone;
    crumbs.push(`<a href="#/official/supervisor/zone/${esc(z.number)}">${esc(z.name)} zone</a>`);
  }
  if (wardInfo) crumbs.push(`Ward ${esc(wardInfo.number)} · ${esc(wardInfo.name)}`);
  const title = wardInfo ? `Ward ${esc(wardInfo.number)} · ${esc(wardInfo.name)}` : zone ? `${esc(d.byZone[0]?.name || '')} zone` : 'Supervisor dashboard';

  const zoneRows = d.level === 'city' ? d.byZone.map(z => `<tr><th scope="row"><a href="#/official/supervisor/zone/${z.number}">${esc(z.number)} · ${esc(z.name)}</a><small>${z.wards} wards</small></th>${countCells(z)}</tr>`).join('') : '';
  const wardRows = d.byWard.filter(w => d.level !== 'city' || w.total).map(w => `<tr><th scope="row"><a href="#/official/supervisor/ward/${w.number}">${esc(w.number)} · ${esc(w.name)}</a><small>${esc(w.zone.name)}</small></th>${countCells(w)}</tr>`).join('');
  const categoryRows = d.byCategory.map(c => `<tr><th scope="row">${esc(c.category)}</th>${countCells(c)}</tr>`).join('');
  const complaintRows = list ? list.items.map(c => `<tr><td><a href="#/issue/${esc(c.id)}">${esc(c.title)}</a><small class="code-inline">${esc(c.id)}</small></td>
      <td>${statusPill(c.status)}</td><td>${c.ageDays} d</td><td>${c.assignee ? esc(c.assignee.displayName) : '—'}</td>
      <td>${c.priority ? esc(c.priority) : '—'}${c.overdue ? ' <span class="pill pill-red">Overdue</span>' : ''}</td></tr>`).join('') : '';

  mount(root, 'Supervisor dashboard', `
    ${heading({eyebrow: 'Supervisor operations', title, lead: 'Counts come from complaint records on the CivicLens server. Merged duplicates are counted once, through their main complaint.',
      actions: link(icon('list') + '<span>Allocate complaints</span>', '#/official/supervisor/allocate', 'primary')})}
    <nav class="micro" aria-label="Breadcrumb">${crumbs.join(' › ')}</nav>
    <div class="stats" role="list">
      ${stat('Total complaints', t.total, d.level === 'city' ? (d.cityWide ? 'City-wide' : 'Across your wards') : 'In this area')}
      ${stat('Open', t.open, 'Not marked resolved', 'stat-navy')}
      ${stat('Overdue', t.overdue, 'Past the due date or work-order target', 'stat-red')}
      ${stat('Resolved', t.resolved, `${t.pendingVerification} awaiting citizen verification`, 'stat-green')}
    </div>
    ${d.unverifiedWard ? notice(`<strong>${d.unverifiedWard.total}</strong> ${d.unverifiedWard.total === 1 ? 'complaint has' : 'complaints have'} no verified ward (${d.unverifiedWard.open} open). No official ward boundary file is loaded, so wards are not set automatically. <a href="#/official/supervisor/unverified">Set wards</a>.`, 'info', 'alert') : ''}
    ${list ? panel(`${panelHead('Unresolved complaints in this ward', `<span class="pill">${list.total}</span>`)}
      ${complaintRows ? `<div class="table-wrap"><table class="data-table"><caption class="sr-only">Unresolved complaints</caption><thead><tr><th scope="col">Complaint</th><th scope="col">Status</th><th scope="col">Age</th><th scope="col">Assigned to</th><th scope="col">Priority</th></tr></thead><tbody>${complaintRows}</tbody></table></div>` : empty('Nothing unresolved', 'Every complaint in this ward is marked resolved.')}`, 'panel-flush') : ''}
    ${zoneRows ? panel(`${panelHead('By zone', '', 'Open a zone to see its wards')}<div class="table-wrap"><table class="data-table"><caption class="sr-only">Complaints by zone</caption><thead><tr><th scope="col">Zone</th>${COUNT_HEAD}</tr></thead><tbody>${zoneRows}</tbody></table></div>`, 'panel-flush mt') : ''}
    ${d.level !== 'ward' ? panel(`${panelHead('By ward', '', d.level === 'city' ? 'Wards with complaints' : 'Every ward in the zone')}${wardRows ? `<div class="table-wrap"><table class="data-table"><caption class="sr-only">Complaints by ward</caption><thead><tr><th scope="col">Ward</th>${COUNT_HEAD}</tr></thead><tbody>${wardRows}</tbody></table></div>` : empty('No complaints with a verified ward yet', 'Set wards on unverified complaints to see them here.')}`, 'panel-flush mt') : ''}
    ${panel(`${panelHead('By category')}${categoryRows ? `<div class="table-wrap"><table class="data-table"><caption class="sr-only">Complaints by category</caption><thead><tr><th scope="col">Category</th>${COUNT_HEAD}</tr></thead><tbody>${categoryRows}</tbody></table></div>` : empty('No complaints yet')}`, 'panel-flush mt')}`);
}

async function renderAllocate(root, {unverified = false} = {}) {
  const http = await serverClient();
  const token = staffSession.get()?.token;
  let scope, staffList, departments;
  try {
    [scope, staffList, departments] = await Promise.all([http.supervisorScope(token), http.supervisorStaff(token), http.listDepartments(token)]);
  } catch (err) {
    return failed(root, 'Allocate complaints', err);
  }
  const allowed = new Set(scope.zones.flatMap(z => z.wards.map(w => w.number)));
  let selected = readSelected().filter(n => allowed.has(n));
  const filters = {category: '', status: '', minAgeDays: '', maxAgeDays: ''};

  const wardPicker = scope.zones.map(z => `<details class="ward-zone"><summary><strong>${esc(z.number)} · ${esc(z.name)}</strong> <span class="micro">${z.wards.length} wards</span>
      <button type="button" class="btn btn-ghost btn-sm" data-zone-all="${z.number}">Select zone</button></summary>
      <div class="ward-grid">${z.wards.map(w => `<label class="check"><input type="checkbox" data-ward="${w.number}" ${selected.includes(w.number) ? 'checked' : ''}> <span>${w.number} · ${esc(w.name)}</span></label>`).join('')}</div></details>`).join('');

  mount(root, unverified ? 'Set wards' : 'Allocate complaints', `
    ${heading({eyebrow: 'Supervisor operations', title: unverified ? 'Complaints without a verified ward' : 'Multi-ward allocation',
      lead: unverified ? 'No official ward boundary file is loaded, so the server never guesses a ward from GPS or address text. Open a complaint to check its location and set the ward.'
        : 'Choose wards, filter the unresolved complaints in them, then allocate a selection to one staff member with a priority, a due date and an internal note. Every allocation is kept as history.',
      actions: link(icon('dashboard') + '<span>Dashboard</span>', '#/official/supervisor', 'secondary')})}
    ${!unverified && !scope.wardCount ? notice('You have no wards in your scope yet. An administrator grants ward or zone scope with the staff CLI.', 'info', 'alert') : ''}
    <div class="split">
      ${unverified ? '' : `<section class="panel" aria-labelledby="wards-title">
        ${panelHead('Wards', `<span class="pill" id="ward-count">${selected.length} selected</span>`, scope.cityWide ? 'City-wide scope' : 'Your scope')}
        <div class="stack" id="ward-picker">${wardPicker || '<p class="micro">No wards.</p>'}</div>
        <div class="row-actions mt">${btn('Clear selection', 'id="ward-clear"', 'ghost', 'btn-sm')}</div>
      </section>`}
      <section class="panel" aria-labelledby="filters-title">
        ${panelHead('Filters')}
        <form id="sup-filters" class="stack">
          <div class="field"><label for="sf-category">Category</label><select id="sf-category" name="category"><option value="">All categories</option>${categories.map(c => `<option>${esc(c)}</option>`).join('')}</select></div>
          <div class="field"><label for="sf-status">Status</label><select id="sf-status" name="status"><option value="">All unresolved</option>${UNRESOLVED.map(s => `<option>${s}</option>`).join('')}</select></div>
          <div class="field"><label for="sf-min">Older than (days)</label><input id="sf-min" name="minAgeDays" type="number" min="0" max="3650"></div>
          <div class="field"><label for="sf-max">Newer than (days)</label><input id="sf-max" name="maxAgeDays" type="number" min="0" max="3650"></div>
          <button type="submit" class="btn btn-secondary">${icon('search')}<span>Show complaints</span></button>
        </form>
      </section>
    </div>
    <section class="panel panel-flush mt">
      ${panelHead('Unresolved complaints', '<span class="pill" id="sup-total">—</span>', 'Oldest first')}
      <div id="sup-list"><p class="micro">${unverified ? 'Loading…' : 'Select wards, then show complaints.'}</p></div>
    </section>
    ${unverified ? '' : `<section class="panel mt" aria-labelledby="alloc-title">
      ${panelHead('Allocate the selected complaints', '<span class="pill" id="alloc-count">0 selected</span>')}
      <form id="alloc-form" class="stack">
        <div class="field"><label for="al-assignee">Assign to</label><select id="al-assignee" required><option value="">Choose a staff member</option>${staffList.map(s => `<option value="${esc(s.username)}">${esc(s.displayName)} (${esc(s.role)}${s.departments.length ? ' · ' + esc(s.departments.join(', ')) : ''})</option>`).join('')}</select><small class="micro">Officials must be members of the department.</small></div>
        <div class="field"><label for="al-department">Department</label><select id="al-department"><option value="">Keep each complaint's department</option>${departments.map(d => `<option value="${esc(d.code)}">${esc(d.name)}</option>`).join('')}</select></div>
        <div class="field"><label for="al-priority">Priority</label><select id="al-priority">${PRIORITIES.map(p => `<option ${p === 'normal' ? 'selected' : ''}>${p}</option>`).join('')}</select></div>
        <div class="field"><label for="al-due">Due date <span class="optional">Optional</span></label><input id="al-due" type="date"></div>
        <div class="field"><label for="al-reason">Reason</label><input id="al-reason" required maxlength="500" placeholder="Why these complaints go to this person"></div>
        <div class="field"><label for="al-note">Internal note <span class="optional">Staff only · never shown publicly</span></label><textarea id="al-note" rows="2" maxlength="2000"></textarea></div>
        <div id="alloc-error" role="alert"></div>
        <button type="submit" class="btn btn-primary">${icon('user')}<span>Allocate</span></button>
      </form>
    </section>`}`);

  const $ = id => root.querySelector('#' + id);
  const picked = () => [...root.querySelectorAll('[data-pick]:checked')].map(i => i.dataset.pick);
  const updateCounts = () => {
    if ($('ward-count')) $('ward-count').textContent = `${selected.length} selected`;
    if ($('alloc-count')) $('alloc-count').textContent = `${picked().length} selected`;
  };

  async function load() {
    const slot = $('sup-list');
    if (!unverified && !selected.length) {
      slot.innerHTML = '<p class="micro">Select at least one ward.</p>';
      $('sup-total').textContent = '—';
      return updateCounts();
    }
    slot.innerHTML = '<p class="micro">Loading…</p>';
    try {
      const list = await http.supervisorComplaints(token, {wards: selected, unverifiedWard: unverified, limit: 100, ...filters});
      $('sup-total').textContent = String(list.total);
      slot.innerHTML = list.items.length ? `<div class="table-wrap"><table class="data-table"><caption class="sr-only">Unresolved complaints</caption>
        <thead><tr>${unverified ? '' : '<th scope="col"><span class="sr-only">Select</span></th>'}<th scope="col">Complaint</th><th scope="col">Ward</th><th scope="col">Status</th><th scope="col">Age</th><th scope="col">Assigned to</th><th scope="col">Priority</th></tr></thead>
        <tbody>${list.items.map(c => `<tr>${unverified ? '' : `<td><input type="checkbox" data-pick="${esc(c.id)}" aria-label="Select ${esc(c.id)}"></td>`}
          <td><a href="#/issue/${esc(c.id)}">${esc(c.title)}</a><small class="code-inline">${esc(c.id)}</small></td>
          <td>${c.ward ? `${esc(c.ward.number)} · ${esc(c.ward.name)}` : '<span class="pill">Unverified</span>'}</td>
          <td>${statusPill(c.status)}</td><td>${c.ageDays} d</td><td>${c.assignee ? esc(c.assignee.displayName) : '—'}</td>
          <td>${c.priority ? esc(c.priority) : '—'}${c.dueAt ? `<small>due ${fmtDate(c.dueAt)}</small>` : ''}${c.overdue ? ' <span class="pill pill-red">Overdue</span>' : ''}</td></tr>`).join('')}</tbody></table></div>
        ${list.total > list.items.length ? `<p class="micro">Showing the oldest ${list.items.length} of ${list.total}. Narrow the filters to see the rest.</p>` : ''}`
        : empty('No unresolved complaints', unverified ? 'Every complaint has a verified ward.' : 'Nothing matches these wards and filters.');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return failed(root, 'Allocate complaints', err);
      slot.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'Complaints could not be loaded.'), 'error', 'alert');
    }
    updateCounts();
  }

  root.querySelector('#ward-picker')?.addEventListener('change', e => {
    const box = e.target.closest('[data-ward]');
    if (!box) return;
    const n = Number(box.dataset.ward);
    selected = box.checked ? [...new Set([...selected, n])] : selected.filter(x => x !== n);
    writeSelected(selected);
    updateCounts();
  });
  root.querySelector('#ward-picker')?.addEventListener('click', e => {
    const all = e.target.closest('[data-zone-all]');
    if (!all) return;
    e.preventDefault();
    const zone = scope.zones.find(z => z.number === Number(all.dataset.zoneAll));
    selected = [...new Set([...selected, ...zone.wards.map(w => w.number)])];
    zone.wards.forEach(w => { const box = root.querySelector(`[data-ward="${w.number}"]`); if (box) box.checked = true; });
    writeSelected(selected);
    updateCounts();
  });
  $('ward-clear')?.addEventListener('click', () => {
    selected = [];
    writeSelected(selected);
    root.querySelectorAll('[data-ward]').forEach(b => { b.checked = false; });
    load();
  });
  $('sup-filters').addEventListener('submit', e => {
    e.preventDefault();
    Object.assign(filters, {category: $('sf-category').value, status: $('sf-status').value, minAgeDays: $('sf-min').value, maxAgeDays: $('sf-max').value});
    load();
  });
  $('sup-list').addEventListener('change', updateCounts);
  $('alloc-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const complaints = picked();
    const error = $('alloc-error');
    error.innerHTML = '';
    if (!complaints.length) { error.innerHTML = notice('Select at least one complaint in the list.', 'error', 'alert'); return; }
    const submit = e.target.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const result = await http.allocateComplaints(token, {complaints, assignee: $('al-assignee').value, department: $('al-department').value || null,
        priority: $('al-priority').value, dueAt: $('al-due').value || null, reason: $('al-reason').value.trim(), note: $('al-note').value.trim()});
      toast(`${result.changed} allocated${result.unchanged ? `, ${result.unchanged} already allocated that way` : ''}.`, 'success');
      $('al-note').value = '';
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return failed(root, 'Allocate complaints', err);
      error.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'The allocation could not be saved.'), 'error', 'alert');
    } finally {
      submit.disabled = false;
    }
  });
  if (unverified || selected.length) load();
}
