// AI panels for the server-mode staff case (#/staff/<id>): detection results for the complaint's photos,
// box overlays on the private evidence viewer, and the before/after repair check with supervisor review.
// Everything shown here comes from the server's stored inference results; nothing is predicted or
// illustrated in the browser. Results are suggestions: no button here changes the complaint's status.

import {esc} from '../services.js';
import {icon, btn, notice, pill, fmtDateTime, toast} from '../ui.js';
import {staffSession} from '../staff-session.js';

const $ = id => document.getElementById(id);
const pct = v => (typeof v === 'number' ? `${Math.round(v * 100)}%` : '—');
// Detections per evidence ID, from the last load, so the evidence viewers can draw boxes.
const byEvidence = new Map();

const STATE = {
  completed: ['Done', 'green'], failed: ['Failed', 'red'], pending: ['Queued', 'neutral'], running: ['Analysing', 'neutral'], skipped: ['Skipped', 'neutral']
};
const OUTCOME = {
  likely_repaired: ['Likely repaired', 'green', 'check'],
  damage_visible: ['Damage still visible', 'red', 'alert'],
  inconclusive: ['Inconclusive · needs manual review', 'neutral', 'search']
};
const REASON = {
  disabled: 'AI analysis is switched off on this server.',
  not_configured: 'AI analysis is enabled but the server has no Roboflow API key, so nothing is analysed.'
};
const ERRORS = {
  unsupported_media: 'Only photos are analysed (videos are not).', invalid_image: 'The photo could not be decoded.',
  image_too_large: 'The photo is too large to analyse.', evidence_unavailable: 'The stored file could not be read.',
  auth_failed: 'The AI provider refused the server’s API key.', model_not_found: 'The configured model or workflow was not found.',
  rate_limited: 'The AI provider’s rate limit was reached.', timeout: 'The AI provider did not answer in time.',
  provider_unavailable: 'The AI provider is unavailable.', provider_unreachable: 'The AI provider could not be reached.',
  bad_response: 'The AI provider sent a response that could not be read.', not_configured: 'The server has no API key.',
  inference_failed: 'An AI analysis it depends on failed; retry the analysis.'
};

const isSupervisor = () => ['supervisor', 'admin'].includes(staffSession.get()?.staff?.role);

