"""Cloudscraper-based fix_dim_orientation — catches CF-blocked images.

The original supabase/fix_dim_orientation.py uses `requests` and
hits a wall on Cloudflare-protected catalog images (Invaluable
CDN, some Christies endpoints, occasionally Bonhams) — operator
audit 2026-06-29 saw 542 of 2086 lots return fetch errors and
the visible orientation mismatch rate on a 100-row sample was
40 %.  This v2 swaps the HTTP client for cloudscraper so the
same image URLs return real bytes instead of the CF challenge
HTML.

Same heuristic as v1:
  • Skip if either dim or image is < 10 % off square.
  • Swap width_cm/height_cm + rewrite dimensions string when
    dim orientation disagrees with image orientation.
  • Respect EXIF orientation (tags 5-8 = rotated 90°).

PATCHes through Supabase REST directly so the change is visible
on the next page render without re-deploying anything.

Run as a one-off OR add to the cron after the v1 step — the v2
will no-op on rows v1 already corrected.
"""
import io
import os
import re
import sys
import time
from pathlib import Path

import cloudscraper
import requests
from PIL import Image, ExifTags

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
SU = os.environ["SUPABASE_URL"]
SK = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SB_R = {"apikey": SK, "Authorization": f"Bearer {SK}"}
SB_W = {**SB_R, "Content-Type": "application/json", "Prefer": "return=minimal"}

NEAR_SQUARE_RATIO = 1.10


def _effective_image_orientation(content: bytes) -> str | None:
    """Return 'L' / 'P' / 'S' (landscape / portrait / square)
    accounting for EXIF rotation, or None on decode fail.

    Operator 2026-06-30 enhancement: when raw pixel aspect is near-
    square, try painting-bbox detection inside the padded thumbnail
    (catalog houses + Invaluable serve 1000×1000 padded squares with
    white borders around the actual painting).  Use bbox aspect when
    decisively non-square + shrink_frac < 0.97 (real padding, not
    painting filling the canvas).
    """
    try:
        img = Image.open(io.BytesIO(content))
        iw, ih = img.size
        orient = None
        try:
            ex = img._getexif() if hasattr(img, "_getexif") else None
            if ex:
                for tag_id, val in ex.items():
                    if ExifTags.TAGS.get(tag_id) == "Orientation":
                        orient = val
                        break
        except Exception:  # noqa: BLE001
            pass
        if orient in (5, 6, 7, 8):
            iw, ih = ih, iw
        if iw <= 0 or ih <= 0:
            return None
        ratio = max(iw, ih) / min(iw, ih)
        # Near-square: try painting bbox before giving up.
        if ratio < NEAR_SQUARE_RATIO:
            try:
                from PIL import ImageChops
                from collections import Counter
                rgb = img.convert("RGB")
                rw, rh = rgb.size
                px = rgb.load()
                corners = [px[0, 0], px[rw - 1, 0],
                           px[0, rh - 1], px[rw - 1, rh - 1]]
                bg = Counter(corners).most_common(1)[0][0]
                bg_img = Image.new("RGB", rgb.size, bg)
                bbox = ImageChops.difference(rgb, bg_img).getbbox()
                if bbox:
                    x1, y1, x2, y2 = bbox
                    bw, bh = x2 - x1, y2 - y1
                    if bw > 0 and bh > 0:
                        bb_ratio = max(bw, bh) / min(bw, bh)
                        shrink = (bw * bh) / (rw * rh)
                        if bb_ratio >= NEAR_SQUARE_RATIO and shrink < 0.97:
                            iw, ih = bw, bh
                            ratio = bb_ratio
            except Exception:  # noqa: BLE001
                pass
        if ratio < NEAR_SQUARE_RATIO:
            return "S"
        return "L" if iw > ih else "P"
    except Exception:  # noqa: BLE001
        return None


def _fetch_candidates(offset: int, batch: int = 200) -> list[dict]:
    r = requests.get(
        f"{SU}/rest/v1/sale_results",
        params={
            "status": "neq.upcoming",
            "image_url": "not.is.null",
            "width_cm": "not.is.null",
            "height_cm": "not.is.null",
            "select": "id,image_url,width_cm,height_cm,dimensions",
            "limit": str(batch),
            "offset": str(offset),
            "order": "id.desc",
        },
        headers=SB_R,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    sc = cloudscraper.create_scraper()
    probed = fixed = skipped_sq = fetch_err = no_swap = 0
    offset = 0
    seen = set()
    while True:
        rows = _fetch_candidates(offset)
        if not rows:
            break
        for s in rows:
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            probed += 1
            w = s["width_cm"]
            h = s["height_cm"]
            try:
                d_ratio = max(w, h) / min(w, h) if min(w, h) > 0 else 0
            except (TypeError, ZeroDivisionError):
                continue
            if d_ratio < NEAR_SQUARE_RATIO:
                skipped_sq += 1
                continue
            try:
                ir = sc.get(s["image_url"], timeout=12)
                if ir.status_code != 200:
                    fetch_err += 1
                    continue
                eo = _effective_image_orientation(ir.content)
            except Exception:  # noqa: BLE001
                fetch_err += 1
                continue
            if not eo or eo == "S":
                skipped_sq += 1
                continue
            dim_landscape = w > h
            img_landscape = eo == "L"
            if dim_landscape == img_landscape:
                no_swap += 1
                continue
            # Swap.
            new_w, new_h = h, w
            patch = {
                "width_cm": new_w,
                "height_cm": new_h,
                "dimensions": f"{new_w:g} x {new_h:g} cm",
                "area_m2": round(new_w * new_h / 10000, 4),
            }
            rp = requests.patch(
                f"{SU}/rest/v1/sale_results",
                params={"id": f"eq.{s['id']}"},
                headers=SB_W,
                json=patch,
                timeout=10,
            )
            if rp.status_code < 300:
                fixed += 1
                if fixed <= 12:
                    print(
                        f"  swap lot {s['id']}: ({w:g},{h:g}) → ({new_w:g},{new_h:g})"
                    )
            time.sleep(0.25)
        if len(rows) < 200:
            break
        offset += 200
    print(
        f"\nDone: probed={probed}, fixed={fixed}, fetch_err={fetch_err}, "
        f"skipped_sq={skipped_sq}, no_swap={no_swap}"
    )


if __name__ == "__main__":
    main()
