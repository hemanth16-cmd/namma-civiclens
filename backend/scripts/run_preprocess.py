import sys
import time
import json
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence.preprocess import process_dataset

base = Path(__file__).resolve().parents[2] / "data" / "historical"
paths = sorted(base.glob("citizen-grievances_part_*.csv"))
print(f"Found {len(paths)} CSV files in {base}")
out = Path(__file__).resolve().parents[1] / "data" / "intelligence"

# We can process all rows or pass a limit
max_rows = int(sys.argv[1]) if len(sys.argv) > 1 else None
print(f"Running preprocessing (max_rows={max_rows})...")
t0 = time.time()
report = process_dataset(paths, out, max_rows=max_rows)
dt = time.time() - t0
print(f"Completed in {dt:.2f}s:")
print(f"  Total seen: {report['total_rows_seen']}")
print(f"  Valid records: {report['valid_records']}")
print(f"  Unique wards: {report['unique_wards']}")
print(f"  Unique categories: {report['unique_categories']}")
print(f"  Top categories: {report['top_categories'][:5]}")

