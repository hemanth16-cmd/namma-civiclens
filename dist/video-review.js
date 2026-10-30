// Admin video review desk: upload, playback controls, timestamped observations, saved frames,
// case linking, manual overall finding and JSON export. Footage never leaves this browser.
import {esc, mediaDB} from './services.js';
import {icon, btn, notice, panel, panelHead, pill, empty} from './ui.js';
import {videoReviewService as service, findings, playbackSupport, formatTime} from './video-service.js';

const save = (blob, name) => {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
};

/** Mounts the review desk into `root`. Returns a cleanup function that releases media and listeners. */
export async function mountVideoReview({root, actor, records, toast}) {
  let selected = sessionStorage.getItem('ncl-selected-video');
  let url = null;
  let disposed = false;
  let clip = null;
  const cleanup = () => {
    disposed = true;
    root.querySelector('video')?.pause();
    if (url) URL.revokeObjectURL(url);
    url = null;
  };

  async function paint({at = 0} = {}) {
    if (disposed) return;
    const clips = await service.list(actor);
    clip = clips.find(c => c.id === selected) || clips[0] || null;
    selected = clip?.id || null;
    if (selected) sessionStorage.setItem('ncl-selected-video', selected);
    if (url) { URL.revokeObjectURL(url); url = null; }
    if (clip) {
      try {
        const blob = await service.media(actor, clip.id);
        if (blob) url = URL.createObjectURL(blob);
      } catch (err) {
        toast(err.message, 'error');
      }
    }
    if (disposed) return;

    const complaintVideos = records.filter(r => r.mediaType === 'video');
    root.innerHTML = `
      ${notice('<strong>Manual review only.</strong> Record what you can see. No automatic dumping-action detector, live CCTV feed or face recognition is connected, and no model confidence is shown.', 'info', 'eye', 'video-notice')}
      <div class="video-layout">
        <aside class="panel video-library" aria-labelledby="library-title">
          ${panelHead(`<span id="library-title">Review library</span>`, pill(`${clips.length} ${clips.length === 1 ? 'clip' : 'clips'}`))}
          <label class="dropzone" id="dropzone" tabindex="0">
            ${icon('upload', 'dropzone-icon')}
            <strong>Upload a review video</strong>
            <small>MP4, WebM or H.264 MOV · up to 200 MB<br>Stored privately in this browser</small>
            <input id="video-file" type="file" accept="video/*,.mov,.mp4,.webm" hidden>
          </label>
          ${complaintVideos.length ? `<div class="field mt"><label for="complaint-video">Or review a complaint video</label><select id="complaint-video"><option value="">Choose a complaint…</option>${complaintVideos.map(r => `<option value="${esc(r.id)}">${esc(r.title)} · ${esc(r.id)}</option>`).join('')}</select></div>` : ''}
          <div class="clip-list" role="list">
            ${clips.length ? clips.map(c => `<button type="button" role="listitem" class="clip-item ${clip?.id === c.id ? 'is-selected' : ''}" data-clip="${esc(c.id)}" ${clip?.id === c.id ? 'aria-current="true"' : ''}>
              <span class="clip-icon">${icon('play', 'mini-icon')}</span>
              <span class="clip-body"><strong>${esc(c.name)}</strong><small>${c.duration === null ? 'Length not loaded' : formatTime(c.duration)} · ${(c.size / 1048576).toFixed(1)} MB</small><small>${esc(c.finding)} · ${c.observations.length} ${c.observations.length === 1 ? 'moment' : 'moments'}</small></span>
            </button>`).join('') : '<p class="micro">Uploaded review clips appear here.</p>'}
          </div>
        </aside>
        <section class="video-workspace" aria-label="Video review workspace">
          ${clip ? `
          <div class="panel player-panel">
            ${panelHead(`<span class="clip-title">${esc(clip.name)}</span>`, btn(icon('download') + '<span>Export review JSON</span>', 'id="export-review"', 'secondary', 'btn-sm'), 'Private admin review')}
            ${url ? `<video id="review-player" class="player" controls playsinline preload="metadata" src="${url}" aria-describedby="playback-note"></video>` : notice('The video file is not available on this device. Re-upload it to continue reviewing.', 'error', 'alert')}
            <div id="playback-error" class="hidden" role="alert">${notice('This browser could not decode this video. Try an H.264 MP4 or WebM file. Your notes and observations stay saved.', 'error', 'alert')}</div>
            ${playbackWarningFor(clip)}
            <div class="transport" role="group" aria-label="Playback controls">
              <button type="button" class="btn btn-secondary btn-sm" data-seek="-5" aria-label="Back 5 seconds">${icon('rewind')}<span>5 s</span></button>
              <button type="button" class="btn btn-primary btn-sm" id="play-pause" aria-label="Play or pause">${icon('play')}<span>Play</span></button>
              <button type="button" class="btn btn-secondary btn-sm" data-seek="5" aria-label="Forward 5 seconds">${icon('fastForward')}<span>5 s</span></button>
              <label class="speed">Speed <select id="video-speed"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label>
              <output class="timecode" id="time-readout" for="review-player" aria-live="off">00:00 / ${clip.duration === null ? '--:--' : formatTime(clip.duration)}</output>
            </div>
            <div class="timeline-track" aria-label="Saved observation markers">
              ${clip.observations.map(o => `<button type="button" class="pin" style="left:${clip.duration ? Math.min(98, o.time / clip.duration * 100) : 0}%" data-jump="${o.time}" aria-label="Jump to ${esc(o.kind)} at ${formatTime(o.time)}" title="${esc(o.kind)} · ${formatTime(o.time)}"></button>`).join('')}
            </div>
            <p id="playback-note" class="micro">Timeline pins are human observations, not automated detections. Footage has no verified GPS: any location comes only from a linked case.</p>
          </div>
          <div class="review-grid">
            <form class="panel form" id="observation-form" aria-labelledby="obs-title">
              ${panelHead(`<span id="obs-title">Mark this moment</span>`, pill('00:00', 'navy'))}
              <div class="field"><label for="observation-kind">What can you see?</label><select id="observation-kind">${findings.map(f => `<option>${esc(f)}</option>`).join('')}</select></div>
              <div class="field"><label for="observation-note">Observation note</label><textarea id="observation-note" rows="3" maxlength="1000" placeholder="Describe visible actions and the position in frame, for example: an object appears to drop beside the bins."></textarea></div>
              <label class="check"><input id="save-frame" type="checkbox" checked> <span>Save the paused frame with a label (clip, timestamp, manual review)</span></label>
              <div id="observation-error" role="alert"></div>
              <button class="btn btn-primary" type="submit">${icon('plus')}<span>Save timestamped observation</span></button>
              <p class="micro">An observation is a review lead, not a confirmed offence and not an identity match.</p>
            </form>
            <form class="panel form" id="finding-form" aria-labelledby="finding-title">
              ${panelHead(`<span id="finding-title">Review outcome</span>`)}
              <div class="field"><label for="video-case">Link to a civic case (optional)</label><select id="video-case"><option value="">No linked case</option>${records.map(r => `<option value="${esc(r.id)}" ${clip.caseId === r.id ? 'selected' : ''}>${esc(r.id)} · ${esc(r.area)}</option>`).join('')}</select><small class="micro">Linking does not publish footage, change status or add location evidence.</small></div>
              <div class="field"><label for="video-finding-select">Overall manual finding</label><select id="video-finding-select">${['Not reviewed', ...findings].map(f => `<option ${clip.finding === f ? 'selected' : ''}>${esc(f)}</option>`).join('')}</select></div>
              <div class="field"><label for="video-summary">Review summary</label><textarea id="video-summary" rows="4" maxlength="3000" placeholder="Summarise the visible evidence and what remains uncertain.">${esc(clip.notes)}</textarea></div>
              <button class="btn btn-primary" type="submit">${icon('check')}<span>Save review outcome</span></button>
              ${clip.caseId ? `<a class="inline-link" href="#/issue/${esc(clip.caseId)}">Open linked case ${icon('forward')}</a>` : ''}
            </form>
          </div>
          <section class="panel" aria-labelledby="moments-title">
            ${panelHead(`<span id="moments-title">Observed moments</span>`, pill(`${clip.observations.length} saved`))}
            ${clip.observations.length ? `<ol class="moment-list">${clip.observations.map(o => `<li class="moment">
              <button type="button" class="btn btn-secondary btn-sm" data-jump="${o.time}" aria-label="Return to ${formatTime(o.time)}">${icon('play', 'mini-icon')}<span>${formatTime(o.time)}</span></button>
              <div class="moment-body"><strong>${esc(o.kind)}</strong><p>${esc(o.notes || 'No additional note.')}</p><small>${esc(o.reviewer)} · recorded ${new Date(o.created).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'})} IST</small></div>
              ${o.frameKey ? `<button type="button" class="btn btn-ghost btn-sm" data-frame="${esc(o.id)}">${icon('download', 'mini-icon')}<span>Frame</span></button>` : '<span class="micro">No frame</span>'}
            </li>`).join('')}</ol>` : empty('No observations yet', 'Pause at a relevant moment, then save your first observation.')}
          </section>` : `
          <div class="panel video-welcome">
            ${icon('video', 'welcome-icon')}
            <p class="eyebrow">A closer look at the action</p>
            <h2>Watch, pause, record</h2>
            <p>Upload footage from a civic report or a recording. Play and seek, mark what you see, save a labelled frame and write a manual finding.</p>
            <ol class="steps"><li>Play and seek</li><li>Mark the moment</li><li>Save your finding</li></ol>
            <p class="micro">Works with local files only. No live feed, trained action detector or identity recognition is configured.</p>
          </div>`}
        </section>
      </div>`;
    wire();
  }

  function playbackWarningFor(c) {
    const p = playbackSupport({name: c.name, type: c.type});
    return p.supported === false || p.supported === null ? notice(esc(p.message), p.supported === false ? 'warn' : 'info', 'alert', 'mt') : '';
  }

  function wire() {
    const input = root.querySelector('#video-file');
    const drop = root.querySelector('#dropzone');
    const upload = async file => {
      if (!file) return;
      const support = playbackSupport(file);
      if (support.supported === false) toast(support.message, 'warn');
      toast('Saving private review video…');
      try {
        const c = await service.import(actor, file);
        selected = c.id;
        await paint();
        toast('Video ready for manual review.', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    };
    if (input) input.onchange = e => upload(e.target.files[0]);
    if (drop) {
      drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
      drop.ondragover = e => { e.preventDefault(); drop.classList.add('dragging'); };
      drop.ondragleave = () => drop.classList.remove('dragging');
      drop.ondrop = e => { e.preventDefault(); drop.classList.remove('dragging'); upload(e.dataTransfer.files[0]); };
    }
    root.querySelectorAll('[data-clip]').forEach(b => b.onclick = async () => { selected = b.dataset.clip; await paint(); });
    const complaint = root.querySelector('#complaint-video');
    if (complaint) complaint.onchange = async () => {
      if (!complaint.value) return;
      try {
        const r = records.find(x => x.id === complaint.value);
        const blob = await mediaDB.get(r.id);
        if (!blob) throw new Error('This complaint has no video file on this browser.');
        const file = new File([blob], `${r.id}.${blob.type.includes('mp4') ? 'mp4' : 'webm'}`, {type: blob.type || 'video/webm'});
        const c = await service.import(actor, file, {caseId: r.id});
        selected = c.id;
        await paint();
      } catch (err) {
        toast(err.message, 'error');
      }
    };
    if (!clip) return;
    wirePlayer();
  }

  function wirePlayer() {
    const player = root.querySelector('#review-player');
    const readout = root.querySelector('#time-readout');
    const playBtn = root.querySelector('#play-pause');
    if (player) {
      player.onloadedmetadata = async () => {
        if (disposed || !Number.isFinite(player.duration)) return;
        try { await service.update(actor, clip.id, {duration: player.duration}); } catch { /* keep playing */ }
        clip.duration = player.duration;
        readout.textContent = `${formatTime(player.currentTime)} / ${formatTime(player.duration)}`;
        root.querySelectorAll('.timeline-track [data-jump]').forEach(e => { e.style.left = Math.min(98, +e.dataset.jump / player.duration * 100) + '%'; });
      };
      player.ontimeupdate = () => {
        readout.textContent = `${formatTime(player.currentTime)} / ${formatTime(player.duration)}`;
        const mark = root.querySelector('#obs-title')?.closest('.panel-head')?.querySelector('.pill');
        if (mark) mark.textContent = formatTime(player.currentTime);
      };
      player.onplay = () => { playBtn.querySelector('span').textContent = 'Pause'; playBtn.querySelector('svg').innerHTML = icon('pause').replace(/^<svg[^>]*>|<\/svg>$/g, ''); };
      player.onpause = () => { playBtn.querySelector('span').textContent = 'Play'; playBtn.querySelector('svg').innerHTML = icon('play').replace(/^<svg[^>]*>|<\/svg>$/g, ''); };
      player.onerror = () => root.querySelector('#playback-error').classList.remove('hidden');
      playBtn.onclick = async () => {
        try { if (player.paused) await player.play(); else player.pause(); } catch { toast('Playback is unavailable for this file. Try an MP4 or WebM copy.', 'error'); }
      };
      root.querySelectorAll('[data-seek]').forEach(b => b.onclick = () => {
        const max = Number.isFinite(player.duration) ? player.duration : 0;
        player.currentTime = Math.max(0, Math.min(max, player.currentTime + Number(b.dataset.seek)));
      });
      root.querySelector('#video-speed').onchange = e => { player.playbackRate = Number(e.target.value); };
      root.querySelectorAll('[data-jump]').forEach(b => b.onclick = () => { player.currentTime = Number(b.dataset.jump); player.pause(); });
    }

    const obsForm = root.querySelector('#observation-form');
    obsForm?.addEventListener('submit', async e => {
      e.preventDefault();
      const err = root.querySelector('#observation-error');
      err.innerHTML = '';
      if (!player || player.readyState < 2) { err.innerHTML = notice('Wait until the video has a playable frame, then mark the moment.', 'error', 'alert'); return; }
      const submit = obsForm.querySelector('button[type=submit]');
      submit.disabled = true;
      try {
        player.pause();
        const at = player.currentTime;
        let frame = null;
        if (root.querySelector('#save-frame').checked) {
          if (!player.videoWidth) throw new Error('This frame could not be captured. Uncheck frame saving and save the observation, or convert the file.');
          frame = await frameWithLabel(player, clip, root.querySelector('#observation-kind').value, at);
        }
        await service.observe(actor, clip.id, {time: at, kind: root.querySelector('#observation-kind').value, notes: root.querySelector('#observation-note').value, frame});
        toast('Observation and timestamp saved.', 'success');
        await paint({at});
      } catch (error) {
        submit.disabled = false;
        err.innerHTML = notice(esc(error.message), 'error', 'alert');
      }
    });

    root.querySelector('#finding-form')?.addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const at = player?.currentTime || 0;
        await service.update(actor, clip.id, {
          caseId: root.querySelector('#video-case').value || null,
          finding: root.querySelector('#video-finding-select').value,
          notes: root.querySelector('#video-summary').value
        });
        toast('Manual review outcome saved.', 'success');
        await paint({at});
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    root.querySelector('#export-review')?.addEventListener('click', async () => {
      try {
        const report = await service.export(actor, clip.id);
        save(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}), `CivicLens-video-review-${clip.id}.json`);
      } catch (error) {
        toast(error.message, 'error');
      }
    });
    root.querySelectorAll('[data-frame]').forEach(b => b.onclick = async () => {
      try {
        const frame = await service.frame(actor, clip.id, b.dataset.frame);
        if (frame) save(frame, `CivicLens-review-frame-${b.dataset.frame}.jpg`);
        else toast('No frame was saved for this observation.', 'warn');
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  /** JPEG of the paused frame with a navy strip: clip name, video timestamp and the manual-review label. */
  async function frameWithLabel(player, c, kind, at) {
    const canvas = document.createElement('canvas');
    canvas.width = player.videoWidth;
    const strip = Math.max(72, Math.round(canvas.width * 0.075));
    canvas.height = player.videoHeight + strip;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(player, 0, 0, player.videoWidth, player.videoHeight);
    ctx.fillStyle = '#18324A';
    ctx.fillRect(0, player.videoHeight, canvas.width, strip);
    ctx.fillStyle = '#F07861';
    ctx.fillRect(0, player.videoHeight, Math.max(6, canvas.width * 0.008), strip);
    const fs = Math.max(13, Math.round(canvas.width * 0.02));
    const family = 'Inter, "Segoe UI", Arial, sans-serif';
    ctx.fillStyle = '#FFFFFF';
    ctx.font = `700 ${fs}px ${family}`;
    ctx.fillText(`NAMMA CIVICLENS · ${formatTime(at)} · MANUAL REVIEW`, fs, player.videoHeight + strip * 0.4, canvas.width - fs * 2);
    ctx.font = `500 ${Math.round(fs * 0.8)}px ${family}`;
    ctx.fillStyle = '#D9D9D2';
    ctx.fillText(`${c.name} · ${kind} · not AI-classified`, fs, player.videoHeight + strip * 0.76, canvas.width - fs * 2);
    return new Promise((resolve, reject) => canvas.toBlob(b => (b ? resolve(b) : reject(new Error('The frame could not be encoded.'))), 'image/jpeg', 0.92));
  }

  await paint();
  return cleanup;
}

export {btn, panel};
