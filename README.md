# Namma CivicLens — Version 5.2

A camera-first civic reporting and resolution platform for Bengaluru, built for Alliance One / SHASTRA 2026, CodeSangram 3.0 (problem CS/26-0012).

It features a native ES module browser frontend (no build step, no framework) paired with a high-performance, production-hardened FastAPI backend supporting both SQLite and PostgreSQL (Supabase), private evidence object storage (local or Supabase Storage), in-memory sliding-window abuse protection, scrypt-hashed secrets, staff operations with access audit logging, and W3C/VAPID Web Push notifications.

## Status labels

Every feature below carries one of four labels:

- **Implemented and tested**: code exists and automated tests (Node unit tests, Playwright browser journeys, Pytest backend suites, concurrency stress harnesses) cover it.
- **Implemented; device/browser verification pending**: code exists but has not been exercised on real phones, physical cameras, or cellular GPS hardware.
- **Illustrative demo**: shows the intended interface using sample or assumed values. Not a measurement.
- **Proposed; not connected or trained**: designed and documented, but no service, model or integration is running.

## Start the app

Requirements:
- Frontend: Node 20 or later (tested on Node 22 and 25). No installation step for the static server.
- Backend: Python 3.11 or later (tested on Python 3.14.3).

### Frontend

```sh
cd namma-civiclens
npm run dev        # serves dist/ at http://localhost:4173
npm test           # 125 Node unit tests across all services, security & push
npm run check      # syntax check over all 45 source, script and test files
npm run e2e        # browser smoke suite (needs Playwright and Chromium)
npm run e2e:camera # camera and location journeys (needs Playwright and Chromium)
npm run e2e:photo  # automatic address, photo stamp and saving journeys (needs Playwright and Chromium)
npm run brand -- path/to/official-logo.png   # install the official logo and regenerate every icon
```

### Backend

```sh
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m app                     # runs API on http://127.0.0.1:8000
.venv/Scripts/pytest                            # runs 330 backend tests (SQLite)
CIVICLENS_TEST_DATABASE=postgres .venv/Scripts/pytest  # runs 324 backend tests (PostgreSQL)
```

### Load and stress testing

```sh
# Run full suite of concurrency workloads and burst stress tests
python scripts/load-stress-harness.py --all

# Run 5-minute sustained load test
python scripts/load-stress-harness.py --sustained --duration 300
```

### Camera and location need a secure page

Browsers only allow the camera and location on HTTPS pages or on `localhost`. On the computer running `npm run dev`, open http://localhost:4173. The server also prints a network address such as `http://192.168.1.20:4173`. A phone opening that plain `http://` address is not on a secure page, so the browser blocks the camera and location, and the app says so instead of showing a blank preview. For phone testing, serve `dist/` over HTTPS (any HTTPS static host, or an HTTPS tunnel). The app does not try to bypass browser permissions.

The app opens on the camera. Close (×) to reach the three-choice entry: track a report by problem ID, log in as a user, or log in as an admin. Camera, location and recording need HTTPS or `localhost` and compatible hardware. Vector map tiles (OpenFreeMap) and optional address lookup (Photon) need network access. The reporting, tracking and workspace features do not need an API key.

To run the browser smoke suite, set `BASE` to the running server and, if Playwright is not resolvable from the project, point `PLAYWRIGHT_PATH` at it:

```sh
BASE=http://localhost:4173 SHOTS=./screenshots npm run e2e
BASE=http://localhost:4173 INSECURE_BASE=http://<your-lan-ip>:4173 SHOTS=./screenshots npm run e2e:camera
```

To deploy, copy `dist/` to any HTTPS static host.

## Demo accounts

These are demonstration credentials only for the local browser prototype mode. They are shown in the sign-in forms and offer no production security. Never reuse a real password here.

- Resident: `resident@civiclens.demo` / `CivicDemo2026`
- Official (admin): `official@civiclens.demo` / `CivicDemo2026`
- Public reference for tracking: `NCL-2026-DEMO01`
- Illustrative private claim code for that case: `DEMO-CLAIM-01` (for testing the claim flow as a signed-in resident)

