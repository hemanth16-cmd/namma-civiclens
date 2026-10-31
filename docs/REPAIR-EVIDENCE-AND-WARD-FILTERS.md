# Repair evidence, progress stages and ward filters

## Staff repair evidence

The server case page now puts **Upload repair photo** and **Take repair photo** directly below the after-evidence figure. Both use a staff-authenticated JPEG upload; a work order is optional. Existing work-order photos remain in the same chronological repair history. Uploading does not mark a case resolved.

The inline camera reuses `createCamera`, including rear-camera preference, retry/play, camera switching, late-stream disposal and track cleanup when cancelled or when navigating away. Live capture exports JPEG. Gallery uploads currently accept JPEG up to 20 MB, subject to the server's configured upload/quota limits. GPS is not inferred from the original complaint.

New routes:

- `POST /api/v1/staff/complaints/{id}/repair-evidence`
- `GET /api/v1/staff/complaints/{id}/repair-evidence/{evidenceId}`

They retain department scope checks, signature/size/quota checks, private storage, staff access logging and mutation audit trails. No schema migration is needed: the existing `repair` evidence role is reused. Staff case detail includes a safe `repairEvidence` metadata array.

## Public before and after

Public tracking automatically loads the latest available before and after evidence, with enlargement and earlier repair-photo history. Resident issue links in server mode lead to server tracking rather than browser-local demo records.

- Original evidence is public only when `shareEvidence` was selected at submission. Private originals retain the existing owner/staff access flow.
- Repair evidence becomes public when the complaint is `Marked Resolved` (administrative completion). Citizen confirmation remains independent.
- Reopening removes public repair access. Each byte request rechecks current visibility, complaint/evidence ownership and the evidence ID format.
- Public JSON exposes only `evidenceId`, `phase`, `mediaType`, `uploadedAt`; no storage keys, object URLs, staff identities or precise location fields.
- Files are streamed from the configured existing storage backend with no-store headers. Supabase buckets do not need to be made public.

Route: `GET /api/v1/complaints/{id}/public-evidence/{evidenceId}`.

## Lifecycle

Adds `Under Construction`, `Work Completed`, `Verification In Progress` and `On Hold`. `Work Completed` means work awaits checks; `Marked Resolved` remains the final administrative completion status used by maps, metrics, publication and citizen verification.

Officials in scope can move between operational stages. Administrators can directly correct stages with a public explanation. Existing low-friction transitions remain; server-side role/scope checks, expected-status concurrency, department assignment requirements and the open-work-order resolution guard still apply. Citizen verification cannot be set through an ordinary status change.

## Ward and zone filters

The supplied e-Aasthi PDF, printed 30 October 2026, provides 198 wards across 10 zones. The `GBA Testing Ward` row is excluded. Its printed source is https://bbmpeaasthi.karnataka.gov.in/frmWardListbyZone.aspx. The checked-in directory follows that supplied document; it is not a claim about other ward schemes or present-day boundary geometry.

Incident management adds Zone name and Ward name alongside search, lifecycle, verification, category and sort. Zone selection narrows ward choices and clears incompatible selections. Both filters combine with the existing filters. Rows display matched ward and zone names.

The PDF has no boundary polygons. Matching uses an explicit ward identifier/name when present or an exact, case-insensitive neighbourhood name. Unknown neighbourhoods, including Chikkahagade, remain **Unassigned / outside directory**. Coordinates alone do not establish ward membership. Accurate automatic GPS assignment needs a boundary dataset using the same ward scheme, followed by backend point-in-polygon assignment. The existing staff-list loading cap of 2,000 records is unchanged.

## Validation

- JavaScript syntax checks and 180 Node tests passed.
- Full backend regression suite: 415 passed, 25 skipped (environment-dependent PostgreSQL tests), one dependency deprecation warning.
- Backend lifecycle/complaint/API tests include direct repair upload without a work order, authentication/scope, private-original protection, case/evidence-ID mismatch, publication after resolution, withdrawal after reopening, new stage persistence and independent citizen verification.
- Local browser UI checked with synthetic, preauthenticated fixtures exported from the tested local API: public before/after images load; combined Byatarayanapura/Jakkur filters return one matching case; staff repair controls and expanded status choices render in the existing design.
- Real-device camera capture and actual deployed Supabase/Render/Vercel integration still need deployment/device verification. No live complaints were changed by these tests.

Deploy the frontend and backend from the same revision. Deploy the backend first or together with the frontend so the new endpoints are available. Existing storage keys, database rows and environment variables remain compatible.
