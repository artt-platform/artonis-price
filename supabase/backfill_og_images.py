"""One-shot backfill: fetch og:image for Bonhams + Aguttes lots that
don't yet have image_url set.  Both sites embed a clean og:image
meta tag in the lot HTML — single GET per lot, no auth required.

Run once after deploy:
  python3 supabase/backfill_og_images.py
  python3 supabase/backfill_og_images.py --source bonhams --limit 50

Going forward, the live crawlers (bonhams.py, aguttes.py) call the
same _fetch_og_image() helper at insert time, so this script only
ever needs a re-run if we batch-import old lots.
"""
from __future__ import annotations
import os, re, sys, time, argparse
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = ROOT / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}


OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"', re.IGNORECASE)


def fetch_og_image(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = OG_IMAGE_RE.search(r.text)
    if not m:
        return None
    img = m.group(1).strip()
    # Unescape & in URL params
    img = img.replace("&amp;", "&")
    return img if img.startswith("http") else None


def get_lots_missing_image(source: str, limit: int) -> list[dict]:
    params = {
        "select": "id,source_url,artist_name_raw",
        "source": f"eq.{source}",
        "image_url": "is.null",
        "source_url": "not.is.null",
        "order": "sale_date.desc.nullslast",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/sale_results", params=params, headers=H, timeout=20)
    return r.json() if r.ok else []


def patch_image(row_id: int, image_url: str) -> bool:
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json={"image_url": image_url}, timeout=10)
    return r.status_code < 300


def process(source: str, limit: int) -> None:
    rows = get_lots_missing_image(source, limit)
    print(f"\n[{source}] {len(rows)} lots missing image_url")
    n_ok = n_skip = n_fail = 0
    for i, row in enumerate(rows, 1):
        url = row["source_url"]
        img = fetch_og_image(url)
        if not img:
            print(f"  [{i}/{len(rows)}] {row['artist_name_raw'][:30]:30s} → no og:image")
            n_fail += 1
        elif patch_image(row["id"], img):
            print(f"  [{i}/{len(rows)}] {row['artist_name_raw'][:30]:30s} → {img[:60]}…")
            n_ok += 1
        else:
            n_skip += 1
        # Polite delay — both sites are fine with this rate
        time.sleep(0.5)
    print(f"\n[{source}] done: {n_ok} OK, {n_fail} no-image, {n_skip} patch-failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["bonhams", "aguttes", "both"], default="both")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    if args.source in ("bonhams", "both"):
        process("bonhams", args.limit)
    if args.source in ("aguttes", "both"):
        process("aguttes", args.limit)


if __name__ == "__main__":
    main()