/** Draw the stored detection boxes for `evidenceId` over the <img> inside `view`. */
export function overlayDetections(view, evidenceId) {
  const entry = byEvidence.get(evidenceId);
  const img = view.querySelector('img');
  if (!entry || !img || !entry.width || !entry.height || !entry.detections.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'ai-overlay';
  img.replaceWith(wrap);
  wrap.appendChild(img);
  for (const d of entry.detections) {
    const box = document.createElement('span');
    box.className = 'ai-box' + (d.confidence >= entry.present && d.category ? ' ai-box-strong' : '');
    box.style.left = `${(d.bbox.x / entry.width) * 100}%`;
    box.style.top = `${(d.bbox.y / entry.height) * 100}%`;
    box.style.width = `${(d.bbox.width / entry.width) * 100}%`;
    box.style.height = `${(d.bbox.height / entry.height) * 100}%`;
    box.innerHTML = `<span class="ai-box-label">${esc(d.className)} ${pct(d.confidence)}</span>`;
    wrap.appendChild(box);
  }
  view.insertAdjacentHTML('beforeend', `<p class="micro">Boxes: AI detections (${esc(entry.source)}), shown for review only.</p>`);
}

export function aiSections() {
  return `<h2 class="mt">AI analysis <span class="micro">(suggestions for staff)</span></h2>
    <div id="staff-ai" aria-live="polite"><p class="micro">Loading AI results…</p></div>
    <h2 class="mt">Repair verification</h2>
    <div id="staff-repair" aria-live="polite"><p class="micro">Loading…</p></div>`;
}

/** Load both panels. `onError(err, slot)` is the staff console's error handler. */
export function mountAiPanels(http, r, {onError, viewOriginal, viewAfter}) {
  byEvidence.clear();
  loadAnalysis(http, r, onError);
  loadAssessments(http, r, {onError, viewOriginal, viewAfter});
}

async function loadAnalysis(http, r, onError) {
  const slot = $('staff-ai');
  try {
    const data = await http.getAiAnalysis(staffSession.get().token, r.id);
    if (slot?.isConnected) renderAnalysis(slot, http, r, data, onError);
  } catch (err) {
    if (slot?.isConnected) onError(err, slot);
  }
}

function renderAnalysis(slot, http, r, data, onError) {
  for (const i of data.inferences) {
    if (i.state !== 'completed') continue;
    const entry = byEvidence.get(i.evidenceId) || {width: i.imageWidth, height: i.imageHeight, detections: [], present: data.ai.thresholds.present, source: []};
    entry.detections.push(...i.detections);
    entry.source.push(i.detector);
    byEvidence.set(i.evidenceId, entry);
  }
  for (const e of byEvidence.values()) e.source = e.source.join(', ');
  if (!data.ai.enabled && !data.inferences.length) {
    slot.innerHTML = notice(esc(REASON[data.ai.reason] || 'AI analysis is not available on this server.'), 'info', 'alert');
    return;
  }
  if (!data.inferences.length) {
    slot.innerHTML = notice(data.routing.detectors.length ? 'AI analysis has not run for this complaint yet.'
      : `No AI detector is configured for “${esc(data.category)}”. Staff assess this category directly.`, 'info', 'alert');
    if (data.routing.detectors.length) slot.insertAdjacentHTML('beforeend', `<div class="row-actions">${btn(icon('refresh') + '<span>Run AI analysis</span>', 'id="ai-retry"', 'secondary')}</div>`);
    wireRetry(slot, http, r, onError);
    return;
  }
  const rows = data.inferences.map(i => {
    const [label, tone] = STATE[i.state] || [i.state, 'neutral'];
    const result = i.state === 'completed'
      ? (i.detections.length
        ? `<strong>${esc(i.detectedCategory || 'Unmapped class')}</strong> · ${esc(i.originalClass || '')} · model confidence ${pct(i.topConfidence)}<br><span class="micro">${i.detections.length} detection${i.detections.length === 1 ? '' : 's'}</span>`
        : '<span class="micro">Nothing detected</span>')
      : (i.errorCode ? `<span class="micro">${esc(ERRORS[i.errorCode] || i.errorCode)}</span>` : '<span class="micro">Waiting for the AI service</span>');
    return `<tr><td>${i.evidenceRole === 'repair' ? 'After-repair photo' : 'Original photo'}</td>
      <td>${esc(i.detector)}<br><span class="micro">${i.mechanism === 'model' ? 'Model' : 'Workflow'} <span class="code-inline">${esc(i.modelId)}</span></span></td>
      <td>${pill(label, tone)}${i.attempts > 1 ? `<br><span class="micro">${i.attempts} attempts</span>` : ''}</td><td>${result}</td></tr>`;
  }).join('');
  const retryable = data.ai.enabled && data.inferences.some(i => i.state === 'failed');
  slot.innerHTML = `<p class="micro">Results from ${data.ai.provider === 'roboflow' ? 'Roboflow' : esc(data.ai.provider || 'the AI provider')}. Model confidence is the model’s own score, not a verification; open the evidence above to see the boxes.</p>
    <div class="table-wrap"><table class="data-table" id="ai-results"><thead><tr><th scope="col">Photo</th><th scope="col">Detector</th><th scope="col">State</th><th scope="col">Result</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${retryable ? `<div class="row-actions">${btn(icon('refresh') + '<span>Retry failed analyses</span>', 'id="ai-retry"', 'secondary')}</div>` : ''}`;
  wireRetry(slot, http, r, onError);
}

function wireRetry(slot, http, r, onError) {
  $('ai-retry')?.addEventListener('click', async e => {
    e.currentTarget.disabled = true;
    try {
      const data = await http.retryAiAnalysis(staffSession.get().token, r.id);
      toast(data.reset ? 'AI analysis queued again.' : 'Nothing needed to be retried.', 'success');
      if (slot.isConnected) setTimeout(() => loadAnalysis(http, r, onError), 1500);
    } catch (err) {
      if (slot.isConnected) onError(err, slot);
    }
  });
}

async function loadAssessments(http, r, handlers) {
  const slot = $('staff-repair');
  try {
    const data = await http.getRepairAssessments(staffSession.get().token, r.id);
    if (slot?.isConnected) renderAssessments(slot, http, r, data, handlers);
  } catch (err) {
    if (slot?.isConnected) handlers.onError(err, slot);
  }
}

function location(a) {
  if (a.locationCheck === 'match') return `Same place (${a.matchedFeatures} matching image features${a.gpsDistanceM !== null ? `, GPS ${Math.round(a.gpsDistanceM)} m apart` : ''})`;
  if (a.locationCheck === 'mismatch') return `Possibly a different place${a.gpsDistanceM !== null ? ` (GPS ${Math.round(a.gpsDistanceM)} m away)` : ''}`;
  return 'Not confirmed to be the same place';
}

function assessmentCard(a, r) {
  const [label, tone, iconName] = OUTCOME[a.suggestedOutcome] || ['Not assessed yet', 'neutral', 'clock'];
  const running = a.state === 'pending' || a.state === 'running';
  const review = a.review.status === 'pending_review'
    ? (a.state === 'completed'
      ? (isSupervisor()
        ? `<form class="panel mt" data-review="${esc(a.id)}">
            <div class="field"><label for="review-${esc(a.id)}">Review note</label><textarea id="review-${esc(a.id)}" maxlength="1000" rows="2"></textarea><p class="micro">Required to reject. Approving records that you agree with the AI suggestion; it does not change the complaint’s status.</p></div>
            <div class="row-actions"><button type="submit" class="btn btn-secondary" data-decision="approved">Approve suggestion</button> <button type="submit" class="btn btn-ghost" data-decision="rejected">Reject suggestion</button></div></form>`
        : '<p class="micro">Awaiting review by a supervisor.</p>')
      : '')
    : `<p>${pill(a.review.status === 'approved' ? 'Suggestion approved' : 'Suggestion rejected', a.review.status === 'approved' ? 'green' : 'red')}
         <span class="micro">by ${esc(a.review.reviewerName || a.review.reviewer || '')} · ${fmtDateTime(a.review.reviewedAt)}</span></p>${a.review.note ? `<p>${esc(a.review.note)}</p>` : ''}`;
  return `<section class="panel mt" aria-label="Repair assessment ${esc(a.id)}">
    <div class="pill-row">${running ? pill('Analysing…', 'neutral', 'clock') : (a.state === 'failed' ? pill('Check failed', 'red', 'alert') : pill(label, tone, iconName))}
      ${pill('AI suggestion', 'navy')}
      ${a.verifiedResolution === 'verified_repaired' ? pill('Verified repaired', 'green', 'check') : a.verifiedResolution === 'verified_not_repaired' ? pill('Verified not repaired', 'red') : pill('Not verified by a person', 'neutral')}</div>
    ${a.state === 'failed' ? `<p class="micro">${esc(ERRORS[a.errorCode] || a.errorCode || 'The check failed.')}</p>` : ''}
    ${a.state === 'completed' ? `<dl class="meta-list two-col">
      <div><dt>Verification confidence</dt><dd>${a.verificationConfidence !== null ? pct(a.verificationConfidence) : '—'} <span class="micro">(of this comparison, separate from model confidence)</span></dd></div>
      <div><dt>Location check</dt><dd>${esc(location(a))}</dd></div></dl>
      <ul class="micro">${a.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    <div class="row-actions">${btn(icon('eye') + '<span>Before</span>', `data-view-before="${esc(a.id)}"`, 'ghost')} ${a.workOrderId ? btn(icon('eye') + '<span>After</span>', `data-view-after="${esc(a.id)}"`, 'ghost') : ''}
      <span class="micro">Requested ${fmtDateTime(a.createdAt)}${a.requestedBy ? ` by ${esc(a.requestedBy)}` : ' automatically on upload'}</span></div>
    <div class="mt" data-compare="${esc(a.id)}"></div>
    ${review}
  </section>`;
}

function renderAssessments(slot, http, r, data, handlers) {
  const canRun = data.ai.enabled && r.workOrders.some(w => w.evidence.some(e => e.phase === 'after'));
  if (!data.assessments.length) {
    slot.innerHTML = `<p class="micro">${data.ai.enabled
      ? (canRun ? 'No before/after check has been run yet.' : 'Upload an after-repair photo to a work order to compare it with the original photo.')
      : 'Before/after checks need AI analysis, which is not available on this server.'}</p>
      ${canRun ? `<div class="row-actions">${btn(icon('search') + '<span>Run before/after check</span>', 'id="ai-verify"', 'secondary')}</div>` : ''}`;
  } else {
    slot.innerHTML = `${notice('These are AI suggestions. A supervisor confirms or rejects each one; the complaint’s status is only changed through case management.', 'info', 'shield')}
      ${data.assessments.map(a => assessmentCard(a, r)).join('')}
      ${canRun ? `<div class="row-actions mt">${btn(icon('refresh') + '<span>Check the latest after photo</span>', 'id="ai-verify"', 'ghost')}</div>` : ''}`;
  }
  const reload = () => { if (slot.isConnected) loadAssessments(http, r, handlers); };
  $('ai-verify')?.addEventListener('click', async e => {
    e.currentTarget.disabled = true;
    try {
      await http.verifyRepair(staffSession.get().token, r.id);
      toast('Before/after check requested.', 'success');
      setTimeout(reload, 1500);
    } catch (err) {
      if (slot.isConnected) handlers.onError(err, slot);
    }
  });
  slot.addEventListener('submit', async e => {
    const form = e.target.closest('[data-review]');
    if (!form) return;
    e.preventDefault();
    const decision = e.submitter?.dataset.decision;
    if (!decision) return;
    e.submitter.disabled = true;
    try {
      await http.reviewRepairAssessment(staffSession.get().token, r.id, form.dataset.review, {decision, note: form.querySelector('textarea').value.trim()});
      toast(decision === 'approved' ? 'Suggestion approved.' : 'Suggestion rejected.', 'success');
      reload();
    } catch (err) {
      e.submitter.disabled = false;
      if (slot.isConnected) handlers.onError(err, form);
    }
  });
  slot.addEventListener('click', e => {
    const before = e.target.closest('[data-view-before]')?.dataset.viewBefore;
    const after = e.target.closest('[data-view-after]')?.dataset.viewAfter;
    const a = data.assessments.find(x => x.id === (before || after));
    if (!a) return;
    const target = slot.querySelector(`[data-compare="${CSS.escape(a.id)}"]`);
    if (before) handlers.viewOriginal(target, a.originalEvidenceId);
    else handlers.viewAfter(target, a.workOrderId, a.repairEvidenceId);
  });
}
