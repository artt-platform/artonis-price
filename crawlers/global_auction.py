"""Global Auction crawler — Indonesian/SEA house at global.auction.

Discovery:
  - sitemap.xml exposes /event/{YYYY}/{MM}/{slug}/{event_id}/products URLs
    for currently-listed sales (typically 2-4 most recent).

Parsing strategy:
  - Each event page is a Livewire component. POST to /livewire/update with
    updates={limitLot: 200} returns ALL lot cards in a single HTML chunk
    (Global Auction sales cap around 200 lots).
  - Each card contains: artist, title, estimate range, and — for closed
    sales — a green "SOLD : SGD XXX,XXX" badge.

Status mapping:
  - SOLD badge present → status='sold' with real hammer price.
  - SOLD badge absent → SKIP. Either the auction hasn't reported results
    yet, or the lot was withdrawn / unsold. Don't synthesise a fake price.
"""
import re
import json
import html as html_lib
import time
from datetime import datetime, timezone

import cloudscraper

from crawlers.common import insert_sale_result, clean_text, log_crawl_run


BASE = "https://global.auction"
HEADERS_GET = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
}

# Approx FX → USD (rough; refresh occasionally)
FX_TO_USD = {"USD": 1.0, "SGD": 0.74, "IDR": 0.000061, "HKD": 0.128,
             "EUR": 1.08, "MYR": 0.21}


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin"}
    )


# ─── VN filter ───────────────────────────────────────────────────────────────

def _load_vn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_raw, vn_catalog, exclusions):
    from artonis_price_mvp import normalize_key
    norm = normalize_key(artist_raw)
    if not norm or norm in exclusions:
        return False
    if norm in vn_catalog:
        return True
    for k in vn_catalog:
        if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
            return True
    return False


# ─── Discovery ───────────────────────────────────────────────────────────────

_EVENT_RE = re.compile(
    r"https://global\.auction/event/(\d{4})/(\d{1,2})/"
    r"([a-z0-9-]+)/(\d+)/products"
)


def discover_events(scraper):
    """Pull current event URLs from sitemap.xml. Returns list of dicts."""
    try:
        r = scraper.get(f"{BASE}/sitemap.xml", timeout=20)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    seen = {}
    for m in _EVENT_RE.finditer(r.text):
        y, mo, slug, eid = m.groups()
        # Skip the lot-URL pattern (slug == "lot")
        if slug == "lot":
            continue
        seen[eid] = {
            "event_id": eid,
            "year": y, "month": mo, "slug": slug,
            "url": f"{BASE}/event/{y}/{mo}/{slug}/{eid}/products",
        }
    return sorted(seen.values(), key=lambda e: (e["year"], e["month"], e["event_id"]))


# ─── Livewire fetch + parsing ────────────────────────────────────────────────

_CARD_RE = re.compile(
    r'<a href="(https?://global\.auction/event/[^"]*?/lot/[^"]+)">'
    r"[\s\S]*?<h3[^>]*>([^<]+)</h3>"
    r"[\s\S]*?<h4[^>]*>([^<]+?)<"
    r"[\s\S]*?<b>Est\.</b>\s*([A-Z]{3})\s*([\d,]+)\s*-\s*([\d,]+)"
    r"([\s\S]{0,500})"
)
_SOLD_RE = re.compile(r"SOLD\s*:\s*([A-Z]{3})\s*([\d,]+(?:\.\d+)?)")
_LOT_URL_RE = re.compile(
    r"/event/(\d{4})/(\d{1,2})/lot/(\d+)/[a-z0-9]+/(\d{8})/"
    r"[a-z0-9]+/\d+/([a-z0-9-]+)/([a-z0-9-]+)"
)


