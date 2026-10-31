// Case detail: shared evidence, repair history, verification, community reviews and admin actions.
import {api, esc, inServiceArea} from '../services.js';
import {inspectJpegFile} from '../exif.js';
import {icon, categoryIcon, resolutionState, workspaceShell, heading, btn, statusPill, verificationPill, priorityPill, notice, panel, panelHead, toast, fmtDate, fmtDateTime, openLightbox, coords, empty, pill} from '../ui.js';
import {viewer, addCleanup, state} from '../state.js';
import {locationSourceLabel} from '../presentation.js';
import {navigate, rerender} from '../router.js';
import {readFilters} from '../filters.js';
import {verificationStates, statuses, citizenResponses} from '../services.js';
import {reportMode} from '../report-gateway.js';
import {usesServerRecords} from '../server-records.js';
import {renderServerCase} from './server-case.js';

const RESPONSE_COPY = {
  'Appears Resolved': 'The repair looks complete on the ground. Confirms the citizen confirmation state.',
  'Still Present': 'The problem is still there. Marks a dispute and records a recurrence if the case was marked resolved.',
  'Cannot Confirm': 'You cannot tell from where you are. Logged as a response; the current state does not change.'
};

function objectURL(blob) {
  const url = URL.createObjectURL(blob);
  addCleanup(() => URL.revokeObjectURL(url));
  return url;
}

function geoBlock(gps, emptyText) {
  if (!gps) return `<div class="geo-caption geo-missing">${icon('alert', 'mini-icon')}<div><strong>${esc(emptyText)}</strong><small>Location is not verified from this file.</small></div></div>`;
  const outside = !inServiceArea(gps.lat, gps.lng);
  const fixTime = gps.timestamp ? ` · fix ${fmtDateTime(new Date(gps.timestamp).toISOString())}` : '';
  return `<div class="geo-caption">${icon('pin', 'mini-icon')}<div><strong>${coords(gps)}</strong><small>${gps.accuracy ? `±${Math.round(gps.accuracy)} m · ` : ''}${esc(locationSourceLabel(gps))}${fixTime}${gps.address ? ` · ${esc(gps.address)}` : ''}${outside ? ' · outside the Bengaluru service area' : ''}</small></div></div>`;
}

function dateCaption(captured, uploaded, label) {
  if (captured) return `<p class="date-caption">${icon('clock', 'mini-icon')}<span>${esc(label)} captured ${fmtDateTime(captured)}</span></p>`;
  return `<p class="date-caption">${icon('clock', 'mini-icon')}<span>Capture time not recorded in the file${uploaded ? `. Uploaded ${fmtDateTime(uploaded)}` : ''}</span></p>`;
}

function figure({title, url, mediaType, gps, emptyGeo, captured, uploaded, label, altText, emptyText}) {
  if (!url) {
    return `<figure class="evidence-figure"><figcaption><span>${esc(title)}</span></figcaption>
      <div class="evidence-empty">${categoryIcon('Other civic issues')}<p><strong>${esc(emptyText)}</strong></p></div>
      ${geoBlock(null, emptyGeo)}</figure>`;
  }
  const media = mediaType === 'video'
    ? `<video class="evidence-media" controls playsinline preload="metadata" src="${url}" aria-label="${esc(title)} video"></video>`
    : `<button type="button" class="evidence-open" data-enlarge="${url}" data-alt="${esc(altText)}" aria-label="Enlarge ${esc(title.toLowerCase())}"><img class="evidence-media" src="${url}" alt="${esc(altText)}">${icon('maximize', 'enlarge-icon')}<span class="sr-only">Enlarge</span></button>`;
  return `<figure class="evidence-figure"><figcaption><span>${esc(title)}</span><span class="pill">${mediaType === 'video' ? 'Video' : 'Photo'}</span></figcaption>
    ${media}${geoBlock(gps, emptyGeo)}${dateCaption(captured, uploaded, label)}</figure>`;
}