In backend mode, staff accounts are managed via the CLI (`python -m app.staff create`) using scrypt password hashing and server-side bearer sessions.

Eight sample cases (`NCL-2026-DEMO01` to `DEMO08`) are seeded across Bengaluru neighbourhoods. Their positions are approximate neighbourhood points, not verified locations. Residents can register additional local profiles from the sign-in screen.

## What changed in version 5.2: automatic address, stamped photos, saving, logo

**Why the address needed the Search button.** Reverse geocoding was wired only to the "Look up street address" button. The result was also written onto the fix object itself, and the location service replaces that object every time a refined fix arrives, so the address vanished on the next refinement. A lookup was also discarded if the fix improved while it was running. Together, these meant the normal flow never showed an address.

**Fix.** `dist/address-service.js` resolves the address automatically whenever the camera screen has a current fix. It waits 700 ms for a refining fix to settle, reuses any address already found within 30 m (no request), shares one in-flight request between the camera, review and submit steps, cancels a request made stale by a move, backs off for 30 s after a failure, and retries by itself when the browser comes back online. Addresses are kept by position, not on the fix object. The manual button remains as **Retry address / Refresh address**.

**Photo stamp.** Every photo is saved as a stamped JPEG (`dist/photo-stamp.js`). The panel is drawn into the pixels with Canvas at the photo's own resolution (scaled only above the 16.7 MP browser canvas limit), sized from the photo's shorter side, bottom-left on landscape and full-width on portrait. It shows the logo, complaint ID, an evidence reference, the address, latitude, longitude with accuracy and source, and the capture time in IST. The review screen shows a preview; the final stamp is drawn after submission with the ID of the record that now exists.

- **Private ID.** The app's private identifier is the claim code, a secret that lets someone take ownership of a report. It is never drawn on a photo or used in a file name. The stamp shows an **Evidence ref** instead (`EV-XXXX-XXXX`, the first 32 bits of the SHA-256 of the original file), which identifies the photo without granting access. It is stored on the record and shown on the receipt.
- **Location consent.** Address and coordinates appear on the photo only when the reporter ticks "Show the address and GPS coordinates on the saved photo". The choice is remembered on the device. Otherwise the stamp says the location is kept with the complaint.
- **File name.** `NCL_2026_ABC123_20261030_124500.jpg`: public ID and IST capture time only.

**Saving to the device** (`dist/photo-save.js`). Browsers cannot silently write to Photos or Gallery, so the app uses the best supported flow and reports only what the browser confirms:

| Platform | Behaviour |
| --- | --- |
| Android Chrome, installed PWA | Share sheet with the JPEG; choose Gallery, Photos or Files. Falls back to a download if sharing fails. |
| iPhone and iPad Safari, home-screen app | Share sheet; choose **Save Image** to put it in Photos. |
| Desktop Chrome, Edge, Firefox | Download starts automatically on submit. |
| Very old browsers | Opens the image in a new tab to press and hold. |

Saving starts automatically after submitting a live-camera photo (toggle on the review screen). If the browser refuses because too much time passed since the tap, the receipt asks for one tap on **Save to Photos**. Each complaint is saved automatically at most once per session, and concurrent saves collapse into one. The stamped copy is also stored in IndexedDB beside the untouched original (key `<ID>:stamped`), so the receipt can offer it again.

**Camera.** Where the browser supports `ImageCapture` (Chrome on Android and desktop), the shutter takes a full-sensor still and falls back to the preview frame if that is slower than 3.5 s, unsupported, or not sharper. Safari uses the preview frame (up to the 1920×1080 stream). A capture lock stops double taps producing two photos, and the fix is copied at the moment of the press.

**Location card.** A Detecting / Located / Needs attention line, an automatically filled address card labelled *approximate* (separate from the GPS coordinates and accuracy), and the address under the coordinates in the viewfinder.

