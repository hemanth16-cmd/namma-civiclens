// Public screens: the three-choice entry, public tracking and demonstration sign-in.
import {api, esc, DEMO_PASSWORD} from '../services.js';
import {icon, btn, statusPill, verificationPill, heading, notice, publicTopbar, fmtDate, fmtDateTime, toast, panel, logo} from '../ui.js';
import {setSession, viewer} from '../state.js';
import {navigate} from '../router.js';
import {reportMode, trackPublic, respondToResolution} from '../report-gateway.js';
import {staffSession} from '../staff-session.js';
import {serverClient} from '../server-records.js';

const SAMPLE_ID = /^NCL-2026-DEMO0[1-8]$/;

const EVENT_LABEL = {state: 'Verification changed', response: 'Community response'};

export async function renderEntry(root) {
  // Server mode: the two cards lead to the real screens, not the demonstration workspaces.
  const server = await reportMode() === 'backend';
  const residentCard = server
    ? `<a class="entry-card entry-link" href="#/my-reports" data-entry="my-reports">
          <span class="entry-icon">${icon('user')}</span>
          <div><p class="eyebrow">Residents · this device</p><h2>My reports</h2><p>Complaints you submitted from this browser and their current status on the CivicLens server.</p></div>
          <span class="entry-go">${icon('forward')}</span>
        </a>`
    : `<a class="entry-card entry-link" href="#/login/resident">
          <span class="entry-icon">${icon('user')}</span>
          <div><p class="eyebrow">Resident workspace</p><h2>Login as user</h2><p>Your reports, claims, neighbourhood issues, notifications and preferences.</p></div>
          <span class="entry-go">${icon('forward')}</span>
        </a>`;
  const adminCard = server
    ? `<a class="entry-card entry-link" href="#/login/official" data-entry="staff">
          <span class="entry-icon">${icon('shield')}</span>
          <div><p class="eyebrow">Civic operations · staff account</p><h2>Login as admin</h2><p>Dashboard, incident management and city map of the complaints on the CivicLens server, with their private evidence.</p></div>
          <span class="entry-go">${icon('forward')}</span>
        </a>`
    : `<a class="entry-card entry-link" href="#/login/official">
          <span class="entry-icon">${icon('shield')}</span>
          <div><p class="eyebrow">Civic operations</p><h2>Login as admin</h2><p>Incident management, assignments, work orders, video review and decision support.</p></div>
          <span class="entry-go">${icon('forward')}</span>
        </a>`;
  root.innerHTML = `<div class="page page-entry">
    ${publicTopbar({fallback: 'camera', right: `<a class="btn btn-ghost" href="#/camera">${icon('camera')}<span>Report an issue</span></a>`})}
    <main id="main" class="entry-main" tabindex="-1">
      <div class="entry-intro">
        <p class="eyebrow">Namma Bengaluru · Namma responsibility</p>
        <h1 id="page-title" tabindex="-1">Where would you like to go?</h1>
        <p class="lead">Track a report with its public ID, or sign in to the workspace that fits your role. Each path has its own screen.</p>
      </div>
      <div class="entry-grid">
        <section class="entry-card entry-track" aria-labelledby="track-title">
          <span class="entry-icon">${icon('search')}</span>
          <p class="eyebrow">Public · no sign-in</p>
          <h2 id="track-title">Track by problem ID</h2>
          <p>See the public status, responsible department and timeline. Claim codes, reporter details and private evidence never appear here.</p>
          <form class="inline-form" id="entry-track-form" role="search">
            <label for="entry-track-id" class="sr-only">Public complaint ID</label>
            <input id="entry-track-id" name="id" autocomplete="off" spellcheck="false" placeholder="${server ? 'NCL-2026-XXXXXX' : 'NCL-2026-DEMO01'}" required>
            <button class="btn btn-primary" type="submit">Track issue</button>
          </form>
        </section>
        ${residentCard}
        ${adminCard}
      </div>
      ${server ? '' : '<p class="micro entry-sample">Sample public ID to try: <a class="inline-link" href="#/track/NCL-2026-DEMO01">NCL-2026-DEMO01</a>. Sample data is illustrative.</p>'}
    </main>
  </div>`;
  root.querySelector('#entry-track-form').onsubmit = e => {
    e.preventDefault();
    const id = new FormData(e.target).get('id').trim().toUpperCase();
    navigate('track/' + encodeURIComponent(id));
  };
}

