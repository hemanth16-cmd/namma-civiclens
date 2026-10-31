// A server complaint in the original case-page design (views/issue.js) for signed-in staff in server mode:
// the same shell, state strip, before/after evidence, histories, "About this case", verification panel and
// case actions, built from GET /staff/complaints/{id} and acting through the complaint-management API.
// The server decides what each person may do (allowedActions); this page only offers those actions and
// re-reads the case after every change. Private evidence opens on request because every view is logged.
import {esc, assessPriority} from '../services.js';
import {icon, categoryIcon, resolutionState, workspaceShell, heading, btn, statusPill, verificationPill, priorityPill, notice, panel, panelHead, toast, fmtDate, fmtDateTime, openLightbox, empty, pill} from '../ui.js';
import {setSession} from '../state.js';
import {navigate, rerender} from '../router.js';
import {ApiError} from '../api-client.js';
import {staffSession} from '../staff-session.js';
import {serverClient} from '../server-records.js';
import {figure, geoBlock, labelOf, objectURL} from './issue.js';
import {aiSections, mountAiPanels, overlayDetections} from './staff-ai.js';

const $ = id => document.getElementById(id);
const can = (r, action) => r.allowedActions.some(a => a.action === action);
const field = (id, label, control, hint = '') => `<div class="field"><label for="${id}">${label}</label>${control}${hint ? `<small class="micro">${hint}</small>` : ''}</div>`;

/** Shown where the original page had a feature that needs data the server does not hold yet. */
const pending = text => `<p class="micro">${icon('alert', 'mini-icon')} ${text}</p>`;

function signedOut() {
  staffSession.clear();
  setSession(null);
  navigate('login/official');
}