def fetch_event_lots(scraper, event_url, limit=200):
    """One Livewire request → all lot cards. Returns list of dicts."""
    try:
        r = scraper.get(event_url, timeout=25, headers=HEADERS_GET)
    except Exception:
        return []
    if r.status_code != 200:
        return []

    m_csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
    m_snap = re.search(r'wire:snapshot="([^"]+)"', r.text)
    if not (m_csrf and m_snap):
        return []
    csrf = m_csrf.group(1)
    snap = html_lib.unescape(m_snap.group(1))

    payload = {
        "components": [{"snapshot": snap, "updates": {"limitLot": limit}, "calls": []}],
        "_token": csrf,
    }
    headers = {
        "Content-Type": "application/json",
        "X-CSRF-TOKEN": csrf,
        "X-Livewire": "1",
        "Referer": event_url,
        "User-Agent": HEADERS_GET["User-Agent"],
    }
    try:
        r2 = scraper.post(f"{BASE}/livewire/update", json=payload, headers=headers, timeout=45)
    except Exception:
        return []
    if r2.status_code != 200:
        return []
    try:
        h = json.loads(r2.text)["components"][0]["effects"]["html"]
    except Exception:
        return []

    cards = []
    seen_urls = set()
    for m in _CARD_RE.finditer(h):
        url, artist, title, cur, low, high, tail = m.groups()
        if url in seen_urls:
            # Each card appears twice in the HTML (mobile + desktop variants)
            continue
        seen_urls.add(url)

        sold_m = _SOLD_RE.search(tail)
        sold_amt = (
            float(sold_m.group(2).replace(",", "")) if sold_m else None
        )
        sold_cur = sold_m.group(1) if sold_m else None

        # Sale date from URL: /event/YYYY/MM/lot/N/.../{YYYYMMDD}/...
        sale_date = ""
        m_url = _LOT_URL_RE.search(url)
        if m_url:
            d8 = m_url.group(4)
            sale_date = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"

        cards.append({
            "url": url,
            "artist_raw": html_lib.unescape(artist).strip(),
            "title": html_lib.unescape(title).strip(),
            "estimate_currency": cur,
            "estimate_low": float(low.replace(",", "")),
            "estimate_high": float(high.replace(",", "")),
            "sold_currency": sold_cur,
            "sold_price": sold_amt,
            "sale_date": sale_date,
        })
    return cards


# ─── Lot detail enrichment (optional) ────────────────────────────────────────

def _enrich_from_lot_page(scraper, lot_url):
    """Fetch lot page → extract medium + dimensions + image. Returns dict."""
    try:
        r = scraper.get(lot_url, timeout=20, headers=HEADERS_GET)
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    out = {}
    # Plain "X x Y cm" form
    m_dim = re.search(
        r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*cm",
        r.text, re.IGNORECASE,
    )
    # Operator 2026-06-29: global.auction wraps dims as
    # "Height 122cm x Width 122cm" — the plain regex above misses
    # the labelled form because it requires the unit ('cm') only
    # ONCE (at the end).  Add a labelled-pair pattern that
    # explicitly captures Height + Width.
    if not m_dim:
        m_hw = re.search(
            r"Height\s*(\d+(?:\.\d+)?)\s*cm\s*[x×]\s*Width\s*(\d+(?:\.\d+)?)\s*cm",
            r.text, re.IGNORECASE,
        )
        if m_hw:
            # Labelled: H × W → store canonical W × H per em convention.
            h_cm, w_cm = m_hw.group(1), m_hw.group(2)
            out["dimensions"] = f"{w_cm} x {h_cm} cm"
    elif m_dim:
        out["dimensions"] = f"{m_dim.group(1)} x {m_dim.group(2)} cm"
    m_med = re.search(
        r"\b(oil on canvas|acrylic on canvas|ink on (?:silk|paper)|"
        r"gouache on (?:silk|paper)|watercolou?r on (?:silk|paper)|"
        r"mixed media on (?:canvas|paper|wood)|"
        r"lacquer on (?:wood|panel)|charcoal on paper|pastel on paper|"
        r"bronze|terracotta)\b",
        r.text, re.IGNORECASE,
    )
    if m_med:
        out["medium"] = m_med.group(0).lower()
    # Image: og:image meta + fall back to product/ image in <img>.
    # Operator 2026-06-29: global.auction lots in DB all had
    # image_url=NULL because the crawler didn't extract them.  The
    # CDN URL pattern is 'aws.global.auction/product/<n>/<size>/<id>.webp'.
    m_og = re.search(
        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r.text, re.IGNORECASE,
    )
    if m_og:
        out["image_url"] = m_og.group(1)
    else:
        m_img = re.search(
            r'<img[^>]+src=["\'](https://aws\.global\.auction/product/[^"\']+\.(?:webp|jpg|jpeg|png))["\']',
            r.text, re.IGNORECASE,
        )
        if m_img:
            out["image_url"] = m_img.group(1)
    return out


