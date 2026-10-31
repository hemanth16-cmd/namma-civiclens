// Inline repair camera with real-time GPS geotagging.
// Obtains a fresh browser geolocation fix for the repair capture. Original complaint
// coordinates and addresses are never copied into the repair evidence.
import {createCamera} from '../camera-service.js';
import {createLocator, isFresh} from '../location-service.js';
import {icon, notice} from '../ui.js';
import {esc} from '../services.js';

export function mountRepairCapture(slot, onPhoto) {
  const camera = createCamera();
  const locator = createLocator();
  let closed = false;
  let preservedBlob = null;
  let preservedCapturedAt = null;

  slot.innerHTML = `<section class="repair-camera stack" aria-label="Capture repair photo">
    <video autoplay muted playsinline aria-label="Live repair camera preview"></video>
    <p class="micro" data-camera-state role="status" aria-live="polite">Starting camera…</p>
    <div class="repair-location-status geo-caption" data-location-status role="status" aria-live="polite">
      ${icon('pin', 'mini-icon')}
      <div>
        <strong data-location-coords>Acquiring GPS fix…</strong>
        <small data-location-detail>Acquiring fresh coordinates for this repair.</small>
      </div>
      <button type="button" class="btn btn-secondary btn-sm" data-retry-location style="display:none; margin-left:auto;">Retry location</button>
    </div>
    <div data-preserved-notice></div>
    <div class="row-actions">
      <button type="button" class="btn btn-primary" data-shutter disabled>${icon('camera')}<span>Capture and upload</span></button>
      <button type="button" class="btn btn-secondary" data-retry>Retry / play preview</button>
      <button type="button" class="btn btn-ghost" data-switch>Switch camera</button>
      <button type="button" class="btn btn-ghost" data-close>Cancel</button>
    </div>
    <p class="micro">GPS is browser-reported location for the repair evidence, not proof of authenticity. Original report coordinates are never copied.</p>
  </section>`;

  const video = slot.querySelector('video');
  const shutter = slot.querySelector('[data-shutter]');
  const status = slot.querySelector('[data-camera-state]');
  const locCoords = slot.querySelector('[data-location-coords]');
  const locDetail = slot.querySelector('[data-location-detail]');
  const retryLoc = slot.querySelector('[data-retry-location]');
  const preservedSlot = slot.querySelector('[data-preserved-notice]');

  const messages = {
    live: 'Camera ready. Capture the completed repair.',
    denied: 'Camera permission was denied. Allow it in browser site settings, then retry.',
    missing: 'No camera was found. Upload a JPEG photo instead.',
    busy: 'Camera is busy. Close other camera apps, then retry.',
    insecure: 'Camera needs HTTPS or localhost. Upload a JPEG photo instead.',
    unsupported: 'This browser cannot open a camera. Upload a JPEG photo instead.',
    'needs-tap': 'Press Retry / play preview to start the video.',
    'no-frames': 'The camera opened but no frames arrived. Retry or upload a photo.',
    ended: 'Camera stopped. Retry to reconnect.',
    error: 'Camera could not start. Retry or upload a photo.'
  };

  const unsubscribeCamera = camera.subscribe(s => {
    if (closed) return;
    shutter.disabled = s.status !== 'live';
    status.textContent = messages[s.status] || 'Starting camera…';
  });

  const submitWithFix = async (blob, capturedAt, fix) => {
    if (closed) return;
    close();
    await onPhoto(blob, {
      capturedAt,
      gps: {
        lat: fix.lat,
        lng: fix.lng,
        accuracy: fix.accuracy,
        source: 'Browser GPS',
        sourceType: fix.sourceType || 'gps',
        timestamp: new Date(fix.timestamp).toISOString()
      }
    });
  };

  const updateLocationUI = s => {
    if (closed) return;
    const {status: locState, fix, refining} = s;
    if (locState === 'locating') {
      locCoords.textContent = 'Acquiring GPS fix…';
      locDetail.textContent = 'Acquiring fresh coordinates for this repair.';
      retryLoc.style.display = 'none';
    } else if (locState === 'acquired' && fix) {
      const acc = Number.isFinite(fix.accuracy) ? `±${Math.round(fix.accuracy)} m` : 'accuracy unknown';
      const refText = refining ? ' (refining…)' : '';
      locCoords.textContent = `${fix.lat.toFixed(5)}°, ${fix.lng.toFixed(5)}°`;
      locDetail.textContent = `${acc} · ${fix.source || 'Device GPS'}${refText}`;
      retryLoc.style.display = 'none';

      // If user already took a photo while waiting for GPS, submit now that we have a usable fix!
      if (preservedBlob && isFresh(fix)) {
        const b = preservedBlob;
        const ca = preservedCapturedAt;
        preservedBlob = null;
        preservedCapturedAt = null;
        submitWithFix(b, ca, fix);
      }
    } else if (locState === 'denied') {
      locCoords.textContent = 'Location permission denied';
      locDetail.textContent = 'Please enable location permissions in your browser site settings, then press Retry location.';
      retryLoc.style.display = 'inline-block';
    } else if (locState === 'timeout') {
      locCoords.textContent = 'Location timed out';
      locDetail.textContent = 'Could not get a GPS fix in time. Check device GPS or press Retry location.';
      retryLoc.style.display = 'inline-block';
    } else if (locState === 'unavailable') {
      locCoords.textContent = 'Location unavailable';
      locDetail.textContent = 'Location services could not determine position. Press Retry location.';
      retryLoc.style.display = 'inline-block';
    } else if (locState === 'insecure') {
      locCoords.textContent = 'Insecure context';
      locDetail.textContent = 'Geolocation requires HTTPS or localhost.';
      retryLoc.style.display = 'none';
    }
  };

  retryLoc.onclick = () => {
    locator.forgetDenial();
    locator.start();
  };

  const unsubscribeLocator = locator.subscribe(updateLocationUI);
  locator.start();

  const close = () => {
    if (closed) return;
    closed = true;
    unsubscribeCamera();
    camera.stop();
    unsubscribeLocator();
    locator.stop();
    preservedBlob = null;
    preservedCapturedAt = null;
    if (slot.isConnected) slot.replaceChildren();
  };

  const start = async () => {
    const stream = await camera.start();
    if (!closed && stream) await camera.attach(video);
  };

  slot.querySelector('[data-close]').onclick = close;
  slot.querySelector('[data-retry]').onclick = () => (camera.stream ? camera.resume() : start());
  slot.querySelector('[data-switch]').onclick = () => camera.switchCamera(video);

  shutter.onclick = async () => {
    if (closed || camera.state.status !== 'live' || !video.videoWidth) return;
    shutter.disabled = true;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
      if (!blob) throw new Error('The camera frame could not be saved. Try again.');
      if (closed) return;

      const capturedAt = new Date().toISOString();
      const fix = locator.usableFix();

      if (!fix) {
        // Location fix not yet available or permission failed: preserve photo so it is not discarded
        preservedBlob = blob;
        preservedCapturedAt = capturedAt;
        preservedSlot.innerHTML = notice(
          'Photo preserved. A usable location fix is required before submitting. Waiting for GPS fix… If permissions were blocked, allow location and click Retry location.',
          'info'
        );
        retryLoc.style.display = 'inline-block';
        shutter.disabled = false;
        return;
      }

      await submitWithFix(blob, capturedAt, fix);
    } catch (err) {
      if (!closed) {
        status.innerHTML = notice(esc(err.message || 'Capture failed.'), 'error', 'alert');
        shutter.disabled = camera.state.status !== 'live';
      }
    }
  };

  start();
  return close;
}