export async function renderServerCase(root, id) {
  const http = await serverClient();
  const token = staffSession.get()?.token;
  if (!http || !token) return signedOut();
  let r;
  try {
    r = await http.getStaffComplaint(token, id);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return signedOut();
    const title = err instanceof ApiError && err.status === 403 ? 'Outside your departments' : 'Case not available';
    root.innerHTML = workspaceShell({title, body: `${heading({title, lead: esc(err instanceof ApiError ? err.message : 'The case could not be loaded.')})}
      ${empty('Nothing to show', 'Check the ID, or return to the incident list.', '<a class="btn btn-secondary" href="#/official/complaints">Back to cases</a>')}`});
    return;
  }

  const solved = resolutionState(r);
  const title = `${r.category} reported by a citizen`;
  const repairs = r.workOrders.flatMap(w => w.evidence.filter(e => e.phase === 'after').map(e => ({...e, order: w})))
    .sort((a, b) => String(a.uploadedAt).localeCompare(String(b.uploadedAt)));
  const latest = repairs.at(-1);
  const activeOrder = r.workOrders.find(w => ['Open', 'In Progress'].includes(w.status));
  const team = (activeOrder || r.workOrders.at(-1))?.team || '';
  const priority = assessPriority({...r, sample: false, recurrence: 0});
  const department = r.assignment.department?.name || r.department;

  const repairHistory = repairs.length
    ? `<ol class="history-list">${repairs.slice().reverse().map(e => `<li>
        <div class="tl-head"><strong>After-repair evidence · ${esc(e.order.team)}</strong><span class="pill">Staff only</span></div>
        <p class="micro">Work order ${esc(e.order.status.toLowerCase())} · uploaded ${fmtDateTime(e.uploadedAt)}</p>
        <p>${btn(icon('eye') + '<span>View repair photo</span>', `data-repair="${esc(e.evidenceId)}" data-order="${esc(e.order.id)}"`, 'ghost', 'btn-sm')}</p>
      </li>`).join('')}</ol>`
    : '<p class="micro">No repair evidence has been uploaded yet.</p>';

  const verificationHistory = r.verificationLog.length
    ? `<ol class="history-list">${r.verificationLog.slice().reverse().map(e => `<li>
        <div class="tl-head"><strong>${e.type === 'response' ? `Citizen response: ${esc(e.response)}` : `Verification: ${esc(e.from || '—')} → ${esc(e.to)}`}</strong><time datetime="${esc(e.date)}">${fmtDate(e.date)}</time></div>
        ${e.note ? `<p>${esc(e.note)}</p>` : ''}<p class="micro">${e.by === 'citizen' ? 'Citizen response' : 'Civic team'} · earlier records are kept</p></li>`).join('')}</ol>`
    : '<p class="micro">No verification events yet.</p>';

  const timeline = r.timeline.slice().reverse().map(t => `<li><div class="tl-head"><strong>${esc(t.status)}</strong><time datetime="${esc(t.date)}">${fmtDate(t.date)}</time></div><p>${esc(t.text)}</p></li>`).join('');

  // The before/after figures keep the original design; the private file is fetched only on request.
  const what = r.mediaType === 'video' ? 'video' : 'photo';
  const beforeFigure = r.evidence
    ? `<figure class="evidence-figure" id="figure-before"><figcaption><span>Before · reported issue</span><span class="pill">${r.mediaType === 'video' ? 'Video' : 'Photo'}</span></figcaption>
        <div class="evidence-empty">${categoryIcon(r.category)}<p><strong>Private evidence</strong></p><p class="micro">Opening it records your name and the time in the access log.</p>
        ${btn(icon('eye') + `<span>View ${what}</span>`, 'data-load="before" id="staff-view-evidence"', 'primary', 'btn-sm')}</div>
        ${geoBlock(r.gps, 'No GPS metadata for this original')}</figure>`
    : figure({title: 'Before · reported issue', url: null, emptyGeo: 'No GPS metadata for this original', emptyText: 'No evidence has been uploaded for this complaint'});
  const afterFigure = latest
    ? `<figure class="evidence-figure" id="figure-after"><figcaption><span>After · repair evidence</span><span class="pill">Photo</span></figcaption>
        <div class="evidence-empty">${categoryIcon(r.category)}<p><strong>Latest repair photo</strong></p><p class="micro">Uploaded ${fmtDateTime(latest.uploadedAt)} for ${esc(latest.order.team)}.</p>
        ${btn(icon('eye') + '<span>View photo</span>', 'data-load="after"', 'primary', 'btn-sm')}</div>
        ${geoBlock(null, 'Location is not read from repair photos')}</figure>`
    : figure({title: 'After · repair evidence', url: null, emptyGeo: 'No GPS metadata supplied for the repair', emptyText: 'No repair evidence yet'});

  const transitions = r.allowedActions.filter(a => a.action === 'transition');
  const orders = r.workOrders.map(w => `<tr data-work-order="${esc(w.id)}">
      <td>${esc(w.team)}${w.description ? `<small>${esc(w.description)}</small>` : ''}</td>
      <td>${esc(w.status)}${w.overdue ? ' <span class="pill pill-red">Overdue</span>' : ''}<small>${w.dueAt ? `Target ${fmtDate(w.dueAt)}` : 'No target date'}</small></td>
      <td class="row-actions">${w.status === 'Open' ? btn('Start', 'data-wo-to="In Progress"', 'secondary', 'btn-sm') : ''}
        ${w.status === 'In Progress' ? btn('Complete', 'data-wo-to="Completed"', 'secondary', 'btn-sm') : ''}
        ${['Open', 'In Progress'].includes(w.status) ? `${btn('Cancel', 'data-wo-to="Cancelled"', 'ghost', 'btn-sm')}
          <label class="btn btn-ghost btn-sm">${icon('upload', 'mini-icon')}<span>After photo</span><input type="file" accept="image/jpeg" data-wo-upload hidden></label>` : ''}</td></tr>`).join('');

  const adminPanel = `
    <section class="panel admin-panel" aria-labelledby="admin-title">
      ${panelHead('Case actions', '', 'Civic team only')}
      <div id="case-message" role="status" aria-live="polite"></div>
      <dl class="meta-list">
        <div><dt>Department</dt><dd>${r.assignment.department ? esc(r.assignment.department.name) : 'Not assigned (triage queue)'}</dd></div>
        <div><dt>Assigned to</dt><dd>${r.assignment.assignee ? `${esc(r.assignment.assignee.displayName)} (${esc(r.assignment.assignee.username)})` : 'Nobody yet'}</dd></div>
      </dl>
      ${transitions.length ? `<form id="case-form" class="stack" novalidate>
        ${field('case-status', 'Lifecycle status', `<select id="case-status" name="to">${transitions.map(t => `<option value="${esc(t.to)}">${esc(t.label || t.to)} → ${esc(t.to)}</option>`).join('')}</select>`, `Now: ${esc(r.status)}. Only the changes the server allows you are listed.`)}
        ${field('case-explanation', 'Public explanation <span class="optional">Shown in public tracking · required for some changes</span>', '<textarea id="case-explanation" rows="2" maxlength="500" placeholder="What the public should know about this change"></textarea>')}
        <div class="row-actions"><button type="submit" class="btn btn-primary">${icon('check')}<span>Save status change</span></button></div>
      </form>` : pending('No status change is available to you at this stage.')}
      ${can(r, 'assign') ? `<form id="assign-form" class="stack mt">
        <h3>${r.assignment.department ? 'Reassign' : 'Assign'}</h3>
        ${field('case-department', 'Department', '<select id="case-department" required><option value="">Loading…</option></select>')}
        ${field('case-assignee', 'Staff username <span class="optional">Optional · officials must be members of the department</span>', '<input id="case-assignee" autocomplete="off" spellcheck="false" maxlength="64">')}
        ${field('case-reason', `Reason${r.assignment.department ? '' : ' <span class="optional">Optional</span>'}`, `<input id="case-reason" maxlength="500" ${r.assignment.department ? 'required' : ''}>`)}
        <div class="row-actions"><button type="submit" class="btn btn-secondary">${icon('user')}<span>Save assignment</span></button></div>
      </form>` : ''}
      ${can(r, 'add_public_update') ? `<form id="update-form" class="stack mt">
        ${field('case-public', 'Public update <span class="optional">Visible to the public timeline</span>', '<textarea id="case-public" rows="2" maxlength="500" required placeholder="What the public should know about progress"></textarea>')}
        <label class="check"><input type="checkbox" id="case-public-dept"> <span>This is a department response</span></label>
        <div class="row-actions"><button type="submit" class="btn btn-secondary">${icon('message')}<span>Publish update</span></button></div>
      </form>` : ''}
      <h3 class="mt">Internal notes <span class="optional">Staff only · never shown publicly</span></h3>
      ${r.notes.length ? `<ol class="history-list">${r.notes.slice().reverse().map(n => `<li><div class="tl-head"><strong>${esc(n.author?.displayName || 'Staff')}</strong><time datetime="${esc(n.createdAt)}">${fmtDateTime(n.createdAt)}</time></div><p>${esc(n.body)}</p></li>`).join('')}</ol>` : '<p class="micro">No internal notes.</p>'}
      ${can(r, 'add_note') ? `<form id="note-form" class="stack">
        ${field('case-internal', 'Add an internal note', '<textarea id="case-internal" rows="2" maxlength="2000" required></textarea>')}
        <div class="row-actions"><button type="submit" class="btn btn-secondary">Save note</button></div>
      </form>` : ''}
      ${can(r, 'request_reinspection') ? `<form id="reinspect-form" class="stack mt">
        ${field('case-note', 'Verification note <span class="optional">Required when requesting reinspection · public</span>', '<textarea id="case-note" rows="2" maxlength="500" required></textarea>')}
        <label class="check"><input type="checkbox" id="case-reopen"> <span>Also reopen the complaint (back to In Progress)</span></label>
        <div class="row-actions"><button type="submit" class="btn btn-secondary">${icon('refresh')}<span>Request reinspection</span></button></div>
      </form>` : ''}
    </section>
    <section class="panel" aria-labelledby="repair-title">
      ${panelHead('Work orders and repair evidence', '', 'Kept as history')}
      ${orders ? `<div class="table-wrap"><table class="data-table" id="work-orders"><caption class="sr-only">Work orders for ${esc(r.id)}</caption>
          <thead><tr><th scope="col">Team</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead><tbody>${orders}</tbody></table></div>
        ${field('wo-note', 'Completion note or cancellation reason', '<input id="wo-note" maxlength="1000">', 'Required to complete or cancel. Kept with the work order.')}`
        : '<p class="micro">No work orders yet.</p>'}
      ${can(r, 'create_work_order') ? `<form id="work-form" class="stack mt">
        <h3>Create a work order</h3>
        ${field('wo-team', 'Responsible team', '<input id="wo-team" required maxlength="120" placeholder="Ward maintenance team">')}
        ${field('wo-due', 'Target date', '<input id="wo-due" type="date">')}
        ${field('wo-instructions', 'Instructions for the team', '<textarea id="wo-instructions" rows="2" maxlength="1000" placeholder="Scope, safety precautions and how to confirm completion."></textarea>')}
        <div class="row-actions"><button class="btn btn-primary" type="submit">${icon('plus')}<span>Create work order</span></button></div>
      </form>` : pending('Assign the complaint before creating a work order.')}
      <a class="inline-link" href="#/official/workorders">${icon('wrench')}<span>All work orders</span></a>
    </section>`;

  const aside = `
    <section class="panel" aria-labelledby="about-title">
      <h2 id="about-title">About this case</h2>
      <p>${r.description ? esc(r.description) : '<span class="micro">No description given.</span>'}</p>
      <dl class="meta-list">
        <div><dt>Department</dt><dd>${esc(department)}</dd></div>
        <div><dt>Team</dt><dd>${esc(team || 'Awaiting assignment')}</dd></div>
        <div><dt>Neighbourhood</dt><dd>${esc(r.area)}</dd></div>
        <div><dt>Reported</dt><dd>${fmtDate(r.created)}${r.captureSource ? ` · ${esc(r.captureSource)}` : ''}</dd></div>
        <div><dt>Priority</dt><dd>${priorityPill(priority.band)} <small class="micro">Score ${priority.score} · evidence ${esc(priority.confidence)}</small></dd></div>
        <div><dt>Visibility</dt><dd>${r.shareEvidence ? 'Shared with community' : 'Private to reporter and staff'}</dd></div>
      </dl>
      <details class="explain"><summary>How this score is built</summary><ul class="contrib">${Object.entries(priority.contributions).map(([k, val]) => `<li><span>${esc(labelOf(k))}</span><strong>${val} pts</strong></li>`).join('')}</ul><p class="micro">Exposure: ${esc(priority.exposureSource)}. Weights are initial expert assumptions, not calibrated values.</p></details>
    </section>
    <section class="panel" aria-labelledby="verify-title">
      ${panelHead('Does the repair hold up?', '', 'Separate from official status')}
      <p class="micro">Lifecycle: ${statusPill(r.status)} Verification: ${verificationPill(r.verification)}</p>
      <p class="micro">${r.status === 'Marked Resolved' ? 'The reporter can confirm or dispute the repair from public tracking with their private claim code.' : 'Citizen confirmation opens after the civic team marks this case resolved.'}</p>
      <p class="micro mt">Green circles show administrative completion only. A dispute or reinspection keeps the case red on the map.</p>
    </section>
    ${adminPanel}`;

  root.innerHTML = workspaceShell({title, body: `
    ${heading({eyebrow: `${esc(r.id)} · ${esc(r.category)}`, title: esc(title), lead: `${esc(r.area)} · ${esc(department)}`, actions: `<a class="btn btn-secondary" href="#/official/map">${icon('pin')}<span>View on map</span></a>`})}
    <div class="state-strip ${solved.className}">
      <span class="state-symbol" aria-hidden="true">${categoryIcon(r.category)}</span>
      <div class="state-text"><p class="state-label"><strong>${esc(solved.label)}</strong></p><p class="state-sub">${statusPill(r.status)} ${verificationPill(r.verification)}</p></div>
      <span class="pill">${icon('shield', 'pill-icon')}<span>CivicLens server</span></span>
    </div>
    <div class="detail-layout">
      <div class="stack">
        <section class="panel evidence-panel" aria-labelledby="evidence-title">
          ${panelHead('Before and after', pill(r.verification, r.verification === 'Disputed' ? 'red' : 'neutral'), 'Private evidence')}
          <p class="micro">Original report and repair evidence stay linked to this case. Geo-tags show the source; a missing tag is stated, not filled in.</p>
          <div class="evidence-pair" id="staff-evidence-view">${beforeFigure}${afterFigure}</div>
          <h3 class="mt">Repair history</h3>
          ${repairHistory}
          <h3 class="mt">Verification history</h3>
          ${verificationHistory}
        </section>
        <section class="panel" id="case-ai" aria-label="AI analysis and repair verification">
          ${aiSections()}
        </section>
        <section class="panel" aria-labelledby="reviews-title">
          ${panelHead('Community reviews', pill('Not on the server yet'), 'Neighbourhood perspective')}
          ${empty('Community reviews need resident accounts', 'The CivicLens server has no resident sign-in yet, so neighbours cannot post reviews here. Citizens confirm or dispute a repair from public tracking.')}
        </section>
        ${duplicatePanel(r)}
        <section class="panel" aria-labelledby="timeline-title">
          ${panelHead('Progress timeline', '', 'Public updates')}
          ${timeline ? `<ol class="timeline">${timeline}</ol>` : '<p class="micro">No updates yet.</p>'}
        </section>
        <section class="panel" aria-labelledby="history-title">
          ${panelHead('Assignment history and audit', '', 'Staff only')}
          ${r.assignmentHistory.length ? `<ol class="history-list">${r.assignmentHistory.slice().reverse().map(h => `<li><div class="tl-head"><strong>${esc(h.department)}${h.assignee ? ` · ${esc(h.assignee)}` : ''}</strong><time datetime="${esc(h.at)}">${fmtDateTime(h.at)}</time></div><p class="micro">${h.reason ? esc(h.reason) : 'No reason given'} · by ${esc(h.by)}</p></li>`).join('')}</ol>` : '<p class="micro">Not assigned yet.</p>'}
          <h3 class="mt">Evidence access log</h3>
          <div id="staff-access-log"><p class="micro">${r.evidence ? 'Loading…' : 'No evidence, so no access.'}</p></div>
          <h3 class="mt">Audit trail</h3>
          <div id="case-audit">${btn('Show audit trail', 'id="case-load-audit"', 'ghost', 'btn-sm')} <span class="micro">Supervisors and admins.</span></div>
        </section>
      </div>
      <aside class="stack side-col">${aside}</aside>
    </div>`});

  wire(root, http, r, latest);
}

