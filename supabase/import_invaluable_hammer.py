"""Helper script for the operator to manually feed Invaluable hammer prices.

Invaluable hides realised hammer prices behind login.  Past lots are
already in our DB with `status='estimate_only'` and `hammer_price=NULL`
(after commit eb5353b stopped the midpoint-as-hammer pattern).  The
operator can paste a few lots at a time:

  python3 supabase/import_invaluable_hammer.py

The script prompts row-by-row for:
  • Invaluable lot URL  (or lot_id from our DB if you know it)
  • hammer price + currency (USD/EUR/GBP/CAD/etc.)

It patches the matching row with the real hammer + recomputes
price_usd, price_with_premium_usd, price_per_m2_usd.  Status flips to
'sold' automatically (the DB guard requires hammer for sold status).

Stops on empty input.  Refreshes artist stats at the end.
"""
from __future__ import annotations
import os, sys
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

# Rough current FX rates → USD.  Historical accuracy is sacrificed for
# simplicity; manual imports are dominated by recent sales where rates
# are stable.  Update once a year if needed.
FX_TO_USD = {
    "USD": 1.00,
    "EUR": 1.08,
    "GBP": 1.26,
    "CAD": 0.74,
    "AUD": 0.66,
    "HKD": 0.128,
    "SGD": 0.74,
    "CHF": 1.10,
    "JPY": 0.0064,
    "CNY": 0.14,
    "MYR": 0.22,
    "THB": 0.028,
}


def find_row_by_url(url: str) -> dict | None:
    """Look up the existing sale_results row by source_url."""
    r = requests.get(
        f"{URL}/rest/v1/sale_results",
        params={"source_url": f"eq.{url}", "select": "id,artist_name_raw,artwork_title,area_m2,currency,estimate_low,estimate_high,hammer_price,status"},
        headers=H, timeout=10,
    )
    rows = r.json()
    return rows[0] if rows else None


def find_row_by_id(row_id: int) -> dict | None:
    r = requests.get(
        f"{URL}/rest/v1/sale_results",
        params={"id": f"eq.{row_id}", "select": "id,artist_name_raw,artwork_title,area_m2,currency,estimate_low,estimate_high,hammer_price,status"},
        headers=H, timeout=10,
    )
    rows = r.json()
    return rows[0] if rows else None


def patch_hammer(row_id: int, hammer: float, currency: str) -> bool:
    fx = FX_TO_USD.get(currency.upper())
    if fx is None:
        print(f"    ✗ Unknown currency {currency!r} — supported: {list(FX_TO_USD)}")
        return False
    # Get current row for area
    row = find_row_by_id(row_id)
    area = row.get("area_m2") if row else None

    price_usd = round(hammer * fx, 2)
    # House-specific buyer premium via data/auction_houses.py.
    # Falls back to 25% only when the upstream house isn't in the
    # registry.  Manual imports run against Invaluable lots so we
    # key by sale_location (Invaluable upstream label), not source.
    from data.auction_houses import AUCTION_HOUSES
    house = ((row or {}).get("sale_location") or "").lower().strip()
    rate_pct = (AUCTION_HOUSES.get(house) or {}).get("premium_rate_pct", 25.0)
    premium = round(hammer * (1 + rate_pct / 100), 2)
    premium_usd = round(premium * fx, 2)
    ppm_usd = round(price_usd / area, 2) if area else None

    patch = {
        "hammer_price": hammer,
        "currency": currency.upper(),
        "price_usd": price_usd,
        "price_with_premium": premium,
        "price_with_premium_usd": premium_usd,
        "price_per_m2_usd": ppm_usd,
        "status": "sold",
    }
    rr = requests.patch(
        f"{URL}/rest/v1/sale_results", params={"id": f"eq.{row_id}"},
        headers=H, json=patch, timeout=10,
    )
    if rr.status_code >= 300:
        print(f"    ✗ HTTP {rr.status_code}: {rr.text[:120]}")
        return False
    print(f"    ✓ id={row_id}  hammer={hammer:.0f} {currency.upper()}  → ${price_usd}  ${ppm_usd}/m²")
    return True


def main():
    print("=" * 70)
    print("Invaluable manual hammer-price importer")
    print("Press Enter on an empty line to stop.")
    print("=" * 70)
    n_ok = 0
    while True:
        print()
        url_or_id = input("Invaluable lot URL or DB id: ").strip()
        if not url_or_id:
            break
        if url_or_id.isdigit():
            row = find_row_by_id(int(url_or_id))
            row_id = int(url_or_id)
        else:
            row = find_row_by_url(url_or_id)
            row_id = row["id"] if row else None
        if not row:
            print(f"    ✗ No row found for {url_or_id!r}")
            continue
        print(f"    matched: {row['artist_name_raw']} | {(row.get('artwork_title') or '')[:50]}")
        if row.get("hammer_price"):
            ans = input(f"    Row already has hammer_price={row['hammer_price']}.  Overwrite? [y/N] ").strip().lower()
            if ans != "y":
                continue
        hammer_str = input("    Hammer price (number only, eg 6500): ").strip()
        if not hammer_str:
            continue
        try:
            hammer = float(hammer_str.replace(",", ""))
        except ValueError:
            print(f"    ✗ Not a number: {hammer_str!r}")
            continue
        currency = (input(f"    Currency [{row.get('currency','USD')}]: ").strip()
                    or row.get("currency", "USD"))
        if patch_hammer(row_id, hammer, currency):
            n_ok += 1
    print(f"\nImported {n_ok} hammer prices.")
    if n_ok:
        print("Refreshing artist stats…")
        import subprocess
        subprocess.run(["python3", str(ROOT / "supabase" / "refresh_artist_stats.py")], check=False)


if __name__ == "__main__":
    main()
