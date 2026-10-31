# Apply this CivicLens update

Based on main commit d8808c130602df2350e14aa5edffed9c8ffea19b from hemanth16-cmd/namma-civiclens.

This folder is a complete source snapshot, not a Git checkout. It includes the frontend, backend, tests and documentation. It excludes credentials, local test databases and evidence fixtures.

## Existing Git checkout (recommended)

Use the accompanying Namma-CivicLens-Repair-Updates.patch in your existing checkout, which preserves your Git remote and history. First commit or safely stash your local changes. Create a feature branch. Run `git apply --check /path/to/Namma-CivicLens-Repair-Updates.patch`, then `git apply /path/to/Namma-CivicLens-Repair-Updates.patch`. If the check reports a conflict, have your coding agent reconcile it against your current branch; do not overwrite local work.

Review the changes, run `npm run check` and `npm test`, then commit and push your feature branch. Run the Python tests in your configured backend environment too. The patch contains the same changes as this source snapshot.

## Run and deploy

Frontend: `npm run dev` from the folder containing package.json. Keep your existing API configuration and backend environment variables. This frontend is still configured to use your Render API.

The repair endpoints and status changes REQUIRE the updated backend. Deploy the backend first or at the same time as the frontend. Until then the new repair actions cannot work against your old Render deployment. No new database migration or public Supabase bucket is required.

Full feature details, publication rules, the ward-directory limitation and test results are in docs/REPAIR-EVIDENCE-AND-WARD-FILTERS.md. Real-device camera capture still needs testing on your phone or laptop over HTTPS or localhost.

The GitHub connector rejected writing with HTTP 403. This snapshot and patch have not been pushed, merged or deployed.
