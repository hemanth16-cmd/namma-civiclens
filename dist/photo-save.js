// Saving the stamped photo to the device with the best flow each platform supports.
//
// Websites cannot write into the Photos or Gallery app silently. The options are:
// - Web Share with files (Android Chrome, iPhone/iPad Safari, installed PWAs): opens the share
//   sheet, where "Save Image" (iOS) or a gallery/Files target (Android) stores the photo. It needs
//   a recent user tap, and the browser only reports that the sheet completed, not which target was
//   chosen, so the outcome is reported as "shared", never as "saved to Photos".
// - Download (desktop Chrome/Edge/Firefox, and Android when sharing is unavailable): the browser
//   starts a download. Completion is not observable from the page, so it is reported as "started".
//
// Each complaint has one save in flight at a time, and automatic saves run once per complaint, so
// re-renders and repeated callbacks cannot produce duplicate files.

const inFlight = new Map();
const AUTO_KEY = 'ncl-autosaved';

/** Phones and tablets get the share sheet; desktops get a download, which is what users expect there. */
export function preferredMethod(env = globalThis) {
  const nav = env.navigator;
  const file = typeof env.File === 'function' ? new env.File([''], 'probe.jpg', {type: 'image/jpeg'}) : null;
  const canShareFiles = !!(file && nav?.canShare && nav?.share && (() => { try { return nav.canShare({files: [file]}); } catch { return false; } })());
  const touch = !!env.matchMedia?.('(pointer: coarse)').matches || /Android|iPhone|iPad|iPod/i.test(nav?.userAgent || '') || (nav?.platform === 'MacIntel' && nav?.maxTouchPoints > 1);
  if (canShareFiles && touch) return 'share';
  if (typeof env.document?.createElement === 'function' && 'download' in env.document.createElement('a')) return 'download';
  return canShareFiles ? 'share' : 'open';
}

export function saveLabel(method = preferredMethod()) {
  return method === 'share' ? 'Save to Photos' : method === 'open' ? 'Open image to save' : 'Save image';
}

function triggerDownload(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

/**
 * Offer the photo for saving. Resolves to {outcome, method, message}:
 * shared | downloaded | opened | cancelled | blocked | failed. Never throws.
 */
export function savePhoto(blob, name, {key = name, method = preferredMethod(), title = 'Namma CivicLens evidence photo'} = {}) {
  if (inFlight.has(key)) return inFlight.get(key);
  const job = (async () => {
    if (!blob) return {outcome: 'failed', method, message: 'The stamped photo is not ready yet.'};
    if (method === 'share') {
      try {
        const file = new File([blob], name, {type: 'image/jpeg', lastModified: Date.now()});
        await navigator.share({files: [file], title});
        return {outcome: 'shared', method, message: 'Share sheet completed. If you chose “Save Image” or a gallery app, the photo is on your device.'};
      } catch (err) {
        if (err?.name === 'AbortError') return {outcome: 'cancelled', method, message: 'Saving was cancelled. Tap Save to Photos to try again.'};
        if (err?.name === 'NotAllowedError') return {outcome: 'blocked', method, message: 'Your browser needs a tap before it can open the save sheet. Tap Save to Photos.'};
        // Some browsers advertise file sharing but fail at runtime; fall back to a download.
        try {
          triggerDownload(blob, name);
          return {outcome: 'downloaded', method: 'download', message: `Download started: ${name}. Check your Downloads folder.`};
        } catch {
          return {outcome: 'failed', method, message: 'The photo could not be shared or downloaded here.'};
        }
      }
    }
    if (method === 'download') {
      try {
        triggerDownload(blob, name);
        return {outcome: 'downloaded', method, message: `Download started: ${name}. Check your Downloads folder.`};
      } catch {
        return {outcome: 'failed', method, message: 'The download could not start in this browser.'};
      }
    }
    // Last resort (very old iOS): open the image so it can be long-pressed and saved.
    const url = URL.createObjectURL(blob);
    const win = window.open(url, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return win
      ? {outcome: 'opened', method, message: 'The photo opened in a new tab. Press and hold it, then choose Save to Photos.'}
      : {outcome: 'blocked', method, message: 'The browser blocked the new tab. Allow pop-ups for this site, then try again.'};
  })().finally(() => inFlight.delete(key));
  inFlight.set(key, job);
  return job;
}

function autoSaved() {
  try { return JSON.parse(sessionStorage.getItem(AUTO_KEY) || '[]'); } catch { return []; }
}

/** Automatic save, at most once per complaint per session. Resolves to null if already done. */
export function autoSavePhoto(blob, name, key, options = {}) {
  const done = autoSaved();
  if (done.includes(key)) return Promise.resolve(null);
  try { sessionStorage.setItem(AUTO_KEY, JSON.stringify([...done, key].slice(-50))); } catch { /* best effort */ }
  return savePhoto(blob, name, {key, ...options});
}

// Per-device preferences for the review screen. Location stamping starts off until the reporter opts in.
const PREF_KEY = 'ncl-photo-prefs';
export function readPhotoPrefs() {
  try { return {stampLocation: false, autoSave: true, ...JSON.parse(localStorage.getItem(PREF_KEY) || '{}')}; } catch { return {stampLocation: false, autoSave: true}; }
}
export function writePhotoPrefs(patch) {
  try { localStorage.setItem(PREF_KEY, JSON.stringify({...readPhotoPrefs(), ...patch})); } catch { /* best effort */ }
}
