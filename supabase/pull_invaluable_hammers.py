"""Pull hammers for Invaluable past estimate_only lots.

Operator 2026-06-27: 111 Invaluable rows sat at status=estimate_only
because the listing-card extraction path can't see the realised
'Sold' amount (login wall on Invaluable's public face).  But the
LOT DETAIL page exposes the post-sale truth in a data island
unquoted JavaScript field:

    "isLotClosed":true,"soldAmount":1400,"currentBid":450, ...

Cloudscraper passes the CF check from a clean IP, so a sequential
walk works fine.  This script:

  1. Pulls all sale_results rows with source='invaluable',
     status='estimate_only', sale_date < today, source_url not null.
  2. Fetches each source_url via cloudscraper (gentle 0.5s pace).
  3. Parses 'soldAmount:<n>' from the response body.
  4. If soldAmount > 0: PATCH hammer_price (= soldAmount),
     price_with_premium = hammer × 1.25 (Invaluable's typical buyer
     premium), price_usd / premium_usd via to_usd, status='sold'.
  5. If soldAmount = 0 or absent: leave as estimate_only.

DB-trigger guard (20260627000000_midpoint_hammer_trigger.sql) blocks
any synthetic value, so a malformed parse can't corrupt the row.

Designed to run from the nightly cron — idempotent + no-op when the
upstream still reports no Sold price.
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import cloudscraper
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

# Invaluable data island carries the post-sale truth at the same key
# the puller already trusts in pull_hammers_local.py.  Use the same
# patterns here so any schema rename is fixed in both places.
_SOLD_AMOUNT_RE = re.compile(r'"soldAmount"\s*:\s*([\d.]+)')
_CURRENCY_RE = re.compile(r'"currency"\s*:\s*"([A-Z]{3})"')
_IS_CLOSED_RE = re.compile(r'"isLotClosed"\s*:\s*(true|false)')


def fetch_candidates():
    """Past estimate_only Invaluable rows, paginated."""
    offset = 0
    while True:
        r = requests.get(
            f"{SU}/rest/v1/sale_results",
            params={
                "source": "eq.invaluable",
                "status": "eq.estimate_only",
                "sale_date": "lt.today",
                "source_url": "not.is.null",
                "select": "id,source_url,artist_name_raw,width_cm,height_cm,currency,sale_location",
                "limit": "200",
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
        if len(rows) < 200:
            return
        offset += 200


def main():
    scraper = cloudscraper.create_scraper()
    fixed = no_sold = err = 0
    for row in fetch_candidates():
        try:
            r = scraper.get(row["source_url"], timeout=20)
            if r.status_code != 200:
                err += 1
                continue
            m_closed = _IS_CLOSED_RE.search(r.text)
            if not m_closed or m_closed.group(1) != "true":
                # Upstream hasn't closed the lot yet — try next cron run
                no_sold += 1
                continue
            m_sold = _SOLD_AMOUNT_RE.search(r.text)
            if not m_sold:
                no_sold += 1
                continue
            sold_val = float(m_sold.group(1))
            if sold_val <= 0:
                no_sold += 1
                continue
            currency = row.get("currency") or "USD"
            m_cur = _CURRENCY_RE.search(r.text)
            if m_cur:
                currency = m_cur.group(1)
            # Invaluable typical buyer's premium = 25% of hammer.
            # (House-specific rates vary; 25% is the canonical default
            # the rest of the Artonis stack uses — pull_hammers_local +
            # import_invaluable_hammer both apply it.)
            premium = round(sold_val * 1.25, 2)
            price_usd, _ = to_usd(sold_val, currency)
            premium_usd, _ = to_usd(premium, currency)
            patch = {
                "hammer_price": sold_val,
                "price_with_premium": premium,
                "price_usd": round(price_usd, 2) if price_usd else None,
                "price_with_premium_usd": round(premium_usd, 2) if premium_usd else None,
                "currency": currency,
                "status": "sold",
            }
            w = row.get("width_cm")
            h = row.get("height_cm")
            if w and h and premium_usd:
                try:
                    area = float(w) * float(h) / 10000
                    if area > 0:
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
            if rp.status_code < 300:
                fixed += 1
                print(
                    f"  id={row['id']} {(row.get('artist_name_raw') or '')[:22]:22s} "
                    f"@ {(row.get('sale_location') or '')[:18]:18s} → "
                    f"{currency} {sold_val:,.0f}",
                    flush=True,
                )
            else:
                err += 1
                print(f"  id={row['id']} PATCH HTTP {rp.status_code}: {rp.text[:120]}", flush=True)
            time.sleep(0.5)
        except Exception as e:
            err += 1
            print(f"  id={row['id']} ERR {e}", flush=True)

    print(f"\nDone: fixed={fixed}, no_sold={no_sold}, err={err}")


if __name__ == "__main__":
    main()