**Logo.** One asset, `dist/assets/logo.svg`, referenced from `dist/brand.js`, feeds the navbar, entry and sign-in screens, the camera chip and viewfinder, the photo stamp, a loading splash, the favicon and the install icons in `manifest.webmanifest`. No official logo file was supplied with this version, so the asset is the app's existing mark (coral tile, navy pin), not a new design. To install the official logo, run `npm run brand -- path/to/logo.png` (PNG, SVG, JPEG or WebP). It copies the file unchanged, updates the path, and regenerates the 32, 180, 192, 512 and maskable 512 px icons, fitted without stretching or cropping.

**Configuration.** No new environment variables or API keys. The geocoder endpoint stays in `dist/map-config.json` (public Photon, free; self-host for production volume).

## What changed in version 5.1: camera and location fixes

Causes found and fixed:

- **Blank preview with permission granted.** The camera state panel (`.viewfinder-state`) is an absolutely positioned layer with an opaque gradient above the video. When the camera went live, the code emptied the panel's contents but left the panel itself in place, so it covered a video that was actually playing. The chip said "Live" while the preview looked blank. The panel is now hidden when the preview is live, and the video has an explicit lower stacking layer.
- **No real readiness check.** "Live" was set as soon as a stream existed, `play()` failures were swallowed, and the shutter was active before any frame arrived. The preview is now reported live only after the mounted `<video>` is playing decoded frames; blocked autoplay shows a "Show camera preview" button; a stream with no frames shows a recoverable state.
- **Stale asynchronous starts.** A `getUserMedia` call that finished after the user left the screen kept its stream running, which held the camera and made the next attempt fail as "No camera found". Every start now has a token: late streams stop themselves, concurrent starts share one request, and leaving always stops every track.
- **Camera did not start on opening.** First visits waited for an "Enable camera" press even when permission was already granted. The camera now starts when the screen opens. A denial is remembered for the session so reopening does not prompt again; Retry camera asks explicitly.
- **Misleading error mapping.** "Device busy" and "constraints not supported" were both reported as "No camera found". Busy, missing, denied, disconnected, no frames, insecure and unsupported now each have their own message and next action. Unsupported constraints fall back to any available camera.
- **Switching.** Switching only flipped a facing-mode hint, which desktops ignore. It now cycles real camera devices, stops the old stream first, restores the previous camera if the new one fails, mirrors the front-camera preview, and is disabled with an explanation when only one camera exists.
- **Location requested only after the camera worked.** If the camera failed, location was never requested. Location now starts with the screen, independently of the camera.
- **Possibly stale or poor fixes.** `maximumAge: 30000` allowed a cached fix, there was no refinement, and an old fix stayed attached to new captures indefinitely. Fixes are now fresh (`maximumAge: 0`), a poor first fix is refined for up to 15 seconds, captures need a fix from the last 2 minutes, and each photo stores the fix it was taken with and how many seconds before capture it was obtained.
- **Location states.** Locating, improving accuracy, acquired, low accuracy, approximate, too old, blocked, unavailable, timed out and needs HTTPS are shown separately, with coordinates, accuracy in metres, fix time and a working Retry location button. Watchers stop when finished or when the screen is left.
- **Location sources.** Every displayed location names its source: device GPS at capture, photo file EXIF, or (for future use) manual map placement. The app has no manual map picker, so no location is ever placed by hand. Gallery uploads keep the existing EXIF rules and never use the device's current location.
- **Gallery rejection dialog.** Its "Choose another photo" button did not close the dialog; all close buttons now work.

## What changed in version 5

