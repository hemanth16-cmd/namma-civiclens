// Shared visual vocabulary: category symbols, resolution colours and the icon set.
// Green and red are the approved semantic colours for resolution state only.

export const RESOLVED_COLOR = '#21835B';
export const UNRESOLVED_COLOR = '#D34F48';
export const NAVY = '#18324A';

// One distinct line symbol per supported category (24×24 grid).
const CATEGORY_PATHS = {
  'Potholes': '<path d="M3 15l3-3 3 2 3-3 3 2 3-2 3 4M4 18h16M7 7h3m4-2h3"/>',
  'Road cracks and damage': '<path d="M5 3L3 21m16-18 2 18M12 3v4l-3 3 5 3-3 4 1 4"/>',
  'Garbage accumulation': '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
  'Illegal dumping': '<path d="M3 19h18M5 19l3-6 4 3 3-7 4 10M5 7l3-3 3 3-3 3zM17 3v3m-1-2h3"/>',
  'Drainage or waterlogging': '<path d="M2 14c3-4 5 4 8 0s5 4 8 0 4 0 4 0M2 19c3-4 5 4 8 0s5 4 8 0 4 0 4 0M8 3v6m4-7v6m4-5v6"/>',
  'Broken streetlights': '<path d="M6 21h8M10 21V7c0-3 2-4 5-4h3v5h-6M14 12l-1 2m5-2 1 2m-3-2v3"/>',
  'Damaged footpaths': '<path d="M3 21V3h18v18M3 9h18M3 15h18M9 3v6m6 0v6m-6 0v6M17 17l-2 2 2 2"/>',
  'Water leakage': '<path d="M12 2C9 7 5 11 5 15a7 7 0 0014 0c0-4-4-8-7-13zM9 14c-1 2 0 4 2 4"/>',
  'Other civic issues': '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>'
};
const FALLBACK = CATEGORY_PATHS['Other civic issues'];

/**
 * Resolution state. Green only when the administration has marked the case resolved AND no
 * citizen dispute or reinspection request is open. Verification is always shown separately.
 */
export function resolutionState(r) {
  const solved = r.status === 'Marked Resolved' && !['Disputed', 'Reinspection Required'].includes(r.verification);
  return {
    solved,
    color: solved ? RESOLVED_COLOR : UNRESOLVED_COLOR,
    label: solved ? 'Marked resolved' : (r.status === 'Marked Resolved' ? 'Resolved but disputed or awaiting reinspection' : 'Unresolved'),
    className: solved ? 'resolved' : 'unresolved'
  };
}

/** Plain-language origin of a location. GPS, photo metadata and manual placement are never blurred together. */
export function locationSourceLabel(gps) {
  if (!gps) return 'No location recorded';
  const type = gps.sourceType || (/EXIF/i.test(gps.source || '') ? 'exif' : /GPS/i.test(gps.source || '') ? 'gps' : /manual/i.test(gps.source || '') ? 'manual' : null);
  if (type === 'gps') return 'Device GPS at capture (browser location)';
  if (type === 'exif') return 'Photo file EXIF metadata (not the uploader’s current location)';
  if (type === 'manual') return 'Manually placed on a map (not a GPS reading)';
  return gps.source || 'Source not recorded';
}

export function categoryIcon(category) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${CATEGORY_PATHS[category] || FALLBACK}</svg>`;
}

/** Standalone marker SVG used by the Leaflet fallback and by image rasterisation for MapLibre. */
export function markerSVG(category, solved) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="88" height="88" viewBox="0 0 44 44"><circle cx="22" cy="22" r="20" fill="white" stroke="${solved ? RESOLVED_COLOR : UNRESOLVED_COLOR}" stroke-width="4"/><g transform="translate(10 10)" fill="none" stroke="${NAVY}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${CATEGORY_PATHS[category] || FALLBACK}</g></svg>`;
}

const I = {
  camera: '<path d="M4 7h3l2-3h6l2 3h3v12H4z"/><circle cx="12" cy="13" r="3.5"/>',
  flip: '<path d="M4 7h3l2-3h6l2 3h3v12H4z"/><path d="M8.5 13a3.5 3.5 0 016-2.4M15.5 13a3.5 3.5 0 01-6 2.4"/><path d="M14.8 8.6v2.2h-2.2M9.2 17.4v-2.2h2.2"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  forward: '<path d="M9 6l6 6-6 6"/>',
  pin: '<path d="M12 21s-6-5.4-6-10.5a6 6 0 0112 0C18 15.6 12 21 12 21z"/><circle cx="12" cy="10.5" r="2.2"/>',
  locate: '<circle cx="12" cy="12" r="7"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="1.5"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  bell: '<path d="M6 16V11a6 6 0 0112 0v5l2 2H4z"/><path d="M10 21h4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 00-2-1.2L14 3h-4l-.5 2.6a7 7 0 00-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 005 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 002 1.2L10 21h4l.5-2.6a7 7 0 002-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>',
  logout: '<path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4m0 3h.01"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-8 9"/>',
  video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/>',
  refresh: '<path d="M4 12a8 8 0 0114-5.3L20 9M20 4v5h-5M20 12a8 8 0 01-14 5.3L4 15M4 20v-5h5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 00-1-1H5a1 1 0 00-1 1v10a1 1 0 001 1h3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  play: '<path d="M7 5v14l12-7z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  rewind: '<path d="M11 6l-7 6 7 6zM20 6l-7 6 7 6z"/>',
  fastForward: '<path d="M13 6l7 6-7 6zM4 6l7 6-7 6z"/>',
  maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
  wrench: '<path d="M14.5 6.5a4 4 0 005.4 5.4l-8 8a2.1 2.1 0 01-3-3z"/>',
  file: '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>',
  star: '<path d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.3L12 16l-4.9 2.6 1.1-5.3-4-3.7 5.4-.6z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  home: '<path d="M4 11l8-7 8 7v9h-5v-6H9v6H4z"/>',
  dashboard: '<rect x="3" y="3" width="8" height="10" rx="1.5"/><rect x="13" y="3" width="8" height="6" rx="1.5"/><rect x="13" y="11" width="8" height="10" rx="1.5"/><rect x="3" y="15" width="8" height="6" rx="1.5"/>',
  message: '<path d="M4 5h16v11H9l-5 4z"/>',
  scale: '<path d="M12 4v16M6 8h12M4 12l2-4 2 4M16 12l2-4 2 4M8 20h8"/>',
  eye: '<path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>'
};

export function icon(name, cls = '') {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${I[name] || I.alert}</svg>`;
}
