// Camera-first reporting: capture → review → category → confirm location → submit → receipt.
import {api, categories, mediaDB, stampedKey, inspectGalleryFile, inServiceArea, MAX_VIDEO_BYTES, esc, StorageError} from '../services.js';
import {icon, categoryIcon, coords, fmtDateTime, fmtTime, btn, toast, heading, notice, demoBadge, publicTopbar, openDialog} from '../ui.js';
import {locationSourceLabel} from '../presentation.js';
import {state, viewer, addCleanup, MAX_RECORDING_SECONDS} from '../state.js';
import {navigate, currentPath, previousRoute} from '../router.js';
import {addressResolver} from './geo.js';
import {distanceM, ADDR} from '../address-service.js';
import {renderStampedPhoto, stampFileName, evidenceRef} from '../photo-stamp.js';
import {savePhoto, autoSavePhoto, preferredMethod, saveLabel, readPhotoPrefs, writePhotoPrefs} from '../photo-save.js';
import {LOGO_SRC, loadLogoImage} from '../brand.js';
import {camera} from '../camera-service.js';
import {locator, fixQuality, fixAgeMs, GEO} from '../location-service.js';
import {submitReport as saveReport, takeServerReceipt, reportMode, serverEvidenceKey, uploadEvidence, uploadState, forgetUpload, notifyAboutReport} from '../report-gateway.js';
import {ApiError, newIdempotencyKey} from '../api-client.js';

const cam = state.camera; // recorder, timer, seconds, mode, holdRecording
const $ = id => document.getElementById(id);

// Stamped photos made at submission (by complaint ID) and their automatic-save jobs, so the
// receipt can show the outcome without re-rendering or saving twice.
const stampedPhotos = new Map();
const saveJobs = new Map();
const nearPoint = (a, b) => distanceM(a, b) <= ADDR.REUSE_RADIUS_M;

export function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

const extFor = type => type?.includes('mp4') ? 'mp4' : type?.includes('webm') ? 'webm' : type?.includes('jpeg') ? 'jpg' : 'bin';

const SECURE_HELP = 'Browsers allow the camera and location only on HTTPS pages or on localhost. On this computer, open http://localhost:4173. A phone opening a plain http:// address on your network (for example http://192.168.1.20:4173) is not a secure page, so the browser blocks camera and location there. Use an HTTPS address for phone testing.';

