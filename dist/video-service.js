// Admin-only video review service. Footage stays private. Linking a case never publishes footage,
// changes complaint status or creates location evidence. Every observation is human-recorded.
import {mediaDB, MAX_VIDEO_BYTES} from './services.js';

const KEY = 'ncl-video-reviews-v1';
export const findings = ['Suspected dumping', 'Waste present', 'No dumping observed', 'Inconclusive'];
export const REVIEW_TYPE = 'Manual administrator review. Not AI detection.';
const SUPPORTED = /\.(mp4|m4v|webm|mov|ogv)$/i;

const admin = actor => {
  if (actor?.role !== 'official') throw Object.assign(new Error('Admin access is required for video review.'), {name: 'AccessError'});
};
const read = () => {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; }
};
const write = clips => {
  try {
    localStorage.setItem(KEY, JSON.stringify(clips));
  } catch (cause) {
    throw Object.assign(new Error('Browser storage is full or blocked, so the review could not be saved. Free up space and try again. Footage already on this device is unchanged.'), {cause, name: 'StorageError'});
  }
};
const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');

/** Pre-check that warns (not blocks) when the browser says it cannot play this container/codec. */
export function playbackSupport(file) {
  const probe = document.createElement('video');
  const type = file.type || '';
  if (type && probe.canPlayType) {
    const answer = probe.canPlayType(type);
    if (answer === '') return {supported: false, message: `This browser reports it cannot play ${type}. Convert to H.264 MP4 or WebM before reviewing; the file is still saved.`};
  }
  if (!type && !SUPPORTED.test(file.name || '')) return {supported: false, message: 'The file type is unknown. Use MP4, WebM or MOV (H.264).'};
  if (/quicktime|mov/i.test(type) || /\.mov$/i.test(file.name || '')) return {supported: null, message: 'MOV files play only with H.264 video in many browsers. If playback fails, convert to MP4.'};
  return {supported: true, message: ''};
}

export const videoReviewService = {
  async list(actor) { admin(actor); return read(); },

  async import(actor, file, {caseId = null} = {}) {
    admin(actor);
    if (!file || !(file.type?.startsWith('video/') || SUPPORTED.test(file.name || ''))) throw new Error('Choose a supported video file, such as MP4, WebM or MOV.');
    if (file.size === 0) throw new Error('This video file is empty.');
    if (file.size > MAX_VIDEO_BYTES) throw new Error('Use a clip under 200 MB. Trim larger recordings first.');
    const clip = {
      id: newId(), name: file.name || 'Review clip', size: file.size, type: file.type || 'video/unknown', caseId,
      duration: null, finding: 'Not reviewed', notes: '', created: new Date().toISOString(), observations: [],
      reviewer: actor.name || 'Admin', mode: 'manual'
    };
    await mediaDB.put('video-review-' + clip.id, file);
    write([clip, ...read()]);
    return clip;
  },

  async media(actor, clipId) {
    admin(actor);
    if (!read().some(c => c.id === clipId)) throw new Error('Video not found.');
    return mediaDB.get('video-review-' + clipId);
  },

  async update(actor, clipId, patch) {
    admin(actor);
    const all = read(), clip = all.find(c => c.id === clipId);
    if (!clip) throw new Error('Video not found.');
    if (patch.finding !== undefined && !['Not reviewed', ...findings].includes(patch.finding)) throw new Error('Choose a valid review finding.');
    if (patch.duration !== undefined && (!Number.isFinite(patch.duration) || patch.duration < 0)) throw new Error('Invalid video duration.');
    for (const k of ['finding', 'notes', 'caseId', 'duration']) {
      if (patch[k] !== undefined) clip[k] = k === 'notes' ? String(patch[k]).slice(0, 3000) : patch[k];
    }
    clip.updated = new Date().toISOString();
    write(all);
    return clip;
  },

  async observe(actor, clipId, {time, kind, notes = '', frame = null}) {
    admin(actor);
    const all = read(), clip = all.find(c => c.id === clipId);
    if (!clip) throw new Error('Video not found.');
    if (!Number.isFinite(time) || time < 0 || (clip.duration !== null && time > clip.duration + 0.2)) throw new Error('Select a valid time inside this video.');
    if (!findings.includes(kind)) throw new Error('Choose an observation type.');
    const observation = {id: newId(), time, kind, notes: String(notes).trim().slice(0, 1000), reviewer: actor.name || 'Admin', created: new Date().toISOString(), frameKey: null};
    if (frame) {
      observation.frameKey = 'video-frame-' + observation.id;
      await mediaDB.put(observation.frameKey, frame);
    }
    clip.observations.push(observation);
    clip.observations.sort((a, b) => a.time - b.time);
    clip.updated = new Date().toISOString();
    write(all);
    return observation;
  },

  async frame(actor, clipId, observationId) {
    admin(actor);
    const clip = read().find(c => c.id === clipId);
    const o = clip?.observations.find(x => x.id === observationId);
    if (!o) throw new Error('Observation not found.');
    return o.frameKey ? mediaDB.get(o.frameKey) : null;
  },

  async export(actor, clipId) {
    admin(actor);
    const clip = read().find(c => c.id === clipId);
    if (!clip) throw new Error('Video not found.');
    return {
      application: 'Namma CivicLens',
      reviewType: REVIEW_TYPE,
      exportedAt: new Date().toISOString(),
      clip: {id: clip.id, name: clip.name, type: clip.type, size: clip.size, duration: clip.duration, created: clip.created, reviewer: clip.reviewer},
      linkedCaseId: clip.caseId,
      overallFinding: clip.finding,
      reviewSummary: clip.notes,
      observations: clip.observations.map(({frameKey, ...o}) => ({...o, timestampLabel: formatTime(o.time), hasSavedFrame: !!frameKey})),
      locationEvidence: 'Video upload does not establish GPS location. Linked case location is separate and must not be inferred from this file.',
      publication: 'Footage is private. Linking to a case does not publish it or change complaint status.'
    };
  }
};

export function formatTime(t) {
  const x = Math.max(0, Math.floor(t || 0));
  return `${String(Math.floor(x / 60)).padStart(2, '0')}:${String(x % 60).padStart(2, '0')}`;
}

export const videoAnalysisAdapter = {
  async analyse() {
    return {available: false, mode: 'manual', reason: 'No dumping-action detection model or live CCTV integration is configured.'};
  }
};
