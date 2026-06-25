"""Backfill image_url for existing Drouot lots in Supabase.

The Drouot crawler started capturing photo.path → image_url on
commit 6906808 (2026-06-26).  Lots inserted BEFORE that commit have
image_url = NULL.  This script fetches each Drouot lot's source_url,
re-parses the data island for photo.path, builds the public
img.drouot.com URL, and PATCHes the row.

Skips:
  - Rows already with image_url (idempotent re-run)
  - Drouot URLs that 404 (lot dropped)
  - Lots without a photo:{path:""} field

Rate-limit: 0.5s between requests.  Adjust SLEEP if needed.

Run: python3 supabase/backfill_drouot_images.py
"""
from __future__ import annotations
import os, sys, time, re
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Quick env load
def _load_env():
    p = ROOT / ".env.local"
    if not p.exists(): return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)
_load_env()

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

from crawlers.drouot import _make_scraper, _drouot_image_url

# Match Drouot data-island photo block: photo:{...path:"FOO"...}
# Non-greedy [^}]*? to avoid eating past the closing brace.
PHOTO_RE = re.compile(r'photo:\{[^}]*?path:\s*"([^"]+)"')

SLEEP = 0.5  # seconds between requests — kind to Drouot


def fetch_lots_without_image() -> list[dict]:
    """Pull all Drouot lots that need image backfill."""
    all_rows: list[dict] = []
    page = 0
    while True:
        r = requests.get(
            f"{URL}/rest/v1/sale_results",
            params={
                "select": "id,source_url",
                "or": "(via_platform.eq.drouot,source.eq.drouot)",
                "image_url": "is.null",
                "source_url": "not.is.null",
                "offset": page * 1000,
                "limit": 1000,
            },
            headers=H, timeout=20,
        )
        rows = r.json()
        if not rows: break
        all_rows.extend(rows)
        if len(rows) < 1000: break
        page += 1
    return all_rows


def backfill_one(row: dict, scraper) -> str:
    """Return 'ok' / 'no_photo' / 'http_err' / 'parse_err'."""
    src_url = row["source_url"]
    try:
        r = scraper.get(src_url, timeout=15)
        r.encoding = "utf-8"
    except Exception:
        return "http_err"
    if r.status_code != 200:
        return "http_err"
    m = PHOTO_RE.search(r.text)
    if not m:
        return "no_photo"
    img_url = _drouot_image_url(m.group(1))
    if not img_url:
        return "no_photo"
    rr = requests.patch(
        f"{URL}/rest/v1/sale_results",
        params={"id": f"eq.{row['id']}"},
        headers=H, json={"image_url": img_url}, timeout=10,
    )
    return "ok" if rr.status_code < 300 else "parse_err"


def main():
    print("Drouot image backfill")
    print("=" * 50)
    rows = fetch_lots_without_image()
    print(f"Lots needing image_url: {len(rows)}")
    if not rows:
        print("Nothing to do.")
        return

    scraper = _make_scraper()
    counts = {"ok": 0, "no_photo": 0, "http_err": 0, "parse_err": 0}
    for i, row in enumerate(rows, 1):
        result = backfill_one(row, scraper)
        counts[result] += 1
        if i % 10 == 0 or result != "ok":
            print(f"  [{i:>4}/{len(rows)}] id={row['id']:>5} {result}")
        time.sleep(SLEEP)

    print("\n=== Done ===")
    for k, v in counts.items():
        print(f"  {k:<12} {v}")


if __name__ == "__main__":
    main()