function ago(ms) {
  if (!Number.isFinite(ms)) return '';
  const s = Math.round(ms / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : 'over an hour ago';
}

// ---------- Camera states ----------
const retryCamera = (label = 'Retry camera') => btn(icon('refresh') + `<span>${label}</span>`, 'data-action="enable-camera"', 'primary');
const galleryButton = () => btn(icon('upload') + '<span>Upload a geo-tagged JPEG</span>', 'data-action="gallery"', 'secondary');

function cameraCopy(st) {
  const switchBtn = st.canSwitch ? btn(icon('flip') + '<span>Try another camera</span>', 'data-action="switch-camera"', 'secondary') : '';
  return {
    idle: {ic: 'camera', tone: 'wait', chip: 'Camera off', title: 'The camera is off', body: 'Turn it on to take a geo-tagged photo or a short video.', actions: retryCamera('Turn on camera') + galleryButton()},
    starting: {ic: 'camera', tone: 'wait', chip: 'Starting…', busy: true, title: 'Starting the camera…', body: 'If your browser asks, choose Allow. Nothing is recorded until you press the shutter.'},
    attaching: {ic: 'camera', tone: 'wait', chip: 'Connecting…', busy: true, title: 'Connecting to the camera…', body: 'The preview appears as soon as the first frame arrives.'},
    'needs-tap': {ic: 'play', tone: 'wait', chip: 'Tap to start', title: 'Tap to show the preview', body: 'Your browser paused the camera preview until you interact with the page.', actions: btn(icon('play') + '<span>Show camera preview</span>', 'data-action="resume-preview"', 'primary')},
    'no-frames': {ic: 'alert', tone: 'error', chip: 'No picture', title: 'The camera started but sent no picture', body: 'Another app may be holding the camera, or the lens may be covered. Close other camera apps, then retry.', actions: retryCamera() + switchBtn},
    denied: {ic: 'lock', tone: 'error', chip: 'Blocked', title: 'Camera access is blocked', body: 'Open your browser’s site settings (the icon beside the web address), set Camera to Allow for this site, then choose Retry camera. You can still upload a geo-tagged JPEG.', actions: retryCamera() + galleryButton()},
    missing: {ic: 'alert', tone: 'error', chip: 'No camera', title: 'No camera found', body: 'This device does not report a usable camera. Connect one and retry, or upload a geo-tagged JPEG.', actions: retryCamera() + galleryButton()},
    busy: {ic: 'alert', tone: 'error', chip: 'In use', title: 'The camera is in use', body: 'Another app or browser tab is using the camera. Close it, then choose Retry camera.', actions: retryCamera() + switchBtn + galleryButton()},
    ended: {ic: 'alert', tone: 'error', chip: 'Disconnected', title: 'The camera disconnected', body: 'The camera stopped sending video. Reconnect it or choose Retry camera.', actions: retryCamera() + galleryButton()},
    insecure: {ic: 'lock', tone: 'error', chip: 'Needs HTTPS', title: 'The camera needs a secure page', body: SECURE_HELP, actions: galleryButton()},
    unsupported: {ic: 'alert', tone: 'error', chip: 'Not supported', title: 'This browser cannot open the camera', body: 'Use a current version of Chrome, Edge, Firefox or Safari, or upload a geo-tagged JPEG.', actions: galleryButton()},
    error: {ic: 'alert', tone: 'error', chip: 'Error', title: 'The camera could not start', body: `${st.detail ? `The browser reported: ${st.detail}. ` : ''}Retry, try another camera, or upload a geo-tagged JPEG.`, actions: retryCamera() + switchBtn + galleryButton()},
    live: {chip: 'Live', tone: 'ok'}
  }[st.status] || {ic: 'alert', tone: 'error', chip: 'Error', title: 'The camera could not start', body: 'Retry the camera.', actions: retryCamera()};
}

function cameraStateHTML(st) {
  const c = cameraCopy(st);
  return `<div class="camera-state ${c.busy ? 'is-busy' : ''}"><span class="camera-state-icon">${c.busy ? '<span class="spinner" aria-hidden="true"></span>' : icon(c.ic)}</span><h2>${esc(c.title)}</h2><p>${esc(c.body)}</p>${c.actions ? `<div class="camera-state-actions">${c.actions}</div>` : ''}</div>`;
}

// ---------- Location presentation ----------
function locationView(st, now = Date.now()) {
  const fix = st.fix;
  const age = fixAgeMs(fix, now);
  const stale = !!fix && age > GEO.MAX_FIX_AGE_MS;
  const quality = fixQuality(fix);
  const acc = fix && Number.isFinite(fix.accuracy) ? `±${Math.round(fix.accuracy)} m` : '';
  const base = {fix, stale, quality, acc};
  switch (st.status) {
    case 'insecure': return {...base, tone: 'error', badge: 'Needs HTTPS', short: 'Location needs HTTPS', message: SECURE_HELP};
    case 'unsupported': return {...base, tone: 'error', badge: 'Not supported', short: 'Location not supported', message: 'This browser cannot read device location. Upload a gallery JPEG that already contains GPS metadata instead.'};
    case 'denied': return {...base, tone: 'error', badge: 'Blocked', short: 'Location blocked', message: 'Location permission is blocked for this site. Open the browser’s site settings (the icon beside the web address), set Location to Allow, then choose Retry location. On a phone, also check that location is on for the browser app.'};
    case 'unavailable': return {...base, tone: 'error', badge: 'Unavailable', short: 'Location unavailable', message: 'Your device could not work out a location. Turn on location services (GPS) for the device and the browser, then choose Retry location.'};
    case 'timeout': return {...base, tone: 'error', badge: 'Timed out', short: 'Location timed out', message: `No location arrived within ${GEO.FIRST_FIX_TIMEOUT_MS / 1000} seconds. Move near a window or outdoors, then choose Retry location.${fix ? ' The last fix shown is too old for a new capture.' : ''}`};
    case 'locating':
      if (!fix) return {...base, tone: 'wait', badge: 'Locating…', short: 'Finding your location…', message: 'Finding your location. Outdoors this usually takes a few seconds; indoors it can take up to 20 seconds.'};
      return {...base, tone: 'wait', badge: 'Updating…', short: `${coords(fix, 5)} · ${acc} · updating`, message: 'Getting a fresh location fix. The previous fix stays visible until a new one arrives.'};
    case 'acquired': {
      if (stale) return {...base, tone: 'warn', badge: 'Too old', short: `${coords(fix, 5)} · fix ${ago(age)}`, message: `This fix was taken ${ago(age)}. A new capture needs a fix from the last ${GEO.MAX_FIX_AGE_MS / 60000} minutes. Choose Retry location.`};
      if (st.refining) return {...base, tone: 'wait', badge: 'Improving accuracy', short: `${coords(fix, 5)} · ${acc} · improving`, message: `Location acquired (${acc}). Trying for a more precise fix for up to ${GEO.REFINE_WINDOW_MS / 1000} seconds. You can capture now.`};
      if (quality === 'approximate') return {...base, tone: 'warn', badge: 'Approximate', short: `${coords(fix, 5)} · ${acc}`, message: `This fix is approximate (${acc}), probably from Wi-Fi or mobile network rather than satellite GPS. Move outdoors and choose Retry location for a better fix. The accuracy is recorded with your report either way.`};
      if (quality === 'poor') return {...base, tone: 'warn', badge: 'Low accuracy', short: `${coords(fix, 5)} · ${acc}`, message: `Location acquired with low accuracy (${acc}). Move to open sky and choose Retry location for a better fix, or capture now; the accuracy is recorded.`};
      return {...base, tone: 'ok', badge: 'Location acquired', short: `${coords(fix, 5)} · ${acc}`, message: 'Location acquired. It will be attached to your next capture.'};
    }
    default: return {...base, tone: 'wait', badge: 'Not requested', short: 'Location not requested yet', message: 'Location is required for live captures. Choose Retry location to request it.'};
  }
}

function locationDL(fix, now = Date.now()) {
  if (!fix) return `<div><dt>Latitude</dt><dd>—</dd></div><div><dt>Longitude</dt><dd>—</dd></div><div><dt>Accuracy</dt><dd>—</dd></div><div><dt>Fix obtained</dt><dd>—</dd></div><div class="span-2"><dt>Source</dt><dd>No location yet</dd></div><div class="span-2"><dt>Street address</dt><dd id="live-address">Not looked up (optional)</dd></div>`;
  const quality = fixQuality(fix);
  const flag = quality === 'approximate' ? ' <span class="pill pill-coral">Approximate</span>' : quality === 'poor' ? ' <span class="pill pill-coral">Low accuracy</span>' : '';
  return `<div><dt>Latitude</dt><dd>${fix.lat.toFixed(6)}</dd></div><div><dt>Longitude</dt><dd>${fix.lng.toFixed(6)}</dd></div>
  <div><dt>Accuracy</dt><dd>${Number.isFinite(fix.accuracy) ? `±${Math.round(fix.accuracy)} m` : 'Not reported'}${flag}</dd></div>
  <div><dt>Fix obtained</dt><dd>${fmtTime(new Date(fix.timestamp).toISOString())} <span class="muted">· ${ago(fixAgeMs(fix, now))}</span></dd></div>
  <div class="span-2"><dt>Source</dt><dd>${esc(locationSourceLabel(fix))}</dd></div>
  <div class="span-2"><dt>Street address</dt><dd id="live-address">${esc(addressResolver.addressFor(fix)?.address || 'Not looked up (optional)')}</dd></div>`;
}

/** Three-word summary for the location card: Detecting, Located or Needs attention. */
function locationPhase(st, view) {
  if (view.fix && !view.stale && (st.status === 'acquired' || st.status === 'locating')) {
    return view.quality === 'poor' || view.quality === 'approximate' ? {phase: 'attention', text: 'Located · low accuracy'} : {phase: 'located', text: 'Located'};
  }
  if (st.status === 'locating' || st.status === 'idle') return {phase: 'detecting', text: 'Detecting'};
  return {phase: 'attention', text: 'Needs attention'};
}

/** What the address card says for the current fix and resolver state. */
function addressLine(st, fix) {
  if (!fix) return {state: 'idle', text: 'Appears automatically once GPS finds you', status: ''};
  const known = addressResolver.addressFor(fix);
  if (known) return {state: 'resolved', text: known.address, short: known.short || known.address, status: 'Approximate: the nearest mapped address to your GPS fix. The coordinates remain the evidence.'};
  const forThisFix = st.point && nearPoint(st.point, fix);
  if (!forThisFix || st.status === 'resolving' || st.status === 'idle') return {state: 'resolving', text: 'Finding the nearest address…', status: 'Looking up the nearest mapped address…'};
  if (st.status === 'offline') return {state: 'offline', text: 'Address unavailable offline', status: st.message};
  return {state: 'failed', text: 'Address not available', status: st.message};
}

// ---------- Capture screen ----------
export function renderCapture(root) {
  cam.mode ??= 'photo';
  const hasPrevious = !!previousRoute() && previousRoute() !== 'camera';
  root.innerHTML = `<div class="capture-page">
    ${publicTopbar({back: hasPrevious, fallback: 'home', right: `${demoBadge()}<a class="close-button" href="#/home" aria-label="Close camera and choose where to go next">${icon('x')}</a>`})}
    <main id="main" class="capture-main" tabindex="-1">
      <h1 class="sr-only" id="page-title" tabindex="-1">Report an issue with the camera</h1>
      <div class="capture-grid">
        <section class="capture-stage" aria-label="Camera and capture controls">
          <div class="viewfinder" id="viewfinder">
            <video id="live" autoplay playsinline muted disablepictureinpicture aria-label="Live camera preview"></video>
            <div class="viewfinder-top">
              <span class="chip chip-brand"><img class="chip-logo" src="${LOGO_SRC}" alt="" width="18" height="18" decoding="async"><span>Geo-tagged capture</span></span>
              <span class="chip chip-status" id="cam-chip" role="status" aria-live="polite"><span class="chip-dot" aria-hidden="true"></span><span id="cam-chip-text">Camera off</span></span>
            </div>
            <div id="cam-state" class="viewfinder-state"></div>
            <div class="viewfinder-bottom">
              <div class="vf-card">
                <img class="vf-logo" src="${LOGO_SRC}" alt="" width="30" height="30" decoding="async">
                <div class="vf-lines">
                  <span id="vf-location" class="vf-location"><span class="chip-dot" aria-hidden="true"></span>${icon('locate')}<span id="vf-location-text">Location not requested yet</span></span>
                  <span id="vf-address" class="vf-address" data-state="idle" hidden></span>
                </div>
              </div>
              <time id="vf-clock">${fmtDateTime(new Date().toISOString())}</time>
            </div>
          </div>
          <div class="capture-controls">
            <div class="mode-switch" role="group" aria-label="Capture mode">
              <button type="button" data-mode="photo" aria-pressed="${cam.mode === 'photo'}">${icon('image')}<span>Photo</span></button>
              <button type="button" data-mode="video" aria-pressed="${cam.mode === 'video'}">${icon('video')}<span>Video (30 s)</span></button>
            </div>
            <div class="shutter-row">
              <div class="control-stack"><button type="button" class="round-btn" id="switch-btn" data-action="switch-camera" aria-label="Switch camera">${icon('flip')}</button><span class="control-label" aria-hidden="true">Switch</span></div>
              <button type="button" class="shutter is-waiting" id="shutter" aria-label="Capture photo" aria-describedby="capture-ready" aria-disabled="true">
                <span class="shutter-ring"><span class="shutter-core"></span></span>
              </button>
              <div class="control-stack"><button type="button" class="round-btn" data-action="refresh-location" aria-label="Retry location">${icon('locate')}</button><span class="control-label" aria-hidden="true">Location</span></div>
            </div>
            <p class="capture-ready" id="capture-ready" role="status" aria-live="polite">Waiting for the camera…</p>
            <p class="control-hint" id="shutter-hint">Press to capture. Hold the shutter to record video, up to 30 seconds.</p>
            <div class="recording-bar hidden" id="recording-bar">
              <span class="rec-dot" aria-hidden="true"></span><span id="rec-time">0:00 / 0:30</span>
              <button type="button" class="btn btn-primary" id="stop-recording" data-action="stop-recording">${icon('pause')}<span>Stop recording</span></button>
            </div>
            <div class="gallery-row">
              <button type="button" class="btn btn-secondary btn-block" data-action="gallery">${icon('upload')}<span>Upload a geo-tagged JPEG from gallery</span></button>
              <p class="micro">Gallery photos must contain their own GPS metadata. Your current location is never used for an uploaded photo.</p>
              <input id="gallery-input" type="file" accept="image/jpeg,.jpg,.jpeg" hidden>
            </div>
          </div>
        </section>
        <aside class="capture-side" aria-label="Location evidence and guidance">
          ${window.isSecureContext === false ? notice(`<strong>Not a secure page.</strong> ${esc(SECURE_HELP)}`, 'warn', 'lock') : ''}
          <section class="card location-card" aria-labelledby="loc-title">
            <div class="card-head"><h2 id="loc-title">Location evidence</h2><span id="loc-badge" class="pill">Not requested</span></div>
            <p class="loc-phase" id="loc-phase" data-phase="detecting"><span class="phase-dot" aria-hidden="true"></span><span id="loc-phase-text">Detecting</span></p>
            <p class="loc-status" id="loc-status" role="status" aria-live="polite">Location is required for live captures.</p>
            <dl class="evidence-dl" id="loc-dl">${locationDL(null)}</dl>
            <div class="row-actions"><button type="button" class="btn btn-secondary" id="loc-retry" data-action="refresh-location">${icon('locate')}<span>Retry location</span></button></div>
            <div class="address-card" id="address-card" data-state="idle">
              <div class="address-card-main">
                <span class="address-icon" aria-hidden="true"><span class="spinner spinner-sm"></span>${icon('pin')}</span>
                <div class="address-card-text">
                  <p class="address-kicker">Nearest address · approximate</p>
                  <p class="address-text" id="address-text">Appears automatically once GPS finds you</p>
                </div>
              </div>
              <p class="micro address-status" id="address-status" role="status" aria-live="polite"></p>
              <button type="button" class="btn btn-secondary" data-action="lookup-live" disabled>${icon('refresh')}<span>Retry address</span></button>
              <p class="micro">The address is looked up automatically by sending your coordinates to Photon (OpenStreetMap data). If the lookup fails, your GPS coordinates are still saved.</p>
            </div>
            <div class="address-box">
              <p class="micro"><strong>Optional street address.</strong> Looking up an address sends these coordinates to Photon (OpenStreetMap data). Your GPS coordinates are kept even if the lookup fails.</p>
              <button type="button" class="btn btn-ghost btn-block" data-action="lookup-street" disabled>${icon('search')}<span>Look up street address</span></button>
              <p class="micro address-status" id="street-status" role="status" aria-live="polite"></p>
            </div>
          </section>
          <section class="card" aria-labelledby="tips-title">
            <h2 id="tips-title">A clear photo helps</h2>
            <ol class="tips">
              <li><strong>Frame the problem.</strong> Keep the issue visible with a little of the street around it.</li>
              <li><strong>Stay safe.</strong> Keep away from traffic, open drains and unstable edges.</li>
              <li><strong>Confirm next.</strong> You will check the details and get a tracking ID.</li>
            </ol>
          </section>
          <section class="card card-quiet">
            <p class="micro" id="storage-note">No account is needed to report. Reports are saved on this device in this prototype and are not sent to any authority.</p>
            <a class="inline-link" href="#/home">Browse public options ${icon('forward')}</a>
          </section>
        </aside>
      </div>
    </main>
  </div>`;

  root.querySelector('#gallery-input').onchange = e => galleryUpload(e.target);
  setupShutterGestures(root.querySelector('#shutter'));
  setModeUI();

  lastCameraKey = '';
  const offCamera = camera.subscribe(updateCameraUI);
  const offLocation = locator.subscribe(updateLocationUI);
  const offAddress = addressResolver.subscribe(() => updateAddressUI());
  updateCameraUI(camera.state);
  updateLocationUI(locator.state);

  // Keep relative times ("fix 40 s ago") and the stale-fix rule current.
  const ticker = setInterval(() => {
    const clock = $('vf-clock');
    if (!clock) return clearInterval(ticker);
    clock.textContent = fmtDateTime(new Date().toISOString());
    updateLocationUI(locator.state);
  }, 5000);

  // Phones reclaim the camera in the background; release it and reopen on return.
  const onVisibility = () => {
    if (document.hidden) {
      if (cam.recorder?.state === 'recording') stopRecording();
      camera.stop();
      locator.stop();
    } else if (currentPath() === 'camera') {
      openCamera({auto: true});
      ensureLocation();
    }
  };
  document.addEventListener('visibilitychange', onVisibility);
  const stopPermissionWatch = watchPermissions();
  reportMode().then(mode => {
    const note = $('storage-note');
    if (!note || mode === 'local') return;
    note.textContent = mode === 'backend'
      ? 'No account is needed to report. Server mode: the report details and then the photo or video are sent to the CivicLens development server. Evidence is stored privately and never shown in public tracking.'
      : 'Reporting is unavailable: the CivicLens API configuration (api-config.json) is invalid.';
  });

  addCleanup(() => {
    clearInterval(ticker);
    document.removeEventListener('visibilitychange', onVisibility);
    stopPermissionWatch();
    offCamera();
    offLocation();
    offAddress();
    addressResolver.pause(); // drop a pending debounce; an in-flight lookup may still finish for review
    stopCameraCompletely();
    locator.stop();
  });

  openCamera({auto: true});
  ensureLocation();
}

let lastCameraKey = '';

/** Start (or reuse) the stream, then attach it to the video element that is mounted now. */
async function openCamera(opts) {
  const stream = await camera.start(opts);
  if (!stream) return;
  const video = $('live');
  if (!video) return;
  if (camera.state.live && video.srcObject === stream && !video.paused) return;
  await camera.attach(video);
}

function ensureLocation() {
  const st = locator.state;
  if (st.active) return;
  const fix = locator.usableFix();
  if (fix && fixQuality(fix) === 'good') return updateLocationUI(locator.state);
  locator.start({auto: true});
}

/** If the user allows access in site settings while the screen is open, recover without a reload. */
function watchPermissions() {
  let disposed = false;
  const offs = [];
  const watch = name => navigator.permissions?.query?.({name}).then(status => {
    if (disposed) return;
    const onChange = () => {
      if (status.state !== 'granted' || currentPath() !== 'camera') return;
      if (name === 'camera' && !camera.stream) { camera.forgetDenial(); openCamera({auto: false}); }
      if (name === 'geolocation' && !locator.usableFix()) { locator.forgetDenial(); locator.start(); }
    };
    status.addEventListener?.('change', onChange);
    offs.push(() => status.removeEventListener?.('change', onChange));
  }).catch(() => { /* not every browser exposes these permission names */ });
  watch('camera');
  watch('geolocation');
  return () => { disposed = true; offs.forEach(off => off()); };
}

function updateCameraUI(st) {
  const overlay = $('cam-state');
  if (!overlay) return;
  const c = cameraCopy(st);
  const key = `${st.status}|${st.detail}|${st.canSwitch}`;
  if (key !== lastCameraKey) {
    lastCameraKey = key;
    if (st.status === 'live') {
      overlay.hidden = true;
      overlay.innerHTML = '';
    } else {
      overlay.hidden = false;
      overlay.innerHTML = cameraStateHTML(st);
    }
  }
  const video = $('live');
  if (video) {
    video.classList.toggle('is-live', st.status === 'live');
    video.classList.toggle('is-mirrored', st.facing === 'user');
  }
  if (cam.recorder?.state !== 'recording') setChip(c.chip, c.tone);
  const sw = $('switch-btn');
  if (sw) {
    // Switching only makes sense with a working camera; failure screens offer their own actions.
    sw.disabled = !st.canSwitch || st.status !== 'live';
    sw.setAttribute('aria-label', st.cameraCount > 1 ? `Switch camera (${st.cameraCount} cameras found)` : st.canSwitch ? 'Switch between front and back camera' : 'Only one camera found');
    sw.title = st.canSwitch ? 'Switch camera' : 'Only one camera found on this device';
  }
  updateReadiness();
}

function updateLocationUI(st) {
  const view = locationView(st);
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  set('vf-location-text', view.short);
  $('vf-location')?.setAttribute('data-tone', view.tone);
  set('loc-status', view.message);
  const badge = $('loc-badge');
  if (badge) {
    badge.textContent = view.badge;
    badge.className = `pill ${view.tone === 'ok' ? 'pill-navy' : view.tone === 'wait' ? '' : 'pill-coral'}`;
  }
  const phase = locationPhase(st, view);
  const phaseEl = $('loc-phase');
  if (phaseEl) phaseEl.dataset.phase = phase.phase;
  set('loc-phase-text', phase.text);
  const dl = $('loc-dl');
  if (dl) dl.innerHTML = locationDL(view.fix);
  // Resolve the address as soon as a current fix exists. The resolver debounces, reuses nearby
  // results and ignores repeats, so calling it on every update costs no extra requests.
  const usable = locator.usableFix();
  if (usable && (st.status === 'acquired' || st.status === 'locating')) addressResolver.resolve(usable);
  updateAddressUI();
  const retry = $('loc-retry');
  if (retry) {
    const locating = st.status === 'locating';
    retry.disabled = locating;
    retry.querySelector('span').textContent = locating ? 'Locating…' : view.fix && !view.stale && st.status === 'acquired' ? 'Update location' : 'Retry location';
  }
  updateReadiness();
}

function setChip(text, tone = '') {
  const chip = $('cam-chip');
  if (!chip) return;
  chip.className = `chip chip-status chip-${tone || 'wait'}`;
  const label = $('cam-chip-text');
  if (label) label.textContent = text;
}

/** One sentence under the shutter saying exactly what capture is waiting for. */
function updateReadiness() {
  if (capturing) return;
  const shutter = $('shutter');
  const out = $('capture-ready');
  if (!shutter || !out) return;
  const st = camera.state;
  const fix = locator.usableFix();
  const loc = locator.state;
  let ready = false;
  let text;
  if (cam.recorder?.state === 'recording') { ready = true; text = 'Recording. Tap the shutter or Stop recording to finish.'; }
  else if (!st.live) {
    text = ['starting', 'attaching', 'idle'].includes(st.status) ? 'Waiting for the camera preview…'
      : st.status === 'needs-tap' ? 'Tap “Show camera preview” to continue.'
      : 'Camera unavailable. Retry the camera above, or upload a geo-tagged JPEG.';
  } else if (!fix) {
    text = loc.status === 'locating' ? 'Camera ready. Waiting for a location fix…'
      : loc.status === 'denied' ? 'Camera ready. Location is blocked: allow it in site settings, then Retry location.'
      : 'Camera ready. A current location fix is needed: choose Retry location.';
  } else {
    ready = true;
    const q = fixQuality(fix);
    text = `Ready. Evidence will be tagged ${coords(fix, 5)} (±${Math.round(fix.accuracy ?? 0)} m, fix at ${fmtTime(new Date(fix.timestamp).toISOString())})${q === 'poor' || q === 'approximate' ? '. Accuracy is low.' : '.'}`;
  }
  out.textContent = text;
  out.dataset.ready = String(ready);
  shutter.classList.toggle('is-waiting', !ready);
  shutter.setAttribute('aria-disabled', String(!ready));
}

export function setCameraMode(mode) {
  if (cam.recorder?.state === 'recording') return toast('Stop recording before changing mode.');
  cam.mode = mode;
  setModeUI();
}

/** Actions reached through delegated data-action buttons on the camera and review screens. */
export const cameraActions = {
  enable() {
    camera.forgetDenial();
    return openCamera({auto: false});
  },
  resume: () => camera.resume(),
  switchCamera() {
    if (cam.recorder?.state === 'recording') return toast('Stop recording before switching camera.');
    const video = $('live');
    if (!video) return undefined;
    return camera.switchCamera(video).then(ok => {
      const st = camera.state;
      if (st.detail && st.status === 'live') toast(st.detail);
      else if (ok) toast(st.facing === 'user' ? 'Front camera on.' : 'Back camera on.');
    });
  },
  locate() {
    locator.forgetDenial();
    locator.start();
  },
  lookupLive: () => resolveLive(),
  stopRecording: () => stopRecording(true),
  openGallery: () => $('gallery-input')?.click(),
  retake() {
    state.capture = null;
    navigate('camera');
  },
  saveOriginal() {
    const c = state.capture;
    if (!c) return;
    const ext = c.type === 'video' ? (c.mimeType?.includes('mp4') ? 'mp4' : 'webm') : 'jpg';
    download(c.blob, `civiclens-original-${Date.now()}.${ext}`);
  }
};

function setModeUI() {
  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === cam.mode)));
  const shutter = $('shutter');
  if (shutter) shutter.setAttribute('aria-label', cam.mode === 'photo' ? 'Capture photo' : (cam.recorder?.state === 'recording' ? 'Stop recording' : 'Start video recording'));
  const hint = $('shutter-hint');
  if (hint) hint.textContent = cam.mode === 'photo'
    ? 'Press to capture. Hold the shutter to record video, up to 30 seconds.'
    : 'Tap to start recording, then tap again to stop. Recording stops automatically at 30 seconds.';
}

