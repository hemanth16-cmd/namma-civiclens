# Passerby, resident and administrator: identity separation

Branch `feature/role-authentication-fix`, started from `integration/v52-fullstack` at
`981255fa219209470aa5e4b604a84294fb22bc69` (chosen with the project owner, to be merged back into
`integration/v52-fullstack` after approval). Nothing has been pushed, merged or applied to the
production Supabase database. The migration conflict is covered separately in
[`MIGRATION_RECONCILIATION.md`](MIGRATION_RECONCILIATION.md).

## Root cause

In server mode, `dist/app.js` (lines 103–106 at `981255f`) made up a resident identity for anyone
who opened `#/resident`:

```js
if (head === 'resident' && role !== 'resident') {
  setSession({role: 'resident', name: 'This device', email: '', server: true, device: true});
```

The entry page's "Residents · this device" card linked to `#/resident`, so every passerby who
clicked it got `role: 'resident'` in `sessionStorage` for the rest of the tab. That opened the
resident dashboard and preferences with no account. (Checked in Chromium: `ncl-session` went from
`null` to that value, and `#/resident/settings` loaded.)

The backend was not the cause, but it had no resident concept at all: no resident sign-in, no
ownership column, and claiming existed only in the browser demo. The "resident" was therefore only
ever a frontend label.

## What changed

**Phase A: passerby.** `#/resident/*` now needs a server resident session. A URL never grants a
role. Access is decided by `serverWorkspaceAccess()` in `dist/router.js`, which is a pure function
with unit tests. On load, the leftover "this device" pseudo-resident and resident identities with no
live token are dropped. The device-local list is renamed **Reports on This Device** and gives no
role. The entry page separates passerby (report, track, device list), resident sign-in and staff
sign-in.

**Phase B: migrations.** Named migrations (`civiclens_features`) reconcile the two different
"version 8" migrations. See `MIGRATION_RECONCILIATION.md`.

**Phase C: resident accounts.** These are server-backed and kept separate from staff accounts:
`app/resident_auth.py` and `app/residents_api.py`, applied as the named migration
`resident_accounts`. Complaint ownership comes only from a validated resident session or a verified
claim code.

**Phase D: demo seeding.** `python -m app.demo seed --confirm-demo` creates the demo resident and the
`demo_admin` staff account (role `admin`). It is guarded and idempotent.

**Phase E: frontend.** Server-mode resident sign-in and registration. The original resident
workspace runs on real data: overview, My complaints, City map, Claim, Notifications and
Preferences. Sign-out revokes the session on the server. One identity per tab: signing in as staff
ends the resident session, and the reverse.

## Identity and data flow

```
Passerby ──POST /complaints (no Authorization)──► complaint, owner NULL ──► receipt: public ID + claim code (shown once)
   │                                                                     └─► GET /complaints/{id}: public projection only
   └─ Reports on This Device: public IDs in localStorage (no claim code, no role)

Resident ──POST /residents/sessions──► token (memory only)
   ├─ POST /complaints + Bearer resident token ──► owner = session's resident (owned_via 'submitted')
   ├─ POST /residents/me/claims {id, claim} ──► scrypt check ──► owner set if NULL (owned_via 'claimed'), audit entry
   └─ GET /residents/me/complaints | /preferences ──► only this resident's data

Staff ──POST /staff/sessions──► token (memory only) ──► /staff/* (role and departments from the server)
```

## Role-permission matrix (enforced on the server)

| Capability | Passerby | Resident | Staff (official / supervisor / admin) |
|---|---|---|---|
| Submit a complaint | ✅ anonymous, unowned | ✅ owned by the account | — (staff tokens are refused on `POST /complaints`) |
| Upload original evidence | ✅ with the receipt's upload token | ✅ same | — |
| Public tracking | ✅ | ✅ | ✅ |
| Respond to a resolution / follow (claim code) | ✅ | ✅ | — |
| Resident profile, My complaints, preferences | ❌ 401 | ✅ own only | ❌ 401 (staff token refused) |
| Claim a complaint | ❌ 401 | ✅ with the valid claim code, only if unowned | ❌ 401 |
| See another resident's complaints or identity | ❌ | ❌ (404 / 409, names nobody) | Complaints yes (staff view); no resident email |
| Staff list, case detail, evidence, access log | ❌ 401 | ❌ 401 (resident token refused) | ✅ within role scope |
| Status, assignment, work orders, audit | ❌ | ❌ | ✅ per existing role rules |