export async function renderIssue(root, id) {
  // Server mode, signed-in staff: the same case page, built from the CivicLens server (views/server-case.js).
  if (await usesServerRecords()) return renderServerCase(root, id);
  if (await reportMode() === 'backend') return navigate('track/' + encodeURIComponent(id));
  const v = viewer();
  const d = await api.detail(id, v);
  if (!d) {
    root.innerHTML = workspaceShell({title: 'Case not found', body: `${heading({title: 'Case not found', lead: 'No case with this ID is available to your workspace.'})}${empty('Nothing to show', 'Check the ID, or return to your list.', `<a class="btn btn-secondary" href="#/${v.role === 'official' ? 'official' : 'resident'}/complaints">Back to cases</a>`)}`});
    return;
  }
  const admin = v.role === 'official';
  const owner = !!d.owner;
  const seeAll = admin || owner;
  const solved = resolutionState(d);
  const beforeUrl = d.gps || d.shareEvidence || seeAll ? await api.mediaFor(v, id, {kind: 'before'}).then(b => b && objectURL(b)).catch(() => null) : null;
  const visibleRepairs = d.repairs.filter(r => seeAll || r.shared);
  const latest = visibleRepairs.at(-1);
  const afterUrl = latest ? await api.mediaFor(v, id, {kind: 'after', repairId: latest.id}).then(b => b && objectURL(b)).catch(() => null) : null;
  const canCompare = beforeUrl && afterUrl && d.mediaType !== 'video';

  const repairHistory = visibleRepairs.length
    ? `<ol class="history-list">${visibleRepairs.slice().reverse().map(r => `<li>
        <div class="tl-head"><strong>After-repair evidence${r.uploadedBy ? ` · ${esc(r.uploadedBy)}` : ''}</strong><span class="pill ${r.shared ? 'pill-navy' : ''}">${r.shared ? 'Shared with community' : 'Admins only'}</span></div>
        <p>${r.gps ? `Coordinates from file: ${coords(r.gps, 5)}` : 'No GPS metadata supplied for this repair photo. The report’s coordinates were not copied into it.'}</p>
        <p class="micro">${r.capturedAt ? `Captured ${fmtDateTime(r.capturedAt)}` : 'Capture time not in file'} · uploaded ${fmtDateTime(r.uploadedAt)}</p>
      </li>`).join('')}</ol>`
    : '<p class="micro">No repair evidence has been shared yet.</p>';

  const verificationHistory = d.verificationLog.length
    ? `<ol class="history-list">${d.verificationLog.slice().reverse().map(e => `<li>
        <div class="tl-head"><strong>${e.type === 'response' ? `Community response: ${esc(e.response)}` : `Verification: ${esc(e.from || '—')} → ${esc(e.to)}`}</strong><time datetime="${esc(e.date)}">${fmtDate(e.date)}</time></div>
        ${e.note ? `<p>${esc(e.note)}</p>` : ''}<p class="micro">${e.by === 'citizen' ? 'Citizen response' : 'Civic team'} · earlier records are kept</p></li>`).join('')}</ol>`
    : '<p class="micro">No verification events yet.</p>';

  const timeline = d.timeline.slice().reverse().map(t => `<li><div class="tl-head"><strong>${esc(t.status)}</strong><time datetime="${esc(t.date)}">${fmtDate(t.date)}</time></div><p>${esc(t.text)}</p></li>`).join('');

  const reviews = d.reviews.slice().reverse();
  const reviewList = reviews.length
    ? `<ul class="review-list">${reviews.map(rv => `<li class="review">
        <span class="review-avatar" aria-hidden="true">${esc((rv.author || 'N')[0].toUpperCase())}</span>
        <div><div class="review-head"><strong>${esc(rv.author)}</strong><span class="stars" role="img" aria-label="Rated ${rv.rating} out of 5">${'★'.repeat(rv.rating)}<span class="stars-off">${'★'.repeat(5 - rv.rating)}</span></span></div>
        <p>${esc(rv.text)}</p><p class="micro">${fmtDate(rv.date)}${rv.sample ? ' · illustrative sample review' : ''}</p></div>
      </li>`).join('')}</ul>`
    : empty('No community reviews yet', 'Be the first to share what you observed.');

  const shareNotice = !d.shareEvidence && owner
    ? notice(`<strong>Your original evidence is private.</strong> Only you and civic admins can see it. ${btn('Share my evidence with the community', 'data-action="share-original" data-share="on"', 'secondary', 'btn-sm')}`, 'info', 'lock')
    : (d.shareEvidence && owner ? notice(`<strong>Shared with signed-in community members.</strong> ${btn('Make private again', 'data-action="share-original" data-share="off"', 'ghost', 'btn-sm')}`, 'info', 'eye') : '');
  const privateNote = !d.shareEvidence && !owner && !admin ? notice('The reporter has not shared the original media or precise location. Community members see the public case, shared repairs and reviews.', 'info', 'lock') : '';

  const citizenActions = !admin && d.status === 'Marked Resolved' && v.role !== 'public'
    ? `<div class="response-grid">${citizenResponses.map(r => `<button type="button" class="response-btn" data-action="citizen-response" data-response="${esc(r)}"><strong>${esc(r)}</strong><span>${esc(RESPONSE_COPY[r])}</span></button>`).join('')}</div>`
    : `<p class="micro">${admin ? 'Admins record verification in the case panel.' : d.status === 'Marked Resolved' ? 'Sign in to respond.' : 'Citizen confirmation opens after the civic team marks this case resolved.'}</p>`;

  const adminPanel = admin ? `
    <section class="panel admin-panel" aria-labelledby="admin-title">
      ${panelHead('Case actions', '', 'Civic team only')}
      <form id="case-form" class="stack" novalidate>
        <div class="field"><label for="case-status">Lifecycle status</label><select id="case-status" name="status">${statuses.map(s => `<option ${s === d.status ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
        <div class="field"><label for="case-team">Assigned team</label><input id="case-team" name="team" maxlength="120" value="${esc(d.team || '')}" placeholder="e.g. Ward maintenance team"></div>
        <div class="field"><label for="case-public">Public update <span class="optional">Visible to the public timeline</span></label><textarea id="case-public" name="publicText" rows="2" maxlength="500" placeholder="What the public should know about progress"></textarea></div>
        <div class="field"><label for="case-internal">Internal note <span class="optional">Admins only · never shown publicly</span></label><textarea id="case-internal" name="internal" rows="2" maxlength="2000">${esc(d.internal || '')}</textarea></div>
        <div class="field"><label for="case-verification">Verification state</label><select id="case-verification" name="verification">${verificationStates.map(s => `<option ${s === d.verification ? 'selected' : ''}>${s}</option>`).join('')}</select><small class="micro">Changes are appended to the history. Earlier disputes are never deleted.</small></div>
        <div class="field"><label for="case-note">Verification note <span class="optional">Required when changing verification or requesting reinspection</span></label><textarea id="case-note" name="note" rows="2" maxlength="500"></textarea></div>
        <div id="case-error" role="alert"></div>
        <div class="row-actions">
          <button type="submit" class="btn btn-primary">${icon('check')}<span>Save case update</span></button>
          <button type="button" class="btn btn-secondary" data-action="request-reinspection">${icon('refresh')}<span>Request reinspection</span></button>
        </div>
      </form>
    </section>
    <section class="panel" aria-labelledby="repair-title">
      ${panelHead('Attach after-repair evidence', '', 'Kept as history')}
      <form id="repair-form" class="stack">
        <div class="field"><label for="repair-file">Repair photograph</label><input id="repair-file" type="file" accept="image/*" required><small class="micro">JPEG with GPS metadata gives location; other formats are kept without location.</small></div>
        <div id="repair-preview" class="micro" aria-live="polite"></div>
        <label class="check"><input type="checkbox" name="shared"> <span>Share this repair photo and its available location with signed-in community members.</span></label>
        <div id="repair-error" role="alert"></div>
        <button class="btn btn-primary" type="submit">${icon('upload')}<span>Save repair evidence</span></button>
        <a class="inline-link" href="#/official/workorders">${icon('wrench')}<span>Create a work order for this case</span></a>
      </form>
    </section>` : '';

  const aside = `
    <section class="panel" aria-labelledby="about-title">
      <h2 id="about-title">About this case</h2>
      <p>${esc(d.description)}</p>
      <dl class="meta-list">
        <div><dt>Department</dt><dd>${esc(d.department)}</dd></div>
        <div><dt>Team</dt><dd>${esc(d.team || 'Awaiting assignment')}</dd></div>
        <div><dt>Neighbourhood</dt><dd>${esc(d.area)}</dd></div>
        <div><dt>Reported</dt><dd>${fmtDate(d.created)}</dd></div>
        <div><dt>Recurrence records</dt><dd>${d.recurrence || 0}</dd></div>
        ${admin && d.priority ? `<div><dt>Priority</dt><dd>${priorityPill(d.priority.band)} <small class="micro">Score ${d.priority.score} · evidence ${esc(d.priority.confidence)}</small></dd></div>` : ''}
        <div><dt>Visibility</dt><dd>${d.shareEvidence ? 'Shared with community' : 'Private to reporter and admins'}</dd></div>
      </dl>
      ${admin && d.priority ? `<details class="explain"><summary>How this score is built</summary><ul class="contrib">${Object.entries(d.priority.contributions).map(([k, val]) => `<li><span>${esc(labelOf(k))}</span><strong>${val} pts</strong></li>`).join('')}</ul><p class="micro">Exposure: ${esc(d.priority.exposureSource)}. ${d.priority.flag ? esc(d.priority.flag) : ''} Weights are initial expert assumptions, not calibrated values.</p></details>` : ''}
      ${v.role === 'resident' && !admin ? `<div class="row-actions mt">${btn(d.following ? `${icon('check')}<span>Following</span>` : `${icon('plus')}<span>Follow this issue</span>`, 'data-action="follow"', d.following ? 'secondary' : 'ghost')}</div>` : ''}
    </section>
    <section class="panel" aria-labelledby="verify-title">
      ${panelHead('Does the repair hold up?', '', 'Separate from official status')}
      <p class="micro">Lifecycle: ${statusPill(d.status)} Verification: ${verificationPill(d.verification)}</p>
      ${citizenActions}
      <p class="micro mt">Green circles show administrative completion only. A dispute or reinspection keeps the case red on the map.</p>
    </section>
    ${admin ? adminPanel : ''}`;

  root.innerHTML = workspaceShell({title: d.title, body: `
    ${heading({eyebrow: `${esc(d.id)} · ${esc(d.category)}`, title: esc(d.title), lead: `${esc(d.area)} · ${esc(d.department)}`, actions: `<a class="btn btn-secondary" href="#/${admin ? 'official' : 'resident'}/map">${icon('pin')}<span>View on map</span></a>`})}
    <div class="state-strip ${solved.className}">
      <span class="state-symbol" aria-hidden="true">${categoryIcon(d.category)}</span>
      <div class="state-text"><p class="state-label"><strong>${esc(solved.label)}</strong></p><p class="state-sub">${statusPill(d.status)} ${verificationPill(d.verification)}</p></div>
      ${d.recurrence ? `<span class="pill pill-red">${icon('refresh', 'pill-icon')}<span>${d.recurrence} recurrence record${d.recurrence === 1 ? '' : 's'}</span></span>` : ''}
    </div>
    <div class="detail-layout">
      <div class="stack">
        <section class="panel evidence-panel" aria-labelledby="evidence-title">
          ${panelHead('Before and after', pill(d.verification, d.verification === 'Disputed' ? 'red' : 'neutral'), 'Shared evidence')}
          <p class="micro">Original report and repair evidence stay linked to this case. Geo-tags show the source; a missing tag is stated, not filled in.</p>
          ${shareNotice}${privateNote}
          <div class="evidence-pair">
            ${figure({title: 'Before · reported issue', url: beforeUrl, mediaType: d.mediaType, gps: d.gps, emptyGeo: 'No GPS metadata for this original', captured: d.capturedAt, uploaded: d.uploadedAt, label: 'Reported', altText: `Reported issue: ${d.title}`, emptyText: d.sample ? 'Sample case: no photograph is attached' : 'Original not visible to you'})}
            ${figure({title: 'After · repair evidence', url: afterUrl, mediaType: 'photo', gps: latest?.gps ?? null, emptyGeo: 'No GPS metadata supplied for the repair', captured: latest?.capturedAt, uploaded: latest?.uploadedAt, label: 'Repair', altText: 'Repair evidence', emptyText: visibleRepairs.length ? 'Repair evidence hidden' : 'No repair evidence yet'})}
          </div>
          ${canCompare ? `<details class="compare-details"><summary>Compare before and after with the slider</summary>
            <div class="compare" id="compare"><img src="${beforeUrl}" alt="Before repair"><img id="compare-after" src="${afterUrl}" alt="After repair" style="clip-path: inset(0 0 0 50%)"><span class="compare-label compare-before">Before</span><span class="compare-label compare-after">After</span><span class="compare-handle" id="compare-handle" style="left:50%" aria-hidden="true"></span></div>
            <label for="compare-range" class="field-label mt">Reveal position</label><input id="compare-range" type="range" min="0" max="100" value="50" aria-valuetext="Showing 50 percent after repair"></details>` : ''}
          <h3 class="mt">Repair history</h3>
          ${repairHistory}
          <h3 class="mt">Verification history</h3>
          ${verificationHistory}
        </section>
        <section class="panel" aria-labelledby="reviews-title">
          ${panelHead('Community reviews', pill(`${reviews.length} ${reviews.length === 1 ? 'review' : 'reviews'}`), 'Neighbourhood perspective')}
          ${reviewList}
          ${v.role === 'public' ? '' : `<form id="review-form" class="review-form stack">
            <h3>Add your perspective</h3>
            <div class="field"><label for="review-text">What did you notice?</label><textarea id="review-text" name="text" minlength="3" maxlength="1000" required rows="3" placeholder="Share what you saw, or whether the repair has held up."></textarea><small class="micro" id="review-count">0 / 1,000</small></div>
            <div class="form-row"><div class="field"><label for="review-rating">Your rating</label><select id="review-rating" name="rating"><option value="5">5 · Excellent</option><option value="4">4 · Good</option><option value="3" selected>3 · Mixed</option><option value="2">2 · Needs work</option><option value="1">1 · Poor</option></select></div>
            <button class="btn btn-primary" type="submit">Post review</button></div>
            <div id="review-error" role="alert"></div>
            <small class="micro">Reviews are visible to signed-in users and admins. They never change the lifecycle status or verification state.</small>
          </form>`}
        </section>
        <section class="panel" aria-labelledby="timeline-title">
          ${panelHead('Progress timeline', '', 'Public updates')}
          ${timeline ? `<ol class="timeline">${timeline}</ol>` : '<p class="micro">No updates yet.</p>'}
        </section>
      </div>
      <aside class="stack side-col">${aside}</aside>
    </div>`});

  wireIssue(root, id, d, {beforeUrl, afterUrl, canCompare, admin, owner});
}

function labelOf(k) {
  return {severity: 'Severity (category)', exposure: 'Exposure', unresolved: 'Time unresolved', recurrence: 'Recurrence'}[k] || k;
}

function wireIssue(root, id, d, ctx) {
  const v = viewer();
  root.querySelectorAll('[data-enlarge]').forEach(b => b.onclick = () => openLightbox(b.dataset.enlarge, b.dataset.alt));
  const range = root.querySelector('#compare-range');
  if (range) range.oninput = () => {
    const pct = Number(range.value);
    const after = root.querySelector('#compare-after');
    if (after) after.style.clipPath = `inset(0 0 0 ${pct}%)`;
    const handle = root.querySelector('#compare-handle');
    if (handle) handle.style.left = pct + '%';
    range.setAttribute('aria-valuetext', `Showing ${100 - pct} percent before and ${pct} percent after`);
  };

  root.querySelectorAll('[data-action="share-original"]').forEach(b => b.onclick = async () => {
    try {
      await api.shareOriginal(v, id, b.dataset.share === 'on');
      toast(b.dataset.share === 'on' ? 'Evidence and location are now shared with the community.' : 'Evidence is private again.', 'success');
      rerender();
    } catch (err) { toast(err.message, 'error'); }
  });
  root.querySelector('[data-action="follow"]')?.addEventListener('click', async () => {
    const following = await api.toggleFollow(v, id);
    toast(following ? 'You are following this issue.' : 'You stopped following this issue.', 'success');
    rerender();
  });
  root.querySelectorAll('[data-action="citizen-response"]').forEach(b => b.onclick = async () => {
    try {
      await api.citizenResponse(v, id, b.dataset.response);
      toast(`Recorded: ${b.dataset.response}. The official status is unchanged.`, 'success');
      rerender();
    } catch (err) { toast(err.message, 'error'); }
  });

  const reviewForm = root.querySelector('#review-form');
  if (reviewForm) {
    const text = root.querySelector('#review-text');
    text.addEventListener('input', () => { root.querySelector('#review-count').textContent = `${text.value.length} / 1,000`; });
    reviewForm.onsubmit = async e => {
      e.preventDefault();
      const f = readFilters(e.target);
      try {
        await api.addReview(id, {author: v.name || 'Neighbour', authorEmail: v.email, text: f.text, rating: +f.rating});
        toast('Your review is visible to the community and admins.', 'success');
        rerender();
      } catch (err) {
        root.querySelector('#review-error').innerHTML = notice(esc(err.message), 'error', 'alert');
      }
    };
  }

  if (!ctx.admin) return;

  root.querySelector('#case-form').onsubmit = async e => {
    e.preventDefault();
    const f = readFilters(e.target);
    const verificationChanged = f.verification !== d.verification;
    if (verificationChanged && !f.note.trim()) {
      root.querySelector('#case-error').innerHTML = notice('Add a verification note so the history explains the change.', 'error', 'alert');
      return root.querySelector('#case-note').focus();
    }
    try {
      await api.updateCase(v, id, {
        status: f.status, team: f.team, internal: f.internal, publicText: f.publicText,
        verification: f.verification, note: f.note
      });
      toast('Case update saved across every view.', 'success');
      rerender();
    } catch (err) {
      root.querySelector('#case-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };
  root.querySelector('[data-action="request-reinspection"]').onclick = async () => {
    const note = root.querySelector('#case-note').value.trim();
    if (!note) {
      root.querySelector('#case-error').innerHTML = notice('Add a note explaining why a reinspection is needed.', 'error', 'alert');
      return root.querySelector('#case-note').focus();
    }
    try {
      await api.requestReinspection(v, id, note);
      toast('Reinspection requested. Earlier records are kept.', 'success');
      rerender();
    } catch (err) {
      root.querySelector('#case-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };

  const file = root.querySelector('#repair-file');
  let picked = null;
  file.onchange = async () => {
    const f = file.files?.[0];
    picked = null;
    const preview = root.querySelector('#repair-preview');
    if (!f) return;
    if (!f.type.startsWith('image/')) { preview.textContent = 'Choose a photograph.'; return; }
    let info = {gps: null, captureTime: null, reason: 'not-jpeg'};
    if (/jpe?g$/i.test(f.name) || f.type === 'image/jpeg') {
      try { info = await inspectJpegFile(f); } catch { /* unreadable: keep as no GPS */ }
    }
    picked = {gps: info.gps ? {...info.gps, sourceType: 'exif', accuracy: null, timestamp: null} : null, capturedAt: info.captureTime || null};
    preview.innerHTML = info.gps
      ? `Location from file: <strong>${coords(info.gps)}</strong>${info.serviceArea === false ? ' (outside the Bengaluru service area)' : ''}. Capture time ${info.captureTime ? fmtDateTime(info.captureTime) : 'not in file'}.`
      : 'No GPS metadata in this file. The report’s location will not be copied to this repair.';
  };
  root.querySelector('#repair-form').onsubmit = async e => {
    e.preventDefault();
    const f = file.files?.[0];
    if (!f) { root.querySelector('#repair-error').innerHTML = notice('Choose a repair photograph first.', 'error', 'alert'); return; }
    const shared = new FormData(e.target).get('shared') === 'on';
    const submit = e.target.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      await api.addRepair(v, id, {blob: f, shared, gps: picked?.gps ?? null, capturedAt: picked?.capturedAt ?? null, uploadedBy: 'Civic team'});
      toast('Repair evidence saved to the case history.', 'success');
      rerender();
    } catch (err) {
      submit.disabled = false;
      root.querySelector('#repair-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };
}

// Shared with views/server-case.js, which renders server complaints in this same design.
export {navigate, state, btn, panel, notice, figure, geoBlock, dateCaption, labelOf, objectURL};