function stopCameraCompletely() {
  if (cam.recorder && cam.recorder.state !== 'inactive') {
    cam.recorder.onstop = null;
    try { cam.recorder.stop(); } catch { /* already stopped */ }
  }
  clearInterval(cam.timer);
  cam.recorder = null;
  cam.seconds = 0;
  camera.stop();
}

/** Manual fallback: look the address up again now, bypassing the cache and back-off. */
function resolveLive() {
  const target = locator.fix;
  if (!target) { toast('Get a location fix first. Address lookup needs coordinates.'); return locator.start(); }
  addressResolver.resolve(target, {force: true});
  return undefined;
}

function updateAddressUI() {
  const card = $('address-card');
  if (!card) return;
  const fix = locator.fix;
  const line = addressLine(addressResolver.state, fix);
  card.dataset.state = line.state;
  const set = (id, text) => { const el = $(id); if (el && el.textContent !== text) el.textContent = text; };
  set('address-text', line.text);
  set('address-status', line.status);
  const vf = $('vf-address');
  if (vf) {
    vf.hidden = !fix;
    vf.dataset.state = line.state;
    set('vf-address', line.state === 'resolved' ? line.short : line.state === 'resolving' ? 'Finding address…' : 'Address unavailable · GPS saved');
  }
  const live = $('live-address');
  if (live) live.textContent = line.state === 'resolved' ? line.text : fix ? (line.state === 'resolving' ? 'Looking up…' : 'Not looked up (optional)') : 'Not looked up (optional)';
  const street = document.querySelector('[data-action="lookup-street"]');
  if (street) street.disabled = !fix || line.state === 'resolving';
  set('street-status', line.state === 'resolved' ? 'Address found. GPS coordinates remain the primary evidence.' : line.state === 'failed' ? line.status : '');
  const button = document.querySelector('[data-action="lookup-live"]');
  if (button) {
    button.disabled = !fix || line.state === 'resolving';
    const label = button.querySelector('span');
    if (label) label.textContent = line.state === 'resolved' ? 'Refresh address' : 'Retry address';
  }
}

