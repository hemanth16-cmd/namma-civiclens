// One place for the Namma CivicLens logo. Every screen, the photo stamp, the favicon and the
// install icons derive from LOGO_SRC. `npm run brand -- <official-logo-file>` replaces the asset,
// updates this path and regenerates the icons; nothing else needs editing.
export const BRAND_NAME = 'Namma CivicLens';
export const LOGO_SRC = 'assets/logo.svg';

let logoPromise = null;

/** Decoded logo for canvas drawing. Resolves to null (never throws) if the asset is missing. */
export function loadLogoImage() {
  logoPromise ??= new Promise(resolve => {
    if (typeof Image === 'undefined') return resolve(null);
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => resolve(img.naturalWidth ? img : null);
    img.onerror = () => { logoPromise = null; resolve(null); };
    img.src = LOGO_SRC;
  });
  return logoPromise;
}
