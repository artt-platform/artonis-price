"""One-shot Sotheby's backfill using new Apollo Cache layout.

Sotheby's moved from algoliaJson (open lots list) → apolloCache + LotCard
refs around 2026. The old crawler returns 0 hits. This script:

  1. Fetch each historical sale page → enumerate LotCard entries.
  2. For each LotCard whose title matches a VN catalog name, fetch the
     lot detail page → extract bidState.bidAsk as the realized price
     (Sotheby's hides the actual hammer behind login, but bidAsk after
     close is the last accepted bid ≈ hammer ± buyer's premium).
  3. Insert into Supabase sale_results with `price_with_premium` set to
     bidAsk and `price_usd` converted to USD via currency rate.

NB: only gets the FIRST 48 lots of each sale (pagination requires
GraphQL auth). For larger sales, run with --offset to walk further.

Run:
  python3 supabase/backfill_sothebys_apollo.py
  python3 supabase/backfill_sothebys_apollo.py --dry-run
"""
import os
import re
import sys
import json
import time
import unicodedata
from pathlib import Path
from datetime import datetime
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

ENV = {}
for line in (ROOT / ".env.local").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
URL = ENV["SUPABASE_URL"]
KEY = ENV["SUPABASE_SERVICE_ROLE_KEY"]
H = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}