function explainNotReady() {
  const st = camera.state;
  if (!st.live) {
    toast(st.status === 'starting' || st.status === 'attaching' ? 'The camera is still starting.' : 'The camera is not available. Retry the camera or upload a geo-tagged JPEG.');
    return true;
  }
  if (!locator.usableFix()) {
    const loc = locator.state;
    if (loc.status === 'denied') toast('Location is blocked. Allow it for this site, then choose Retry location.');
    else if (loc.status === 'locating') toast('Still finding your location. Capture will be ready as soon as a fix arrives.');
    else { toast('A current location fix is needed for live evidence. Retrying location…'); locator.start(); }
    return true;
  }
  return false;
}

function setupShutterGestures(shutter) {
  let holdTimer = null;
  let holding = false;
  let suppressClick = false;
  shutter.addEventListener('pointerdown', e => {
    if (cam.mode !== 'photo' || e.button > 0 || !camera.state.live || !locator.usableFix()) return;
    holdTimer = setTimeout(() => {
      holding = true;
      cam.holdRecording = true;
      cam.mode = 'video';
      setModeUI();
      startRecording();
    }, 450);
  });
  const end = () => {
    clearTimeout(holdTimer);
    if (holding) {
      holding = false;
      suppressClick = true;
      stopRecording();
    }
  };
  shutter.addEventListener('pointerup', end);
  shutter.addEventListener('pointercancel', end);
  shutter.addEventListener('pointerleave', () => { if (!holding) clearTimeout(holdTimer); });
  shutter.addEventListener('click', () => {
    if (suppressClick) { suppressClick = false; return; }
    handleShutter();
  });
}

async function handleShutter() {
  if (cam.recorder?.state === 'recording') return stopRecording();
  if (explainNotReady()) return undefined;
  if (cam.mode === 'photo') return capturePhoto();
  return startRecording();
}

/** Copy of the fix with the capture time, so the evidence carries the fix it was taken with. */
function evidenceLocation(fix, capturedAt) {
  return {...fix, fixAgeSeconds: Math.max(0, Math.round((capturedAt - fix.timestamp) / 1000))};
}

const withTimeout = (promise, ms) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))]);

/** The preview frame as a JPEG, at the stream's resolution. */
async function grabFrame(video) {
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
  canvas.width = canvas.height = 0;
  return blob;
}

/**
 * A full-sensor still where the browser offers ImageCapture (Chrome on Android and desktop).
 * Returns null when unsupported, slow, or not sharper than the preview frame, so the caller can
 * fall back to the frame without delaying the user for long.
 */
async function takeStill(stream, video) {
  const track = stream?.getVideoTracks?.()[0];
  if (!track || track.readyState !== 'live' || typeof globalThis.ImageCapture !== 'function') return null;
  try {
    const capture = new ImageCapture(track);
    const caps = await withTimeout(capture.getPhotoCapabilities(), 1200).catch(() => null);
    const settings = caps?.imageWidth?.max && caps?.imageHeight?.max ? {imageWidth: caps.imageWidth.max, imageHeight: caps.imageHeight.max} : undefined;
    let blob = await withTimeout(capture.takePhoto(settings), 3500).catch(() => null);
    if (!blob && settings) blob = await withTimeout(capture.takePhoto(), 2500).catch(() => null);
    if (!blob?.size) return null;
    const bitmap = await createImageBitmap(blob);
    const sharper = bitmap.width * bitmap.height > video.videoWidth * video.videoHeight;
    if (!sharper) { bitmap.close?.(); return null; }
    if (blob.type === 'image/jpeg') { bitmap.close?.(); return blob; }
    // Some cameras return PNG; store evidence consistently as JPEG.
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0);
    bitmap.close?.();
    const jpeg = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    canvas.width = canvas.height = 0;
    return jpeg;
  } catch {
    return null;
  }
}

let capturing = false;

function setCapturingUI(on) {
  const shutter = $('shutter');
  if (shutter) {
    shutter.classList.toggle('is-capturing', on);
    shutter.setAttribute('aria-busy', String(on));
  }
  const out = $('capture-ready');
  if (on && out) out.textContent = 'Capturing the photo…';
  else updateReadiness();
}

async function capturePhoto() {
  if (capturing) return undefined; // a double tap must not produce two captures
  const video = $('live');
  const fix = locator.usableFix();
  if (!video || !fix || !camera.state.live || video.readyState < 2 || !video.videoWidth) {
    return toast('The camera frame is not ready yet. Wait for the live preview and try again.');
  }
  capturing = true;
  setCapturingUI(true);
  try {
    const capturedAt = Date.now();
    // The fix is fixed at the moment of the press, so a later GPS update cannot be stamped instead.
    const gps = evidenceLocation(fix, capturedAt);
    const blob = (await takeStill(camera.stream, video)) || (await grabFrame(video));
    if (!blob) return toast('The photo could not be captured. Try again.');
    const known = addressResolver.addressFor(gps);
    if (known) Object.assign(gps, {address: known.address, neighbourhood: known.neighbourhood, short: known.short});
    state.capture = {blob, type: 'photo', mimeType: 'image/jpeg', gps, capturedAt: new Date(capturedAt).toISOString(), captureSource: 'Live camera', duration: null};
    return navigate('review');
  } finally {
    capturing = false;
    setCapturingUI(false);
  }
}

function startRecording() {
  const stream = camera.stream;
  if (explainNotReady() || !stream) return undefined;
  if (!window.MediaRecorder) return toast('Video recording is not supported in this browser. Use photo mode.');
  const fix = locator.usableFix();
  const preferred = ['video/webm;codecs=vp9', 'video/webm', 'video/mp4'];
  const mimeType = preferred.find(t => MediaRecorder.isTypeSupported?.(t));
  let recorder;
  try {
    recorder = new MediaRecorder(stream, mimeType ? {mimeType} : undefined);
  } catch (err) {
    return toast('Recording could not start: ' + (err.message || 'unsupported format.'));
  }
  const chunks = [];
  const startedAt = Date.now();
  recorder.ondataavailable = e => { if (e.data?.size) chunks.push(e.data); };
  recorder.onerror = () => { toast('Recording stopped unexpectedly. Try again.'); stopRecording(true); };
  recorder.onstop = () => {
    clearInterval(cam.timer);
    cam.timer = null;
    const type = recorder.mimeType || mimeType || 'video/webm';
    const blob = new Blob(chunks, {type});
    cam.recorder = null;
    setRecordingUI(false);
    if (cam.holdRecording) cam.mode = 'photo';
    cam.holdRecording = false;
    if (!blob.size) return toast('No video was recorded. Try again.');
    if (blob.size > MAX_VIDEO_BYTES) return toast('This clip is too large (over 50 MB). Record a shorter clip.');
    state.capture = {blob, type: 'video', mimeType: type, gps: evidenceLocation(fix, startedAt), capturedAt: new Date(startedAt).toISOString(), captureSource: 'Live camera', duration: Math.round((Date.now() - startedAt) / 1000)};
    return navigate('review');
  };
  cam.recorder = recorder;
  cam.seconds = 0;
  recorder.start(1000);
  setRecordingUI(true);
  cam.timer = setInterval(() => {
    cam.seconds += 1;
    setRecordingUI(true);
    if (cam.seconds >= MAX_RECORDING_SECONDS) stopRecording();
  }, 1000);
  return undefined;
}

function setRecordingUI(recording) {
  const bar = $('recording-bar');
  if (bar) bar.classList.toggle('hidden', !recording);
  const time = $('rec-time');
  if (time) time.textContent = `${Math.floor(cam.seconds / 60)}:${String(cam.seconds % 60).padStart(2, '0')} / 0:30`;
  if (recording) setChip(`Recording ${cam.seconds}s of 30s`, 'recording');
  else { const c = cameraCopy(camera.state); setChip(c.chip, c.tone); }
  $('shutter')?.classList.toggle('recording', recording);
  setModeUI();
  updateReadiness();
}

function stopRecording(force = false) {
  if (cam.recorder?.state === 'recording') {
    cam.recorder.stop();
  } else if (force) {
    clearInterval(cam.timer);
    setRecordingUI(false);
  }
}

async function galleryUpload(input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  toast('Checking the photo’s location metadata…');
  let result;
  try {
    result = await inspectGalleryFile(file);
  } catch {
    result = {ok: false, message: 'The file could not be read. Choose a different JPEG.'};
  }
  if (!result.ok) {
    openDialog({
      title: 'This photo cannot be geo-tagged',
      size: 'sm',
      body: `<p class="dialog-message">${esc(result.message)}</p><p class="dialog-message micro">Your phone’s current location is not used instead, because it may not be where the photo was taken. Use the live camera, or choose a photo saved with location turned on.</p>`,
      actions: btn('Choose another photo', 'data-dialog-close', 'primary')
    });
    return;
  }
  if (result.serviceArea === false) toast('This photo’s location is outside the Bengaluru service area. Check it before submitting.');
  state.capture = {
    blob: file, type: 'photo', mimeType: file.type || 'image/jpeg',
    gps: {...result.gps, accuracy: null, source: 'Submitted EXIF metadata', sourceType: 'exif', timestamp: null},
    capturedAt: result.captureTime || null,
    captureSource: result.captureTime ? 'Gallery photo · EXIF capture time' : 'Gallery photo · file date (no EXIF capture time)',
    uploadedAt: new Date().toISOString(),
    duration: null
  };
  navigate('review');
}

// ---------- Stamped photo helpers ----------
/** Stamp details for a capture. The claim code is never part of this object. */
function stampMeta(c, complaintId, includeLocation) {
  return {complaintId, evidenceRef: c.evidenceRef || null, includeLocation, gps: c.gps, address: c.gps?.address || null, capturedAt: c.capturedAt};
}

async function makeStamped(blob, meta, opts = {}) {
  return renderStampedPhoto(blob, meta, {logo: await loadLogoImage(), ...opts});
}