export async function renderTrack(root, rawId = '') {
  const id = decodeURIComponent(rawId || '').trim().toUpperCase();
  const mode = await reportMode();
  if (mode !== 'local') return renderServerTrack(root, id);
  const r = id ? await api.publicRecord(id) : null;
  const signedIn = viewer().role !== 'public';
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1">
      ${heading({eyebrow: 'Public complaint tracking', title: 'Follow the change', lead: 'Enter the public complaint ID. You will see the same status and timeline the public can see, and nothing private.'})}
      <form class="panel form-row" id="track-form" role="search">
        <label for="track-id" class="sr-only">Complaint ID</label>
        <input id="track-id" name="id" autocomplete="off" spellcheck="false" placeholder="e.g. NCL-2026-DEMO01" value="${esc(id)}" required>
        <button class="btn btn-primary" type="submit">${icon('search')}<span>Track</span></button>
      </form>
      ${id && !r ? notice(`No public record matches <strong>${esc(id)}</strong> on this device. Check the ID for typos. Reports saved on other devices are not visible here.`, 'warn', 'alert', 'mt') : ''}
      ${r ? trackResult(r, signedIn) : (!id ? `<p class="micro mt">Try <a class="inline-link" href="#/track/NCL-2026-DEMO01">NCL-2026-DEMO01</a>.</p>` : '')}
    </main>
  </div>`;
  root.querySelector('#track-form').onsubmit = e => {
    e.preventDefault();
    navigate('track/' + encodeURIComponent(new FormData(e.target).get('id').trim().toUpperCase()));
  };
}

// Server-mode tracking: shows only the server's public projection. A failed or empty server lookup
// is reported as such; local records are never shown in its place.
async function renderServerTrack(root, id) {
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1" data-track-source="server">
      ${heading({eyebrow: 'Public complaint tracking · CivicLens server', title: 'Follow the change', lead: 'Enter the public complaint ID. You will see the status and timeline the CivicLens server shows the public, and nothing private.'})}
      <form class="panel form-row" id="track-form" role="search">
        <label for="track-id" class="sr-only">Complaint ID</label>
        <input id="track-id" name="id" autocomplete="off" spellcheck="false" placeholder="e.g. NCL-2026-K7Q2MX" value="${esc(id)}" required>
        <button class="btn btn-primary" type="submit">${icon('search')}<span>Track</span></button>
      </form>
      <div id="track-outcome" role="status" aria-live="polite">${id ? `<p class="micro mt">Checking the CivicLens server for <strong>${esc(id)}</strong>…</p>` : ''}</div>
    </main>
  </div>`;
  root.querySelector('#track-form').onsubmit = e => {
    e.preventDefault();
    navigate('track/' + encodeURIComponent(new FormData(e.target).get('id').trim().toUpperCase()));
  };
  if (!id) return;
  const {record, error} = await trackPublic(id);
  const slot = root.querySelector('#track-outcome');
  if (!slot?.isConnected) return; // the user navigated away while the request was in flight
  if (error) {
    slot.innerHTML = notice(`<strong>Tracking is unavailable right now.</strong> ${esc(error)}`, 'error', 'alert', 'mt');
  } else if (!record) {
    slot.innerHTML = notice(SAMPLE_ID.test(id)
      ? `<strong>${esc(id)}</strong> is a sample report. Samples exist only in local demo mode and are not stored on the CivicLens server, which starts empty.`
      : `No complaint with the ID <strong>${esc(id)}</strong> was found on the CivicLens server. Check the ID for typos. Reports saved in local mode stay on the device that created them and are not on the server.`, 'warn', 'alert', 'mt');
  } else {
    slot.innerHTML = trackResult(record, false)
      + (record.status === 'Marked Resolved' ? verifyForm(record) : '')
      + `<p class="mt"><a class="btn btn-secondary" href="#/notifications/follow/${esc(record.id)}" id="track-notify">${icon('bell')}<span>Get notified about this report</span></a> <span class="micro">Needs the private claim code from the receipt.</span></p>`;
    wireVerifyForm(root, record);
  }
}

// Citizen verification (server mode): the reporter confirms or disputes a resolution with the
// private claim code from the receipt. The code is sent once in the request body; it is never
// stored in this browser or put in the address bar.
function verifyForm(r) {
  return `<form class="panel mt" id="verify-form" autocomplete="off">
      <h2>Is the issue fixed?</h2>
      <p class="micro">The civic team marked this complaint resolved. If you reported it, tell them whether the problem is really gone. Your answer is shown publicly as “Citizen”, without your name.</p>
      <div class="field"><label for="verify-claim">Private claim code from your receipt</label>
        <input id="verify-claim" name="claim" type="password" autocomplete="off" spellcheck="false" maxlength="100" required></div>
      <div class="row-actions">
        <button class="btn btn-primary" type="submit" value="Appears Resolved">Yes, it appears resolved</button>
        <button class="btn btn-secondary" type="submit" value="Still Present">No, it is still there</button>
        <button class="btn btn-ghost" type="submit" value="Cannot Confirm">I cannot confirm</button>
      </div>
      <div id="verify-outcome" role="status" aria-live="polite"></div>
    </form>`;
}