function wire(root, http, r, latest) {
  const token = () => staffSession.get()?.token;
  const message = $('case-message');
  const fail = (err, slot = message) => {
    if (err instanceof ApiError && err.status === 401) return signedOut();
    if (slot?.isConnected) slot.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'The request failed.'), 'error', 'alert');
  };
  const run = async (button, fn, done) => {
    if (button) button.disabled = true;
    if (message) message.innerHTML = '';
    try {
      await fn();
      toast(done, 'success');
      rerender();  // re-read the case from the server
    } catch (err) {
      if (button) button.disabled = false;
      fail(err);
    }
  };
  const value = id => ($(id)?.value || '').trim();
  wireDuplicates(http, r, run, fail);

  // Evidence: fetched only on request; each view is written to the access log by the server.
  const show = async (slotId, load, mediaType, gps, captured, uploaded, label, titleText) => {
    const fig = $(slotId);
    fig.querySelector('button')?.setAttribute('disabled', '');
    try {
      const blob = await load();
      if (!fig.isConnected) return;
      fig.outerHTML = figure({title: titleText, url: objectURL(blob), mediaType: blob.type.startsWith('video/') ? 'video' : mediaType, gps, emptyGeo: 'No GPS metadata', captured, uploaded, label, altText: titleText});
      root.querySelectorAll('[data-enlarge]').forEach(b => b.onclick = () => openLightbox(b.dataset.enlarge, b.dataset.alt));
      loadAccessLog(http, r);
    } catch (err) {
      fig.querySelector('button')?.removeAttribute('disabled');
      fail(err, fig.querySelector('.evidence-empty'));
    }
  };
  // AI analysis and repair verification (app/ai): suggestions for staff, loaded with the staff session.
  // The before/after viewers use the same staff evidence endpoints, so each view is in the access log.
  const showInto = async (target, load, evidenceId, alt) => {
    target.innerHTML = '<p class="micro" role="status">Loading evidence…</p>';
    try {
      const blob = await load();
      if (!target.isConnected) return;
      const url = objectURL(blob);
      target.innerHTML = blob.type.startsWith('video/')
        ? `<video class="receipt-media" controls playsinline src="${url}" aria-label="${esc(alt)}"></video>`
        : `<img class="receipt-media" src="${url}" alt="${esc(alt)}">`;
      overlayDetections(target, evidenceId);
      target.insertAdjacentHTML('beforeend', '<p class="micro">Viewing is recorded in the evidence access log.</p>');
      loadAccessLog(http, r);
    } catch (err) {
      fail(err, target);
    }
  };
  mountAiPanels(http, r, {
    onError: (err, slot) => fail(err, slot),
    viewOriginal: (target, evidenceId) => showInto(target, () => http.fetchStaffEvidence(token(), r.id), evidenceId, 'Original evidence'),
    viewAfter: (target, workOrderId, evidenceId) => showInto(target, () => http.fetchWorkOrderEvidence(token(), workOrderId, evidenceId), evidenceId, 'After-repair evidence')
  });

  root.querySelector('[data-load="before"]')?.addEventListener('click', () => show('figure-before', () => http.fetchStaffEvidence(token(), r.id),
    r.mediaType, r.gps, r.capturedAt, r.uploadedAt, 'Reported', 'Before · reported issue'));
  root.querySelector('[data-load="after"]')?.addEventListener('click', () => show('figure-after', () => http.fetchWorkOrderEvidence(token(), latest.order.id, latest.evidenceId),
    'photo', null, null, latest.uploadedAt, 'Repair', 'After · repair evidence'));
  root.querySelectorAll('[data-repair]').forEach(b => b.addEventListener('click', async () => {
    try {
      const blob = await http.fetchWorkOrderEvidence(token(), b.dataset.order, b.dataset.repair);
      openLightbox(objectURL(blob), 'Repair evidence');
      loadAccessLog(http, r);
    } catch (err) { fail(err); }
  }));
  if (r.evidence) loadAccessLog(http, r);

  $('case-form')?.addEventListener('submit', e => {
    e.preventDefault();
    const to = value('case-status');
    run(e.submitter, () => http.transitionComplaint(token(), r.id, {to, expectedStatus: r.status, explanation: value('case-explanation')}), `Status changed to ${to}.`);
  });
  const departments = $('case-department');
  if (departments) {
    http.listDepartments(token()).then(list => {
      if (!departments.isConnected) return;
      departments.innerHTML = '<option value="">Choose a department</option>' + list.map(d => `<option value="${esc(d.code)}" ${r.assignment.department?.code === d.code ? 'selected' : ''}>${esc(d.name)}</option>`).join('');
    }).catch(err => fail(err));
  }
  $('assign-form')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.assignComplaint(token(), r.id, {department: value('case-department'), assignee: value('case-assignee').toLowerCase() || null,
      reason: value('case-reason'), expectedStatus: r.status}), 'Assignment saved.');
  });
  $('update-form')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.addPublicUpdate(token(), r.id, {text: value('case-public'), kind: $('case-public-dept').checked ? 'department_response' : 'progress'}), 'Public update published.');
  });
  $('note-form')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.addStaffNote(token(), r.id, value('case-internal')), 'Internal note saved.');
  });
  $('reinspect-form')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.requestReinspection(token(), r.id, {note: value('case-note'), reopen: $('case-reopen').checked, expectedStatus: r.status}),
      'Reinspection requested. Earlier records are kept.');
  });
  $('work-form')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.createWorkOrder(token(), r.id, {team: value('wo-team'), dueAt: value('wo-due') || null, description: value('wo-instructions')}),
      'Work order created.');
  });
  $('work-orders')?.addEventListener('click', e => {
    const to = e.target.closest('[data-wo-to]')?.dataset.woTo;
    const row = e.target.closest('[data-work-order]');
    const order = row && r.workOrders.find(w => w.id === row.dataset.workOrder);
    if (to && order) run(e.target.closest('button'), () => http.changeWorkOrder(token(), order.id, {to, expectedStatus: order.status, note: value('wo-note')}),
      `Work order ${to === 'In Progress' ? 'started' : to.toLowerCase()}.`);
  });
  $('work-orders')?.addEventListener('change', e => {
    const file = e.target.closest('[data-wo-upload]')?.files?.[0];
    if (file) run(null, () => http.uploadRepairEvidence(token(), e.target.closest('[data-work-order]').dataset.workOrder, file), 'After-repair photo uploaded.');
  });
  $('case-load-audit')?.addEventListener('click', async () => {
    const slot = $('case-audit');
    try {
      const entries = await http.getAuditTrail(token(), r.id);
      if (!slot.isConnected) return;
      slot.innerHTML = entries.length
        ? `<ol class="history-list">${entries.map(a => `<li><div class="tl-head"><strong>${esc(a.action.replace(/_/g, ' '))}</strong><time datetime="${esc(a.at)}">${fmtDateTime(a.at)}</time></div><p class="micro">${esc(a.actorType === 'staff' ? `${a.actorName || ''} (${a.actor || ''})` : a.actorType)}</p></li>`).join('')}</ol>`
        : '<p class="micro">No audit entries yet.</p>';
    } catch (err) { fail(err, slot); }
  });
}

