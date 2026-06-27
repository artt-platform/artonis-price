"""Manual Invaluable hammer entry — bypass CF entirely.

CF protection on invaluable.com is too aggressive for Playwright
to beat reliably.  Sothebys works fine via direct GraphQL (no
browser).  For Invaluable we fall back to operator-driven entry:

  1. Script prints the next 10 lots needing a hammer + their URLs
  2. Operator opens each URL in normal Chrome (no automation)
  3. Reads the hammer price from the page
  4. Pastes back into terminal
  5. Script computes USD + premium + $/m², updates DB

Run:
  python3 supabase/import_invaluable_manual.py
  python3 supabase/import_invaluable_manual.py --limit 5
"""
from __future__ import annotations
import os, sys, re, argparse
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}

FX = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.26, "CAD": 0.74, "AUD": 0.66,
    "HKD": 0.128, "SGD": 0.74, "CHF": 1.10, "JPY": 0.0064, "CNY": 0.14,
    "MYR": 0.21, "THB": 0.028,
}


def fetch_queue(limit: int) -> list[dict]:
    params = {
        "select": "id,source_url,artist_name_raw,artwork_title,sale_date,estimate_low,estimate_high,currency,area_m2",
        "source": "eq.invaluable",
        "hammer_price": "is.null",
        "source_url": "not.is.null",
        "status": "not.in.(passed,withdrawn)",
        "order": "estimate_low.desc.nullslast,sale_date.desc.nullslast",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/sale_results", params=params, headers=H, timeout=20)
    return r.json() if r.ok else []


def patch_hammer(row_id: int, hammer: float, currency: str, area_m2: float | None,
                 source: str = "") -> bool:
    fx = FX.get(currency.upper(), 1.0)
    price_usd = round(hammer * fx, 2)
    # House-specific buyer premium from data/auction_houses.py.
    # Falls back to 25% only when source isn't catalogued.
    from data.auction_houses import AUCTION_HOUSES
    rate_pct = (AUCTION_HOUSES.get(source) or {}).get("premium_rate_pct", 25.0)
    premium = round(hammer * (1 + rate_pct / 100), 2)
    premium_usd = round(premium * fx, 2)
    ppm = round(price_usd / area_m2, 2) if area_m2 else None
    payload = {
        "hammer_price": hammer,
        "currency": currency.upper(),
        "price_usd": price_usd,
        "price_with_premium": premium,
        "price_with_premium_usd": premium_usd,
        "price_per_m2_usd": ppm,
        "status": "sold",
    }
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json=payload, timeout=10)
    return r.status_code < 300


def mark_passed(row_id: int) -> bool:
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json={"status": "passed"}, timeout=10)
    return r.status_code < 300


def mark_sold_hidden(row_id: int) -> bool:
    """Some lots show 'Sold' but no price even with login (consignor
    privacy).  Record that we observed a sale without overwriting the
    hammer slot, so the lot stops re-appearing in the queue."""
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json={"status": "sold_hidden"}, timeout=10)
    if r.status_code >= 300:
        # Fall back to 'unknown' if the enum doesn't have sold_hidden
        r = requests.patch(f"{URL}/rest/v1/sale_results",
                           params={"id": f"eq.{row_id}"},
                           headers=H, json={"status": "unknown"}, timeout=10)
    return r.status_code < 300


def parse_amount_currency(text: str) -> tuple[float, str] | None:
    """Parse '$5,000' / '5000 USD' / 'HKD 1,200,000' / '5,000'."""
    text = text.strip().replace(",", "")
    # Currency symbols / codes
    cur = None
    cur_map = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
    for sym, c in cur_map.items():
        if sym in text:
            cur = c
            text = text.replace(sym, "")
            break
    if cur is None:
        m = re.search(r"\b(USD|EUR|GBP|HKD|CAD|AUD|SGD|CHF|JPY|CNY|MYR|THB)\b", text, re.IGNORECASE)
        if m:
            cur = m.group(1).upper()
            text = re.sub(r"\b(?:USD|EUR|GBP|HKD|CAD|AUD|SGD|CHF|JPY|CNY|MYR|THB)\b", "", text, flags=re.IGNORECASE)
    text = text.strip()
    m_num = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m_num:
        return None
    try:
        amt = float(m_num.group(1))
    except ValueError:
        return None
    if amt <= 0:
        return None
    return amt, (cur or "USD")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    rows = fetch_queue(args.limit)
    if not rows:
        print("No Invaluable lots missing hammer.")
        return

    print("=" * 70)
    print(f"  Invaluable manual hammer entry — {len(rows)} lots queued")
    print("=" * 70)
    print()
    print("  For each lot:")
    print("    - Open the URL in Chrome (normal, not automation)")
    print("    - Read the hammer price on the page")
    print("    - Type 'hammer currency' (e.g. '5000 USD') and press Enter")
    print("    - Or type 'p' if the lot passed/unsold")
    print("    - Or type 'h' if page shows 'Sold' but price hidden")
    print("    - Or type 's' to skip this lot, 'q' to quit")
    print()

    n_ok = n_passed = n_skipped = n_hidden = 0
    for i, row in enumerate(rows, 1):
        title = (row.get("artwork_title") or "")[:60]
        print(f"\n[{i}/{len(rows)}] {row['artist_name_raw']} | {title}")
        if row.get("estimate_low"):
            est_cur = row.get("currency") or ""
            est = f"  estimate: {est_cur} {int(row['estimate_low']):,} – {int(row['estimate_high'] or row['estimate_low']):,}"
            print(est)
        print(f"  URL: {row['source_url']}")
        while True:
            try:
                ans = input("  hammer (e.g. '5000 USD' / p=passed / h=sold-hidden / s=skip / q=quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
            if not ans:
                continue
            if ans.lower() == "q":
                print("Quit.")
                _summarize(n_ok, n_passed, n_skipped, n_hidden)
                return
            if ans.lower() == "s":
                n_skipped += 1
                break
            if ans.lower() == "p":
                if mark_passed(row["id"]):
                    n_passed += 1
                    print(f"  → marked passed")
                else:
                    print(f"  ✗ DB patch failed")
                break
            if ans.lower() == "h":
                if mark_sold_hidden(row["id"]):
                    n_hidden += 1
                    print(f"  → marked sold (price hidden)")
                else:
                    print(f"  ✗ DB patch failed")
                break
            parsed = parse_amount_currency(ans)
            if not parsed:
                print(f"  ✗ couldn't parse {ans!r} — try '5000 USD' or '$5,000'")
                continue
            hammer, cur = parsed
            if patch_hammer(row["id"], hammer, cur, row.get("area_m2"),
                            source=row.get("source", "")):
                fx = FX.get(cur, 1.0)
                print(f"  ✓ {cur} {hammer:,.0f} → ${hammer * fx:,.0f} USD saved")
                n_ok += 1
            else:
                print(f"  ✗ DB patch failed")
            break

    _summarize(n_ok, n_passed, n_skipped, n_hidden)


def _summarize(n_ok: int, n_passed: int, n_skipped: int, n_hidden: int = 0) -> None:
    print()
    print("=" * 70)
    parts = [f"{n_ok} hammer", f"{n_passed} passed", f"{n_hidden} sold-hidden", f"{n_skipped} skipped"]
    print(f"  Done: " + " + ".join(parts))
    print("=" * 70)
    print("\n  Refreshing artist stats…")
    import subprocess
    subprocess.run(["python3", str(ROOT / "supabase" / "refresh_artist_stats.py")], check=False)


if __name__ == "__main__":
    main()
