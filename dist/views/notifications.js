// Notification inbox (server mode): push on/off for this device, persistent history with
// read/unread, registered devices and, for citizens, the reports this device follows.
// #/notifications is the citizen inbox (credential: this device's inbox token); the staff console
// mounts the same inbox at #/staff/notifications with the in-memory staff session.
// Incoming notifications are shown only by the operating system (via the service worker); this
// screen is their history, not a second pop-up channel.

import {esc} from '../services.js';
import {icon, btn, heading, notice, publicTopbar, fmtDateTime, toast} from '../ui.js';
import {addCleanup} from '../state.js';
import {navigate} from '../router.js';
import {loadApiConfig} from '../api-config.js';
import {ApiError, createApiClient} from '../api-client.js';
import {disablePush, enablePush, ensureInbox, inbox, pushState, pushSupport, requestPermissionNow} from '../push-client.js';

const PUBLIC_ID = /^NCL-\d{4}-[A-Z0-9]{6}$/;
const PAGE = 20;
const $ = id => document.getElementById(id);
const message = err => (err instanceof ApiError ? err.message : err?.message) || 'The request failed.';

/** Routes: #/notifications and #/notifications/follow/<publicId> (prefills the follow form). */
export async function renderNotifications(root, sub = '', arg = '') {
  const config = await loadApiConfig();
  const prefill = sub === 'follow' && PUBLIC_ID.test(decodeURIComponent(arg || '').toUpperCase()) ? decodeURIComponent(arg).toUpperCase() : '';
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="narrow" tabindex="-1" data-notifications="citizen">
      ${heading({eyebrow: 'Namma CivicLens · server', title: 'Notifications', lead: 'Get a system notification on this phone or computer when something happens to a report you follow, even when CivicLens is closed.'})}
      <div id="notifications-root" role="status" aria-live="polite"><p class="micro">Loading…</p></div>
    </main>
  </div>`;
  const slot = $('notifications-root');
  if (config.mode !== 'backend') {
    slot.innerHTML = notice('Notifications work only in <strong>server mode</strong> (set <code>"mode": "backend"</code> in <code>api-config.json</code>). Reports in local mode stay on this device and do not send notifications.', 'info', 'alert');
    return;
  }
  const http = createApiClient({baseUrl: config.baseUrl});
  return mountInbox(slot, {http, audience: 'citizen', token: inbox.get(), prefill, onUnauthorized: () => {
    inbox.clear();  // the server no longer knows this inbox (for example after a database reset)
    return renderNotifications(root, sub, arg);
  }});
}

/**
 * Renders the inbox into `slot`. options: {http, audience: 'citizen'|'staff', token (null for a
 * citizen without an inbox yet), prefill, onUnauthorized()}.
 */
export async function mountInbox(slot, options) {
  const {http, audience} = options;
  let token = options.token;
  let push;
  try {
    push = await http.getPushConfig();
  } catch (err) {
    slot.innerHTML = notice(esc(message(err)), 'error', 'alert');
    return;
  }
  if (!slot.isConnected) return;
  const citizen = audience === 'citizen';
  slot.innerHTML = `
    <section class="panel stack" aria-labelledby="push-title"><h2 id="push-title">On this device</h2><div id="push-panel"></div></section>
    ${citizen ? '<section class="panel stack mt" aria-labelledby="follow-title"><h2 id="follow-title">Reports you follow</h2><div id="follow-panel"></div></section>' : ''}
    <section class="panel stack mt" aria-labelledby="history-title">
      <div class="row-actions"><h2 id="history-title">History <span id="unread-count" class="count-badge" hidden></span></h2>
        <span class="spacer"></span>${btn('<span>Mark all as read</span>', 'id="mark-all-read" disabled', 'ghost')}</div>
      <div id="notification-list"><p class="micro">${token ? 'Loading…' : 'No notifications yet.'}</p></div>
    </section>
    <section class="panel stack mt" aria-labelledby="devices-title"><h2 id="devices-title">Devices receiving notifications</h2><div id="device-list"><p class="micro">${token ? 'Loading…' : 'None yet.'}</p></div></section>`;

  const fail = (err, target) => {
    if (err instanceof ApiError && err.status === 401) return options.onUnauthorized?.();
    if (target?.isConnected) target.innerHTML = notice(esc(message(err)), 'error', 'alert');
    else toast(message(err), 'error');
  };
  const needToken = async () => {
    if (!token) token = await ensureInbox(http);
    return token;
  };

  // ----- push on this device -----
  function renderPush() {
    const panel = $('push-panel');
    if (!panel) return;
    const state = pushState(audience);
    const support = pushSupport();
    let body;
    if (!push.pushEnabled) body = notice('The CivicLens server has not been set up to send push notifications. Notifications are still kept in the history below.', 'warn', 'alert');
    else if (state === 'unsupported') body = notice(esc(support.reason), 'warn', 'alert');
    else if (state === 'denied') body = notice('Notifications are <strong>blocked</strong> for this site. Allow them in the browser’s site settings (the icon beside the address), then reload this page.', 'warn', 'lock');
    else if (state === 'enabled') body = `<p><strong>On.</strong> This ${citizen ? 'device' : 'browser'} shows system notifications${citizen ? ' for the reports you follow' : ' for new complaints and evidence'}, also when CivicLens is closed (where the browser allows it).</p>`;
    else body = `<p>Off. Turning it on asks your browser for permission to show notifications.</p>`;
    const actions = [];
    if (push.pushEnabled && state === 'off') actions.push(btn(icon('bell') + '<span>Turn on notifications</span>', 'id="push-enable"', 'primary'));
    if (state === 'enabled') actions.push(btn('<span>Turn off on this device</span>', 'id="push-disable"', 'secondary'));
    if (token && push.pushEnabled && state === 'enabled') actions.push(btn('<span>Send a test notification</span>', 'id="push-test"', 'ghost'));
    panel.innerHTML = `${body}<div class="row-actions">${actions.join('')}</div><div id="push-feedback"></div>`;
    $('push-enable')?.addEventListener('click', async e => {
      e.currentTarget.disabled = true;
      try {
        await requestPermissionNow();  // while the click is being handled, before any network request
        const result = await enablePush({http, token: await needToken(), audience, config: push});
        if (result.state === 'enabled') toast('Notifications are on for this device.', 'success');
        renderPush(); loadDevices(); loadHistory();
        if (result.state !== 'enabled' && $('push-feedback')) $('push-feedback').innerHTML = notice(esc(result.message), 'warn', 'alert');
      } catch (err) {
        renderPush();
        fail(err, $('push-feedback'));
      }
    });
    $('push-disable')?.addEventListener('click', async e => {
      e.currentTarget.disabled = true;
      try {
        await disablePush({http, token, audience});
        toast('Notifications are off for this device. Your history is kept.', 'success');
      } catch (err) {
        fail(err, $('push-feedback'));
      }
      renderPush(); loadDevices();
    });
    $('push-test')?.addEventListener('click', async e => {
      e.currentTarget.disabled = true;
      try {
        await http.sendTestNotification(token);
        $('push-feedback').innerHTML = notice('Test sent. It should appear as a system notification within a few seconds.', 'info', 'bell');
        setTimeout(loadHistory, 1500);
      } catch (err) {
        fail(err, $('push-feedback'));
      }
    });
  }

  // ----- follows (citizens) -----
  async function renderFollows() {
    const panel = $('follow-panel');
    if (!panel) return;
    panel.innerHTML = `<form id="follow-form" class="stack" autocomplete="off">
        <p class="micro">Follow a report you made with its public ID and the private claim code from its receipt.</p>
        <div class="field"><label for="follow-id">Complaint ID</label><input id="follow-id" name="id" required spellcheck="false" placeholder="NCL-2026-K7Q2MX" value="${esc(options.prefill || '')}"></div>
        <div class="field"><label for="follow-claim">Claim code</label><input id="follow-claim" name="claim" required spellcheck="false" autocomplete="off"></div>
        <div id="follow-feedback" role="alert"></div>
        <button class="btn btn-secondary" type="submit">${icon('bell')}<span>Follow this report</span></button>
      </form><div id="follow-list" class="mt"></div>`;
    $('follow-form').addEventListener('submit', async e => {
      e.preventDefault();
      const form = new FormData(e.target);
      const id = String(form.get('id')).trim().toUpperCase();
      const button = e.target.querySelector('button[type=submit]');
      if (!PUBLIC_ID.test(id)) { $('follow-feedback').innerHTML = notice('Enter a complaint ID like NCL-2026-K7Q2MX.', 'warn', 'alert'); return; }
      button.disabled = true;
      try {
        await http.followComplaint(await needToken(), id, String(form.get('claim')).trim());
        $('follow-claim').value = '';
        $('follow-feedback').innerHTML = notice(`You now follow <strong>${esc(id)}</strong>.`, 'success', 'check');
        loadFollows(); setTimeout(loadHistory, 800);
        if (!options.prefill) $('follow-id').value = '';
      } catch (err) {
        fail(err, $('follow-feedback'));
      } finally {
        button.disabled = false;
      }
      renderPush();
    });
    loadFollows();
  }

  async function loadFollows() {
    const list = $('follow-list');
    if (!list) return;
    if (!token) { list.innerHTML = '<p class="micro">You do not follow any reports yet.</p>'; return; }
    try {
      const follows = await http.listFollows(token);
      if (!list.isConnected) return;
      list.innerHTML = follows.length
        ? `<ul class="plain-list">${follows.map(f => `<li class="row-actions"><a class="inline-link code-inline" href="#/track/${esc(f.complaintId)}">${esc(f.complaintId)}</a><span class="micro">since ${fmtDateTime(f.followedAt)}</span><span class="spacer"></span>${btn('<span>Unfollow</span>', `data-unfollow="${esc(f.complaintId)}"`, 'ghost')}</li>`).join('')}</ul>`
        : '<p class="micro">You do not follow any reports yet.</p>';
      list.querySelectorAll('[data-unfollow]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await http.unfollowComplaint(token, b.dataset.unfollow); loadFollows(); } catch (err) { fail(err, list); }
      }));
    } catch (err) {
      fail(err, list);
    }
  }

  // ----- history -----
  let items = [];
  let nextBefore = null;

  function setUnread(count) {
    const badge = $('unread-count');
    if (badge) {
      badge.hidden = count === 0;
      badge.textContent = `${count} unread`;
    }
    const all = $('mark-all-read');
    if (all) all.disabled = count === 0;
  }

  function renderHistory() {
    const list = $('notification-list');
    if (!list) return;
    if (!items.length) { list.innerHTML = '<p class="micro">No notifications yet.</p>'; return; }
    list.innerHTML = `<ol class="notification-list">${items.map(n => `<li class="notification-item ${n.read ? '' : 'unread'}" data-id="${esc(n.id)}">
        <div class="notification-text"><p class="notification-title">${n.read ? '' : '<span class="unread-dot" aria-hidden="true"></span><span class="sr-only">Unread: </span>'}<strong>${esc(n.title)}</strong></p>
          <p>${esc(n.body)}</p><p class="micro"><time datetime="${esc(n.createdAt)}">${fmtDateTime(n.createdAt)}</time></p></div>
        <div class="row-actions">${btn('<span>Open</span>', `data-open="${esc(n.id)}"`, 'secondary')}${btn(`<span>${n.read ? 'Mark as unread' : 'Mark as read'}</span>`, `data-toggle="${esc(n.id)}"`, 'ghost')}</div>
      </li>`).join('')}</ol>${nextBefore ? `<div class="row-actions mt">${btn('<span>Show older</span>', 'id="notifications-more"', 'ghost')}</div>` : ''}`;
    list.querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', async () => {
      const n = items.find(i => i.id === b.dataset.open);
      if (!n) return;
      if (!n.read) { try { await http.markNotification(token, n.id, true); } catch { /* open anyway */ } }
      navigate(n.url.slice(2));
    }));
    list.querySelectorAll('[data-toggle]').forEach(b => b.addEventListener('click', async () => {
      const n = items.find(i => i.id === b.dataset.toggle);
      b.disabled = true;
      try {
        Object.assign(n, await http.markNotification(token, n.id, !n.read));
        renderHistory();
        setUnread(await http.unreadCount(token));
      } catch (err) {
        fail(err, list);
      }
    }));
    $('notifications-more')?.addEventListener('click', () => loadHistory(true));
  }

  async function loadHistory(more = false) {
    const list = $('notification-list');
    if (!list || !token) return;
    try {
      const page = await http.listNotifications(token, {limit: PAGE, before: more ? nextBefore : null});
      if (!list.isConnected) return;
      items = more ? [...items, ...page.items] : page.items;
      nextBefore = page.nextBefore;
      setUnread(page.unreadCount);
      renderHistory();
    } catch (err) {
      fail(err, list);
    }
  }

  $('mark-all-read').addEventListener('click', async e => {
    e.currentTarget.disabled = true;
    try { await http.markAllNotificationsRead(token); loadHistory(); } catch (err) { fail(err, $('notification-list')); }
  });

  // ----- devices -----
  async function loadDevices() {
    const list = $('device-list');
    if (!list || !token) return;
    try {
      const devices = await http.listPushDevices(token);
      if (!list.isConnected) return;
      list.innerHTML = devices.length
        ? `<ul class="plain-list">${devices.map(d => `<li class="row-actions"><span>${icon('bell', 'mini-icon')} ${esc(d.label || 'Browser')}</span><span class="micro">added ${fmtDateTime(d.createdAt)}${d.lastSuccessAt ? ` · last delivered ${fmtDateTime(d.lastSuccessAt)}` : ''}${d.failureCount ? ` · ${d.failureCount} failed deliveries` : ''}</span><span class="spacer"></span>${btn('<span>Remove</span>', `data-remove-device="${esc(d.subscriptionId)}"`, 'ghost')}</li>`).join('')}</ul>`
        : '<p class="micro">No devices. Turn notifications on above to add this one.</p>';
      list.querySelectorAll('[data-remove-device]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await http.removePushDevice(token, b.dataset.removeDevice); } catch (err) { if (err?.status !== 404) return fail(err, list); }
        loadDevices(); renderPush();
      }));
    } catch (err) {
      fail(err, list);
    }
  }

  // New notifications arrive through the service worker; refresh the history when it says so.
  const refresh = () => { loadHistory(); loadDevices(); };
  window.addEventListener('civiclens:notifications-changed', refresh);
  addCleanup(() => window.removeEventListener('civiclens:notifications-changed', refresh));

  renderPush();
  if (citizen) renderFollows();
  loadHistory();
  loadDevices();
}
