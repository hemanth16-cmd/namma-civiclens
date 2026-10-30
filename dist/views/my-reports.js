// "My reports" in server mode (#/my-reports): the complaints submitted from this browser, with their
// current public status from the CivicLens server. There are no citizen accounts yet, so the list is
// the IDs this browser remembered at submission (report-gateway.js); the claim code is never kept.
// Each status comes from public tracking, so nothing restricted is ever shown here.
import {esc} from '../services.js';
import {icon, btn, heading, notice, publicTopbar, statusPill, verificationPill, fmtDateTime} from '../ui.js';
import {navigate} from '../router.js';
import {reportMode, rememberedServerReports, forgetServerReport, trackPublic} from '../report-gateway.js';

const $ = id => document.getElementById(id);

export async function renderMyReports(root) {
  if (await reportMode() !== 'backend') return navigate('login/resident');  // local mode: the demo resident workspace
  const reports = rememberedServerReports();
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1">
      ${heading({eyebrow: 'Your reports · this device', title: 'My reports', lead: 'Complaints you submitted from this browser, with their current status on the CivicLens server.'})}
      ${notice('This list is kept only in this browser; there are no resident accounts yet. On another device, track a report with its public ID. Your private claim code is never stored here.', 'info', 'lock')}
      <div id="my-reports" class="mt" role="status" aria-live="polite">${reports.length ? '<p class="micro">Loading current status…</p>' : ''}</div>
      <p class="row-actions mt">${btn(icon('camera') + '<span>Report an issue</span>', 'data-action="go-camera"', 'secondary')}<a class="btn btn-ghost" href="#/home">${icon('search')}<span>Track by ID</span></a></p>
    </main>
  </div>`;
  const slot = $('my-reports');
  if (!reports.length) {
    slot.innerHTML = notice('No reports have been submitted from this browser yet.', 'info', 'alert');
    return;
  }
  const results = await Promise.all(reports.map(async r => ({...r, ...(await trackPublic(r.id))})));
  if (!slot.isConnected) return;
  slot.innerHTML = `<ul class="plain-list" id="my-reports-list">${results.map(item).join('')}</ul>`;
  slot.querySelectorAll('[data-forget]').forEach(button => button.addEventListener('click', () => {
    forgetServerReport(button.dataset.forget);
    renderMyReports(root);
  }));
}

function item(r) {
  const forget = btn('<span>Remove from this list</span>', `data-forget="${esc(r.id)}" aria-label="Remove ${esc(r.id)} from this list (the complaint stays on the server)"`, 'ghost');
  const head = `<a class="inline-link code-inline" href="#/track/${esc(r.id)}">${esc(r.id)}</a>`;
  if (r.error) {
    return `<li class="panel" data-report="${esc(r.id)}"><p>${head} · ${esc(r.category)}</p>${notice('Status unavailable: ' + esc(r.error), 'warn', 'alert')}<p class="row-actions">${forget}</p></li>`;
  }
  if (!r.record) {
    return `<li class="panel" data-report="${esc(r.id)}"><p>${head} · ${esc(r.category)}</p>${notice('The server has no complaint with this ID.', 'warn', 'alert')}<p class="row-actions">${forget}</p></li>`;
  }
  const rec = r.record;
  const latest = rec.timeline.at(-1);
  return `<li class="panel" data-report="${esc(r.id)}">
      <div class="track-head"><p>${head} · ${esc(rec.category)} · ${esc(rec.area)}</p><div class="pill-row">${statusPill(rec.status)}${verificationPill(rec.verification)}</div></div>
      <p class="micro">${esc(rec.department)}${rec.updated ? ` · updated ${fmtDateTime(rec.updated)}` : ''}</p>
      ${latest ? `<p>${esc(latest.text)}</p>` : ''}
      <p class="row-actions"><a class="btn btn-secondary" href="#/track/${esc(r.id)}">${icon('search')}<span>Open tracking</span></a>${forget}</p>
    </li>`;
}