## Frontend route verification (server mode, Chromium)

| Route | Passerby | Resident (signed in) | Staff (signed in) |
|---|---|---|---|
| `#/home`, `#/camera`, `#/track/…` | open | open | open |
| `#/my-reports` (Reports on This Device) | open, no role | open | open |
| `#/resident`, `/complaints`, `/claim`, `/settings`, `/map`, `/community` | → `#/login/resident`, no session | open | → `#/login/resident` |
| `#/resident/notifications` | → `#/login/resident` | → `#/notifications` (device inbox) | → `#/login/resident` |
| `#/official/*` | → `#/login/official` | → `#/login/official` | open |
| `#/issue/{id}` | → `#/track/{id}` | → `#/track/{id}` | staff case page |
| After sign-out | — | `#/resident` → sign-in; old token is `401` on the server | existing behaviour |
| After reload | — | signed out (memory-only token) → sign-in | signed out → staff sign-in |
| Leftover `ncl-session` (old pseudo-resident or local demo) | dropped; no access | — | — |

## Admin evidence access (verified in Chromium as `demo_admin`)

The admin signs in, sees the dashboard and the list (guest and resident complaints), and opens a
case. **View photo** streams the private JPEG through `GET /staff/complaints/{id}/evidence`
(320 px wide, shown from a `blob:` URL). **View video** streams the WebM, which then plays
(`readyState ≥ 2`, `currentTime > 0`). The admin can change status (it appears in the history),
assign to a department, and create a work order (listed on the work-order page). The same evidence
URL without a staff token returns `401`. Evidence stays private: there are no public or signed URLs,
and no bucket setting was changed.

## Tests

Backend tests count the same on both engines, except Postgres mode also runs 8 Postgres-specific
tests.

| Suite | Result |
|---|---|
| Backend, SQLite (`pytest`) | **401 passed**, 25 skipped (Postgres-only) |
| Backend, PostgreSQL mode (`CIVICLENS_TEST_DATABASE=postgres`) | **418 passed**, 8 skipped (SQLite-only) |
| Backend, Postgres-only tests (`-m postgres`) | **25 passed** |
| New: `test_residents.py`, `test_demo.py`, `test_migration_reconcile.py` | **42 passed** (SQLite and Postgres) |
| Frontend unit (`npm test`) | **175 / 176**. The one failure is pre-existing: the shipped `api-config.json` is in backend mode, but a test expects local mode. It also fails on `981255f`. |
| Browser: `scripts/roles-e2e.mjs` (new) | **56 / 56** |
| Browser: `scripts/backend-e2e.mjs` (existing, with demo staff) | **63 / 66**. The 3 "Local mode" failures are the same shipped-config issue, and the same 3 fail on `981255f`. |

How the required scenarios map to tests:

1. Anonymous reporting: `test_a_passerby_reports_without_any_account`, plus roles-e2e (passerby) and
   backend-e2e.
2. `#/resident` never creates a session: `server-mode-routing.test.js` and roles-e2e (7 routes and 2
   leftover-identity cases).
3. Passerby complaints stay unowned: `test_passerby_complaints_never_appear…` and
   `_check_unowned` across every migration state.
4. and 5. Valid and invalid sign-in: `test_sign_in_sign_out_and_session_validation` and roles-e2e.
6. Owned complaints in the dashboard: `test_a_signed_in_resident_owns_what_they_submit` and roles-e2e.
7. and 8. Claiming with right and wrong codes: `test_claiming_a_guest_complaint…`,
   `test_a_wrong_claim_code…` and roles-e2e.