- **Camera-first journey**: capture → review → category → confirm location → submit → receipt → track. The shutter is above the fold at 1280×800 and reachable at phone width. Back uses an in-app history with a safe fallback.
- **Gallery evidence**: only JPEG files with valid EXIF GPS are accepted. Missing, malformed or non-JPEG files are rejected with a specific reason. The device's current location is never substituted for an image's original location. Arbitrary uploaded video is not labelled as geo-tagged.
- **Public ID and claim code are separate.** The claim code is shown once, to the submitting browser, and is excluded from shared evidence and public projections.
- **Projections by role.** Public, community, owner and official views are built by separate functions, and restricted fields are absent from the others (tests cover this).
- **Lifecycle and verification are independent.** Lifecycle (Submitted → Under Review → Assigned → In Progress → Marked Resolved) and verification (Pending Verification, Citizen Confirmed, Disputed, Reinspection Required) are separate fields. Each has its own history. Reviews never change either status.
- **Repair history is append-only.** Earlier repair records are never overwritten.
- **Before/after evidence** shows capture time and upload time separately, and states "No GPS metadata supplied" instead of filling a missing location.
- **City map**: MapLibre with OpenFreeMap vector styles, falling back automatically to Leaflet with OpenStreetMap raster tiles. Each category has its own symbol. Green means administratively marked resolved with no open dispute; red means unresolved, disputed or awaiting reinspection; clusters are neutral. The map and the list read from one filter path.
- **Admin video review**: admin-only, up to 200 MB, timestamped manual observations, saved frames labelled as manual review, case linking that does not publish footage or change status, and JSON export labelled "Manual administrator review. Not AI detection."
- **Intelligence studio**: the priority formula is explained with visible contributions. The other panels are labelled as demos or proposals.
- **Backend-readiness documentation**: `docs/ARCHITECTURE.md` and `docs/api-contract.md`.

## Feature status

### Reporting and evidence

- Camera lifecycle (`dist/camera-service.js`): starts on opening, one stream at a time, frame-ready gating, stops on leaving or when the page is backgrounded, restarts on return, rear camera preferred with fallback to any camera, device switching, and distinct recovery states. **Implemented and tested** in Node and in headless Chromium with a synthetic camera. **Implemented; device/browser verification pending** on real phones and real permission prompts.
- Location (`dist/location-service.js`): fresh high-accuracy fixes, bounded refinement, freshness rule for captures, separate status states, Retry location. **Implemented and tested** with real browser permissions and injected positions. **Device verification pending** for a real GPS receiver.
- Photo capture with a frame-ready wait. Photo-mode press-and-hold recording and accessible tap-to-start video mode, capped at 30 seconds. **Implemented; device/browser verification pending** for real camera pipelines and codecs.
- Live capture requires device GPS with accuracy and fix time. **Implemented and tested** (flow); **device verification pending** (real GPS).
- Gallery JPEG EXIF GPS parsing, capture-time reading (as IST when present), and rejection reasons. **Implemented and tested** with synthetic JPEG fixtures. Real phone photos **device verification pending**.
- Automatic address lookup through Photon: debounced, cached by position, cancellable, offline-aware, disclosed, coordinates kept on failure. **Implemented and tested** in Node and in headless Chromium against a mocked Photon. The live Photon service was blocked in the test sandbox, so **not verified live**.
- Stamped photo: logo, public ID, evidence reference, IST capture time and (with consent) address and coordinates drawn into the JPEG. Never the claim code. The original file is untouched. **Implemented and tested** (unit rules and pixel checks on the downloaded file).
- Saving: share sheet on phones, download on desktop, honest outcome messages, one automatic save per complaint. **Implemented and tested** with a stubbed share sheet; **device verification pending** for real Android and iOS share targets.
- Private receipt text file with the claim code, separate from the evidence copy. **Implemented and tested**.

### Backend, storage, and push notifications

