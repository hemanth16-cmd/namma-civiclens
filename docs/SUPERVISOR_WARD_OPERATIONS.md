# Supervisor and ward/zone operations (P0)

Branch `feature/supervisor-wards-p0` (from `feature/supervisor-wards` at `0eb4836`). Nothing here has been applied to production Supabase.

## Permissions

| Who | Supervisor endpoints | Geographic scope |
|---|---|---|
| Resident token / no token | 401 (not a staff session) | — |
| Official | 403 | — |
| Supervisor | allowed | only the wards/zones granted with `python -m app.staff scope grant`; none granted = nothing (deny by default) |
| Admin | allowed | always city-wide (admin already outranks supervisor in `lifecycle.ROLES`) |

Supervisors keep every existing complaint-management action (assign, reopen, reinspection, merge, audit trail) and gain `set_ward`. Roles and scope are changed only by the CLI; no API route can widen them. Existing case endpoints are unchanged: geographic scope applies to the new supervisor endpoints and to ward changes.

## Schema: named feature `ward_zone_operations` (SQLite and PostgreSQL)

- `zones`, `wards` (number, name, zone, active, testing, nullable `boundary_geojson` + source).
- `staff_geographic_scope` (staff, `city` | `zone` | `ward`).
- `complaints.ward_pk`, `ward_assignment_status` (`auto` | `manual` | `unverified`, default `unverified`), `priority`, `due_at`.
- `ward_assignment_history` and `ward_allocations`: append-only (update/delete refused by triggers on both engines).

## Ward assignment

There are **no ward boundary polygons** in the repository or the database. A new complaint is therefore `unverified` with no ward, and the reason is recorded (`no_boundaries`, `outside_service_area`, `no_gps`). Address and neighbourhood text are never used. When an official GeoJSON boundary file is imported (`python -m app.staff wards import-boundaries --file … --ward-property … --source …`), new complaints inside exactly one active ward polygon become `auto`; zero or overlapping matches stay `unverified`. Supervisors and admins set wards by hand (`POST /staff/complaints/{id}/ward`, reason required, audited, GPS kept).

## Endpoints (`/api/v1/staff/…`)

| Route | Purpose |
|---|---|
| `GET supervisor/scope` | wards in the caller's scope, by zone |
| `GET supervisor/dashboard[?zone=N\|?ward=N]` | totals, open, resolved, pending verification, overdue; by status, category, zone and ward |
| `GET supervisor/complaints?wards=1,2&category&status&minAgeDays&maxAgeDays` or `?unverifiedWard=true` | unresolved complaints, oldest first |
| `GET supervisor/staff` | active staff to allocate to (no credentials) |
| `POST supervisor/allocations` | bulk allocate (≤100, all or nothing): assignee, department, priority, due date, reason, internal note |
| `GET complaints/{id}/allocations` | allocation history, previous → new |
| `POST complaints/{id}/ward` | manual ward |

Dashboard definitions: merged duplicates excluded; open = not `Marked Resolved`; pending verification = resolved and `Pending Verification`; overdue = open with a past allocation due date or a past-due open work order (IST dates). Complaints without a ward are reported separately (city-wide callers only).

## Setup

```
python -m app.staff wards seed                       # 198 operational wards in 10 zones + GBA testing ward 298 (inactive)
python -m app.staff scope grant --username ravi --zone 7
```

## Not done (P1/P2)

Historical CSV import (1.23M rows; needs a reviewed ward-alias table), hotspots, CCTV registry, admin staff directory, official ward boundaries.