// ---------- Review ----------
export function renderReview(root) {
  const c = state.capture;
  if (!c) return navigate('camera');
  const url = URL.createObjectURL(c.blob);
  addCleanup(() => URL.revokeObjectURL(url));
  const gps = c.gps;
  const isPhoto = c.type === 'photo';
  const prefs = readPhotoPrefs();
  const categoryButtons = categories.map(name => `<button type="button" class="category-option" data-category="${esc(name)}" aria-pressed="false">${categoryIcon(name)}<span>${esc(name)}</span></button>`).join('');
  root.innerHTML = `<div class="page page-review">
    ${publicTopbar({fallback: 'camera', right: demoBadge()})}
    <main id="main" class="review-main" tabindex="-1">
      ${heading({eyebrow: 'Step 2 of 3 · Review your evidence', title: 'Check the evidence, then describe the issue', lead: 'Your evidence stays on this device in this prototype. The original is kept unchanged; the saved copy carries the Namma CivicLens stamp with the complaint ID, time and, if you choose, the location.'})}
      <div class="review-grid">
        <section class="review-evidence" aria-labelledby="evidence-title">
          <h2 class="sr-only" id="evidence-title">Captured evidence</h2>
          <figure class="review-media ${isPhoto ? 'is-stamped' : ''}">${isPhoto
            ? `<img id="review-photo" src="${url}" alt="Captured civic issue with its evidence stamp preview">
               <div class="stamp-progress" id="stamp-progress" role="status" aria-live="polite"><span class="spinner spinner-sm" aria-hidden="true"></span><span>Adding the evidence stamp…</span></div>`
            : `<video src="${url}" controls playsinline aria-label="Captured video clip"></video>`}
            <figcaption class="media-geo ${gps ? '' : 'is-missing'}">${icon(gps ? 'pin' : 'alert', 'mini-icon')}<span>${gps ? `${coords(gps, 5)}${gps.accuracy ? ` · ±${Math.round(gps.accuracy)} m` : ''} · ${gps.sourceType === 'exif' ? 'from photo EXIF' : 'device GPS'}` : 'No location attached'}</span></figcaption></figure>
          ${isPhoto ? '<p class="micro stamp-note">Preview of the stamp burned into your saved photo. The complaint ID is filled in when you submit.</p>' : ''}
          <dl class="meta-list">
            <div><dt>Source</dt><dd>${esc(c.captureSource)}</dd></div>
            <div><dt>Captured</dt><dd>${c.capturedAt ? fmtDateTime(c.capturedAt) : 'Not recorded in the file'}</dd></div>
            ${c.uploadedAt ? `<div><dt>Uploaded to CivicLens</dt><dd>${fmtDateTime(c.uploadedAt)}</dd></div>` : ''}
            ${c.duration ? `<div><dt>Length</dt><dd>${c.duration} s (limit 30 s)</dd></div>` : ''}
          </dl>
          <div class="row-actions">
            ${btn(icon('camera') + '<span>Retake</span>', 'data-action="retake"', 'secondary')}
            ${btn(icon('download') + '<span>Save original to device</span>', 'data-action="save-original"', 'ghost')}
          </div>
        </section>
        <form class="review-form panel" id="review-form" novalidate>
          <p class="pill pill-navy">${icon('eye', 'pill-icon')}<span>Manual classification · no AI model is connected</span></p>
          <fieldset class="field">
            <legend>What needs attention? <span class="required">Required</span></legend>
            <div class="category-grid" id="category-grid">${categoryButtons}</div>
            <p class="field-error" id="category-error" role="alert" hidden></p>
          </fieldset>
          <div class="field">
            <label for="description">Anything else we should know? <span class="optional">Optional · up to 1,000 characters</span></label>
            <textarea id="description" name="description" rows="3" maxlength="1000" placeholder="For example: the pothole is near the bus stop and widens after rain."></textarea>
          </div>
          <div class="field" id="location-block">
            <p class="field-label">Evidence location</p>
            <div class="location-review card-quiet">
              <p class="location-coords">${icon('pin')}<span>${gps ? coords(gps) : 'No coordinates'}</span></p>
              ${gps ? `<dl class="loc-facts">
                <div><dt>Source</dt><dd>${esc(locationSourceLabel(gps))}</dd></div>
                <div><dt>Accuracy</dt><dd>${Number.isFinite(gps.accuracy) ? `±${Math.round(gps.accuracy)} m` : 'Not stated in the file'}</dd></div>
                ${gps.timestamp ? `<div><dt>Fix obtained</dt><dd>${fmtDateTime(new Date(gps.timestamp).toISOString())}${Number.isFinite(gps.fixAgeSeconds) ? ` · ${gps.fixAgeSeconds} s before capture` : ''}</dd></div>` : ''}
              </dl>` : notice('<strong>No location is attached to this evidence.</strong> Live reports need a location fix. Retake the photo once the camera screen shows “Location acquired”.', 'error', 'alert')}
              <p class="address-line"><span class="micro">Nearest address (approximate):</span> <span id="review-address">${gps?.address ? esc(gps.address) : gps ? 'Finding the nearest address…' : 'Not available'}</span></p>
              <button type="button" class="btn btn-secondary" data-action="lookup-review" ${gps ? '' : 'disabled'}>${icon('refresh')}<span>Look up again</span></button>
              <p class="address-line"><span class="micro">Street address:</span> <span id="review-street-address">${gps?.address ? esc(gps.address) : 'Not looked up'}</span></p>
              <button type="button" class="btn btn-secondary" data-action="lookup-review-street" ${gps ? '' : 'disabled'}>${icon('search')}<span>Look up street address</span></button>
              <p class="micro">Optional. Sends these coordinates to Photon (OpenStreetMap). If it fails, your coordinates are still saved.</p>
              <p class="micro">Looked up automatically from Photon (OpenStreetMap). If it fails, your coordinates are still saved.</p>
              ${gps && !inServiceArea(gps.lat, gps.lng) ? notice('These coordinates are outside the Bengaluru service area. Check them before you submit.', 'warn', 'alert') : ''}
            </div>
            <label class="check"><input type="checkbox" id="confirm-location" name="confirm"> <span>I confirm this is where the issue was observed.</span></label>
            <p class="field-error" id="confirm-error" role="alert" hidden></p>
          </div>
          ${isPhoto ? `<fieldset class="field stamp-options">
            <legend>Saved photo</legend>
            <label class="check"><input type="checkbox" id="stamp-location" ${prefs.stampLocation && gps ? 'checked' : ''} ${gps ? '' : 'disabled'}> <span><strong>Show the address and GPS coordinates on the saved photo.</strong> Anyone you send the photo to will see where it was taken. Your choice is remembered on this device.</span></label>
            ${c.captureSource === 'Live camera' ? `<label class="check"><input type="checkbox" id="auto-save" ${prefs.autoSave ? 'checked' : ''}> <span>Save the stamped photo to this device when I submit.</span></label>` : ''}
            <p class="micro">The stamp always shows the Namma CivicLens logo, the complaint ID, an evidence reference and the capture time in IST. Your private claim code is never printed on photos or used in file names.</p>
          </fieldset>` : ''}
          <div class="field">
            <label class="check"><input type="checkbox" id="share-evidence" name="share"> <span><strong>Share this evidence and its location with signed-in community members.</strong> Off by default. You can change this later from your report.</span></label>
          </div>
          <div id="submit-error" role="alert"></div>
          <button type="submit" class="btn btn-primary btn-block btn-lg" id="submit-btn" ${gps ? '' : 'disabled'}>${icon('upload')}<span>${gps ? 'Submit report' : 'Location needed to submit'}</span></button>
          <p class="micro" id="submit-note">Submission creates a local demonstration record. No government authority is contacted.</p>
        </form>
      </div>
    </main>
  </div>`;
  let chosen = null;
  root.querySelectorAll('[data-category]').forEach(b => b.onclick = () => {
    chosen = b.dataset.category;
    root.querySelectorAll('[data-category]').forEach(o => o.setAttribute('aria-pressed', String(o === b)));
    document.getElementById('category-error').hidden = true;
  });
  root.querySelector('#review-form').onsubmit = e => submitReport(e, () => chosen);
  reportMode().then(mode => {
    const note = $('submit-note');
    if (note && mode === 'backend') note.textContent = 'Submission sends the report details, then the original photo or video, to the CivicLens development server. No government authority is contacted.';
  });

  // Stamp preview: re-rendered (at preview size) when the address arrives or a toggle changes.
  let previewSeq = 0, previewUrl = null, previewTimer = null;
  const renderPreview = async () => {
    if (!isPhoto) return;
    const my = ++previewSeq;
    const progress = $('stamp-progress');
    if (progress) progress.hidden = false;
    try {
      c.evidenceRef ??= await evidenceRef(c.blob);
      const blob = await makeStamped(c.blob, stampMeta(c, null, !!$('stamp-location')?.checked), {maxSide: 1600, quality: 0.85});
      if (my !== previewSeq || state.capture !== c) return;
      const img = $('review-photo');
      if (!img) return;
      const next = URL.createObjectURL(blob);
      img.src = next;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = next;
    } catch {
      if (my === previewSeq) toast('The stamp preview could not be drawn. Your photo is unchanged.');
    } finally {
      if (my === previewSeq && progress) progress.hidden = true;
    }
  };
  const schedulePreview = () => { clearTimeout(previewTimer); previewTimer = setTimeout(renderPreview, 120); };
  addCleanup(() => { previewSeq += 1; clearTimeout(previewTimer); if (previewUrl) URL.revokeObjectURL(previewUrl); });

  root.querySelector('#stamp-location')?.addEventListener('change', e => { writePhotoPrefs({stampLocation: e.target.checked}); schedulePreview(); });
  root.querySelector('#auto-save')?.addEventListener('change', e => writePhotoPrefs({autoSave: e.target.checked}));

  // Address: filled automatically, also when the photo was taken before the lookup finished.
  const showAddress = text => {
    for (const id of ['review-address', 'review-street-address']) {
      const el = $(id);
      if (el && el.textContent !== text) el.textContent = text;
    }
  };
  const applyAddress = a => {
    if (!a || !c.gps) return;
    const changed = c.gps.address !== a.address;
    Object.assign(c.gps, {address: a.address, neighbourhood: a.neighbourhood || c.gps.neighbourhood, short: a.short || null});
    showAddress(a.address);
    if (changed) schedulePreview();
  };
  if (gps) {
    const off = addressResolver.subscribe(st => {
      if (state.capture !== c || !nearPoint(st.point, c.gps)) return;
      if (st.status === 'resolved') applyAddress(st);
      else if (st.status === 'resolving') showAddress(c.gps.address || 'Finding the nearest address…');
      else if ((st.status === 'failed' || st.status === 'offline') && !c.gps.address) showAddress(st.message || 'Not available. Your coordinates are still saved.');
    });
    addCleanup(off);
    if (!gps.address) {
      const known = addressResolver.addressFor(gps);
      if (known) applyAddress(known);
      else {
        addressResolver.resolve(gps, {immediate: true});
        const st = addressResolver.state;
        if ((st.status === 'failed' || st.status === 'offline') && nearPoint(st.point, gps)) showAddress(st.message);
      }
    }
    root.querySelector('[data-action="lookup-review-street"]')?.addEventListener('click', () => {
      showAddress('Looking up the nearest mapped address…');
      addressResolver.resolve(c.gps, {force: true});
    });
    root.querySelector('[data-action="lookup-review"]')?.addEventListener('click', () => {
      showAddress('Finding the nearest address…');
      addressResolver.resolve(c.gps, {force: true});
    });
  }
  renderPreview();
}

