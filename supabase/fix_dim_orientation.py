"""Backfill width_cm/height_cm by cross-checking the lot's image aspect.

Operator audit 2026-06-26: lot 28703 had width_cm/height_cm swapped
relative to the actual painting orientation.  Cross-check across all
sources found ~32% of probed lots had the same problem — French
catalogues ('38 x 54 cm') conventionally write H × W, but the
shared parser was guessing by which side was larger.

Heuristic
---------
For every Supabase row with image_url + width_cm + height_cm:
  - Skip if either dim is near-square (<10% off square) — the photo
    aspect is too noisy to decide a swap when the painting itself is
    nearly square.
  - Fetch the image, measure pixel aspect.  Skip if photo is near-
    square (<10% off square) — auction houses crop/pad with frames
    or backgrounds, so a 1.05 ratio doesn't mean landscape.
  - If dim says LANDSCAPE (w>h) but photo is PORTRAIT (or vice
    versa), swap width_cm/height_cm and rewrite the dimensions
    string with the two numbers reordered.

The fix never UNswaps a row by accident: we only ever flip when
both signals are decisive and they CONFLICT.

Designed to be safe to re-run nightly — no-op for any row that
already agrees with the photo orientation.
"""
import os
import io
import re
import sys
import time

import requests
from PIL import Image


def _env(name):
    v = os.environ.get(name)
    if v:
        return v
    # Fallback: parse .env.local in repo root (same convention as
    # other one-off scripts in this folder).
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env.local")
    if os.path.exists(env_path):
        text = open(env_path).read()
        m = re.search(rf"{re.escape(name)}=(\S+)", text)
        if m:
            return m.group(1)
    raise RuntimeError(f"missing {name}")


SU = _env("SUPABASE_URL")
SK = _env("SUPABASE_SERVICE_ROLE_KEY")
H_GET = {"apikey": SK, "Authorization": f"Bearer {SK}"}
H_PATCH = {
    "apikey": SK,
    "Authorization": f"Bearer {SK}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

NEAR_SQUARE = 0.10  # require ≥10% imbalance on both sides to act


def fetch_candidates():
    """Pull every row with image_url + (W, H) populated, paginated."""
    offset = 0
    while True:
        r = requests.get(
            f"{SU}/rest/v1/sale_results",
            params={
                "image_url": "not.is.null",
                "width_cm": "not.is.null",
                "height_cm": "not.is.null",
                "select": "id,source,image_url,width_cm,height_cm,dimensions",
                "limit": "1000",
                "offset": str(offset),
                "order": "id.asc",
            },
            headers=H_GET,
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            return
        for row in page:
            yield row
        if len(page) < 1000:
            return
        offset += 1000


def image_aspect(sess, url):
    try:
        rr = sess.get(url, timeout=8)
        if rr.status_code != 200:
            return None
        img = Image.open(io.BytesIO(rr.content))
        return img.size
    except Exception:
        return None


def swap_dim_string(dim_str):
    """Rewrite '38 x 54 cm' as '54 x 38 cm', preserving suffix."""
    if not dim_str:
        return dim_str
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)", dim_str)
    if not m:
        return dim_str
    a, b = m.group(1), m.group(2)
    return dim_str[: m.start()] + f"{b} x {a}" + dim_str[m.end():]


def main():
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"

    probed = 0
    fixed = 0
    fetch_err = 0
    for row in fetch_candidates():
        try:
            w_cm = float(row["width_cm"])
            h_cm = float(row["height_cm"])
        except (TypeError, ValueError):
            continue
        if w_cm == 0 or h_cm == 0:
            continue
        dim_ratio = w_cm / h_cm
        if abs(dim_ratio - 1) < NEAR_SQUARE:
            continue
        size = image_aspect(sess, row["image_url"])
        if size is None:
            fetch_err += 1
            continue
        pw, ph = size
        if pw == 0 or ph == 0:
            continue
        img_ratio = pw / ph
        if abs(img_ratio - 1) < NEAR_SQUARE:
            continue
        probed += 1
        if (dim_ratio > 1) == (img_ratio > 1):
            continue  # already consistent
        # CONFLICT — swap
        new_dim = swap_dim_string(row.get("dimensions") or "")
        rp = requests.patch(
            f"{SU}/rest/v1/sale_results",
            params={"id": f"eq.{row['id']}"},
            headers=H_PATCH,
            json={"width_cm": h_cm, "height_cm": w_cm, "dimensions": new_dim},
            timeout=10,
        )
        if rp.status_code < 300:
            fixed += 1
        if probed % 50 == 0:
            print(f"  ... probed={probed} fixed={fixed} fetch_err={fetch_err}", flush=True)

    print(f"\nDone: probed={probed} fixed={fixed} fetch_err={fetch_err}")


if __name__ == "__main__":
    main()
