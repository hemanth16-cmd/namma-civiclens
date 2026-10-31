// My Complaints for a resident signed in with a server account (server mode): the complaints linked to
// their account, from GET /residents/me/complaints, with the photos and videos they uploaded. Evidence is
// private: each file is fetched with the resident's session from the owner-only endpoint and shown from a
// blob URL that lives only on this page; there is no public or signed link. Other residents' complaints
// never reach this page (the server decides ownership from the session). Public tracking (#/track) and
// community issues (GET /complaints) are separate screens with separate data.
import {esc} from '../services.js';
import {icon, categoryIcon, resolutionState, workspaceShell, heading, link, statusPill, verificationPill, notice, empty, panel, panelHead, fmtDate, fmtDateTime} from '../ui.js';
import {addCleanup, setSession} from '../state.js';
import {navigate} from '../router.js';
import {ApiError} from '../api-client.js';
import {serverClient} from '../server-records.js';
import {residentSession} from '../resident-session.js';
import {wireWorkspace} from './workspace.js';

const THUMB_CONCURRENCY = 3;
const UNNAMED_AREA = 'Bengaluru · location on file';
const placeLabel = area => (area === UNNAMED_AREA ? 'Neighbourhood not named' : area);
const $ = id => document.getElementById(id);

function mount(root, title, body) {
  root.innerHTML = workspaceShell({title, body});
  wireWorkspace(root);
}

function sessionEnded() {
  residentSession.clear();
  setSession(null);
  navigate('login/resident');
}

/** A blob URL for one of the resident's own files, revoked when the page is left. */
async function evidenceURL(http, complaintId, evidenceId) {
  const blob = await http.fetchResidentEvidence(residentSession.get().token, complaintId, evidenceId);
  const url = URL.createObjectURL(blob);
  addCleanup(() => URL.revokeObjectURL(url));
  return {url, type: blob.type};
}

const firstPhoto = c => c.evidence.find(e => e.mediaType === 'photo');
const evidenceSummary = c => {
  const photos = c.evidence.filter(e => e.mediaType === 'photo').length;
  const videos = c.evidence.filter(e => e.mediaType === 'video').length;
  const parts = [photos && `${photos} ${photos === 1 ? 'photo' : 'photos'}`, videos && `${videos} ${videos === 1 ? 'video' : 'videos'}`].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'No evidence uploaded';
};

function card(c) {
  const s = resolutionState(c);
  const photo = firstPhoto(c);
  const video = !photo && c.evidence.find(e => e.mediaType === 'video');
  const media = photo
    ? `<div class="mc-thumb" data-thumb="${esc(photo.evidenceId)}" data-complaint="${esc(c.id)}"><span class="mc-thumb-msg">${icon('image', 'mini-icon')} Loading photo…</span></div>`
    : video
      ? `<div class="mc-thumb mc-thumb-video">${icon('video')}<span class="mc-thumb-msg">Video evidence · open to play</span></div>`
      : `<div class="mc-thumb mc-thumb-none">${categoryIcon(c.category)}<span class="mc-thumb-msg">No photo uploaded</span></div>`;
  const description = c.description ? (c.description.length > 140 ? c.description.slice(0, 137) + '…' : c.description) : '';
  return `<li class="mc-card" data-complaint-card="${esc(c.id)}">
    ${media}
    <div class="mc-body">
      <p class="mc-meta"><span class="code-inline">${esc(c.id)}</span> · ${esc(c.category)}</p>
      <h3 class="mc-title"><a href="#/resident/complaints/${esc(c.id)}">${esc(c.title)}</a></h3>
      ${description ? `<p class="mc-desc">${esc(description)}</p>` : '<p class="mc-desc micro">No description given.</p>'}
      <p class="pill-row">${statusPill(c.status)} ${verificationPill(c.verification)}</p>
      <p class="micro">${icon('pin', 'mini-icon')} ${esc(placeLabel(c.area))} · ${c.created ? fmtDateTime(c.created) : 'Date not recorded'}</p>
      <p class="micro">${icon('image', 'mini-icon')} ${esc(evidenceSummary(c))}${c.ownedVia === 'claimed' ? ' · claimed with its claim code' : ''}</p>
      <p class="row-actions"><a class="btn btn-secondary btn-sm" href="#/resident/complaints/${esc(c.id)}"><span>View details</span>${icon('forward', 'mini-icon')}</a>
        <span class="state-text-inline micro"><span class="state-dot ${s.className}" aria-hidden="true"></span>${esc(s.label)}</span></p>
    </div>
  </li>`;
}

