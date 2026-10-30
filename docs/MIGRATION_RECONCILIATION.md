# Migration reconciliation: the two "schema version 8" migrations

Status: implemented on `feature/role-authentication-fix` (from `integration/v52-fullstack` at `981255f`).
Nothing here has been applied to the production Supabase database.

## The problem

Schema changes are numbered migrations: SQLite stores the number in `PRAGMA user_version`, PostgreSQL in
the `civiclens_schema` table. A migration runs when its number is higher than the stored one. Two
branches each added their own **version 8**:

| Branch | Commit | `SCHEMA_VERSION` | `MIGRATIONS[8]` |
|---|---|---|---|
| `integration/v52-fullstack` (this branch's base) | `981255f` | 7 | — |
| `main` (PR #9, Roboflow AI) | `ba434d0` | 8 | AI analysis: `ai_inferences`, `repair_assessments`, `repair_assessment_inferences` |
| `feature/core-platform-development` | `778cc54` | 8 | Duplicate detection: `complaints.merged_into_pk`, two indexes, `complaint_merges`, `duplicate_dismissals` |

A database at version 8 can therefore hold either set of tables, and the number cannot tell which.
Merging the branches as they are would break one environment silently: a database that ran main's
version 8 would never get the duplicate tables (8 is not greater than 8), and the reverse. Simply giving
duplicates or residents the number 9 does not help: a version-8 AI database would skip a renumbered
duplicate migration 8, and a duplicates database would be marked 9 without the AI tables.

## Compatibility matrix

Checked by comparing the migration text on all three branches (SHA-1 of each migration string):

| Item | v52 | main | core-platform | Compatible? |
|---|---|---|---|---|
| Base `SCHEMA` (SQLite v1) | `c55aefee` | `c55aefee` | `c55aefee` | identical |
| `POSTGRES_SCHEMA` (PG v5) | `e37f9e7c` | `e37f9e7c` | `e37f9e7c` | identical |
| `MIGRATIONS[2..7]` | same | same | same | identical |
| `POSTGRES_MIGRATIONS[6..7]` | same | same | same | identical |
| `MIGRATIONS[8]` / PG `[8]` | — | AI (`27c93792` / `54f7733f`) | duplicates (`27e77f54` / `e53a1e6e`) | **conflict in number only** |

The two version-8 changes do not touch the same objects. No table, column, index or trigger name
overlaps, and neither changes an existing constraint. So both can be applied to one database in
either order.

Database states this code must support:

| State | Where it comes from | Tables beyond v7 |
|---|---|---|
| empty | new deployment, tests | — |
| v7 | databases created by `integration/v52-fullstack`, `develop`, older `main` | — |
| v8-AI | databases created or upgraded by current `main` | AI tables |
| v8-duplicates | databases created or upgraded by `feature/core-platform-development` | duplicate tables, `merged_into_pk` |

Which state the production Supabase database is in was **not checked** (no production access was
used). Its owner can find out without changing anything:

```sql
SELECT version FROM civiclens_schema;
SELECT to_regclass('ai_inferences') AS ai, to_regclass('complaint_merges') AS duplicates;
```

## The fix: named migrations

`app/db.py` adds `Feature(name, sqlite, postgres, adopt_if)`. Named migrations live in
`store.FEATURES`. They run after the numbered ones, and each is recorded by name in a new
`civiclens_features` table (`name`, `how` = `applied` | `adopted`, `applied_at`):

- A feature runs once, whatever the database's version number.
- `adopt_if` names a table whose presence shows an earlier numbered migration already created the
  feature's objects. The feature is then only recorded (`adopted`), and its SQL is not run.
- SQLite applies each feature in its own transaction, checking foreign keys before commit. PostgreSQL
  applies them in the same transaction and advisory lock as the numbered migrations. A failure rolls
  everything back.
- Numbered migrations stop at 7, and `SCHEMA_VERSION` stays 7. A database already at 8 keeps its
  number; nothing lowers it.

The first named migration on this branch is `resident_accounts` (see "Resident accounts" below).

### Recipe for merging `main` and `feature/core-platform-development` later

When the branches are merged, resolve the `store.py` conflict like this. Don't renumber anything.

1. Remove both `MIGRATIONS[8]` and both `POSTGRES_MIGRATIONS[8]` entries. Keep `SCHEMA_VERSION = 7`.
2. Add the two changes to `FEATURES`, **before** `resident_accounts`, with their SQL unchanged:
   ```python
   Feature("ai_analysis", AI_SQL, AI_PG_SQL, adopt_if="ai_inferences"),
   Feature("duplicate_detection", DUP_SQL, DUP_PG_SQL, adopt_if="complaint_merges"),
   ```
3. Keep both branches' table names in `TABLES`.
4. Update the tests that assert version 8 so they check `db.features()` instead: on `main`,
   `test_evidence.py:472`, `test_migration_v7.py:73`, `test_migration_v8.py:66,102` and
   `test_postgres.py:134`; on core-platform, `test_evidence.py:472` and `test_postgres.py:132`.

Result for each state:

| Starting state | `ai_analysis` | `duplicate_detection` | `resident_accounts` | Version afterwards |
|---|---|---|---|---|
| empty | applied | applied | applied | 7 |
| v7 | applied | applied | applied | 7 |
| v8-AI | **adopted** | applied | applied | 8 (unchanged) |
| v8-duplicates | applied | **adopted** | applied | 8 (unchanged) |

`tests/test_migration_reconcile.py` proves this table on SQLite and PostgreSQL. It uses the real
version-8 SQL copied verbatim from both branches (`tests/legacy_v8.py`). For every state it checks
that complaints, claim hashes (the old claim code still verifies), evidence references and staff
accounts are unchanged. It also checks that a repeat run changes nothing, and that a failing feature
leaves nothing behind.

## Production rollout (needs approval; not done)

1. Back up first: take a Supabase backup or PITR snapshot, or `pg_dump --schema-only` plus a data dump.
2. Run the two read-only queries above and record the state.
3. Rehearse on a restored copy: start this code against it once (`python -m app` runs
   `initialize()`), then check `SELECT * FROM civiclens_features` and that the complaint count is unchanged.
4. Only then deploy to production. The named migrations run on first start, under the advisory lock.
5. Rollback: the resident migration only adds tables and nullable columns. Older code ignores them,
   so redeploying the previous version needs no schema change. Dropping them is never required.
