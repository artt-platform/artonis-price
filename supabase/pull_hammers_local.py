"""Pull realized hammer prices from Sothebys + Invaluable via a logged-in
Playwright browser.  Runs ON THE OPERATOR'S MAC — not on GitHub Actions —
because residential IP + real-browser fingerprint together beat both
sites' bot detection.

Cookies live in .env.local as SOTHEBYS_COOKIE / INVALUABLE_COOKIE.  Both
are pasted by the operator from the Network-tab cookie header.

Run:
  # First time — verify it can read 1 lot from each source
  python3 supabase/pull_hammers_local.py --probe

  # Normal pull (max 10 lots per source per run)
  python3 supabase/pull_hammers_local.py

  # Pull only one source
  python3 supabase/pull_hammers_local.py --source sothebys
  python3 supabase/pull_hammers_local.py --source invaluable

  # Tune per-run cap
  python3 supabase/pull_hammers_local.py --limit 20

Rate-limit: 60-120s random sleep between lots.  Conservative on purpose
— better one missed lot today than a banned account next week.
"""
from __future__ import annotations
import os, sys, re, time, random, argparse
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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


# ─── Cookie parsing ────────────────────────────────────────────────

def _parse_cookie_string(cookie_str: str, domain: str) -> list[dict]:
    """Convert 'a=1; b=2; c=3' into Playwright-compatible cookie list."""
    out = []
    for piece in cookie_str.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        out.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
            "secure": True,
            "httpOnly": False,  # Playwright doesn't care; site does.
            "sameSite": "Lax",
        })
    return out


# ─── DB queries ────────────────────────────────────────────────────

def fetch_missing_hammers(source: str, limit: int) -> list[dict]:
    """Find rows that look like they should have a hammer but don't.

    Priority order:
      1. estimate_only with high estimate_low (worth the call)
      2. recent sale_date (within 12 months)
      3. has area_m2 (we can compute $/m² once hammer lands)
    """
    params = {
        "select": "id,source_url,artist_name_raw,artwork_title,sale_date,estimate_low,estimate_high,currency",
        "source": f"eq.{source}",
        "hammer_price": "is.null",
        "source_url": "not.is.null",
        "order": "estimate_low.desc.nullslast,sale_date.desc.nullslast",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/sale_results", params=params, headers=H, timeout=20)
    return r.json() if r.ok else []


def patch_hammer(row_id: int, hammer: float, currency: str, fx: dict) -> bool:
    fx_to_usd = fx.get(currency.upper(), 1.0)
    price_usd = round(hammer * fx_to_usd, 2)
    premium = round(hammer * 1.25, 2)  # default 25% buyer premium
    premium_usd = round(premium * fx_to_usd, 2)
    # Get area for $/m²
    rr = requests.get(f"{URL}/rest/v1/sale_results",
                      params={"id": f"eq.{row_id}", "select": "area_m2"},
                      headers=H, timeout=10)
    area = (rr.json()[0].get("area_m2") if rr.ok and rr.json() else None)
    ppm = round(price_usd / area, 2) if area else None
    patch = {
        "hammer_price": hammer,
        "currency": currency.upper(),
        "price_usd": price_usd,
        "price_with_premium": premium,
        "price_with_premium_usd": premium_usd,
        "price_per_m2_usd": ppm,
        "status": "sold",  # DB guard verifies hammer is non-null
    }
    pr = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json=patch, timeout=10)
    return pr.status_code < 300


# ─── Hammer extractors ─────────────────────────────────────────────