async function submitReport(e, getCategory) {
  e.preventDefault();
  const c = state.capture;
  if (!c) return navigate('camera');
  const category = getCategory();
  const confirmed = document.getElementById('confirm-location').checked;
  const catErr = document.getElementById('category-error');
  const confErr = document.getElementById('confirm-error');
  catErr.hidden = !!category;
  confErr.hidden = confirmed;
  if (!c.gps) {
    document.getElementById('submit-error').innerHTML = notice('<strong>Location missing.</strong> Retake the evidence after the camera screen shows a location fix.', 'error', 'alert');
    return undefined;
  }
  if (!category) return document.querySelector('#category-grid [data-category]').focus();
  if (!confirmed) return document.getElementById('confirm-location').focus();
  const btn = document.getElementById('submit-btn');
  if (btn.disabled) return undefined; // already submitting
  const label = btn.querySelector('span');
  btn.disabled = true;
  const mode = await reportMode();
  label.textContent = mode === 'backend' ? 'Sending to the CivicLens server…' : 'Saving evidence…';
  document.getElementById('submit-error').innerHTML = '';
  const isPhoto = c.type === 'photo';
  const includeLocation = !!document.getElementById('stamp-location')?.checked;
  const autoSave = c.captureSource === 'Live camera' && !!document.getElementById('auto-save')?.checked;
  try {
    if (!c.idempotencyKey) {
      // A lookup still running for this spot gets a short, bounded chance to finish.
      if (!c.gps.address) {
        const a = await addressResolver.whenSettled(c.gps, 3000);
        if (a) Object.assign(c.gps, {address: a.address, neighbourhood: a.neighbourhood || c.gps.neighbourhood, short: a.short || null});
      }
      // One idempotency key per captured report, kept (in memory, with the evidence) across retries, so
      // a retry after an interrupted connection can never create a second complaint. The location sent
      // with the first attempt is frozen with the key: an address that resolves later must not change
      // the retried request, or the server would reject it as a different report.
      c.idempotencyKey = newIdempotencyKey();
      c.sentGps = {...c.gps};
    }
    if (isPhoto) c.evidenceRef ??= await evidenceRef(c.blob);
    const saved = await saveReport({
      category,
      description: document.getElementById('description').value,
      gps: c.sentGps,
      neighbourhood: c.sentGps?.neighbourhood,
      created: c.capturedAt || null,
      uploadedAt: c.uploadedAt,
      owner: viewer().role === 'resident' ? viewer().email : null,
      mediaType: c.type,
      shareEvidence: document.getElementById('share-evidence').checked,
      captureSource: c.captureSource,
      evidenceRef: c.evidenceRef || null  // local records only; the API payload is allowlisted and never carries it
    }, c.blob, {idempotencyKey: c.idempotencyKey});
    const id = saved.id;
    let uploadedAt = c.uploadedAt;
    if (saved.mode === 'local') {
      const created = JSON.parse(sessionStorage.getItem('ncl-created') || '[]');
      created.push(id);
      try { sessionStorage.setItem('ncl-created', JSON.stringify(created.slice(-50))); } catch { /* receipt gating is best-effort */ }
      uploadedAt = (await api.receipt(id))?.uploadedAt ?? uploadedAt;
    }
    if (isPhoto) {
      // The stamp uses the ID the store (local or server) has just issued, never a provisional one.
      // The complaint already exists, so a stamping failure only affects the convenience copy.
      label.textContent = 'Stamping your photo…';
      try {
        const stamped = await makeStamped(c.blob, stampMeta({...c, gps: c.sentGps}, id, includeLocation));
        const name = stampFileName(id, c.capturedAt || uploadedAt);
        stampedPhotos.set(id, {blob: stamped, name, evidenceRef: c.evidenceRef || null});
        // The original is the evidence (and is what server mode uploads); the copy is a convenience.
        await mediaDB.put(stampedKey(saved.mode === 'local' ? id : serverEvidenceKey(id)), stamped).catch(() => {});
        if (autoSave) saveJobs.set(id, autoSavePhoto(stamped, name, id));
      } catch {
        stampedPhotos.delete(id);
      }
    }
    state.capture = null;
    navigate('receipt/' + id);
  } catch (err) {
    btn.disabled = false;
    label.textContent = 'Try submitting again';
    if (err instanceof ApiError && err.code === 'idempotency_key_conflict') {
      // The report was edited after an attempt that may have reached the server. The next attempt
      // is a separate submission, and the message says so.
      c.idempotencyKey = null;
      c.sentGps = null;
    }
    document.getElementById('submit-error').innerHTML = submitErrorNotice(err);
    toast(err instanceof ApiError && err.code === 'idempotency_key_used' ? 'This report was already received.' : 'Report not saved. Your evidence is still here.', 'error');
  }
}

function submitErrorNotice(err) {
  if (err instanceof ApiError) {
    if (err.ambiguous) {
      return notice(`<strong>Not confirmed.</strong> ${esc(err.message)} The server may or may not have saved this report. Submitting again is safe: it reuses this report’s submission key, so the server will not create a duplicate. Your evidence is still here.`, 'error', 'alert');
    }
    if (err.code === 'idempotency_key_used') {
      const id = err.complaintId;
      return notice(`<strong>Already received.</strong> This report was already saved${id ? ` as <a class="inline-link" href="#/track/${esc(id)}">${esc(id)}</a>` : ''}. For security, its private claim code cannot be shown again because it has already been used.`, 'warn', 'alert');
    }
    if (err.code === 'idempotency_key_conflict') {
      return notice('<strong>Not sent.</strong> The report details changed after a submission that may already have reached the server. If you submit again, it will be sent as a separate report, which may duplicate the earlier one.', 'warn', 'alert');
    }
    if (err.code === 'rate_limited') {
      return notice(`<strong>Not sent yet.</strong> ${esc(err.message)} Your evidence is still here; submitting again later will not create a duplicate.`, 'warn', 'clock');
    }
    const fields = err.details.slice(0, 4).map(d => `<li>${esc(d.field.replace(/^body\./, ''))}: ${esc(d.message)}</li>`).join('');
    return notice(`<strong>Not sent.</strong> ${esc(err.message)}${fields ? `<ul class="micro">${fields}</ul>` : ''} Nothing was saved on this device either. Your evidence is still here, so you can retry.`, 'error', 'alert');
  }
  const message = err instanceof StorageError ? err.message : (err.message || 'The report could not be saved.');
  return notice(`<strong>Not saved.</strong> ${esc(message)} Your evidence is still here, so you can retry.`, 'error', 'alert');
}

// ---------- Receipt ----------
const SAVE_COPY = {
  shared: ['success', 'check'],
  downloaded: ['success', 'download'],
  opened: ['info', 'image'],
  cancelled: ['info', 'alert'],
  blocked: ['warn', 'alert'],
  failed: ['error', 'alert']
};

function showSaveResult(result) {
  const out = $('save-status');
  if (!out || !result) return;
  const [tone, ic] = SAVE_COPY[result.outcome] || ['info', 'alert'];
  out.dataset.tone = tone;
  out.innerHTML = `${icon(ic, 'mini-icon')}<span>${esc(result.message)}</span>`;
}