# ─── Main ────────────────────────────────────────────────────────────────────

def crawl(conn, event_urls=None, delay=1.0, verbose=True, filter_vn=True,
          max_pages=20, enrich_details=True):
    """Crawl Global Auction past events. Returns total inserted."""
    scraper = _make_scraper()
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    if event_urls is None:
        events = discover_events(scraper)
        if verbose:
            print(f"  [global_auction] discovered {len(events)} events", flush=True)
        event_urls = [e["url"] for e in events]
    event_urls = list(event_urls)[:max_pages]

    total_inserted = 0
    for i, event_url in enumerate(event_urls, 1):
        run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if verbose:
            print(f"\n  [{i}/{len(event_urls)}] {event_url[-70:]}", flush=True)

        cards = fetch_event_lots(scraper, event_url)
        sold_cards = [c for c in cards if c["sold_price"]]
        if verbose:
            print(f"    {len(cards)} lots, {len(sold_cards)} sold", flush=True)

        inserted_this = 0
        for c in cards:
            # Skip lots without a real SOLD price — don't synthesise from estimate.
            if not c["sold_price"]:
                continue
            if filter_vn and not _is_vietnamese(c["artist_raw"], vn_catalog, exclusions):
                continue

            currency = c["sold_currency"] or c["estimate_currency"]
            sold_usd = c["sold_price"] * FX_TO_USD.get(currency, 1.0)

            extra = _enrich_from_lot_page(scraper, c["url"]) if enrich_details else {}

            rec = {
                "source": "global-auction",
                "source_url": c["url"],
                "sale_page_url": event_url,
                "auction_title": "Global Auction — Southeast Asian Modern & Contemporary",
                "sale_date": c["sale_date"] or None,
                "sale_location": "Singapore",
                "artist_name_raw": c["artist_raw"][:200],
                "artwork_title": c["title"][:300],
                "medium": extra.get("medium", ""),
                "dimensions": extra.get("dimensions", ""),
                "estimate_low": c["estimate_low"],
                "estimate_high": c["estimate_high"],
                "hammer_price": c["sold_price"],
                "price_with_premium": None,
                "currency": currency,
                "price_usd": sold_usd,
                "price_with_premium_usd": None,
                "status": "sold",
                "kind": "painting",
                "raw_snapshot": f"{c['artist_raw']} | {c['title'][:80]}"[:300],
            }
            try:
                insert_sale_result(conn, rec)
                inserted_this += 1
                if verbose and inserted_this <= 5:
                    print(f"      + {c['artist_raw'][:24]:<24} | {c['title'][:30]:<30} | "
                          f"{currency} {c['sold_price']:>10,.0f} ≈ ${sold_usd:,.0f}", flush=True)
            except Exception as e:
                if verbose:
                    print(f"      ✗ insert err: {e}", flush=True)

        conn.commit()
        log_crawl_run(
            conn, "global-auction",
            target_slug=event_url.split("/")[-2],
            started_at=run_started,
            lots_scanned=len(cards),
            lots_inserted=inserted_this,
            status="ok",
            note=event_url[-80:],
        )
        if verbose:
            print(f"    → {inserted_this} VN inserted", flush=True)
        total_inserted += inserted_this
        time.sleep(delay)

    return total_inserted
