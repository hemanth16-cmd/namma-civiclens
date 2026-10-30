// Burns the evidence panel (logo, complaint ID, evidence reference, address, coordinates and
// IST capture time) into the pixels of a photo, so the saved file itself carries the details.
//
// Privacy rules enforced here:
// - The private claim code is never an input. The "Evidence ref" row is a fingerprint of the
//   original file (SHA-256), which identifies the photo without granting any access.
// - Address and coordinates are drawn only when meta.includeLocation is true (the reporter's
//   explicit choice on the review screen).
// - A complaint ID is drawn only when the record exists; before submission the row says so.

const ZONE = 'Asia/Kolkata';
const MAX_CANVAS_PIXELS = 16777216; // iOS Safari's canvas limit; larger photos are scaled to fit

export const formatLat = lat => `${Math.abs(lat).toFixed(6)}° ${lat >= 0 ? 'N' : 'S'}`;
export const formatLng = lng => `${Math.abs(lng).toFixed(6)}° ${lng >= 0 ? 'E' : 'W'}`;

function istParts(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  }).formatToParts(d).map(p => [p.type, p.value]));
  return parts;
}

/** "30 Oct 2026 | 12:45 PM IST" */
export function formatIST(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return 'Not recorded in the file';
  const date = d.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: 'numeric', timeZone: ZONE});
  const time = d.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', hour12: true, timeZone: ZONE});
  return `${date} | ${time} IST`;
}

/** NCL_2026_ABC123_20261030_124500.jpg — public ID and IST time only, never the claim code or address. */
export function stampFileName(id, iso) {
  const safeId = String(id || 'NCL').toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'NCL';
  const p = istParts(iso || Date.now());
  const ts = p ? `${p.year}${p.month}${p.day}_${p.hour}${p.minute}${p.second}` : 'undated';
  return `${safeId}_${ts}.jpg`;
}