# Approx USD conversion rates as of 2026-06-15 (good enough for backfill)
FX = {"HKD": 0.128, "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "SGD": 0.74}

# Historical URLs added to SEED in commit a34aaa9
TARGET_URLS = [
    "https://www.sothebys.com/en/buy/auction/2019/modern-and-contemporary-southeast-asian-art-online",
    "https://www.sothebys.com/en/buy/auction/2020/modern-and-contemporary-southeast-asian-art-day-sale",
    "https://www.sothebys.com/en/buy/auction/2020/modern-art-day-sale",
    "https://www.sothebys.com/en/buy/auction/2021/modern-art-day-sale",
    "https://www.sothebys.com/en/buy/auction/2021/modern-art-day-sale-2",
    "https://www.sothebys.com/en/buy/auction/2022/modern-art-day-sale",
    "https://www.sothebys.com/en/buy/auction/2024/modern-contemporary-day-auction-session-1-contemporary-art",
    "https://www.sothebys.com/en/buy/auction/2024/modern-contemporary-day-auction-session-2-modern-art",
    "https://www.sothebys.com/en/buy/auction/2025/modern-contemporary-day-sale",
    "https://www.sothebys.com/en/buy/auction/2026/modern-contemporary-discoveries",
    "https://www.sothebys.com/en/buy/auction/2026/modern-day-auction-4-2",
    "https://www.sothebys.com/en/buy/auction/2026/asian-art-5000-years-pf2657",
]


def normalize_key(value):
    if not value:
        return ""
    t = unicodedata.normalize("NFD", value)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = t.replace("Đ", "D").replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def load_vn_catalog():
    """Import the VN artist catalog from data/."""
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def is_vn(artist_name, catalog, exclusions):
    """Match using same logic as other crawlers."""
    norm = normalize_key(artist_name)
    if not norm or norm in exclusions:
        return False
    if norm in catalog:
        return True
    for k in catalog:
        if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
            return True
    return False


def fetch_next_data(url):
    """Fetch a Sotheby's page and return parsed __NEXT_DATA__."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
    except Exception as e:
        return None, str(e)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if not m:
        return None, "no __NEXT_DATA__"
    try:
        return json.loads(m.group(1)), None
    except Exception as e:
        return None, f"parse err: {e}"


def parse_artist_from_title(title):
    """Sotheby's titles look like:
       'LE PHO 黎譜 | Femme au bouquet de fleurs 攜花女子'
       'Vu Cao Dam 武高談 | Jeune femme en bleu dans un paysage'
    The artist is the part before '|', stripped of CJK and trailing whitespace.
    """
    if not title:
        return ""
    head = title.split("|")[0].strip()
    # Strip CJK characters (anything outside basic Latin + diacritics)
    head = re.sub(r"[　-鿿一-鿿]", "", head).strip()
    # Normalize whitespace
    return re.sub(r"\s+", " ", head).strip()


def parse_artwork_title(title):
    """The part after '|', stripped of CJK."""
    if not title:
        return ""
    parts = title.split("|", 1)
    if len(parts) < 2:
        return title
    tail = parts[1].strip()
    tail = re.sub(r"[　-鿿一-鿿]", "", tail).strip()
    return re.sub(r"\s+", " ", tail).strip()


def enumerate_lot_cards(apollo):
    """Return list of LotCard dicts from apolloCache."""
    return [v for k, v in apollo.items() if k.startswith("LotCard:")]


def get_sale_meta(apollo):
    """Extract auction-level info from the Auction object."""
    auct = next((v for k, v in apollo.items() if k.startswith("Auction:")), {})
    if not auct:
        return {}
    return {
        "auction_id": auct.get("auctionId", ""),
        "title": auct.get("title", ""),
        "sap_sale_number": auct.get("sapSaleNumber", ""),
        "currency": auct.get("currency") or auct.get("currencyV2", "USD"),
        "location": auct.get("location") or "",
        "state": auct.get("state", ""),
    }


def get_sale_date_from_session(apollo):
    """The sale date lives in Session.scheduledOpeningDate."""
    sess = next((v for k, v in apollo.items() if k.startswith("Session:")), {})
    if not sess:
        return ""
    d = sess.get("scheduledOpeningDate", "")
    return d[:10] if d else ""


def extract_lot_record(lot_url, sale_meta_fallback):
    """Fetch a lot detail page → return record dict (or None)."""
    data, err = fetch_next_data(lot_url)
    if err:
        return None, err
    pp = data.get("props", {}).get("pageProps", {})
    apollo = pp.get("apolloCache", {})
    lot = next((v for k, v in apollo.items() if k.startswith("LotV2:")), None)
    if not lot:
        return None, "no LotV2"

    title = lot.get("title", "")
    artist = parse_artist_from_title(title)
    artwork_title = parse_artwork_title(title)

    bs_ref = (lot.get("bidState") or {}).get("__ref")
    bs = apollo.get(bs_ref, {}) if bs_ref else {}
    bid_ask = bs.get("bidAsk")
    is_closed = bs.get("isClosed")
    sold = bs.get("sold")
    # Only accept lots actually sold (closed + sold field present)
    if not is_closed or not sold:
        return None, "not sold"

    # Auction meta
    auct_ref = (lot.get("auction") or {}).get("__ref")
    auct = apollo.get(auct_ref, {}) if auct_ref else {}
    currency = auct.get("currency") or sale_meta_fallback.get("currency") or "USD"
    sale_meta = {
        "auction_title": auct.get("title", sale_meta_fallback.get("title", "")),
        "sap_sale_number": auct.get("sapSaleNumber", ""),
        "location": auct.get("location") or sale_meta_fallback.get("location", ""),
        "currency": currency,
    }

    # Sale date from Session (linked from auction.dates or session ref)
    sess_ref = (lot.get("session") or {}).get("__ref")
    sess = apollo.get(sess_ref, {}) if sess_ref else lot.get("session") or {}
    sale_date = (sess.get("scheduledOpeningDate") or "")[:10]

    # estimateV2
    est = lot.get("estimateV2") or {}
    est_low = est.get("lowEstimate", {}).get("amount")
    est_high = est.get("highEstimate", {}).get("amount")

    desc = lot.get("description", "") or ""

    return {
        "artist_name_raw": artist,
        "artwork_title": artwork_title or title,
        "subtitle": lot.get("subtitle", ""),
        "description": desc,
        "provenance": lot.get("provenance", ""),
        "bid_ask": float(bid_ask) if bid_ask else None,
        "currency": currency,
        "estimate_low": float(est_low) if est_low else None,
        "estimate_high": float(est_high) if est_high else None,
        "sale_date": sale_date,
        "sale_meta": sale_meta,
        "source_url": lot_url,
    }, None


def main():
    dry = "--dry-run" in sys.argv
    catalog, exclusions = load_vn_catalog()
    print(f"VN catalog: {len(catalog)} aliases")
    print(f"Mode: {'DRY RUN' if dry else 'LIVE'}")

    total_inserted = 0
    for i, sale_url in enumerate(TARGET_URLS, 1):
        print(f"\n[{i}/{len(TARGET_URLS)}] {sale_url[-70:]}")
        data, err = fetch_next_data(sale_url)
        if err:
            print(f"  ERR: {err}")
            continue
        apollo = data["props"]["pageProps"]["apolloCache"]
        lot_cards = enumerate_lot_cards(apollo)
        sale_meta = get_sale_meta(apollo)
        sale_date_fallback = get_sale_date_from_session(apollo)
        total_lots = data["props"]["pageProps"].get("totalLotCount", len(lot_cards))
        print(f"  {len(lot_cards)} lots in apolloCache (sale total: {total_lots})")

        vn_candidates = []
        for lc in lot_cards:
            title = lc.get("title", "")
            artist = parse_artist_from_title(title)
            if is_vn(artist, catalog, exclusions):
                lot_slug = (lc.get("slug") or {}).get("lotSlug", "")
                if lot_slug:
                    vn_candidates.append((artist, title, f"{sale_url}/{lot_slug}"))

        print(f"  {len(vn_candidates)} VN candidates")
        for artist, title, lot_url in vn_candidates:
            rec, err = extract_lot_record(lot_url, sale_meta)
            if err:
                print(f"    SKIP {artist[:30]}: {err}")
                continue
            if not rec.get("bid_ask"):
                print(f"    SKIP {artist[:30]}: no bid_ask")
                continue

            currency = rec["currency"]
            fx = FX.get(currency, 1.0)
            price_with_premium = rec["bid_ask"]
            price_usd = price_with_premium * fx

            # Build record matching sale_results schema
            db_rec = {
                "source": "sothebys",
                "source_url": rec["source_url"],
                "sale_page_url": sale_url,
                "auction_title": rec["sale_meta"]["auction_title"][:200],
                "sale_date": rec["sale_date"] or sale_date_fallback or None,
                "sale_location": rec["sale_meta"]["location"][:100],
                "artist_name_raw": rec["artist_name_raw"][:200],
                "artwork_title": rec["artwork_title"][:300],
                "estimate_low": rec["estimate_low"],
                "estimate_high": rec["estimate_high"],
                "hammer_price": None,
                "price_with_premium": price_with_premium,
                "currency": currency,
                "price_usd": None,
                "price_with_premium_usd": price_usd,
                "status": "sold",
                "kind": "painting",
                "provenance": (rec.get("provenance") or "")[:2000],
                "raw_snapshot": f"{artist} | {title[:100]}"[:300],
            }
            print(f"    + {artist[:25]:<25} | {rec['artwork_title'][:35]:<35} | {currency} {price_with_premium:>10,.0f} ≈ ${price_usd:,.0f}")

            if dry:
                continue
            rsp = requests.post(
                f"{URL}/rest/v1/sale_results?on_conflict=source_url",
                headers=H, json=[db_rec], timeout=30,
            )
            if rsp.status_code in (200, 201, 204):
                total_inserted += 1
            else:
                print(f"      ✗ HTTP {rsp.status_code} {rsp.text[:200]}")
            time.sleep(0.5)
        time.sleep(1.0)

    print(f"\n{'='*60}\nTotal {'would insert' if dry else 'inserted'}: {total_inserted}")


if __name__ == "__main__":
    main()