export async function renderReceipt(root, id) {
  const held = takeServerReceipt(id);
  if (held) return renderServerReceipt(root, held);
  const created = JSON.parse(sessionStorage.getItem('ncl-created') || '[]');
  if (!created.includes(id)) return navigate('track/' + id);
  const r = await api.receipt(id);
  if (!r) return navigate('track/' + id);
  const blob = await mediaDB.get(id).catch(() => null);
  const isPhoto = r.mediaType !== 'video';
  let stamped = stampedPhotos.get(id) || null;
  if (isPhoto && !stamped && blob) {
    // Coming back to the receipt later: reuse the stored copy, or stamp the original again.
    const name = stampFileName(r.id, r.capturedAt || r.uploadedAt);
    const saved = await mediaDB.get(stampedKey(id)).catch(() => null);
    try {
      stamped = {name, blob: saved || await buildEvidenceCopy(blob, r)};
      stampedPhotos.set(id, stamped);
    } catch { stamped = null; }
  }
  const shown = isPhoto && stamped ? stamped.blob : blob;
  const url = shown ? URL.createObjectURL(shown) : null;
  if (url) addCleanup(() => URL.revokeObjectURL(url));
  const method = preferredMethod();
  const sharing = r.shareEvidence ? 'Shared with signed-in community members' : 'Private: visible to you and civic admins only';
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="receipt-main" tabindex="-1">
      <div class="receipt-hero">
        <span class="success-mark">${icon('check')}</span>
        <div>
          <p class="eyebrow">Step 3 of 3 · Receipt</p>
          <h1 id="page-title" tabindex="-1">Report saved on this device</h1>
          <p class="lead">Track progress with the public reference. Keep the private claim code separate: it is needed only to claim this report in a resident account.</p>
        </div>
      </div>
      <div class="receipt-grid">
        <section class="card reference-card" aria-labelledby="ref-title">
          <p class="eyebrow">Public reference · share freely</p>
          <h2 id="ref-title">Complaint ID</h2>
          <p class="code-value">${esc(r.id)}</p>
          <div class="row-actions">${btn(icon('copy') + '<span>Copy ID</span>', `data-copy="${esc(r.id)}"`, 'secondary')}<a class="btn btn-primary" href="#/track/${esc(r.id)}">${icon('search')}<span>Track this issue</span></a></div>
        </section>
        <section class="card private-card" aria-labelledby="claim-title">
          <p class="eyebrow">Private claim code · keep secret</p>
          <h2 id="claim-title">Claim code</h2>
          <p class="code-value code-private">${esc(r.claim)}</p>
          <p class="micro">Never post this code or include it in shared photos. It is not part of the public ID, the stamped photo, its file name or public tracking.</p>
          <div class="row-actions">${btn(icon('copy') + '<span>Copy claim code</span>', `data-copy="${esc(r.claim)}"`, 'ghost')}</div>
        </section>
      </div>
      <div class="receipt-body">
        <section class="card" aria-labelledby="summary-title">
          <h2 id="summary-title">Summary</h2>
          <dl class="meta-list">
            <div><dt>Category</dt><dd>${esc(r.category)}</dd></div>
            <div><dt>Neighbourhood</dt><dd>${esc(r.area)}</dd></div>
            <div><dt>Captured</dt><dd>${r.capturedAt ? fmtDateTime(r.capturedAt) : 'Not recorded in the file'}${r.captureSource ? ` · ${esc(r.captureSource)}` : ''}</dd></div>
            <div><dt>Uploaded to CivicLens</dt><dd>${fmtDateTime(r.uploadedAt)}</dd></div>
            <div><dt>Evidence location</dt><dd>${r.gps ? `${coords(r.gps)}${r.gps.accuracy ? ` · ±${Math.round(r.gps.accuracy)} m` : ''}<br><span class="micro">${esc(locationSourceLabel(r.gps))}${r.gps.timestamp ? ` · fix ${fmtDateTime(new Date(r.gps.timestamp).toISOString())}` : ''}${r.gps.address ? ` · ${esc(r.gps.address)}` : ''}</span>` : 'No coordinates recorded'}</dd></div>
            ${r.evidenceRef ? `<div><dt>Evidence ref</dt><dd><span class="code-inline">${esc(r.evidenceRef)}</span> <span class="micro">fingerprint of the original photo</span></dd></div>` : ''}
            <div><dt>Visibility</dt><dd>${sharing}</dd></div>
          </dl>
        </section>
        <section class="card" aria-labelledby="evidence-preview-title">
          <h2 id="evidence-preview-title">${isPhoto ? 'Your stamped photo' : 'Your evidence'}</h2>
          ${url
            ? (r.mediaType === 'video' ? `<video class="receipt-media" controls playsinline src="${url}" aria-label="Submitted video"></video>` : `<img class="receipt-media" src="${url}" alt="Evidence photo for ${esc(r.id)} with the Namma CivicLens stamp">`)
            : notice('The original file is not available on this device. Summary and reference remain valid.', 'info', 'alert')}
          <div class="row-actions">
            ${url ? btn(icon('download') + `<span>${r.mediaType === 'video' ? 'Download video and metadata' : saveLabel(method)}</span>`, `data-evidence="${esc(r.id)}"`, 'primary') : ''}
            ${isPhoto && blob ? btn(icon('download') + '<span>Download evidence copy</span>', `data-evidence-copy="${esc(r.id)}"`, 'secondary') : ''}
            ${btn(icon('file') + '<span>Download private receipt</span>', `data-receipt="${esc(r.id)}"`, 'secondary')}
          </div>
          ${isPhoto && blob ? '<p class="micro">The evidence copy is a JPEG with a navy strip showing the public ID, category, capture time and location. Your original file is kept unchanged.</p>' : ''}
          ${isPhoto && url ? `<p class="save-status" id="save-status" role="status" aria-live="polite"></p>` : ''}
          <p class="micro">${isPhoto
            ? `Saved as <span class="code-inline">${esc(stamped?.name || stampFileName(r.id, r.capturedAt || r.uploadedAt))}</span>. The stamp shows the logo, complaint ID, evidence reference, capture time and, if you chose it, the address and coordinates. Never the claim code. Your original file is kept unchanged.${method === 'share' ? ' On iPhone choose “Save Image”; on Android choose your gallery or Files.' : ''}`
            : 'Video is delivered with a separate metadata file, not an embedded watermark.'}</p>
        </section>
      </div>
      <p class="receipt-foot"><a class="inline-link" href="#/camera">${icon('camera')}<span>Report another issue</span></a></p>
    </main>
  </div>`;
  root.querySelector('[data-evidence]')?.addEventListener('click', async () => {
    if (!isPhoto) return downloadEvidence(r, blob);
    if (!stamped?.blob) return downloadEvidence(r, blob);
    showSaveResult({outcome: 'opened', message: method === 'share' ? 'Opening the save sheet…' : 'Preparing the download…'});
    return showSaveResult(await savePhoto(stamped.blob, stamped.name, {key: r.id}));
  });
  root.querySelector('[data-receipt]')?.addEventListener('click', () => downloadReceipt(r));
  root.querySelector('[data-evidence-copy]')?.addEventListener('click', () => downloadNavyCopy(r, blob));
  if (isPhoto && !stamped && blob) showSaveResult({outcome: 'failed', message: 'The stamped copy could not be created on this device. Use “Save original to device” from a new report, or try again later.'});
  const job = saveJobs.get(id);
  if (job) {
    saveJobs.delete(id); // show the automatic outcome once; later visits use the button
    showSaveResult({outcome: 'opened', message: method === 'share' ? 'Opening the save sheet…' : 'Saving your stamped photo…'});
    job.then(result => { if (result && currentPath() === 'receipt/' + id) showSaveResult(result); });
  }
}

// Server-mode receipt. The claim code arrives in memory from report-gateway.js and is shown on this
// render only: it is not stored anywhere in the browser, so reloading this page cannot show it again.
async function renderServerReceipt(root, {receipt: r, evidenceKept, following = false}) {
  const blob = evidenceKept ? await mediaDB.get(serverEvidenceKey(r.id)).catch(() => null) : null;
  const isPhoto = r.mediaType !== 'video';
  const what = isPhoto ? 'photo' : 'video';
  // The stamped copy (made at submission with the server-issued ID) is a local convenience only:
  // the evidence uploaded to the server is always the untouched original.
  let stamped = stampedPhotos.get(r.id) || null;
  const ref = stamped?.evidenceRef || (isPhoto && blob ? await evidenceRef(blob) : null);
  const rec = {...r, evidenceRef: ref};
  if (isPhoto && !stamped && blob) {
    const name = stampFileName(r.id, r.capturedAt || r.uploadedAt);
    const saved = await mediaDB.get(stampedKey(serverEvidenceKey(r.id))).catch(() => null);
    try {
      stamped = {name, blob: saved || await buildEvidenceCopy(blob, rec), evidenceRef: ref};
      stampedPhotos.set(r.id, stamped);
    } catch { stamped = null; }
  }
  const shown = isPhoto && stamped ? stamped.blob : blob;
  const url = shown ? URL.createObjectURL(shown) : null;
  if (url) addCleanup(() => URL.revokeObjectURL(url));
  const method = preferredMethod();
  root.innerHTML = `<div class="page">
    ${publicTopbar({fallback: 'home'})}
    <main id="main" class="receipt-main" tabindex="-1" data-receipt-source="server">
      <div class="receipt-hero">
        <span class="success-mark">${icon('check')}</span>
        <div>
          <p class="eyebrow">Step 3 of 3 · Receipt</p>
          <h1 id="page-title" tabindex="-1">Report received by the CivicLens server</h1>
          <p class="lead">The server stored the report details and issued the public ID below. Save the private claim code now: it is shown only once.</p>
        </div>
      </div>
      ${r.replayed ? notice('<strong>Recovered after an interrupted connection.</strong> This is the same complaint the server saved on the first attempt; no duplicate was created. A new claim code was issued for it, and any earlier code for this complaint no longer works.', 'info', 'refresh') : ''}
      <section class="card" aria-labelledby="status-title" id="submission-status">
        <h2 id="status-title">Submission status</h2>
        <dl class="meta-list">
          <div><dt>Complaint details</dt><dd data-step="metadata">Saved on the CivicLens server</dd></div>
          <div><dt>${what === 'video' ? 'Video' : 'Photo'}</dt><dd id="evidence-upload-status" role="status" aria-live="polite"></dd></div>
        </dl>
        <div id="evidence-upload-actions" class="row-actions"></div>
      </section>
      <section class="card" aria-labelledby="notify-title" id="receipt-notifications">
        <h2 id="notify-title">Updates about this report</h2>
        <div id="notify-status" role="status" aria-live="polite">${following
          ? `<p>${icon('bell', 'mini-icon')} You will get a notification on this device when something happens to this report. <a class="inline-link" href="#/notifications">Notification history</a></p>`
          : `<p>Get a system notification on this phone or computer when the evidence is stored and when staff review it, even after you close CivicLens.</p>
             <div class="row-actions">${btn(icon('bell') + '<span>Notify me about this report</span>', 'id="notify-me"', 'secondary')}</div>
             <div id="notify-feedback"></div>`}</div>
      </section>
      <div class="receipt-grid">
        <section class="card reference-card" aria-labelledby="ref-title">
          <p class="eyebrow">Public reference · share freely</p>
          <h2 id="ref-title">Complaint ID</h2>
          <p class="code-value">${esc(r.id)}</p>
          <div class="row-actions">${btn(icon('copy') + '<span>Copy ID</span>', `data-copy="${esc(r.id)}"`, 'secondary')}<a class="btn btn-primary" href="#/track/${esc(r.id)}">${icon('search')}<span>Track this issue</span></a></div>
        </section>
        <section class="card private-card" aria-labelledby="claim-title">
          <p class="eyebrow">Private claim code · keep secret</p>
          <h2 id="claim-title">Claim code</h2>
          <p class="code-value code-private">${esc(r.claim)}</p>
          <p class="micro"><strong>Save this code now. It will not be shown again.</strong> The server keeps only a scrambled (hashed) form and cannot recover it, and leaving or reloading this page removes it. Never post it or include it in shared photos.</p>
          <div class="row-actions">${btn(icon('copy') + '<span>Copy claim code</span>', `data-copy="${esc(r.claim)}"`, 'ghost')}${btn(icon('file') + '<span>Download private receipt</span>', `data-receipt="${esc(r.id)}"`, 'secondary')}</div>
        </section>
      </div>
      <div class="receipt-body">
        <section class="card" aria-labelledby="summary-title">
          <h2 id="summary-title">Summary</h2>
          <dl class="meta-list">
            <div><dt>Category</dt><dd>${esc(r.category)}</dd></div>
            <div><dt>Neighbourhood</dt><dd>${esc(r.area)}</dd></div>
            <div><dt>Captured</dt><dd>${r.capturedAt ? fmtDateTime(r.capturedAt) : 'Not recorded in the file'}${r.captureSource ? ` · ${esc(r.captureSource)}` : ''}</dd></div>
            <div><dt>Received by the server</dt><dd>${r.uploadedAt ? fmtDateTime(r.uploadedAt) : 'Not reported'}</dd></div>
            <div><dt>Evidence location</dt><dd>${coords(r.gps)}${r.gps.accuracy ? ` · ±${Math.round(r.gps.accuracy)} m` : ''}<br><span class="micro">${esc(locationSourceLabel(r.gps))}${r.gps.timestamp ? ` · fix ${fmtDateTime(new Date(r.gps.timestamp).toISOString())}` : ''}${r.gps.address ? ` · ${esc(r.gps.address)}` : ''}</span></dd></div>
            ${ref ? `<div><dt>Evidence ref</dt><dd><span class="code-inline">${esc(ref)}</span> <span class="micro">fingerprint of the original photo</span></dd></div>` : ''}
            <div><dt>Server status</dt><dd>${esc(r.status)} · ${esc(r.verification)}</dd></div>
            <div><dt>Sharing preference</dt><dd>${r.shareEvidence ? 'Share with signed-in community members' : 'Private'} <span class="micro">(recorded; uploaded evidence stays private until sharing is built)</span></dd></div>
          </dl>
        </section>
        <section class="card" aria-labelledby="evidence-preview-title">
          <h2 id="evidence-preview-title">${isPhoto && stamped ? 'Your stamped photo · copy in this browser' : 'Your evidence · copy in this browser'}</h2>
          ${url
            ? (r.mediaType === 'video' ? `<video class="receipt-media" controls playsinline src="${url}" aria-label="Captured video, kept on this device"></video>` : `<img class="receipt-media" src="${url}" alt="${stamped ? `Evidence photo for ${esc(r.id)} with the Namma CivicLens stamp` : `Captured evidence for ${esc(r.id)}, kept on this device`}">`)
            : notice('The original file is not available in this browser. The server receipt and public ID remain valid.', 'info', 'alert')}
          <div class="row-actions">
            ${url ? btn(icon('download') + `<span>${r.mediaType === 'video' ? 'Download video and metadata' : saveLabel(method)}</span>`, `data-evidence="${esc(r.id)}"`, 'primary') : ''}
            ${isPhoto && blob ? btn(icon('download') + '<span>Download evidence copy</span>', `data-evidence-copy="${esc(r.id)}"`, 'secondary') : ''}
          </div>
          ${isPhoto && url ? `<p class="save-status" id="save-status" role="status" aria-live="polite"></p>` : ''}
          ${isPhoto && blob ? '<p class="micro">The evidence copy is a JPEG with a navy strip showing the public ID, category, capture time and location. Your original file is kept unchanged.</p>' : ''}
          <p class="micro">This preview comes from this browser’s copy, which is kept even after the upload. ${isPhoto ? 'The server receives the original photo, not the stamped copy. The stamp shows the logo, complaint ID, evidence reference, capture time and, if you chose it, the address and coordinates; never the claim code. ' : ''}Uploaded evidence is private: it is not shown in public tracking, and it cannot yet be viewed from the server.</p>
        </section>
      </div>
      <p class="receipt-foot"><a class="inline-link" href="#/camera">${icon('camera')}<span>Report another issue</span></a> · <a class="inline-link" href="#/my-reports" id="receipt-my-reports">${icon('list')}<span>Reports on This Device</span></a></p>
    </main>
  </div>`;
  root.querySelector('[data-evidence]')?.addEventListener('click', async () => {
    if (!isPhoto || !stamped?.blob) return downloadEvidence(rec, blob);
    showSaveResult({outcome: 'opened', message: method === 'share' ? 'Opening the save sheet…' : 'Preparing the download…'});
    return showSaveResult(await savePhoto(stamped.blob, stamped.name, {key: r.id}));
  });
  if (isPhoto && !stamped && blob) showSaveResult({outcome: 'failed', message: 'The stamped copy could not be created on this device. The original was kept and is what the server receives.'});
  root.querySelector('[data-evidence-copy]')?.addEventListener('click', () => downloadNavyCopy(r, blob));
  const job = saveJobs.get(r.id);
  if (job) {
    saveJobs.delete(r.id); // show the automatic outcome once
    showSaveResult({outcome: 'opened', message: method === 'share' ? 'Opening the save sheet…' : 'Saving your stamped photo…'});
    job.then(result => { if (result && currentPath() === 'receipt/' + r.id) showSaveResult(result); });
  }
  root.querySelector('[data-receipt]')?.addEventListener('click', () => downloadReceipt(r, uploadState(r.id) === 'uploaded'
    ? `The complaint details and the ${what} were stored privately by the CivicLens server. A copy of the ${what} also remains in the browser that captured it.`
    : `The complaint details were stored by the CivicLens server. The ${what} was NOT confirmed as uploaded; a copy is kept in the browser that captured it.`));
  $('notify-me')?.addEventListener('click', async e => {
    const button = e.currentTarget;
    button.disabled = true;
    const result = await notifyAboutReport(r.id);  // asks for notification permission (this click)
    const slot = $('notify-status');
    if (!slot) return;
    if (!result.following) {
      button.disabled = false;
      $('notify-feedback').innerHTML = notice(esc(result.message || 'Notifications could not be turned on.'), 'warn', 'alert');
      return;
    }
    slot.innerHTML = `${result.push === 'enabled'
        ? `<p>${icon('bell', 'mini-icon')} Done. This device will show a notification when something happens to this report.</p>`
        : notice(`<strong>Following, but this device will not show system notifications.</strong> ${esc(result.message || '')} Updates are kept in your notification history.`, 'warn', 'alert')}
      <p><a class="inline-link" href="#/notifications">Notification history</a></p>`;
  });
  addCleanup(() => forgetUpload(r.id));
  runEvidenceUpload(r.id, what);
}

// Evidence upload is a separate step after the complaint exists. Retrying never re-creates the
// complaint: it sends the same file to the same complaint ID.
async function runEvidenceUpload(id, what) {
  const status = $('evidence-upload-status');
  const actions = $('evidence-upload-actions');
  if (!status) return;
  if (uploadState(id) === 'unavailable') {
    status.dataset.state = 'unavailable';
    status.textContent = `Not uploaded. There is no ${what} file to send for this report.`;
    return;
  }
  status.dataset.state = 'uploading';
  status.textContent = `Uploading the ${what} to the CivicLens server…`;
  actions.innerHTML = '';
  const result = await uploadEvidence(id);
  if (!status.isConnected) return; // the user left the page; the upload result is not shown anywhere else
  status.dataset.state = result.state;
  if (result.state === 'uploaded') {
    const e = result.evidence;
    status.innerHTML = `<strong>Uploaded privately</strong> to the CivicLens server (${Math.max(1, Math.round(e.byteSize / 1024))} KB, checksum <span class="code-inline">${esc(e.sha256.slice(0, 12))}…</span>). It is not shown in public tracking.`;
    return;
  }
  status.innerHTML = `<strong>Upload ${result.state === 'unavailable' ? 'unavailable' : 'failed'}.</strong> ${esc(result.message)} Your complaint <span class="code-inline">${esc(id)}</span> is saved; only the ${what} upload is affected. A copy of the ${what} stays in this browser.`;
  if (result.retryable) {
    actions.innerHTML = `${btn(icon('upload') + `<span>Retry ${what} upload</span>`, 'id="retry-upload"', 'primary')}<p class="micro">Retrying sends the same ${what} to this complaint again. It does not create a new complaint.</p>`;
    $('retry-upload').addEventListener('click', () => runEvidenceUpload(id, what));
  }
}

function downloadReceipt(r, storageNote = 'This receipt is stored on this device only. It is not sent to any authority.') {
  const text = [
    'NAMMA CIVICLENS — PRIVATE RECEIPT (KEEP SECRET)',
    '',
    `Public complaint ID: ${r.id}`,
    `Private claim code: ${r.claim}`,
    `Category: ${r.category}`,
    `Captured: ${r.capturedAt ?? 'not recorded'}`,
    `Uploaded: ${r.uploadedAt}`,
    `Location: ${r.gps ? `${r.gps.lat}, ${r.gps.lng}` : 'not recorded'}`,
    `Visibility: ${r.shareEvidence ? 'shared with community' : 'private'}`,
    '',
    storageNote
  ].join('\n');
  download(new Blob([text], {type: 'text/plain'}), `${r.id}-private-receipt.txt`);
}

async function downloadEvidence(r, blob) {
  if (!blob) return toast('The original file is not available on this device.');
  if (r.mediaType === 'video') {
    download(blob, `${r.id}-original.${extFor(blob.type)}`);
    download(new Blob([JSON.stringify({id: r.id, category: r.category, captured: r.capturedAt, uploaded: r.uploadedAt, location: r.gps, note: 'Location is associated metadata for this upload. It is not embedded in the video file.'}, null, 2)], {type: 'application/json'}), `${r.id}-metadata.json`);
    return;
  }
  try {
    const copy = await buildEvidenceCopy(blob, r);
    const result = await savePhoto(copy, stampFileName(r.id, r.capturedAt || r.uploadedAt), {key: r.id});
    showSaveResult(result);
  } catch {
    toast('The stamped copy could not be created. The original is still on this device.');
  }
}

/** v5.1 evidence copy (navy strip), offered beside the v5.2 stamped photo. */
async function downloadNavyCopy(r, blob) {
  if (!blob) return toast('The original file is not available on this device.');
  try {
    download(await buildNavyEvidenceCopy(blob, r), `${r.id}-evidence-copy.jpg`);
  } catch {
    toast('The evidence copy could not be created. The original is still on this device.');
  }
}

/** JPEG with a navy/coral strip: public ID, category, capture time and location. Never a claim code. */
export async function buildNavyEvidenceCopy(blob, r) {
  const img = new Image();
  img.src = URL.createObjectURL(blob);
  try {
    await img.decode();
  } finally {
    setTimeout(() => URL.revokeObjectURL(img.src), 5000);
  }
  if (document.fonts?.load) await document.fonts.load('700 20px Inter').catch(() => {});
  const w = img.naturalWidth, h = img.naturalHeight;
  const strip = Math.max(110, Math.round(w * 0.1));
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h + strip;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, w, h);
  ctx.fillStyle = '#18324A';
  ctx.fillRect(0, h, w, strip);
  ctx.fillStyle = '#F07861';
  ctx.fillRect(0, h, Math.max(8, w * 0.01), strip);
  const fs = Math.max(16, Math.round(w * 0.024));
  const pad = fs * 1.4;
  const font = (weight, size) => `${weight} ${size}px Inter, "Segoe UI", Arial, sans-serif`;
  const fitText = (text, x, y, maxW) => {
    let t = text;
    while (ctx.measureText(t).width > maxW && t.length > 4) t = t.slice(0, -2) + '…';
    ctx.fillText(t, x, y);
  };
  const maxW = w - pad * 2;
  ctx.fillStyle = '#FFFFFF';
  ctx.font = font(700, fs);
  fitText(`NAMMA CIVICLENS  ·  ${r.id}`, pad, h + strip * 0.34, maxW);
  ctx.font = font(500, Math.round(fs * 0.78));
  ctx.fillStyle = '#D9D9D2';
  const captured = r.capturedAt ? new Date(r.capturedAt).toLocaleString('en-IN', {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata'}) : 'capture time not recorded';
  fitText(`${r.category}  |  Captured ${captured} IST`, pad, h + strip * 0.62, maxW);
  const where = r.gps ? `${r.gps.lat.toFixed(6)}, ${r.gps.lng.toFixed(6)}${r.gps.accuracy ? ` ±${Math.round(r.gps.accuracy)} m` : ''}  ·  ${r.gps.source}` : `Location not recorded  ·  ${r.area}`;
  fitText(where, pad, h + strip * 0.86, maxW);
  return new Promise((resolve, reject) => canvas.toBlob(b => b ? resolve(b) : reject(new Error('encode failed')), 'image/jpeg', 0.95));
}

/** Stamped JPEG for an existing record: logo, public ID, evidence ref, time, optional location. Never a claim code. */
export async function buildEvidenceCopy(blob, r) {
  const includeLocation = readPhotoPrefs().stampLocation;
  return makeStamped(blob, {complaintId: r.id, evidenceRef: r.evidenceRef || null, includeLocation, gps: r.gps, address: r.gps?.address || null, capturedAt: r.capturedAt});
}
