"""Backfill missing image_url for existing Invaluable rows.

The Invaluable crawler captures the listing-page thumbnail at INSERT
time (see crawlers/invaluable.py:_extract_cards_via_browser), but
older rows that were inserted before that fix still have
image_url=NULL.  Walking the artist-listing pages directly from a
single host hits Cloudflare's rate limit after ~5-6 artists (the
machine's IP gets flagged for the rest of the session).

This script piggybacks on the nightly GitHub Actions cron, where
every run uses a fresh runner IP and CF treats it as a clean
visitor.  For each VN artist with remaining NULL-image lots:

  1. Fresh Playwright browser (stealth flags + spoofed navigator).
  2. Warm up the homepage so cookies / headers look organic.
  3. Visit /artist/<slug>/sold-at-auction-prices/.
  4. Extract cards via the same helper the crawler uses.
  5. PATCH image_url on DB rows whose canonical source_url matches a
     card href.

The script no-ops cleanly when:
  - Cloudflare challenges the request (cards=0 → skipped, retry next
    cron).
  - A given artist has no remaining NULL-image rows (skipped).
  - The URL canonicalisation finds no match (extraction succeeded but
    the row was already filled by a sibling run).
"""
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from playwright.sync_api import sync_playwright

from crawlers.invaluable import _extract_cards_via_browser, BASE, VN_ARTISTS


def _env(name):
    v = os.environ.get(name)
    if v:
        return v
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k == name:
                    return val
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

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def canon(u):
    sp = urlsplit(u)
    return urlunsplit((sp.scheme, sp.netloc, sp.path.rstrip("/"), "", ""))


def remaining_urls():
    """source_url → row_id for Invaluable lots needing image_url."""
    out = {}
    offset = 0
    while True:
        r = requests.get(
            f"{SU}/rest/v1/sale_results",
            params={
                "source": "eq.invaluable",
                "image_url": "is.null",
                "select": "id,source_url,artist_name_raw",
                "limit": "500",
                "offset": str(offset),
                "order": "id.asc",
            },
            headers=H_GET,
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        for row in page:
            if row.get("source_url"):
                out[canon(row["source_url"])] = row["id"]
        if len(page) < 500:
            break
        offset += 500
    return out


def main():
    url_to_id = remaining_urls()
    print(f"Invaluable lots needing image: {len(url_to_id)}", flush=True)
    if not url_to_id:
        return

    # Only visit artists with remaining lots.  Build the set from the
    # artist_name_raw of unfilled rows so we don't waste calls on
    # artists already fully backfilled.
    r = requests.get(
        f"{SU}/rest/v1/sale_results",
        params={
            "source": "eq.invaluable",
            "image_url": "is.null",
            "select": "artist_name_raw",
        },
        headers=H_GET,
        timeout=30,
    ).json()
    needed_names = {s.get("artist_name_raw") for s in r if s.get("artist_name_raw")}
    slug_map = {name: slug for slug, name in VN_ARTISTS}
    to_visit = [(slug_map[n], n) for n in needed_names if n in slug_map]
    print(f"Artists to visit: {len(to_visit)}", flush=True)

    total_patched = 0
    for i, (slug, name) in enumerate(to_visit):
        cards_count = 0
        local_p = 0
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                ctx = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                )
                page = ctx.new_page()
                # Warm-up — homepage cookie / fingerprint
                page.goto(
                    "https://www.invaluable.com/",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                page.wait_for_timeout(2500 + random.randint(0, 2000))
                # Artist listing
                page.goto(
                    f"{BASE}/artist/{slug}/sold-at-auction-prices/",
                    timeout=45000,
                    wait_until="domcontentloaded",
                )
                page.wait_for_timeout(4500 + random.randint(0, 2500))
                title = page.evaluate("document.title") or ""
                if "Just a moment" in title or "Cloudflare" in title:
                    print(f"  [{i+1}/{len(to_visit)}] {name[:25]:25s}: CF — skip", flush=True)
                    browser.close()
                    time.sleep(20)
                    continue
                # Scroll to load all lazy thumbnails
                for _ in range(4):
                    page.evaluate("window.scrollBy(0, 2000)")
                    page.wait_for_timeout(1300 + random.randint(0, 500))
                cards = _extract_cards_via_browser(page) or []
                cards_count = len(cards)
                for c in cards:
                    if not c.get("image"):
                        continue
                    href = canon(urljoin(BASE, c.get("href", "")))
                    if href not in url_to_id:
                        continue
                    rp = requests.patch(
                        f"{SU}/rest/v1/sale_results",
                        params={"id": f"eq.{url_to_id[href]}"},
                        headers=H_PATCH,
                        json={"image_url": c["image"]},
                        timeout=10,
                    )
                    if rp.status_code < 300:
                        local_p += 1
                browser.close()
            total_patched += local_p
            print(
                f"  [{i+1}/{len(to_visit)}] {name[:25]:25s}: cards={cards_count:3d} patched={local_p}",
                flush=True,
            )
        except Exception as e:
            print(
                f"  [{i+1}/{len(to_visit)}] {name[:25]:25s}: ERR {str(e)[:80]}",
                flush=True,
            )
        # Long cooldown between artists — CF watches request cadence.
        if i < len(to_visit) - 1:
            time.sleep(30 + random.randint(0, 20))

    print(f"\nDone: patched={total_patched}", flush=True)


if __name__ == "__main__":
    main()