/** Fills card thumbnails a few at a time; a file that cannot be loaded says so instead of breaking the page. */
async function loadThumbnails(root, http) {
  const slots = [...root.querySelectorAll('[data-thumb]')];
  const next = async () => {
    const slot = slots.shift();
    if (!slot) return;
    try {
      const {url} = await evidenceURL(http, slot.dataset.complaint, slot.dataset.thumb);
      if (slot.isConnected) slot.innerHTML = `<img src="${url}" alt="Photo you uploaded for ${esc(slot.dataset.complaint)}" loading="lazy">`;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return sessionEnded();
      if (slot.isConnected) {
        slot.classList.add('mc-thumb-failed');
        slot.innerHTML = `${icon('alert')}<span class="mc-thumb-msg">${err instanceof ApiError && err.status === 404 ? 'Photo not available on this server' : 'Photo could not be loaded'}</span>`;
      }
    }
    return next();
  };
  await Promise.all(Array.from({length: THUMB_CONCURRENCY}, next));
}

/** #/resident/complaints: the resident's own complaints as cards with their uploaded photos. */
export async function renderMyComplaints(root) {
  mount(root, 'My complaints', `
    ${heading({eyebrow: 'Your workspace', title: 'My complaints', lead: 'Every complaint linked to your resident account, with the photos and videos you uploaded. Only you can see your evidence here.',
      actions: link(icon('camera') + '<span>Report an issue</span>', '#/camera', 'primary')})}
    <p class="result-count" id="mc-count" role="status" aria-live="polite">Loading your complaints…</p>
    <div id="mc-results"></div>`);
  const results = $('mc-results');
  const count = $('mc-count');
  const http = await serverClient();
  let items;
  try {
    ({items} = await http.listResidentComplaints(residentSession.get().token, {limit: 100}));
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return sessionEnded();
    if (!results.isConnected) return;
    count.textContent = '';
    results.innerHTML = notice(esc(err instanceof ApiError ? err.message : 'Your complaints could not be loaded.') + ' Try again in a moment.', 'error', 'alert');
    return;
  }
  if (!results.isConnected) return;
  if (!items.length) {
    count.textContent = '';
    results.innerHTML = panel(empty('You have not submitted any complaints yet',
      'Reports you make while signed in appear here with their photos. An anonymous report can be added with its public ID and private claim code.',
      `<div class="row-actions">${link(icon('camera') + '<span>Report an issue</span>', '#/camera', 'primary')}${link('Claim a complaint', '#/resident/claim', 'secondary')}</div>`));
    return;
  }
  count.textContent = `${items.length} ${items.length === 1 ? 'complaint' : 'complaints'} linked to your account`;
  results.innerHTML = `<ul class="mc-grid" id="mc-grid">${items.map(card).join('')}</ul>`;
  loadThumbnails(root, http);
}

