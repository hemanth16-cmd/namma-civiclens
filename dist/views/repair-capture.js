// Inline repair camera uses the same tested camera lifecycle as citizen reporting.
import {createCamera} from '../camera-service.js';
import {icon, notice} from '../ui.js';
import {esc} from '../services.js';

export function mountRepairCapture(slot, onPhoto) {
  const camera = createCamera();
  let closed = false;
  slot.innerHTML = `<section class="repair-camera stack" aria-label="Capture repair photo">
    <video autoplay muted playsinline aria-label="Live repair camera preview"></video>
    <p class="micro" data-camera-state role="status" aria-live="polite">Starting camera…</p>
    <div class="row-actions">
      <button type="button" class="btn btn-primary" data-shutter disabled>${icon('camera')}<span>Capture and upload</span></button>
      <button type="button" class="btn btn-secondary" data-retry>Retry / play preview</button>
      <button type="button" class="btn btn-ghost" data-switch>Switch camera</button>
      <button type="button" class="btn btn-ghost" data-close>Cancel</button>
    </div>
    <p class="micro">This captures a repair photo. GPS metadata is not added or inferred from the original report.</p>
  </section>`;
  const video = slot.querySelector('video');
  const shutter = slot.querySelector('[data-shutter]');
  const status = slot.querySelector('[data-camera-state]');
  const messages = {
    live: 'Camera ready. Capture the completed repair.', denied: 'Camera permission was denied. Allow it in browser site settings, then retry.',
    missing: 'No camera was found. Upload a JPEG photo instead.', busy: 'Camera is busy. Close other camera apps, then retry.',
    insecure: 'Camera needs HTTPS or localhost. Upload a JPEG photo instead.', unsupported: 'This browser cannot open a camera. Upload a JPEG photo instead.',
    'needs-tap': 'Press Retry / play preview to start the video.', 'no-frames': 'The camera opened but no frames arrived. Retry or upload a photo.',
    ended: 'Camera stopped. Retry to reconnect.', error: 'Camera could not start. Retry or upload a photo.'
  };
  const unsubscribe = camera.subscribe(s => {
    if (closed) return;
    shutter.disabled = s.status !== 'live';
    status.textContent = messages[s.status] || 'Starting camera…';
  });
  const close = () => {
    if (closed) return;
    closed = true;
    unsubscribe();
    camera.stop();
    if (slot.isConnected) slot.replaceChildren();
  };
  const start = async () => {
    const stream = await camera.start();
    if (!closed && stream) await camera.attach(video);
  };
  slot.querySelector('[data-close]').onclick = close;
  slot.querySelector('[data-retry]').onclick = () => camera.stream ? camera.resume() : start();
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
      close();
      await onPhoto(blob);
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
