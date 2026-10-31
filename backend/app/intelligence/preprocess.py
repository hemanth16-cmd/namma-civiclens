"""Historical grievance dataset preprocessing pipeline.

Reads the split citizen-grievances CSV parts in a memory-efficient streaming fashion,
validates dates, normalizes wards/zones/categories, produces aggregates and a
data-quality report. Designed for the ~1.26M row BBMP-style export.

Does not invent GPS coordinates. Historical records lack lat/lng; hotspot work
for historical data is therefore ward-level only.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

MIN_VALID_DATE = datetime(2018, 1, 1, tzinfo=timezone.utc)
MAX_VALID_DATE = datetime(2027, 1, 1, tzinfo=timezone.utc)

EXPECTED_COLUMNS = [
    "Complaint ID", "Category", "Sub Category", "Grievance Date",
    "Zone Name", "Ward Name", "Grievance Mode", "Grievance Status",
    "Staff Remarks", "Staff Name", "Staff Designation", "Rating",
]


def iter_csv_rows(csv_paths: list[Path]) -> Iterator[dict[str, str]]:
    for i, path in enumerate(csv_paths):
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}


def parse_grievance_date(raw: str) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_ward(name: str) -> str:
    if not name:
        return "UNKNOWN"
    return " ".join(name.strip().split()).title()


def normalize_zone(name: str) -> str:
    if not name:
        return "UNKNOWN"
    return " ".join(name.strip().split()).title()


def process_dataset(
    csv_paths: list[Path],
    output_dir: Path,
    max_rows: int | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    valid = 0
    excluded = Counter()
    categories = Counter()
    zones = Counter()
    wards = Counter()
    statuses = Counter()
    monthly = Counter()
    ward_category = Counter()
    ratings_present = 0
    future_dates = 0
    past_dates = 0
    seen_ids: set[str] = set()
    duplicates = 0

    for row in iter_csv_rows(csv_paths):
        total += 1
        if max_rows and total > max_rows:
            break

        cid = row.get("Complaint ID", "")
        if not cid:
            excluded["missing_id"] += 1
            continue
        if cid in seen_ids:
            duplicates += 1
            excluded["duplicate_id"] += 1
            continue
        seen_ids.add(cid)

        dt = parse_grievance_date(row.get("Grievance Date", ""))
        if dt is None:
            excluded["bad_date"] += 1
            continue
        if dt < MIN_VALID_DATE:
            past_dates += 1
            excluded["too_old"] += 1
            continue
        if dt > datetime(2025, 12, 31, tzinfo=timezone.utc):
            future_dates += 1
        if dt > MAX_VALID_DATE:
            excluded["beyond_max"] += 1
            continue

        cat = (row.get("Category") or "Unknown").strip() or "Unknown"
        zone = normalize_zone(row.get("Zone Name") or "")
        ward = normalize_ward(row.get("Ward Name") or "")
        status = (row.get("Grievance Status") or "Unknown").strip() or "Unknown"
        rating = row.get("Rating") or ""

        categories[cat] += 1
        zones[zone] += 1
        wards[ward] += 1
        statuses[status] += 1
        monthly[dt.strftime("%Y-%m")] += 1
        ward_category[(ward, cat)] += 1
        if rating and rating.strip():
            ratings_present += 1

        valid += 1

    report = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total_rows_seen": total,
        "valid_records": valid,
        "excluded": dict(excluded),
        "duplicates": duplicates,
        "future_date_count": future_dates,
        "too_old_count": past_dates,
        "ratings_present": ratings_present,
        "unique_categories": len(categories),
        "unique_zones": len(zones),
        "unique_wards": len(wards),
        "top_categories": categories.most_common(20),
        "top_zones": zones.most_common(20),
        "top_wards": wards.most_common(30),
        "status_distribution": dict(statuses),
        "monthly_counts": dict(sorted(monthly.items())),
        "date_validation": {
            "min_accepted": MIN_VALID_DATE.isoformat(),
            "max_accepted": MAX_VALID_DATE.isoformat(),
        },
        "notes": [
            "Historical dataset has no latitude/longitude; hotspot detection for historical records is ward-level only.",
            "Many dates after 2025 appear in the export; they are flagged.",
            "Rating field is sparsely populated and is not used as a core predictor.",
            "Category taxonomy differs from the live CivicLens category set.",
        ],
    }

    aggregates = {
        "by_ward": dict(wards),
        "by_zone": dict(zones),
        "by_category": dict(categories),
        "by_month": dict(sorted(monthly.items())),
        "by_status": dict(statuses),
        "ward_category_top": [
            {"ward": w, "category": c, "count": n}
            for (w, c), n in ward_category.most_common(500)
        ],
    }

    (output_dir / "data_quality_report.json").write_text(json.dumps(report, indent=2))
    (output_dir / "historical_aggregates.json").write_text(json.dumps(aggregates, indent=2))

    logger.info("Preprocessing complete: %s valid of %s rows", valid, total)
    return report


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    base = Path(__file__).resolve().parents[3] / "data" / "historical"
    paths = sorted(base.glob("citizen-grievances_part_*.csv"))
    out = Path(__file__).resolve().parents[2] / "data" / "intelligence"
    report = process_dataset(paths, out, max_rows=None)  # or 50000 for testing
    print(report["valid_records"], "valid records")