/** #/resident/complaints/<id>: one of the resident's own complaints with all its evidence and its timeline. */
export async function renderMyComplaint(root, id) {
  mount(root, 'My complaint', `<p><a class="inline-link" href="#/resident/complaints">${icon('back', 'mini-icon')}<span>All my complaints</span></a></p>
    <div id="mc-detail" role="status" aria-live="polite"><p class="micro">Loading ${esc(id)}…</p></div>`);
  const slot = $('mc-detail');
  const http = await serverClient();
  let c;
  try {
    c = await http.getResidentComplaint(residentSession.get().token, id);  // null when it is not in this account
    if (!c) throw new ApiError('This complaint is not in your account.', {status: 404, code: 'not_found'});
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return sessionEnded();
    if (!slot.isConnected) return;
    slot.innerHTML = err instanceof ApiError && err.status === 404
      ? panel(empty('This complaint is not in your account', 'Only complaints you submitted while signed in, or claimed with their claim code, open here. Anyone can follow a report by its public ID on public tracking.',
        `<div class="row-actions">${link('All my complaints', '#/resident/complaints', 'secondary')}${link('Public tracking', '#/track/' + encodeURIComponent(id), 'ghost')}</div>`))
      : notice(esc(err instanceof ApiError ? err.message : 'This complaint could not be loaded.'), 'error', 'alert');
    return;
  }
  if (!slot.isConnected) return;
  const timeline = [...c.timeline].reverse().map(t => `<li><div class="tl-head"><strong>${esc(t.status)}</strong><time datetime="${esc(t.date)}">${fmtDateTime(t.date)}</time></div><p>${esc(t.text)}</p></li>`).join('');
  const verification = [...c.verificationLog].reverse().map(e => `<li><div class="tl-head"><strong>${esc(e.type === 'response' ? `Citizen response: ${e.response || ''}` : `Verification: ${e.to || ''}`)}</strong><time datetime="${esc(e.date)}">${fmtDateTime(e.date)}</time></div>${e.note ? `<p>${esc(e.note)}</p>` : ''}</li>`).join('');
  const figures = c.evidence.map(e => `<figure class="evidence-figure" data-evidence="${esc(e.evidenceId)}" data-media="${esc(e.mediaType)}">
      <figcaption><span>${e.role === 'repair' ? 'After repair · from the civic team' : 'Your report'}</span><span class="micro">${esc(e.mediaType === 'video' ? 'Video' : 'Photo')} · ${Math.max(1, Math.round(e.byteSize / 1024))} KB</span></figcaption>
      <div class="evidence-empty mc-evidence-slot">${icon(e.mediaType === 'video' ? 'video' : 'image')}<span>Loading…</span></div>
      <p class="micro">${e.capturedAt ? `Captured ${fmtDateTime(e.capturedAt)} · ` : ''}Uploaded ${fmtDateTime(e.uploadedAt)}</p>
    </figure>`).join('');
  slot.innerHTML = `
    ${heading({eyebrow: `${esc(c.id)} · ${esc(c.category)}`, title: esc(c.title), lead: `${esc(placeLabel(c.area))} · reported ${c.created ? esc(fmtDate(c.created)) : 'on an unrecorded date'}`,
      actions: `<a class="btn btn-ghost" href="#/track/${esc(c.id)}">${icon('search')}<span>Public tracking page</span></a>`})}
    <div class="detail-layout">
      <div class="stack">
        <section class="panel evidence-panel" aria-labelledby="mc-evidence-title">
          ${panelHead(`<span id="mc-evidence-title">Your evidence</span>`, `<span class="pill">${esc(evidenceSummary(c))}</span>`, 'Private to you and the civic team')}
          ${c.evidence.length ? `<div class="mc-evidence-grid">${figures}</div>` : empty('No evidence uploaded', 'This complaint has no photo or video on the server. If the upload failed, retry it from the receipt on the device you reported from.')}
        </section>
        <section class="panel">${panelHead('Progress timeline', '', 'Public updates')}${timeline ? `<ol class="timeline">${timeline}</ol>` : '<p class="micro">No updates yet.</p>'}</section>
        ${verification ? `<section class="panel">${panelHead('Verification history', '', 'Separate from the official status')}<ol class="timeline">${verification}</ol></section>` : ''}
      </div>
      <aside class="stack side-col">
        <section class="panel">
          <h2>About this complaint</h2>
          <p>${c.description ? esc(c.description) : '<span class="micro">No description given.</span>'}</p>
          <dl class="meta-list">
            <div><dt>Status</dt><dd>${statusPill(c.status)}</dd></div>
            <div><dt>Verification</dt><dd>${verificationPill(c.verification)}</dd></div>
            <div><dt>Department</dt><dd>${esc(c.department || 'Awaiting civic review')}</dd></div>
            <div><dt>Neighbourhood</dt><dd>${esc(placeLabel(c.area))}</dd></div>
            <div><dt>Location</dt><dd>${c.gps ? `${c.gps.lat.toFixed(5)}, ${c.gps.lng.toFixed(5)}${c.gps.accuracy ? ` · ±${Math.round(c.gps.accuracy)} m` : ''}${c.gps.address ? `<br><span class="micro">${esc(c.gps.address)}</span>` : ''}` : '<span class="micro">Not recorded</span>'}</dd></div>
            <div><dt>Submitted</dt><dd>${c.created ? fmtDateTime(c.created) : '—'}</dd></div>
            <div><dt>In your account</dt><dd>${c.ownedVia === 'claimed' ? 'Claimed with its claim code' : 'Submitted while signed in'}${c.ownedAt ? ` · ${fmtDate(c.ownedAt)}` : ''}</dd></div>
          </dl>
        </section>
      </aside>
    </div>`;
  for (const fig of slot.querySelectorAll('[data-evidence]')) {
    const holder = fig.querySelector('.mc-evidence-slot');
    try {
      const {url, type} = await evidenceURL(http, c.id, fig.dataset.evidence);
      if (!holder.isConnected) return;
      holder.outerHTML = type.startsWith('video/')
        ? `<video class="receipt-media" controls playsinline preload="metadata" src="${url}" aria-label="Video evidence for ${esc(c.id)}"></video>`
        : `<img class="receipt-media" src="${url}" alt="Photo evidence for ${esc(c.id)}">`;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return sessionEnded();
      if (holder.isConnected) holder.innerHTML = `${icon('alert')}<span>${err instanceof ApiError && err.status === 404 ? 'This file is not available on this server.' : 'This file could not be loaded. Try again later.'}</span>`;
    }
  }
}
