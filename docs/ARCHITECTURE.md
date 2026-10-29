# Namma CivicLens — Architecture (version 5)

This document describes the browser prototype as it runs today, the data contract it already follows, and the production direction. Status labels used throughout:

- **Implemented and tested** — code exists and automated tests cover it.
- **Implemented; device/browser verification pending** — code exists, but the behaviour has not been exercised on real hardware.
- **Illustrative demo** — shows the intended interface using sample or assumed values. Not a measurement.
- **Proposed; not connected or trained** — designed, not running. No model, service or integration is wired in.

## 1. Current runtime (frontend prototype)

Native ES modules, no build step, no runtime dependencies. `scripts/serve.mjs` serves `dist/`.

| Module | Responsibility | Status |
| --- | --- | --- |
| `dist/app.js` | Route table, role guards, delegated actions, focus management, error recovery | Implemented and tested (guards and routing covered by browser smoke tests) |
| `dist/router.js` | Hash router, in-app history for Back, safe fallbacks for direct links | Implemented and tested |
| `dist/state.js` | Session, viewer identity, camera state, per-user preferences and read state | Implemented and tested |
| `dist/services.js` | Data layer and projections; mirrors the planned API (see `api-contract.md`) | Implemented and tested (Node tests) |
| `dist/filters.js` | Single filter and sort path used by lists, tables and maps | Implemented and tested |
| `dist/exif.js` | JPEG EXIF GPS and capture-time parser with explicit rejection reasons | Implemented and tested (unit fixtures) |
| `dist/presentation.js` | Category symbols, marker colours, resolution state, icon set | Implemented and tested |
| `dist/camera-service.js` | Camera stream lifecycle: one stream, tokens against stale starts, frame-ready gating, switching, error states | Implemented and tested (Node fakes and headless Chromium synthetic camera); real phone cameras pending |
| `dist/location-service.js` | Device location: fresh fixes, bounded refinement, timeouts, freshness rule, no default coordinates | Implemented and tested (Node fakes and browser permissions); real GPS receiver pending |
| `dist/views/capture.js` | Camera screen UI, review, submit, receipt, evidence copy | Implemented and tested in headless Chromium; device verification pending for real camera and GPS hardware |
| `dist/views/public.js` | Three-choice entry, public tracking, sign-in | Implemented and tested |
| `dist/views/workspace.js` | Overview, complaint lists, community, claim, notifications, preferences, work orders | Implemented and tested |
| `dist/views/issue.js` | Case detail, before/after, repair and verification history, reviews, admin actions | Implemented and tested |
| `dist/views/map.js`, `map-page.js` | MapLibre vector map with automatic Leaflet fallback, clusters, popups, filters | Implemented and tested in headless Chromium (WebGL available there); tile-level verification pending |
| `dist/views/intelligence.js` | Explainable priority, graph, cognitive map, scenario lab, model readiness | Priority formula implemented and tested; other panels are illustrative demos |
| `dist/video-service.js`, `video-review.js` | Admin-only footage review, observations, frames, export | Implemented and tested (service tests and browser upload/observe/export) |
| `dist/views/geo.js` | Optional, disclosed Photon address lookup with session cache | Implemented; requires network access to the configured endpoint |

### Route map

- `#/camera` (default), `#/review`, `#/receipt/:id` — capture journey
- `#/home` — three choices: track by problem ID, login as user, login as admin
- `#/track/:publicId` — public tracking
- `#/login/resident`, `#/login/official`, `#/register` — demonstration sign-in
- `#/resident/*` — complaints, community, map, claim, notifications, preferences
- `#/official/*` — overview, incident management, map, work orders, video review, intelligence studio, preferences
- `#/issue/:id` — case detail (signed in)

Cross-role routes redirect to the matching sign-in. Frontend guards are demonstration controls; they are not authorisation.

## 2. Data model in the prototype

Structured records live in `localStorage` (`ncl-records-v2`, migrated from v1). Media lives in IndexedDB (`ncl-evidence`). Video reviews are in `localStorage` (`ncl-video-reviews-v1`) with footage and frames in IndexedDB.

Each complaint is a **case** with separate concerns, kept distinct by field:

- **Public identity**: `id` (public reference). `claim` is a private code and is never in any public or community projection.
- **Physical incident context**: `area` (neighbourhood label), `category`, `gps` (precise, visibility-controlled).
- **Evidence**: `mediaType`, `shareEvidence` (owner's explicit choice), `repairs[]` (append-only history with `shared` flag, EXIF coordinates when present, capture and upload times kept separate).
- **Administrative lifecycle**: `status` (Submitted → Under Review → Assigned → In Progress → Marked Resolved), `team`, `workOrders[]`, `timeline[]` (public updates), `internal` (admin-only note).
- **Verification**: `verification` (Pending Verification, Citizen Confirmed, Disputed, Reinspection Required) and `verificationLog[]`, which is append-only. Citizen responses (Appears Resolved, Still Present, Cannot Confirm) are log entries, and `recurrence` counts repeat disputes after a marked resolution.
- **Community**: `reviews[]` (never change status), `followers[]` (per account, not per browser-wide flag).

Visibility rules implemented in `services.js`:

| Viewer | Sees |
| --- | --- |
| Public | Public ID, category, neighbourhood, lifecycle, verification, department, public timeline and verification history |
| Community resident (non-owner) | Community projection: shared original evidence and location only if the owner shared them; shared repairs only; reviews; no claim code, owner, or internal notes |
| Owner | Own evidence, full repair history, its own case; no internal notes; no claim code in case views |
| Admin | Operational fields, internal notes, priority inputs, all repairs, work orders |

The claim code is returned only to the submitting browser on its receipt page, through `api.receipt()`, and is excluded from the evidence copy.

## 3. Evidence and geo-tagging rules (implemented)

- Live captures attach the device GPS fix they were taken with: coordinates, accuracy, fix time, source type `gps`, and seconds between fix and capture. The shutter requires a live preview and a fix from the last 2 minutes. A poor first fix is refined for up to 15 seconds. No default coordinate is ever substituted.
- Every displayed location names its source (device GPS, photo EXIF, or manual map placement). There is no manual map picker in this version.
- Gallery uploads must be JPEG with valid EXIF GPS. Missing, malformed or non-JPEG files are rejected with a specific reason. Device location is never substituted.
- EXIF capture time is read as IST when present. Without it the UI labels the time as unknown, and separately shows upload time.
- Repair photos read EXIF when available. Missing GPS is shown as "No GPS metadata supplied"; original coordinates are not copied.
- Evidence copies are separate JPEGs with a navy/coral strip (public ID, category, capture time, location). They never include the claim code. The original file is untouched.
- Video location is associated metadata only; the UI and exports say it is not embedded in the file.

## 4. Intelligence contracts (proposed, adapters report unavailability)

Each technique has one job and an evidence requirement. Adapters exist so a real model can replace them without changing the interface.

| Component | Job | Adapter in prototype | Status |
| --- | --- | --- | --- |
| Road-damage vision (YOLO11n proposed) | Detect potholes and cracks in images | `classificationService` → unavailable | Proposed; not connected or trained |
| Waste-specific vision | Detect visible waste accumulation | none | Proposed; not connected or trained |
| Before/after repair assessment | Apparent change with uncertainty | Manual comparison and verification states | Implemented; device verification pending for UI. Automated assessment proposed |
| Incident linking | Suggest duplicates or recurrence for review | Recurrence records only | Proposed; never auto-merges |
| Knowledge graph | Relationships between complaint, incident, evidence, repair | Illustrative SVG graph | Illustrative demo |
| Priority engine | Normalised weighted score with visible contributions | `assessPriority()` | Implemented and tested; inputs illustrative |
| Fuzzy Cognitive Mapping | Expert-informed influence exploration | `fcmRun()` with documented assumed weights | Illustrative demo; not causal |
| Prediction (30-day ward/category activity) | Elevated complaint activity versus baseline | Target defined only | Proposed; not connected or trained |
| Monte Carlo scenarios | Compare uncertain strategies | Seeded labelled simulation | Illustrative demo; exploratory |
| Video dumping-action detection | Not planned for this release | `videoAnalysisAdapter` → unavailable | Proposed; not connected |
| Push notifications (FCM) | Device alerts | `notificationAdapter` → not configured | Proposed; not connected |

Priority baseline: `score = 100 × (0.35·severity + 0.30·exposure + 0.20·unresolved + 0.15·recurrence)`, where severity comes from category, exposure is assessed or a labelled default, unresolved time is capped at 30 days, and recurrence is capped at three records. Evidence confidence is reported separately and flags severe, weakly evidenced cases for verification.

## 5. Production direction

The brief's target shape is a modular backend, not a microservice fleet:

| Layer | Responsibility | Prototype equivalent |
| --- | --- | --- |
| Frontend | Citizen reporting, maps, evidence, workspaces | `dist/` |
| FastAPI | Validation, identity, server-enforced roles, complaint lifecycle, controlled media access | `services.js` (same rules, client-side) |
| PostgreSQL/PostGIS | Authoritative records, geographic queries, relationship and history tables | `localStorage` |
| Private object storage | Original media, repairs, reviewed clips, frame exports | IndexedDB |
| Background workers | Media processing, inference, matching, notifications | none |
| Analytics | Versioned priority inputs, baselines, predictions, scenario results | illustrative only |

Production controls still to build: server-side authentication and sessions, hashed claim secrets, rate limits, upload scanning, short-lived signed media links, audit log, retention rules, and municipal integration. None of these are claimed in this prototype.

## 6. Limits

- Everything is local to one browser. Two devices do not share data.
- Local demonstration sign-in offers no production security. Local storage is readable by anyone with access to the browser profile.
- The map basemap uses OpenFreeMap and OpenStreetMap over the network; the sample incidents are approximate neighbourhood positions, not verified locations.
- Address lookup uses the public Photon demo endpoint configured in `dist/map-config.json`. Self-host it for real use.
- Real camera, GPS, EXIF from phone photos, video codecs, and mobile layouts on physical devices still need device verification.