// ---------- Duplicate review (Workstream 2): suggestions only; merging needs a supervisor ----------

const LABEL_TEXT = {likely_duplicate: 'Likely duplicate', possible_duplicate: 'Possible duplicate', recurrence: 'Recurrence after a fix', nearby_separate: 'Probably a separate issue'};

function duplicatePanel(r) {
  if (r.mergedInto) {
    const active = r.merges.find(m => m.duplicate === r.id && !m.revertedAt);
    return `<section class="panel" aria-labelledby="dup-title">
      ${panelHead('Merged report', '', 'Duplicate review')}
      ${notice(`This report was merged into <a class="inline-link code-inline" href="#/issue/${esc(r.mergedInto)}">${esc(r.mergedInto)}</a> as a report of the same issue${active ? ` by ${esc(active.by)}: “${esc(active.reason)}”` : ''}. Manage it there; this record and its evidence are kept.`, 'info', 'alert')}
      ${can(r, 'revert_merge') && active ? `<form id="dup-revert" class="stack" data-merge="${esc(active.id)}">
        ${field('dup-revert-reason', 'Reason for reverting', '<input id="dup-revert-reason" maxlength="500" required>')}
        <div class="row-actions">${btn('Revert merge', 'type="submit"', 'secondary', 'btn-sm')}</div></form>` : ''}
    </section>`;
  }
  const linked = r.merges.filter(m => m.canonical === r.id && !m.revertedAt);
  if (!linked.length && !can(r, 'review_duplicates')) return '';
  return `<section class="panel" aria-labelledby="dup-title" id="dup-panel">
    ${panelHead('Possible duplicates', '', 'Suggestions only')}
    ${linked.length ? `<p class="micro">Reports merged into this complaint: ${linked.map(m => `<a class="inline-link code-inline" href="#/issue/${esc(m.duplicate)}">${esc(m.duplicate)}</a>`).join(', ')}.</p>` : ''}
    ${can(r, 'review_duplicates') ? `<p class="micro">From location (against GPS accuracy), category, time, wording and identical files. Nothing is merged without a supervisor.</p>
      <div class="row-actions">${btn(icon('search') + '<span>Find possible duplicates</span>', 'id="dup-find"', 'secondary', 'btn-sm')}</div>
      <div id="dup-list" class="mt" role="status" aria-live="polite"></div>` : ''}
  </section>`;
}

