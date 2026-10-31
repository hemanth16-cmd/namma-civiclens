# Ward master data and historical grievance dataset: inspection report

Branch `feature/supervisor-wards` (from `integration/v52-fullstack` at `d372728`), 2026-10-31. These are inspection results, measured from the files themselves. Nothing has been imported into any database yet.

## 1. Ward ↔ zone master data (`e-Aasthi.pdf`)

Source: e-Aasthi "Ward list of all zones" (`bbmpeaasthi.karnataka.gov.in/frmWardListbyZone.aspx`), printed 2026-10-30, 8 pages. Extracted by `backend/scripts/extract_e_aasthi.py` into `backend/seeds/wards_e_aasthi.json`. The script stops unless serial numbers run unbroken from 1, and stops on duplicate ward numbers.

| Zone no. | Zone | Wards |
|---|---|---|
| 1 | Byatarayanapura | 22 |
| 2 | Mahadevapura | 8 |
| 3 | Yelahanka | 19 |
| 4 | Rajarajeshwari Nagar | 24 |
| 5 | Bommanahalli | 16 |
| 6 | Jayanagar | 18 |
| 7 | Malleshwaram | 40 |
| 8 | K R Pura | 9 |
| 9 | C V Raman Nagar | 14 |
| 10 | Gandhinagar | 28 |
| 20 | GBA Testing Zone (testing entry, `testing: true`) | 1 (ward 298, "GBA Testing Ward") |

**Totals:** 199 rows (serial numbers 1–199, unbroken), made up of 198 operational wards in 10 zones plus 1 testing ward. Ward numbers are unique and range from 1 to 298 (not consecutive). Values are kept exactly as printed.

**No boundaries.** The PDF is a list, not a map. The repository and the database contain **no ward boundary GeoJSON or shapefile**. Automatic GPS → ward assignment therefore needs an official boundary file for this ward delimitation. Until one is supplied, any GPS-based assignment must stay **unverified**: it must not be guessed from coordinates, neighbourhood strings or addresses.

## 2. Historical grievances (`citizen-grievances.csv`)

| Property | Measured |
|---|---|
| Size | 214,404,690 bytes, UTF-8, no BOM |
| Records | **1,230,890** (every `Complaint ID` unique, so no duplicate IDs) |
| Columns (12) | `Complaint ID, Category, Sub Category, Grievance Date, Zone Name, Ward Name, Grievance Mode, Grievance Status, Staff Remarks, Staff Name, Staff Designation, Rating` |
| Date range | 2020-01 → 2026-10 for 1,230,416 rows. **474 rows have impossible dates**: 47 in 1900 and 427 in 2048. These must be flagged, not imported as real dates. |
| Records per year | 2020: 91,620 · 2021: 103,504 · 2022: 118,394 · 2023: 119,140 · 2024: 207,016 · 2025: 342,560 · 2026: 248,182 |
| Categories | 32 (top: Electrical 429,277 · Solid Waste (Garbage) 361,295 · Road Maintenance (Engg) 175,522 · Forest 57,769) |
| Sub-categories | 187 (top: Street Light Not Working 406,109 · Garbage dump 138,009 · Garbage vehicle not arrived 134,418 · Potholes 49,462) |
| Status | Closed 1,118,036 · Registered 56,494 · Non Relevant 26,929 · Rejected 16,803 · Resolved 6,953 · ReOpen 4,020 · In Progress 1,076 · Long Term Solution 522 · empty 57 |
| Channel (`Grievance Mode`) | 41 values (top: Sahaaya Helpline 417,255 · Nammabengaluru App 412,540 · BBMP-UDP 155,067 · Whatsapp 85,034); 587 empty |
| Staff | 2,263 distinct `Staff Name` values (often "name/designation") and 2,075 `Staff Designation` values (frequently a person, e.g. "Ramesh/AE"). The two columns look shifted in many rows and need cleaning before workload analysis. |
| Rating | Empty in 1,190,790 rows (96.7%); values 1–5 otherwise |
| Location | **No coordinates, no address.** Location is only `Zone Name` / `Ward Name`. |

### Zones in the CSV are the old BBMP zones, not the 10 e-Aasthi zones

The CSV has 17 zone values: East 205,686 · South 203,421 · Mahadevapura 188,384 · Bommanahalli 170,358 · RRNagar 156,564 · West 138,897 · Yelahanka 121,095 · Dasarahalli 39,423 · BBMP HO 1,335 · Rajarajeshwarinagar 1,077 · Byatarayanapura 1,049 · Jayanagar 921 · Malleshwaram 667 · (empty) 587 · K.R. PURA 540 · Gandhinagara 525 · C.V. Raman Nagar 361. Historical zones therefore have to be **re-derived from the ward** using the e-Aasthi mapping, not copied across.

### Ward-name matching to the e-Aasthi list

There are 485 distinct CSV ward names. After normalising case, punctuation and the word "Ward", **206 names covering 1,096,561 rows (89.1%) match an e-Aasthi ward exactly**. The rest are mostly spelling variants or wards from older delimitations, for example: Someshwara 16,182 · Banaswadi 14,873 (e-Aasthi: Banasawadi) · Dodda Bidarkallu 14,681 (e-Aasthi: Doddabidarakallu) · Vishwanathnagenahalli 8,496 · Chikpete 8,376 · Anjanapur 8,317 · Jayangar 7,637 · Newthippasandra 7,102 · Yelahanka old Satellite Town 6,322 · Visvesvarapuram 5,926.

**Recommendation:** use a reviewed alias table (CSV ward name → e-Aasthi ward number), with each alias approved by a person. Do not auto-match fuzzily. Rows left unmatched are imported with `ward_id = NULL` and listed in the import report.

## 3. What this means for the supervisor work

- **Ward/zone master data:** ready to seed (199 rows; the testing ward flagged inactive).
- **Live complaint → ward:** blocked on boundary data. Complaints carry GPS, but without polygons the honest state is `unverified`, with manual assignment by an authorised supervisor and an audit trail.
- **Historical analytics:** ward-level and zone-level only (counts, categories, trends, staff workload). **Coordinate-based hotspots cannot be computed from this dataset**; live hotspots use live complaint GPS.
- **Import scale:** 1.23M rows must be streamed and batch-inserted with an import-batch ID and an idempotent source key (`Complaint ID`). Historical rows stay in their own table, never as live complaints or open tasks. This also matters for Supabase free-plan storage, which should be checked before importing.
