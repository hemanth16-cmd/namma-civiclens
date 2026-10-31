# Frontend design preservation: comparison and restoration plan

Reference design: **v5.1, commit `a40afe6`** (chosen by the user; `origin/main` no longer contains a
frontend, because `5e59f8d`/`015fee1` deleted `dist/` as "build output"). Target: local
`integration/v52-fullstack`. **Plan only; no frontend code has been changed for this task.**

## 0. Branch state (before this audit)

- Local `integration/v52-fullstack` had a half-finished `git pull` merge of
  `origin/integration/v52-fullstack` (`eee7afc`, which brings Workstream 1 complaint management via PR #6)
  with 2 conflicts. With the user's approval both were resolved keeping both sides:
  - `backend/app/store.py` `list_complaints`: Workstream 1's department scoping + the two-query staff list.
  - `dist/views/public.js` imports: `respondToResolution` + `staffSession`/`serverClient`.
  - One semantic clash found by the tests and fixed: `stream_evidence()` (Workstream 1) logged
    `public_id`, which does not exist there; it now uses `basename`.
- Result: `npm run check` passed (56 files); `npm test` 162/163 (only the known `api-config` test);
  backend SQLite **365 passed / 19 skipped**, PostgreSQL **376 passed / 8 skipped**.
- The merge is **resolved and staged but not committed** (needs approval).

## A. Comparison report (measured, not assumed)

Method: v5.1 served from `git archive a40afe6`, the current branch served side by side, the same routes
captured with Playwright (desktop 1440 px and phone 375 px; fake camera; same GPS) and compared
**pixel by pixel**. Server mode was captured against a local scratch API with seeded complaints.

### A1. Local (demonstration) mode: effectively identical to v5.1

| Page | Result vs v5.1 |
|---|---|
| Entry, tracking, admin login, resident login | identical except a 36×36 px box: the **logo** (v5.2 branding) |
| Admin overview, incident management, work orders, intelligence, case page (desktop and phone) | identical except the logo |
| Resident overview, my complaints, community | identical except the logo (+ mini-map tiles) |
| City map (admin and resident) | 0.56% of pixels differ, all inside the map: the report symbols now draw (the pre-existing "Source already exists" bug is fixed) |
| Camera | different: the **v5.2 upgrade** (stamp badge on the preview, automatic nearest-address card, "Located" state). Same layout, panels and controls |

No v5.1 file was removed. `style.css` only gained rules (+87 lines). No console errors, no horizontal
scroll at 375 px.

### A2. Server mode (what the deployed site uses): this is where the design shifted

| Feature / screen | v5.1 design | integration/v52-fullstack (server mode) | Required action |
|---|---|---|---|
| Entry page | 3 cards: Track · Login as user · Login as admin | Same layout; cards relabelled "My reports" and "Login as admin" (staff account) | Keep (labels only) |
| Admin login | Demo login card | Same card design, username/password for the server account | Keep |
| Admin overview, incident management, city map | Workspace shell with sidebar | **Same design, real server data** (restored earlier) | Keep; update the stale "read-only" notice (Workstream 1 now provides actions) |
| **Case page** | Workspace shell: header with "View on map", resolution banner, **Before/After evidence panels**, repair and verification history, "About this case" card, priority panel, "Does the repair hold up?" panel, **Case actions** panel | **Plain staff page (`#/staff/<id>`)**: no sidebar, single column, separate "Case management" forms | **Restore the v5.1 case page design for server cases**, wired to Workstream 1 endpoints |
| Work orders | Workspace page: create form, awaiting-assignment list, orders table | Workspace page with a stale "not on the server yet" notice; no form | **Restore the form and table on the real work-order API** |
| Staff complaint list `#/staff` | (did not exist) | Plain table page | Send signed-in staff to the workspace incident list; keep `#/staff` as an entry point |
| Staff notifications | (bell in workspace top bar) | Plain page `#/staff/notifications` | Render inside the workspace shell |
| Resident dashboard | Workspace shell: stats, recent reports, neighbourhood map, "Close the loop" | **Plain "My reports" list** (no resident accounts on the server) | **Restore the resident workspace shell** for server mode, fed by this device's reports and public tracking; keep panels whose data cannot exist yet with a clear note |
| Public tracking | Card with ID, pills, details, two-checks note, timeline | **Same design**; Workstream 1 adds the confirm/dispute block only for resolved cases | Keep |
| Camera, review, receipt | v5.1 | v5.2 additions (stamping, address, server upload/receipt) on the same layout | Keep (intentional enhancements) |
| Logo / icons | v5.1 mark | v5.2 brand mark and PWA icons | Keep (intentional branding); revert only if you want the v5.1 mark |

## B. Missing or degraded feature inventory (server mode)

1. Case page design (before/after evidence, about card, priority, verification panel, case actions) → replaced by a plain form page.
2. Work-order creation form and table → replaced by a notice, although the server now supports work orders.
3. Resident dashboard (sidebar, stats, recent reports, map, close-the-loop) → plain list.
4. In-workspace notifications → separate plain page.
5. Navigation continuity: from the workspace, opening a case leaves the sidebar layout.

Not missing: every v5.1 route still loads in local mode, pixel-equivalent apart from the logo.

## C. Integration preservation map (must stay untouched)

| Module | Why |
|---|---|
| `dist/api-client.js` (all methods, incl. Workstream 1: transitions, assignment, notes, updates, reinspection, work orders, repair evidence, audit, departments, citizen response) | The only HTTP adapter; strict response mapping |
| `dist/report-gateway.js` (submission, upload, retry, tracking, `respondToResolution`, remembered reports) | Citizen server flow, idempotency, no local fallback |
| `dist/api-config.js` / `api-config.json` | Mode selection (deployed URL) |
| `dist/staff-session.js`, `push-client.js`, `sw.js`, `views/notifications.js` | Memory-only staff session; notifications (Ruchika's area) |
| `dist/server-records.js` | Server records for the workspace |
| `dist/views/capture.js`, `photo-stamp.js`, `photo-save.js`, `address-service.js` | v5.2 capture and server upload |
| `backend/**` | No backend change is needed for this task |

Supabase credentials never reach the frontend (unchanged; checked).

## D. Restoration plan (file by file; all frontend, no backend change)

| # | Target file | Current | v5.1 behaviour | Change | Risk | Tests |
|---|---|---|---|---|---|---|
| 1 | `dist/views/issue.js` | Local records only (`api.detail`, `api.updateCase`, ...) | Rich case page | Add a server branch: when signed-in staff open a case in server mode, build the same page from `getStaffComplaint` (+ evidence via `fetchStaffEvidence`, repairs via `fetchWorkOrderEvidence`, audit via `getAuditTrail`). Case actions map to `transitionComplaint` (only `allowedActions` from the server), `assignComplaint`, `addPublicUpdate`, `addStaffNote`, `requestReinspection`, `createWorkOrder`, `uploadRepairEvidence`. Community reviews and the owner's sharing toggle have no server API: keep the panels with a note | Medium: large view; must never show controls the server would refuse | unit tests for the server-case mapping; browser journey: assign, start, work order, repair photo, resolve; 403 for an out-of-scope official |
| 2 | `dist/router.js` `serverModeRoute` | `#/issue/<id>` → `#/staff/<id>` for staff | — | Keep `#/issue/<id>` (rich page) for staff; residents still go to tracking | Low | route unit tests |
| 3 | `dist/views/workspace.js` | Server work orders: notice only; overview notice says read-only | Work-order form and table | Server branch: list via `listWorkOrders`, create via `createWorkOrder` (cases in Assigned/In Progress), start/complete via `changeWorkOrder`; update the overview notice | Low | browser: create and complete a work order |
| 4 | `dist/views/staff.js` | Plain list, case, notifications pages | — | Signed-in staff landing on `#/staff` go to `#/official/complaints`; `#/staff/<id>` → `#/issue/<id>`; keep the sign-in form; keep the module's server logic (reused by #1) | Low | e2e staff checks updated |
| 5 | `dist/views/workspace.js` + `ui.js` (resident) | `#/resident/*` → plain `#/my-reports` | Resident workspace shell | Server mode: a resident shell without sign-in ("This device"): overview stats and recent reports from remembered IDs + public tracking; "My complaints" list; notifications → server inbox; community, map and claim kept as panels with "needs resident accounts" notes (no fake data) | Medium: no server identity, so data is limited to this device | browser: submit → appears in overview and list; phone width |
| 6 | Staff notifications | Plain page | Bell in the workspace top bar | Render the existing inbox (`mountInbox`) inside the workspace shell for staff | Low | browser |
| 7 | Logo | v5.2 mark | v5.1 mark | **No change unless you ask** (v5.2 branding is an intentional upgrade) | — | — |

Order: 1+2 (largest gap, biggest value) → 3 → 4 → 6 → 5. Each group: unit tests, backend suites
unchanged, browser checks, screenshots against v5.1.

## E. Verification plan

- Unit: `npm test` (+ new tests for server-case mapping and routing); `npm run check`.
- Backend unchanged: pytest SQLite and PostgreSQL must stay green.
- Browser (local scratch API, never production): the existing `backend-e2e`, server-mode and admin
  workspace suites; new journeys for the case page actions and work orders; negative checks (official
  outside the department gets 403 and sees no controls; no internal notes in public tracking).
- Visual: re-run the v5.1-vs-current capture; server case page and resident dashboard compared against
  v5.1 at 1440 px and 375 px; no horizontal scroll; no console errors. Equivalence is reported only
  where the screenshots were actually compared.

## F. Implementation status (steps 1–6 done, uncommitted)

The user's `Namma-CivicLens-v5.1-Source.zip` was extracted and compared: its frontend is **content-identical
to `a40afe6`** (only line endings differ), so every comparison above used the right reference.

| Step | Result | Files |
|---|---|---|
| 1 Case page | Server complaints open in the v5.1 case page (shell, state strip, before/after evidence, repair and verification history, community reviews panel, timeline, About this case, Does the repair hold up?, Case actions). Actions use the complaint-management API and only what the server allows; evidence opens on request (each view is logged) | `dist/views/server-case.js` (new), `dist/views/issue.js`, `dist/api-client.js` (staff case now keeps `verificationLog`) |
| 2 Case links | `#/issue/<id>` stays on the case page for signed-in staff; reloading a case asks staff to sign in again; others get public tracking | `dist/router.js`, `dist/app.js` |
| 3 Work orders | Original form and table on the real API (create, start, complete, cancel) | `dist/views/workspace.js` |
| 4 Console | `#/staff` hands signed-in staff to Incident management, `#/staff/<id>` to the case page; the plain list/detail/management views were removed from `staff.js` (moved, not duplicated) | `dist/views/staff.js` |
| 5 Notifications | Staff inbox inside the dashboard shell (`#/official/notifications`, the bell) | `dist/views/workspace.js` |
| 6 Resident | Original resident shell in server mode, "this device": overview stats, recent reports, map, My complaints from this browser's reports and public status; Community and Claim keep their place with a "needs resident accounts" note | `dist/server-records.js`, `dist/views/workspace.js`, `dist/ui.js`, `dist/router.js`, `dist/app.js`, `dist/views/public.js` |

Verification: `npm run check` (57 files) passed; `npm test` 163/164 (only the known `api-config` test);
`backend-e2e` 62/66 (the same 4 config-related failures; its staff section updated to the restored flow);
new restored-screens journey **37/37** (case page structure, view photo, review → assign → work order →
start → repair photo → complete → note → public update → resolve → audit, work-orders page, staff inbox,
console hand-over, field official refused outside their department, resident dashboard, community/claim
notes, public tracking never shows internal notes, phone width without horizontal scroll). Screenshots of
the restored case page and work orders were compared side by side with the zip's v5.1 pages.

### F2. v5.1 camera elements restored beside v5.2 (user decision)

- Camera location panel: the v5.1 **"Street address"** row and the **"Optional street address"** box with
  **"Look up street address"** (exact v5.1 wording), beside v5.2's automatic nearest-address card. Both use
  the same resolver, so they always agree; repeats for one spot are answered from the per-session cache, as in v5.1.
- Review: the v5.1 **"Street address:"** line and **"Look up street address"** button, beside v5.2's
  nearest-address line.
- Receipts (local and server): v5.1's **"Download evidence copy"**: the navy/coral strip JPEG
  (`<id>-evidence-copy.jpg`, builder copied verbatim from the zip as `buildNavyEvidenceCopy`), beside v5.2's
  stamped photo, with the v5.1 explanation.
- Verified: 22/22 browser checks in local and server mode (rows, boxes, lookups, a real 1920×1272 JPEG
  download from a 1920×1080 photo); `backend-e2e` 62/66 (same 4 config-related failures); restored-pages
  journey 37/37; `npm test` 163/164 (only the known `api-config` test).

Still different from v5.1: the v5.2 logo; the before/after compare slider for server cases (needs both
private files opened); community reviews and claiming (need resident accounts).