function candidateRow(r, c) {
  const factors = c.factors.map(f => `<li><span>${esc(f.name)}: ${esc(f.detail)}</span><strong>${Math.round(f.contribution * 100)} pts</strong></li>`).join('');
  return `<li class="panel" data-candidate="${esc(c.id)}">
    <div class="tl-head"><strong><a class="inline-link code-inline" href="#/issue/${esc(c.id)}">${esc(c.id)}</a> · ${esc(LABEL_TEXT[c.label] || c.label)}</strong>
      <span>${Math.round(c.score * 100)}% · ${Math.round(c.distanceM)} m away</span></div>
    <p class="micro">${esc(c.category)} · ${esc(c.area)} · ${esc(c.status)} · reported ${c.created ? fmtDate(c.created) : '—'}</p>
    ${c.reasons.length ? `<ul>${c.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${c.caution ? notice(esc(c.caution), 'warn', 'alert') : ''}
    <details class="explain"><summary>How the score was worked out</summary><ul class="contrib">${factors}</ul></details>
    ${can(r, 'merge') ? `${field(`dup-reason-${esc(c.id)}`, 'Reason', `<input id="dup-reason-${esc(c.id)}" maxlength="500" placeholder="e.g. same pothole, same photo angle">`)}
      <div class="row-actions">
        ${btn(`Merge this report into ${esc(c.id)}`, 'data-dup-act="merge-into"', 'secondary', 'btn-sm')}
        ${btn(`Merge ${esc(c.id)} into this report`, 'data-dup-act="merge-from"', 'secondary', 'btn-sm')}
        ${btn('Not a duplicate', 'data-dup-act="dismiss"', 'ghost', 'btn-sm')}
      </div>` : ''}
  </li>`;
}

function wireDuplicates(http, r, run, fail) {
  const token = () => staffSession.get()?.token;
  $('dup-revert')?.addEventListener('submit', e => {
    e.preventDefault();
    run(e.submitter, () => http.revertMerge(token(), e.target.dataset.merge, $('dup-revert-reason').value.trim()), 'Merge reverted.');
  });
  $('dup-find')?.addEventListener('click', async () => {
    const list = $('dup-list');
    list.innerHTML = '<p class="micro">Looking for nearby reports…</p>';
    try {
      const result = await http.getDuplicateCandidates(token(), r.id);
      if (!list.isConnected) return;
      list.innerHTML = result.note ? notice(esc(result.note), 'info', 'alert')
        : result.candidates.length ? `<p class="micro">Within ${Math.round(result.radiusM)} m and ${result.days} days, same or related category, best first.</p><ol class="stack">${result.candidates.map(c => candidateRow(r, c)).join('')}</ol>`
        : '<p class="micro">No similar reports nearby.</p>';
    } catch (err) {
      fail(err, list);
    }
  });
  $('dup-list')?.addEventListener('click', e => {
    const act = e.target.closest('[data-dup-act]')?.dataset.dupAct;
    const other = e.target.closest('[data-candidate]')?.dataset.candidate;
    if (!act || !other) return;
    const reason = ($(`dup-reason-${other}`)?.value || '').trim();
    const button = e.target.closest('button');
    if (act === 'merge-into') return run(button, () => http.mergeComplaint(token(), r.id, {canonical: other, reason}), `Merged into ${other}.`);
    if (act === 'merge-from') return run(button, () => http.mergeComplaint(token(), other, {canonical: r.id, reason}), `${other} merged into this complaint.`);
    return run(button, () => http.dismissDuplicate(token(), r.id, other, reason), 'Marked as not a duplicate.');
  });
}

async function loadAccessLog(http, r) {
  const slot = $('staff-access-log');
  try {
    const entries = await http.getEvidenceAccessLog(staffSession.get().token, r.id);
    if (!slot?.isConnected) return;
    slot.innerHTML = entries.length
      ? `<ol class="history-list">${entries.map(e => `<li><div class="tl-head"><strong>${esc(e.displayName)} (${esc(e.username)})</strong><time datetime="${esc(e.accessedAt)}">${fmtDateTime(e.accessedAt)}</time></div><p class="micro">${esc(e.action === 'view' ? 'Viewed the evidence' : e.action)}</p></li>`).join('')}</ol>`
      : '<p class="micro">Nobody has viewed this evidence yet.</p>';
  } catch (err) {
    if (slot?.isConnected) slot.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'The access log could not be loaded.'), 'error', 'alert');
  }
}
