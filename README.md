# Namma CivicLens — Version 5.1

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
- Optional address lookup through Photon, disclosed, cached per session, coordinates kept on failure. **Implemented**; requires network access to the configured endpoint, which was blocked in the test sandbox, so **not verified live**.
- Evidence copy download with a navy and coral strip (public ID, category, capture time, location). It never includes the claim code. The original file is untouched. **Implemented and tested** in the browser suite.
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

Still needs your device: physical phone camera (rear and front lenses), the browser's real permission prompt and hardware denial, real GPS receiver outdoors, hardware video codecs (e.g. iOS HEVC/MOV), vector tile rendering with unconstrained network basemap access, live Photon geocoder lookup, and mobile Web Push on physical Android/iOS devices.

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
