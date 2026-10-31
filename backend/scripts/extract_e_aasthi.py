"""Extract the ward-to-zone list from the e-Aasthi "Ward list of all zones" PDF into backend/seeds/wards_e_aasthi.json.

    pip install pypdf          # extraction-time tool only; not a runtime dependency of the API
    python scripts/extract_e_aasthi.py path/to/e-Aasthi.pdf

Rows are "<serial> <zone no> <zone name> <ward no> <ward name>". Long zone names wrap onto a second line in
the PDF text ("50 4 Rajarajeshwari" / "Nagar" / "38 H.M.T"); those are rejoined. Values are kept exactly as
printed: nothing is corrected or invented. The run fails unless serial numbers are contiguous from 1, so a
missed or mangled row cannot pass silently. Zone 20 ("GBA Testing Zone") is marked as a testing entry.
"""

import json
import re
import sys
from pathlib import Path

ROW = re.compile(r"^(\d+)\s+(\d+)\s+([A-Za-z][A-Za-z .]*?)\s+(\d+)\s+(.+?)$")
WRAPPED_HEAD = re.compile(r"^(\d+)\s+(\d+)\s+([A-Za-z][A-Za-z .]*)$")
WRAPPED_WARD = re.compile(r"^(\d+)\s+(.+)$")
TESTING_ZONE = 20


def parse(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines()]
    rows, i = [], 0
    while i < len(lines):
        m = ROW.match(lines[i])
        head = WRAPPED_HEAD.match(lines[i])
        if not m and head and i + 2 < len(lines) and re.fullmatch(r"[A-Za-z .]+", lines[i + 1]) and WRAPPED_WARD.match(lines[i + 2]):
            ward = WRAPPED_WARD.match(lines[i + 2])
            rows.append((int(head[1]), int(head[2]), f"{head[3]} {lines[i + 1]}".strip(), int(ward[1]), ward[2].strip()))
            i += 3
            continue
        if m:
            rows.append((int(m[1]), int(m[2]), m[3].strip(), int(m[4]), m[5].strip()))
        i += 1
    rows.sort()
    if [r[0] for r in rows] != list(range(1, len(rows) + 1)):
        raise SystemExit("Serial numbers are not contiguous: a row was missed or mis-parsed. Nothing written.")
    if len({r[3] for r in rows}) != len(rows):
        raise SystemExit("Duplicate ward numbers in the source. Nothing written.")
    return [{"serial": s, "zone_number": zn, "zone_name": z, "ward_number": wn, "ward_name": w, "testing": zn == TESTING_ZONE}
            for s, zn, z, wn, w in rows]


def main(pdf: str) -> None:
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    wards = parse(text)
    out = Path(__file__).resolve().parents[1] / "seeds" / "wards_e_aasthi.json"
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    existing["wards"] = wards
    out.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    zones = sorted({(w["zone_number"], w["zone_name"]) for w in wards})
    print(f"{len(wards)} wards in {len(zones)} zones written to {out}")


if __name__ == "__main__":
    main(sys.argv[1])