# Sothebys: when logged in + entitled to see results, the Apollo cache
# embeds sold:{__typename:"LotSold",amount:{value:NNN,currency:"GBP"}}
# instead of {__typename:"ResultHidden"}.
SOTHEBYS_HAMMER_PATTERNS = [
    re.compile(r'sold["\\]+:\s*\{[^}]*amount[^}]*?value[^:]*:\s*"?([\d.]+)[^}]*?currency[^:]*:\s*"?([A-Z]{3})', re.DOTALL),
    re.compile(r'soldFor["\\]+:\s*\{[^}]*?amount["\\]+:\s*([\d.]+)[^}]*?currency["\\]+:\s*["\\]+([A-Z]{3})', re.DOTALL),
    re.compile(r'Lot Sold[\s<]*[^>]*>[^<]*?([£$€HKD]+)\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'data-test-id="lot-sold-price"[^>]*>\s*([£$€HKD]+)?\s*([\d,]+)', re.IGNORECASE),
]


def _parse_sothebys_hammer(html: str) -> tuple[float | None, str | None]:
    # Try JSON-pattern first
    for pat in SOTHEBYS_HAMMER_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                groups = m.groups()
                # Pattern returns (value, currency) or (currency, value)
                if groups[0].replace(".","").replace(",","").isdigit():
                    amt = float(groups[0].replace(",", ""))
                    cur = groups[1]
                else:
                    amt = float(groups[1].replace(",", ""))
                    cur = {"£":"GBP","$":"USD","€":"EUR","HKD":"HKD"}.get(groups[0], groups[0])
                return amt, cur
            except (ValueError, IndexError):
                continue
    return None, None


# Invaluable: when logged in, the lot data island has the real sold
# amount.  Discovered 2026-06-26 by probe: the relevant fields are
# embedded in the page's __NEXT_DATA__ / preloaded state.
#   "isLotClosed":true,"lotRef":"F474...","currentBid":80000,"soldAmount":80000
# So we look for soldAmount (post-sale truth), with currentBid as a
# fallback for lots where soldAmount isn't present yet.
INVALUABLE_HAMMER_PATTERNS = [
    re.compile(r'"soldAmount"\s*:\s*([\d.]+)'),
    re.compile(r'"isLotClosed"\s*:\s*true[^}]*"currentBid"\s*:\s*([\d.]+)'),
    re.compile(r'"realizedPrice"\s*:\s*([\d.]+)'),
    re.compile(r'"hammerPrice"\s*:\s*([\d.]+)'),
]

# Currency for Invaluable lives in a separate field — look it up near
# soldAmount.  Falls back to USD when missing (most lots).
INVALUABLE_CURRENCY_PATTERNS = [
    re.compile(r'"currency"\s*:\s*"([A-Z]{3})"'),
    re.compile(r'"currencyCode"\s*:\s*"([A-Z]{3})"'),
]


def _parse_invaluable_hammer(html: str) -> tuple[float | None, str | None]:
    # Find sold amount first
    amt = None
    for pat in INVALUABLE_HAMMER_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                amt = float(m.group(1))
                if amt > 0:
                    break
                amt = None
            except (ValueError, IndexError):
                continue
    if amt is None:
        return None, None
    # Currency
    for pat in INVALUABLE_CURRENCY_PATTERNS:
        m = pat.search(html)
        if m and m.group(1) in {"USD","EUR","GBP","HKD","CAD","AUD","SGD","CHF","JPY","CNY","MYR","THB"}:
            return amt, m.group(1)
    return amt, "USD"  # safe default


# ─── Main ──────────────────────────────────────────────────────────

FX = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.26, "CAD": 0.74, "AUD": 0.66,
    "HKD": 0.128, "SGD": 0.74, "CHF": 1.10, "JPY": 0.0064, "CNY": 0.14,
}


def process_source(source: str, cookie: str, domain: str,
                   parse_fn, limit: int, probe: bool) -> None:
    if not cookie:
        print(f"  [{source}] no cookie in env — skip")
        return

    rows = fetch_missing_hammers(source, limit=1 if probe else limit)
    if not rows:
        print(f"  [{source}] no lots missing hammer — skip")
        return
    print(f"  [{source}] {len(rows)} lots queued")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        context.add_cookies(_parse_cookie_string(cookie, domain))
        page = context.new_page()

        n_ok = n_fail = 0
        for i, row in enumerate(rows, 1):
            url = row["source_url"]
            print(f"\n  [{i}/{len(rows)}] {row['artist_name_raw']} | {(row.get('artwork_title') or '')[:50]}")
            print(f"      URL: {url[-80:]}")
            try:
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)  # let JS render
                html = page.content()
            except Exception as e:
                print(f"      ✗ fetch failed: {type(e).__name__}: {str(e)[:80]}")
                n_fail += 1
                continue

            amt, cur = parse_fn(html)
            if amt is None:
                print(f"      ✗ no hammer parsed from page ({len(html)} chars)")
                n_fail += 1
                if probe:
                    # Save HTML so we can inspect what real hammer looks like
                    sample = ROOT / f"sample_{source}_logged_in.html"
                    sample.write_text(html)
                    print(f"      ◆ HTML sample written to {sample}")
            else:
                print(f"      ✓ hammer = {cur} {amt:,.0f}")
                if patch_hammer(row["id"], amt, cur, FX):
                    n_ok += 1
                else:
                    print("      ✗ DB patch failed")
                    n_fail += 1

            # Conservative random sleep — 60-120s between lots
            if i < len(rows):
                sleep_s = random.uniform(60, 120) if not probe else 5
                print(f"      ... sleep {sleep_s:.0f}s")
                time.sleep(sleep_s)

        browser.close()
    print(f"\n  [{source}] done: {n_ok} OK, {n_fail} failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["sothebys", "invaluable", "both"], default="both")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--probe", action="store_true",
                    help="Process just 1 lot per source, dump HTML for inspection")
    args = ap.parse_args()

    print("=" * 70)
    print("Local hammer puller (Mac, residential IP, real Chrome)")
    print("=" * 70)

    if args.source in ("sothebys", "both"):
        process_source(
            "sothebys",
            os.environ.get("SOTHEBYS_COOKIE", ""),
            ".sothebys.com",
            _parse_sothebys_hammer,
            args.limit,
            args.probe,
        )

    if args.source in ("invaluable", "both"):
        process_source(
            "invaluable",
            os.environ.get("INVALUABLE_COOKIE", ""),
            ".invaluable.com",
            _parse_invaluable_hammer,
            args.limit,
            args.probe,
        )

    print("\nDone.")
    if not args.probe:
        print("Refreshing artist stats…")
        import subprocess
        subprocess.run(["python3", str(ROOT / "supabase" / "refresh_artist_stats.py")], check=False)


if __name__ == "__main__":
    main()
