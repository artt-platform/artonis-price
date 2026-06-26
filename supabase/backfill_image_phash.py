"""Compute pHash for every sale_results row that has an image_url.

pHash = perceptual hash, 64-bit (16 hex chars).  Used to detect
re-listed artworks across auctions / houses — title + dims matching
gives false positives (different artworks share both).

Run:
  python3 supabase/backfill_image_phash.py
  python3 supabase/backfill_image_phash.py --source millon
  python3 supabase/backfill_image_phash.py --refresh   # recompute ALL
"""
from __future__ import annotations
import os, sys, io, argparse, time
from pathlib import Path
import requests
from PIL import Image, UnidentifiedImageError
import imagehash

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    p = ROOT / ".env.local"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"


def compute_phash(image_url: str) -> str | None:
    """Download + compute 64-bit pHash.  Returns 16-char hex."""
    try:
        r = requests.get(image_url, headers={"User-Agent": UA}, timeout=15, stream=True)
        if r.status_code != 200:
            return None
        # Stream-read up to 4 MiB to avoid blowing memory on giant images
        buf = io.BytesIO()
        for chunk in r.iter_content(64 * 1024):
            buf.write(chunk)
            if buf.tell() > 4 * 1024 * 1024:
                break
        buf.seek(0)
        img = Image.open(buf)
        img.load()
    except (requests.RequestException, UnidentifiedImageError, OSError):
        return None
    try:
        h = imagehash.phash(img, hash_size=8)  # 8x8 grid → 64-bit
        return str(h)  # 16-char hex
    except Exception:
        return None


def fetch_pending(source: str | None, limit: int, refresh: bool) -> list[dict]:
    params = {
        "select": "id,image_url",
        "image_url": "not.is.null",
        "limit": str(limit),
    }
    if not refresh:
        params["image_phash"] = "is.null"
    if source:
        params["source"] = f"eq.{source}"
    r = requests.get(f"{URL}/rest/v1/sale_results",
                     params=params, headers=H, timeout=30)
    return r.json() if r.ok else []


def patch_phash(row_id: int, phash: str) -> bool:
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json={"image_phash": phash}, timeout=10)
    return r.status_code < 300


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--refresh", action="store_true",
                    help="Recompute phash even for rows that already have one")
    ap.add_argument("--sleep", type=float, default=0.15,
                    help="Seconds between requests (rate-limit politeness)")
    args = ap.parse_args()

    rows = fetch_pending(args.source, args.limit, args.refresh)
    print(f"Lots queued: {len(rows)}"
          + (f" (source={args.source})" if args.source else "")
          + (" [refresh]" if args.refresh else ""))
    n_ok = n_fail = 0
    for i, row in enumerate(rows, 1):
        phash = compute_phash(row["image_url"])
        if phash and patch_phash(row["id"], phash):
            n_ok += 1
            if i <= 5 or i % 50 == 0:
                print(f"  [{i}/{len(rows)}] id={row['id']} phash={phash}")
        else:
            n_fail += 1
        time.sleep(args.sleep)
    print(f"\nDone: {n_ok} hashed, {n_fail} failed")


if __name__ == "__main__":
    main()