function wireVerifyForm(root, r) {
  const form = root.querySelector('#verify-form');
  if (!form) return;
  form.onsubmit = async e => {
    e.preventDefault();
    const response = e.submitter?.value;
    const claim = form.querySelector('#verify-claim').value.trim();
    const outcome = form.querySelector('#verify-outcome');
    if (!response || !claim) return;
    form.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const result = await respondToResolution(r.id, claim, response);
      form.querySelector('#verify-claim').value = '';
      outcome.innerHTML = notice(result.changed
        ? `Thank you. Verification is now <strong>${esc(result.verification)}</strong>.`
        : 'Thank you. Your response was recorded.', 'success', 'check', 'mt');
      setTimeout(() => { if (form.isConnected) renderTrack(root, r.id); }, 1500);
    } catch (err) {
      form.querySelectorAll('button').forEach(b => { b.disabled = false; });
      outcome.innerHTML = notice(esc(err?.message || 'Your response could not be sent.'), 'error', 'alert', 'mt');
    }
  };
}

function trackResult(r, signedIn) {
  const timeline = [...r.timeline].reverse().map(t => `<li><div class="tl-head"><strong>${esc(t.status)}</strong><time datetime="${esc(t.date)}">${fmtDateTime(t.date)}</time></div><p>${esc(t.text)}</p></li>`).join('');
  const log = (r.verificationLog || []).slice().reverse().map(e => `<li><div class="tl-head"><strong>${EVENT_LABEL[e.type] || 'Verification'}${e.response ? `: ${esc(e.response)}` : ''}</strong><time datetime="${esc(e.date)}">${fmtDateTime(e.date)}</time></div><p>${e.to ? `Now: ${esc(e.to)}.` : ''} ${e.note ? esc(e.note) : ''}</p></li>`).join('');
  return `<article class="track-result panel" aria-labelledby="track-result-title">
    <header class="track-head">
      <div><p class="eyebrow">Public ID</p><h2 id="track-result-title" class="code-inline">${esc(r.id)}</h2></div>
      <div class="pill-row">${statusPill(r.status)}${verificationPill(r.verification)}</div>
    </header>
    <dl class="meta-list two-col">
      <div><dt>Category</dt><dd>${esc(r.category)}</dd></div>
      <div><dt>Neighbourhood</dt><dd>${esc(r.area)}</dd></div>
      <div><dt>Responsible department</dt><dd>${esc(r.department)}</dd></div>
      <div><dt>Last updated</dt><dd>${fmtDate(r.updated)}</dd></div>
    </dl>
    ${notice('<strong>Two separate checks.</strong> The civic team’s lifecycle status is reported by the administration. Verification shows whether community members or inspections confirmed, disputed or requested a recheck of the repair.', 'info', 'eye')}
    <h3 class="mt">Public timeline</h3>
    ${timeline ? `<ol class="timeline">${timeline}</ol>` : '<p class="micro">No public updates yet.</p>'}
    ${log ? `<h3 class="mt">Verification history</h3><ol class="timeline">${log}</ol>` : ''}
    <footer class="track-foot">
      ${signedIn ? `<a class="btn btn-secondary" href="#/issue/${esc(r.id)}">Open in your workspace</a>` : ''}
      <a class="btn btn-ghost" href="#/camera">${icon('camera')}<span>Report a new issue</span></a>
    </footer>
  </article>`;
}