- FastAPI service (`backend/app/main.py`): asynchronous endpoints, lifespan schema bootstrap, OpenAPI generation at `/api/v1/openapi.json`. **Implemented and tested** (Pytest + live E2E).
- Pluggable databases (`backend/app/db.py`): SQLite WAL mode with `BEGIN IMMEDIATE` serialization, and PostgreSQL with `psycopg-pool` connection pooling, advisory migration locks, and row-level locking. **Implemented and tested** on both backends.
- Evidence storage (`backend/app/evidence_storage.py`, `backend/app/supabase_storage.py`): local atomic file storage and private Supabase Storage adapter with chunked streaming, SHA-256 integrity verification, 2 GB quota with reservations, and access audit logging. **Implemented and tested**.
- Abuse protection (`backend/app/ratelimit.py`): in-memory sliding-window limiter with memory bounding (50,000-key LRU cap), IP extraction behind trusted proxies, and `429` / `Retry-After` responses. **Implemented and tested**.
- Web Push notifications (`backend/app/push.py`, `backend/app/notifications.py`, `dist/sw.js`): RFC 8291/8292 standard VAPID Web Push, Service Worker delivery, unread tracking, automatic device pruning on 410 Gone and consecutive delivery failures. **Implemented and tested** (verified with real WNS push delivery on Windows).

### Tracking and workspaces

- Public tracking by problem ID with an allowlisted projection. **Implemented and tested**.
- Resident overview, owned complaints, community issues with persistent reviews, claim by private code, followed issues, notification preferences. **Implemented and tested** (service tests and browser checks).
- Admin overview, incident management with priority ordering, lifecycle and verification updates, internal notes, reinspection requests, work orders with overdue flags. **Implemented and tested**.
- Case detail with before/after comparison, enlargement and a range slider, repair history and verification history. **Implemented and tested** for the logic; **device verification pending** for layout on real photos.
- Staff authentication & console: scrypt-hashed passwords, session revocation, staff-only evidence streaming. **Implemented and tested**.

### Map

- Vector map with category symbols, clusters, popups, fit-to-reports, My location, and map-style toggle. **Implemented; tested in headless Chromium** (WebGL present there). Tile-level rendering **not verified** because the basemap is blocked in the sandbox.
- Leaflet fallback after three early load errors or ten seconds without a load. **Implemented and tested**: the browser suite and a targeted check confirmed eight plotted markers, symbols and cluster in the fallback.
- Filters for search, category, lifecycle, colour, verification and neighbourhood. The list and the markers always agree. **Implemented and tested**.

### Video review (admin only)

- Upload or select a clip up to 200 MB, native playback with ±5 s, speed, timecode, timeline pins. **Implemented and tested** (service and browser). Codec coverage **device verification pending**; MOV may need conversion to H.264 MP4 or WebM.
- Manual observations with a frame saved and labelled, observed-moment list, manual finding and summary, JSON export, frame download. **Implemented and tested**.
- No automated dumping-action detection, face recognition, live CCTV or model confidence output. The adapter reports unavailable. **Proposed; not connected**.

### Intelligence studio (admin)

- Priority score: `100 × (0.35·severity + 0.30·exposure + 0.20·unresolved + 0.15·recurrence)`, with separate evidence confidence and a "Verify evidence first" flag. **Implemented and tested**. Inputs are illustrative defaults.
- Priority queue, relationship graph, cognitive map (fuzzy cognitive mapping with documented assumed weights), and seeded 1,000-draw scenario lab. **Illustrative demo**. These are not causal or predictive claims.
- Hotspot outlook (30-day ward/category activity versus baseline). **Proposed; not connected or trained**.
- Road-damage and waste detection from images. **Proposed; not connected or trained**.

### Accessibility and layout

- Skip link, aria-current, status announcements, `<dialog>`-based modals with Escape, focus-visible rings, 44 px touch targets, reduced-motion support, labelled filters. **Implemented and tested** in the suite where automated.
- No horizontal scroll at 375 px on the tested routes, and at 320 px on the camera screen (0 px overflow). Other screens at 320 px are not checked. Layout on physical phones: **device verification pending**.

## Architecture