/** Non-secret reference derived from the original file: "EV-3F9A-21C0". Null if hashing is unavailable. */
export async function evidenceRef(blob) {
  try {
    const subtle = globalThis.crypto?.subtle;
    if (!subtle || !blob) return null;
    const digest = new Uint8Array(await subtle.digest('SHA-256', await blob.arrayBuffer()));
    const hex = Array.from(digest.slice(0, 4), b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
    return `EV-${hex.slice(0, 4)}-${hex.slice(4)}`;
  } catch {
    return null;
  }
}

/** The rows drawn on the photo, in order. Pure, so the privacy rules are unit-tested. */
export function stampRows({complaintId = null, evidenceRef: ref = null, includeLocation = false, gps = null, address = null, capturedAt = null} = {}) {
  const rows = [{label: 'Complaint ID', value: complaintId || 'Assigned when you submit', strong: !!complaintId}];
  if (ref) rows.push({label: 'Evidence ref', value: ref});
  if (includeLocation && gps && Number.isFinite(gps.lat) && Number.isFinite(gps.lng)) {
    rows.push({label: 'Location', value: address || 'Address not available · see coordinates', wrap: true});
    rows.push({label: 'Latitude', value: formatLat(gps.lat)});
    const acc = Number.isFinite(gps.accuracy) ? `±${Math.round(gps.accuracy)} m` : 'accuracy not stated';
    const source = gps.sourceType === 'exif' ? 'photo EXIF' : gps.sourceType === 'manual' ? 'placed on map' : 'device GPS';
    rows.push({label: 'Longitude', value: `${formatLng(gps.lng)}   ${acc} · ${source}`});
  } else {
    rows.push({label: 'Location', value: 'Not shown on this copy · kept with the complaint'});
  }
  rows.push({label: 'Captured', value: formatIST(capturedAt)});
  return rows;
}

/** Sizes for the panel, proportional to the photo's shorter side so it reads the same at any resolution. */
export function stampLayout(width, height) {
  const base = Math.min(width, height);
  const landscape = width > height * 1.15;
  const fs = Math.max(12, Math.round(base * (landscape ? 0.019 : 0.021)));
  const margin = Math.round(base * 0.03);
  const pad = Math.round(fs * 0.95);
  const panelWidth = landscape
    ? Math.min(width - margin * 2, Math.max(Math.round(width * 0.42), fs * 30)) // ~30 em fits the longest row
    : width - margin * 2;
  return {fs, margin, pad, panelWidth, landscape, lineHeight: Math.round(fs * 1.36), logoHeight: Math.round(fs * 1.9)};
}

/** Panel height for a number of text lines, using the same arithmetic as the renderer. */
export function stampPanelHeight(L, lines) {
  const gap = Math.round(L.fs * 0.75);
  return L.pad + L.logoHeight + gap + 1 + gap + lines * L.lineHeight + L.pad - Math.round(L.lineHeight - L.fs);
}

async function decode(blob) {
  if (typeof createImageBitmap === 'function') {
    try { return await createImageBitmap(blob, {imageOrientation: 'from-image'}); } catch { /* fall back to <img> */ }
  }
  const img = new Image();
  const url = URL.createObjectURL(blob);
  try {
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function wrapText(ctx, text, maxWidth, maxLines) {
  const words = String(text).split(/\s+/);
  const lines = [];
  let line = '';
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (ctx.measureText(next).width <= maxWidth || !line) line = next;
    else { lines.push(line); line = word; }
  }
  if (line) lines.push(line);
  if (lines.length > maxLines) {
    lines.length = maxLines;
    lines[maxLines - 1] += '…';
  }
  return lines.map(l => fit(ctx, l, maxWidth));
}

function fit(ctx, text, maxWidth) {
  let t = text;
  while (ctx.measureText(t).width > maxWidth && t.length > 4) t = t.slice(0, -2) + '…';
  return t;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else {
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }
}

const FONT = (weight, size) => `${weight} ${size}px Inter, "Segoe UI", Roboto, Arial, sans-serif`;

/**
 * Draw the photo at full resolution (capped at the browser canvas limit) with the evidence panel
 * near the bottom, and return a JPEG Blob. `logo` is an optional decoded image; without it the
 * panel shows the name only (a replacement logo is never invented).
 */
export async function renderStampedPhoto(blob, meta, {logo = null, quality = 0.92, maxSide = Infinity} = {}) {
  const source = await decode(blob);
  const srcW = source.width || source.naturalWidth;
  const srcH = source.height || source.naturalHeight;
  if (!srcW || !srcH) throw new Error('The photo could not be read.');
  // Full resolution for the saved file; previews pass maxSide. The panel scales with the image either way.
  const scale = Math.min(1, Math.sqrt(MAX_CANVAS_PIXELS / (srcW * srcH)), maxSide / Math.max(srcW, srcH));
  const W = Math.round(srcW * scale), H = Math.round(srcH * scale);
  if (globalThis.document?.fonts?.load) {
    await Promise.all([document.fonts.load(FONT(700, 20)), document.fonts.load(FONT(500, 20))]).catch(() => {});
  }
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(source, 0, 0, W, H);
  source.close?.();

  const L = stampLayout(W, H);
  const rows = stampRows(meta);
  const innerW = L.panelWidth - L.pad * 2;
  const labelSize = Math.round(L.fs * 0.8);
  ctx.font = FONT(500, labelSize);
  const labelW = Math.max(...rows.map(r => ctx.measureText(r.label).width)) + L.fs * 0.9;
  const valueW = innerW - labelW;

  // Measure every row first so the panel fits its content exactly.
  const laid = rows.map(r => {
    ctx.font = FONT(r.strong ? 700 : 600, L.fs);
    const lines = r.wrap ? wrapText(ctx, r.value, valueW, 2) : [fit(ctx, r.value, valueW)];
    return {...r, lines};
  });
  const headerH = L.logoHeight;
  const gap = Math.round(L.fs * 0.75);
  const panelH = stampPanelHeight(L, laid.reduce((sum, r) => sum + r.lines.length, 0));
  const x = L.margin, y = H - L.margin - panelH;
  const radius = Math.round(L.fs * 0.7);

  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.35)';
  ctx.shadowBlur = Math.round(L.fs * 1.2);
  roundRect(ctx, x, y, L.panelWidth, panelH, radius);
  ctx.fillStyle = 'rgba(12, 27, 43, 0.74)';
  ctx.fill();
  ctx.restore();
  ctx.save();
  roundRect(ctx, x, y, L.panelWidth, panelH, radius);
  ctx.clip();
  ctx.fillStyle = '#F07861';
  ctx.fillRect(x, y, Math.max(3, Math.round(L.fs * 0.2)), panelH);
  ctx.restore();
  ctx.lineWidth = Math.max(1, Math.round(L.fs * 0.05));
  ctx.strokeStyle = 'rgba(255,255,255,0.14)';
  roundRect(ctx, x, y, L.panelWidth, panelH, radius);
  ctx.stroke();

  // Header: logo at its own aspect ratio, then the name.
  let cursorX = x + L.pad;
  const headY = y + L.pad;
  if (logo && (logo.naturalWidth || logo.width)) {
    const lw = logo.naturalWidth || logo.width, lh = logo.naturalHeight || logo.height;
    const drawH = L.logoHeight;
    const drawW = Math.min(innerW * 0.45, drawH * (lw / lh));
    const fittedH = drawW / (lw / lh);
    ctx.drawImage(logo, cursorX, headY + (drawH - fittedH) / 2, drawW, fittedH);
    cursorX += drawW + L.fs * 0.7;
  }
  ctx.textBaseline = 'middle';
  ctx.fillStyle = '#FFFFFF';
  ctx.font = FONT(700, Math.round(L.fs * 1.08));
  const nameRight = x + L.panelWidth - L.pad;
  ctx.fillText(fit(ctx, 'NAMMA CIVICLENS', nameRight - cursorX), cursorX, headY + headerH * 0.28);
  ctx.font = FONT(500, Math.round(L.fs * 0.74));
  ctx.fillStyle = '#F7B3A5';
  ctx.fillText(fit(ctx, 'Geo-tagged civic evidence', nameRight - cursorX), cursorX, headY + headerH * 0.8);

  const divY = headY + headerH + gap;
  ctx.fillStyle = 'rgba(255,255,255,0.18)';
  ctx.fillRect(x + L.pad, divY, innerW, Math.max(1, Math.round(L.fs * 0.05)));

  // Rows: muted label column, high-contrast values.
  let rowY = divY + gap + L.fs * 0.55;
  ctx.textBaseline = 'middle';
  for (const r of laid) {
    ctx.font = FONT(500, labelSize);
    ctx.fillStyle = '#B9C6D3';
    ctx.fillText(r.label, x + L.pad, rowY);
    ctx.font = FONT(r.strong ? 700 : 600, L.fs);
    ctx.fillStyle = '#FFFFFF';
    r.lines.forEach((line, i) => ctx.fillText(line, x + L.pad + labelW, rowY + i * L.lineHeight));
    rowY += r.lines.length * L.lineHeight;
  }

  const out = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
  canvas.width = canvas.height = 0; // release the bitmap memory promptly on phones
  if (!out) throw new Error('The stamped photo could not be encoded.');
  return out;
}
