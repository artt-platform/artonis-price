"""Pull hammer prices for past Drouot lots stuck at status=estimate_only.

Operator audit 2026-06-27: lot 27842 (Nguyễn Huyên, sale 178740) sat at
estimate_only for 2 days even though Drouot exposes the realised
result in the lot detail page's data island as `result:10400`
(unquoted JavaScript style).  The main drouot crawler had a per-lot
re-fetch fallback but it only ran inside the sale-page loop.  Once
the sale closed and the sale page started returning lotCount=0, the
watchlist retired the URL and the per-lot fallback never executed
for already-inserted rows.

This script walks the DB directly (independent of watchlist) and
for every past estimate_only Drouot/sub-house row:
  1. Fetches the lot URL via cloudscraper.
  2. Reads `result:<n>` and `fees:<pct>` from the data island.
  3. If result > 0: PATCH hammer_price, price_with_premium, price_usd,
     price_with_premium_usd, price_per_m2_usd, status=sold.

Designed to be safe to re-run nightly — no-op for any row that
either already has a hammer or whose lot page still reports
result:0 (genuinely no published hammer yet).
"""
import os
import re
import sys
import time
from pathlib import Path

# Allow `from crawlers...` when this script runs from cron with cwd=repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from crawlers.drouot import _make_scraper  # cloudscraper preconfigured for Drouot
from crawlers.common import to_usd


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

# Drouot lot data island keys.  Both `result` (final price unquoted)
# and `tenderingAmount` carry the hammer; they always agree.  `fees`
# is the buyer's premium percentage stored as an integer
# (30 → 1.30× multiplier).
_RESULT_RE = re.compile(r"\bresult\s*:\s*(\d+)")
_FEES_RE = re.compile(r"\bfees\s*:\s*(\d+)")
_CURRENCY_RE = re.compile(r'currencyId\s*:\s*"([A-Z]{3})"')


def fetch_candidates():
    """Past estimate_only Drouot-or-subhouse lots needing hammer."""
    offset = 0
    while True:
        r = requests.get(
            f"{SU}/rest/v1/sale_results",
            params={
                "via_platform": "eq.drouot",
                "status": "eq.estimate_only",
                "sale_date": "lt.today",
                "source_url": "not.is.null",
                "select": "id,source_url,width_cm,height_cm,artist_name_raw",
                "limit": "500",
                "offset": str(offset),
                "order": "id.asc",
            },
            headers=H_GET,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < 500:
            return
        offset += 500


def patch_hammer(row, hammer, fees_pct, currency):
    premium = hammer * (1 + fees_pct / 100)
    price_usd, _ = to_usd(hammer, currency)
    premium_usd, _ = to_usd(premium, currency)
    patch = {
        "hammer_price": hammer,
        "price_with_premium": round(premium, 2),
        "price_usd": round(price_usd, 2) if price_usd else None,
        "price_with_premium_usd": round(premium_usd, 2) if premium_usd else None,
        "currency": currency,
        "status": "sold",
    }
    w = row.get("width_cm")
    h = row.get("height_cm")
    if w and h:
        try:
            area = float(w) * float(h) / 10000
            if area > 0 and premium_usd:
                patch["price_per_m2_usd"] = round(premium_usd / area, 2)
        except (TypeError, ValueError):
            pass
    rp = requests.patch(
        f"{SU}/rest/v1/sale_results",
        params={"id": f"eq.{row['id']}"},
        headers=H_PATCH,
        json=patch,
        timeout=15,
    )
    return rp.status_code < 300


def main():
    scraper = _make_scraper()
    fixed = no_result = err = 0
    for row in fetch_candidates():
        try:
            r = scraper.get(row["source_url"], timeout=20)
            if r.status_code != 200:
                err += 1
                continue
            m = _RESULT_RE.search(r.text)
            if not m:
                no_result += 1
                continue
            result_val = int(m.group(1))
            if result_val == 0:
                no_result += 1
                continue
            fees_pct = int(_FEES_RE.search(r.text).group(1)) if _FEES_RE.search(r.text) else 30
            currency = _CURRENCY_RE.search(r.text).group(1) if _CURRENCY_RE.search(r.text) else "EUR"
            ok = patch_hammer(row, float(result_val), fees_pct, currency)
            if ok:
                fixed += 1
                print(
                    f"  id={row['id']} {row.get('artist_name_raw', '')[:25]:25s} → "
                    f"{result_val:,} {currency} (fees {fees_pct}%)",
                    flush=True,
                )
            time.sleep(0.5)  # gentle to Drouot
        except Exception as e:
            err += 1
            print(f"  id={row['id']} ERR {e}", flush=True)

    print(f"\nDone: fixed={fixed}, no_result={no_result}, err={err}")


if __name__ == "__main__":
    main()