```
dist/index.html          Entry document; loads fonts, map CSS and vendored scripts
dist/style.css           Brand tokens, components, responsive layouts
dist/app.js              Route table, role guards, delegated actions, error recovery
dist/router.js           Hash router, route history for Back, safe fallbacks
dist/state.js            Session, viewer, recording state, per-user preferences
dist/camera-service.js   Camera stream lifecycle, readiness, switching, error states
dist/location-service.js Device location: fresh fixes, refinement, timeouts, freshness
dist/services.js         Data layer and projections (mirrors the API contract)
dist/api-client.js       Hardened fetch client, ApiError, idempotency keys, safe URL handling
dist/report-gateway.js   Dual-mode gateway: routes submit/track to local store or backend API
dist/push-client.js      VAPID subscription management, citizen inbox tokens, click handling
dist/sw.js               Service Worker: push payload parsing, system notifications, deep routing
dist/filters.js          One filter and sort path for lists, tables and maps
dist/exif.js             JPEG EXIF GPS and capture-time parser with rejection reasons
dist/presentation.js     Category symbols, colour rules, resolution state, icons
dist/ui.js               Shared components: pills, dialogs, shell, toast, lightbox
dist/video-service.js    Admin-only video library, observations, export
dist/video-review.js     Video review UI
dist/address-service.js  Automatic reverse geocoding: debounce, position cache, cancellation, offline retry
dist/photo-stamp.js      Evidence stamp drawn into the photo; file names; evidence reference
dist/photo-save.js       Share-sheet or download saving with de-duplication and honest outcomes
dist/brand.js            The single logo path used everywhere
dist/assets/             Logo and the icons generated from it
dist/manifest.webmanifest Install manifest
dist/views/              Capture, geo lookup, public, workspace, issue, map, map page, intelligence, staff, notifications
dist/map-config.json     Geocoder endpoint configuration
dist/fonts/, fonts.css   Bundled Inter (no external font requests)
dist/*.js, *.css         Vendored MapLibre GL JS 5.6.2, Leaflet 1.9.4, Leaflet.markercluster 1.5.3
backend/                 FastAPI application package (see backend/README.md)
scripts/load-stress-harness.py Load testing and sustained stress execution CLI
scripts/serve.mjs        Dependency-free static server
scripts/check.mjs        Syntax check over source, scripts and tests
scripts/browser-smoke.mjs Headless-browser journey checks
scripts/camera-location-e2e.mjs Camera and location journeys with a synthetic camera
scripts/backend-e2e.mjs  Full integration journey against live FastAPI service
scripts/notifications-e2e.mjs End-to-end Web Push journey with real WNS delivery
tests/*.test.js          Node unit tests (services, EXIF, video, security gateway, notifications)
docs/ARCHITECTURE.md     Module status, data model, visibility rules, production direction
docs/api-contract.md     FastAPI endpoint contract and schema documentation
docs/IMPLEMENTATION_PROGRESS.md Chronological engineering progress and audit record
```

## Validation & Test Results

Results recorded on 30 October 2026 on branch `develop`:

- `npm test`: **125 of 125 pass**. Covers camera lifecycle, location refinement, EXIF parsing, video observations, API integration, evidence upload retry, idempotency handling, staff console, Web Push encryption & delivery, and security gateway URL validation.
- `npm run check`: **Syntax check passes for 45 files**.
- `npm run e2e`: **56 of 56 checks pass**, including viewport checks, role guards, and visible top-layer video preview.
- `npm run e2e:camera`: **45 of 45 checks pass**. Includes synthetic camera frame capture, 30s video recording, permission denial/recovery, device switching, location refinement, and responsive mobile layouts (320px/375px).
- `scripts/backend-e2e.mjs`: **66 of 66 checks pass** against live uvicorn server on port 8010.
- `scripts/notifications-e2e.mjs`: **36 of 36 checks pass** with live browser service worker and real WNS push delivery on Windows.
- Pytest (SQLite): **330 of 330 pass**.
- Pytest (PostgreSQL): **324 of 330 pass** (6 SQLite-only PRAGMA tests safely skipped via `@pytest.mark.sqlite_only`).
- 5-minute sustained load test (`scripts/load-stress-harness.py`):
  - **29,527 requests completed with 0 errors (100% success rate)**.
  - Sustained throughput: **98.4 requests/second**.
  - Latencies: p50 = 24.56 ms, p90 = 74.07 ms, p95 = 145.47 ms, p99 = 546.82 ms.
  - Peak resident memory: 47.5 MB.
  - Zero database lock errors, zero connection exhaustion events, zero data corruption.