9. Resident A versus B: `test_one_resident_cannot_take_or_see…` and roles-e2e (second resident).
10. Admin sign-in: `test_seed_is_idempotent_and_the_accounts_work` and roles-e2e.
11. to 13. Unauthenticated, resident-token and staff-token boundaries:
    `test_resident_and_staff_tokens_are_not_interchangeable`, `test_a_stale_or_foreign_bearer_token…`
    and roles-e2e.
14. Admin photo and video evidence: `test_the_demo_admin_can_run_the_whole_admin_journey` and
    roles-e2e.
15. No secrets in the frontend or logs: `resident-accounts.test.js` (scans `dist/`), roles-e2e
    (console), and the API log has no password.
16. Existing suites: all of the above.
17. Migrations preserve records: `test_migration_reconcile.py` (SQLite and Postgres, every starting
    state).

## Changed files (relative to `981255f`)

- Backend:
  - new: `app/resident_auth.py`, `app/residents_api.py`, `app/demo.py`
  - changed: `app/db.py` (named migrations), `app/store.py` (resident migration and methods,
    ownership on create), `app/complaints.py` (owner from the session), `app/models.py`,
    `app/ratelimit.py`, `app/main.py`, `app/db_migrate.py`
- Backend tests:
  - new: `test_residents.py`, `test_demo.py`, `test_migration_reconcile.py`, `legacy_v8.py`
  - changed: `test_postgres.py`
- Frontend:
  - new: `dist/resident-session.js`
  - changed: `dist/app.js`, `dist/router.js`, `dist/api-client.js`, `dist/server-records.js`,
    `dist/report-gateway.js`, `dist/views/public.js`, `dist/views/workspace.js`,
    `dist/views/my-reports.js`, `dist/views/capture.js` (one receipt line), `dist/views/staff.js`
    (one line), `dist/ui.js` (sidebar note)
- Frontend tests:
  - new: `tests/resident-accounts.test.js`
  - changed: `tests/server-mode-routing.test.js`
- Scripts and docs:
  - new: `scripts/roles-e2e.mjs`, `docs/ROLE_IDENTITY.md`, `docs/MIGRATION_RECONCILIATION.md`
  - changed: `backend/README.md`, `docs/api-contract.md`

Not touched: priority engine, knowledge graph, FCM, Monte Carlo and predictive analytics, the
notification infrastructure (`notifications*.py`, `push*.js`, `views/notifications.js`), and the
S3 and Telegram work.

## Outstanding limitations and deployment notes

- **Production database:** not checked and not migrated. Follow the rollout in
  `MIGRATION_RECONCILIATION.md`: back up, identify the schema state with two read-only queries,
  rehearse on a copy, and get approval first.
- **Demo credentials are public.** Only seed them on local or isolated databases. Never run
  `app.demo` against production, which `CIVICLENS_ENV=production` blocks. The older local-only
  prototype login (`DEMO_PASSWORD = 'CivicDemo2026'` in `dist/services.js`) has no server meaning,
  but its value is nearly the same as the server demo password. Consider renaming it to avoid
  confusion.
- **Development-grade resident auth:** no email verification, password reset, MFA or lockout beyond
  rate limits. Rate limits are per process, in memory. Tokens are bearer tokens: use HTTPS outside
  localhost.
- **Sessions are memory-only** (the same choice as staff), so a reload signs the resident out. This
  is safer, but a deliberate trade-off against convenience.
- **Community issues** has no server endpoint yet. The screen keeps its design and says so instead
  of showing sample data. Resident notifications use the existing device inbox (`#/notifications`),
  not per-account delivery: that belongs to the notification workstream.
- **Owner view of evidence** is not built: residents see their complaint details, not the uploaded
  file. Evidence stays staff-only.
- **Merging into `main` / core-platform** needs the recipe in `MIGRATION_RECONCILIATION.md`, which
  moves both version 8s into named migrations.
- The pre-existing `api-config.json` versus test mismatch, listed above, is unrelated and was left
  as it is.