export async function renderLogin(root, role = 'resident', register = false) {
  if (role === 'official' && await reportMode() === 'backend') return renderServerAdminLogin(root);
  const official = role === 'official';
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1">
      <section class="panel auth-panel" aria-labelledby="login-title">
        <p class="pill pill-coral">${icon(official ? 'shield' : 'user', 'pill-icon')}<span>${official ? 'Admin access' : 'Resident access'} · demonstration</span></p>
        <h1 id="login-title" tabindex="-1">${register ? 'Create a demo resident profile' : official ? 'Civic operations sign-in' : 'Sign in to your neighbourhood'}</h1>
        <p class="lead">${official ? 'Triage, assign and verify civic work from one workspace.' : 'Your reports, claims, neighbourhood issues and notifications.'}</p>
        <form id="login-form" class="stack">
          ${register ? `<div class="field"><label for="login-name">Your name</label><input id="login-name" name="name" required placeholder="Your name" autocomplete="name"></div>` : ''}
          <div class="field"><label for="login-email">Email address</label><input id="login-email" name="email" type="email" autocomplete="username" value="${official ? 'official' : 'resident'}@civiclens.demo" required></div>
          <div class="field"><label for="login-password">${register ? 'Demo passphrase (6+ characters)' : 'Password'}</label><input id="login-password" name="password" type="password" minlength="6" autocomplete="${register ? 'new-password' : 'current-password'}" value="${register ? '' : DEMO_PASSWORD}" required></div>
          <div id="login-error" role="alert"></div>
          <button class="btn btn-primary btn-block btn-lg" type="submit">${register ? 'Create demo profile' : 'Sign in'} ${icon('forward')}</button>
        </form>
        ${notice(`Demonstration only. Local demo credentials are pre-filled. Local sign-in gives no production security. Never enter a real password here.`, 'warn', 'lock', 'mt')}
        <div class="auth-foot">
          ${!official ? (register ? '<a class="inline-link" href="#/login/resident">Already have a profile? Sign in</a>' : '<a class="inline-link" href="#/register">New here? Create a demo profile</a>') : ''}
          <a class="inline-link" href="#/login/${official ? 'resident' : 'official'}">Use ${official ? 'resident' : 'admin'} sign-in instead</a>
        </div>
      </section>
    </main>
  </div>`;
  reportMode().then(mode => {
    if (mode !== 'backend') return;
    root.querySelector('.auth-panel')?.insertAdjacentHTML('afterbegin', notice('<strong>Server mode:</strong> reports you sent to the CivicLens server are under <a class="inline-link" href="#/my-reports">My reports</a>. No sign-in is needed.', 'info', 'user'));
  });
  root.querySelector('#login-form').onsubmit = async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const submit = e.target.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const session = await api.login(role, String(fd.get('email')).trim(), String(fd.get('password')), register, String(fd.get('name') || '').trim());
      setSession(session);
      toast(`Signed in as ${session.name}.`, 'success');
      navigate(session.role);
    } catch (err) {
      submit.disabled = false;
      document.getElementById('login-error').innerHTML = notice(esc(err.message), 'error', 'alert');
    }
  };
}

// Server mode: the admin workspace signs in with a server staff account (python -m app.staff create).
// The session token stays in this tab's memory only (staff-session.js); the dashboard, incident list
// and city map then show the complaints stored on the server.
function renderServerAdminLogin(root) {
  if (staffSession.get()) return navigate('official');
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1">
      <section class="panel auth-panel" aria-labelledby="login-title">
        <p class="pill pill-coral">${icon('shield', 'pill-icon')}<span>Civic operations · server account</span></p>
        <h1 id="login-title" tabindex="-1">Civic operations sign-in</h1>
        <p class="lead">Sign in with the staff account created for you on the CivicLens server. Your dashboard, incident list and city map then show the complaints stored on the server.</p>
        <form id="login-form" class="stack" data-server-login autocomplete="on">
          <div class="field"><label for="login-username">Username</label><input id="login-username" name="username" autocomplete="username" required maxlength="64" spellcheck="false"></div>
          <div class="field"><label for="login-password">Password</label><input id="login-password" name="password" type="password" autocomplete="current-password" required maxlength="256"></div>
          <div id="login-error" role="alert"></div>
          <button class="btn btn-primary btn-block btn-lg" type="submit">Sign in ${icon('forward')}</button>
        </form>
        ${notice('Your session is kept only in this tab’s memory: reloading or closing the page signs you out. Staff accounts are created by an administrator with <code>python -m app.staff create</code>.', 'info', 'lock', 'mt')}
        <div class="auth-foot"><a class="inline-link" href="#/my-reports">Looking for your own reports? Open My reports</a></div>
      </section>
    </main>
  </div>`;
  root.querySelector('#login-form').onsubmit = async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const submit = e.target.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const session = await (await serverClient()).staffSignIn(String(fd.get('username')), String(fd.get('password')));
      staffSession.set(session);
      setSession({role: 'official', name: session.staff.displayName, email: session.staff.username, server: true});
      toast(`Signed in as ${session.staff.displayName}.`, 'success');
      navigate('official');
    } catch (err) {
      submit.disabled = false;
      e.target.querySelector('#login-password').value = '';
      document.getElementById('login-error').innerHTML = notice(esc(err?.name === 'ApiError' ? err.message : 'Sign-in failed.'), 'error', 'alert');
    }
  };
}

export {logo, panel, btn};