Version 5.2 frontend additions (recorded on `main` before integration): `npm test` **61 of 61 pass** (20 new: resolver debounce, caching, cancellation, back-off, offline, bounded wait; stamp rows, consent, no claim code, file names, layout proportions, evidence reference; save method, share outcomes, de-duplication). `npm run e2e:photo` **45 of 45 pass**. The earlier suites still pass unchanged.

Still needs your device: physical phone camera (rear and front lenses), the browser's real permission prompt and hardware denial, real GPS receiver outdoors, hardware video codecs (e.g. iOS HEVC/MOV), vector tile rendering with unconstrained network basemap access, live Photon geocoder lookup, mobile Web Push on physical Android/iOS devices, and real share sheets for stamped photos (Save Image on iOS, gallery targets on Android).

## Limits

This is a municipal civic reporting system designed for high integrity and resilience.

- Local storage mode is self-contained within a single browser profile. Backend mode provides multi-user persistence via PostgreSQL and Supabase Storage.
- Passwords in the local demonstration mode are fixed values; backend mode uses scrypt password hashing and server-side bearer session revocation.
- The map basemap (OpenFreeMap, OpenStreetMap) is fetched over the network. Sample incident positions are approximate.
- Address lookup uses the public Photon demo endpoint in `dist/map-config.json`. It has no uptime guarantee. Self-host it for municipal production use.
- EXIF data can be altered and is never cryptographic proof. A repair photo alone does not prove a repair.
- The seeded activity, priority inputs, cognitive weights, scenario assumptions and forecast areas are illustrative.
- Admin video review is manual. No model output, detection or confidence value is produced.

## Remaining integrations

Items not integrated or requiring external municipal provisioning:

- Municipal routing and authoritative ward boundary shapefiles.
- Self-hosted geocoding (Photon) and reverse-geocoding policy.
- Production-trained road-damage and waste-detection computer vision models with evaluation weights and human review.
- Automated duplicate and recurrence matching with official workflow approval.
- Hotspot forecasting against historical municipal baseline.
- Real device testing matrix: physical Android and iOS hardware testing across low-end devices and various mobile webviews.
- Live Supabase project credentials: the adapter is tested end-to-end with mock/fake Supabase APIs; connecting to a real project is blocked on provisioning production credentials.

## Third-party notice

Vendored libraries: Leaflet 1.9.4 (BSD-2-Clause), MapLibre GL JS 5.6.2 (BSD-3-Clause), Leaflet.markercluster 1.5.3 (MIT), and Inter (SIL Open Font License). Licence files are at the project root. Map data © OpenStreetMap contributors; vector tiles by OpenFreeMap. Address lookup uses Photon, which uses OpenStreetMap data. The public map and geocoding services have their own usage and availability policies.

## Version history

- Version 2: camera-first entry with three-choice navigation, self-hosted Inter, role-separated navigation, disclosed address lookup, vector map with Leaflet fallback.
- Version 3: category symbols, community issues with persistent reviews, before/after evidence, explicit sharing controls.
- Version 4: admin video review with observations, frames, case linking and JSON export.
- Version 5.2: automatic address, stamped photos with consent-based location, device saving, central logo with favicon and install icons, full-resolution stills where supported, 20 new unit tests and a 45-check photo browser suite.
- Version 5.1: camera preview and location fixes (see above), 18 new unit tests and a 48-check camera and location browser suite.
- Version 5: consolidated design system; public ID and private claim code separated; role projections built separately; append-only repair and verification history; independent lifecycle and verification; EXIF rejection without location substitution; map and list driven by one filter path; accessible dialogs and focus handling; intelligence studio with explained priority and labelled demos; backend-readiness documents; 23 unit tests and a 55-check browser suite.
