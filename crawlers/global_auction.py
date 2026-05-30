"""Global Auction crawler — Indonesian/SEA house at global.auction.

Public catalog uses URL pattern:
  /event/YYYY/MM/{slug}/{auction_id}/products?page=N
Lots:
  /event/YYYY/MM/lot/{lot_no}/{type}/{date}/{code}/{artist_id}/{artist-slug}/{title-slug}

Lot pages embed currency (SGD/IDR/USD), estimate range, and artist+title in the <title> tag.
"""
import re
import time
import requests
from urllib.parse import urlparse

from crawlers.common import insert_sale_result, clean_text, parse_amount, log_crawl_run


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
}


# ---- VN filter --------------------------------------------------------------

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


# ---- Discovery: list catalog pages -----------------------------------------

# Known past auctions (manually curated; user gave us the 2026-01 example)
DEFAULT_CATALOG_URLS = [
    "https://global.auction/event/2026/01/global-auction-southeast-asian-chinese-modern-contemporary-art-auction-14-31-jan-2026/1093/products",
    # The other 3 from earlier discovery — IDs from bid.global.auction:
    # 1-BZKK1C, 1-C1LWLS, 1-C7HP0Q. We need the global.auction-side numeric ID.
    # For now, rely on caller to supply URLs or scrape the past-events page.
]


def list_past_catalogs(limit=20):
    """Find past auction catalog URLs from global.auction. Returns list of /event/.../products URLs."""
    r = requests.get("https://global.auction/", headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    # URLs to past events look like /stories/2026/MM/... but we want /event/YYYY/MM/{slug}/{id}/products
    # The home page links to /stories. Past events live under another nav. Try /past-auctions or /events.
    for path in ["/past-auctions", "/events", "/auctions", "/event"]:
        rr = requests.get("https://global.auction" + path, headers=HEADERS, timeout=15)
        if rr.status_code == 200:
            urls = re.findall(r'/event/\d{4}/\d{1,2}/[a-z0-9\-]+/\d+/products', rr.text)
            if urls:
                return ["https://global.auction" + u for u in sorted(set(urls))[:limit]]
    return []


def list_lots_from_catalog(catalog_url, max_pages=15):
    """Paginate ?page=N until no new lot URLs appear."""
    all_lots = set()
    base = catalog_url.split("?")[0]
    for pg in range(1, max_pages + 1):
        url = f"{base}?page={pg}" if pg > 1 else base
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except Exception:
            break
        if r.status_code != 200:
            break
        # The catalog page is server-rendered with Livewire, but lot links are in HTML.
        # Pattern: /event/YYYY/MM/lot/N/<type>/<date>/<code>/<artist-id>/<artist-slug>/<title-slug>
        lots = set(re.findall(r'/event/\d{4}/\d{1,2}/lot/\d+/[a-z0-9]+/\d+/[a-z0-9]+/\d+/[a-z0-9\-]+/[a-z0-9\-]+', r.text))
        new = lots - all_lots
        if not new:
            # If catalog uses JS, page=1 may be all; need Playwright fallback
            break
        all_lots |= lots
    return sorted(all_lots)


def list_lots_via_playwright(catalog_url, max_pages=25):
    """Use Playwright to render server-side + paginate by ?page=N (Livewire)."""
    from playwright.sync_api import sync_playwright
    all_lots = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        base = catalog_url.split("?")[0]
        for pg in range(1, max_pages + 1):
            url = f"{base}?page={pg}" if pg > 1 else base
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                break
            page.wait_for_timeout(2500)
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
            hrefs = page.eval_on_selector_all(
                'a[href*="/event/"][href*="/lot/"]',
                'els => els.map(e => e.getAttribute("href"))',
            )
            new = set(hrefs) - all_lots
            if not new:
                break
            all_lots |= set(hrefs)
        browser.close()
    return sorted(all_lots)


# ---- Lot page parsing ------------------------------------------------------

_PRICE_PATS = [
    # "SGD 6,800 - 9,200" (HTML strips show currency once before low-high)
    (r"(SGD|IDR|USD|HKD|EUR|MYR)\s*\$?([\d,]+(?:\.\d+)?)\s*[-–]\s*\$?([\d,]+(?:\.\d+)?)", None),
]


def parse_lot_page(html, lot_url):
    """Parse a global.auction lot detail page → record dict (or None)."""
    # Title tag: "Bui Huu Hung, Landscape In Red 0 | GLOBAL AUCTION ..."
    m_title = re.search(r"<title>([^<]+)</title>", html)
    if not m_title:
        return None
    title_full = clean_text(m_title.group(1))
    # Take everything before " | "
    head = title_full.split(" | ")[0].strip()
    # Strip trailing " 0" / paddle-prefix that some pages have
    head = re.sub(r"\s+\d+\s*$", "", head).strip()
    # "Artist, Title" → split
    if "," in head:
        artist_raw, artwork = head.split(",", 1)
        artist_raw = artist_raw.strip()
        artwork = artwork.strip()
    else:
        artist_raw, artwork = head, ""

    # Estimate: strip HTML first so whitespace between currency and amount collapses
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    est_low = est_high = None
    currency = "USD"
    # Look for "Estimate: <CUR> low - high" specifically
    m_est_block = re.search(r"Estimate:\s*([A-Z]{3})\s*([\d,]+(?:\.\d+)?)\s*[-–]\s*([\d,]+(?:\.\d+)?)", plain, re.IGNORECASE)
    if m_est_block:
        try:
            currency = m_est_block.group(1).upper()
            est_low = float(m_est_block.group(2).replace(",", ""))
            est_high = float(m_est_block.group(3).replace(",", ""))
        except ValueError:
            pass
    if est_low is None:
        # Fallback: any currency-prefixed range
        for pat, _ in _PRICE_PATS:
            m = re.search(pat, plain, re.IGNORECASE)
            if m:
                try:
                    currency = m.group(1).upper()
                    est_low = float(m.group(2).replace(",", ""))
                    est_high = float(m.group(3).replace(",", ""))
                    break
                except (ValueError, IndexError):
                    pass

    # Dimensions: look for "WxH cm" pattern
    m_dim = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*cm", html, re.IGNORECASE)
    dimensions = f"{m_dim.group(1)} x {m_dim.group(2)} cm" if m_dim else ""

    # Date — extract from URL: /event/2026/01/lot/.../20260103/...
    m_date = re.search(r"/event/(\d{4})/(\d{1,2})/lot/\d+/[a-z0-9]+/(\d{8})/", lot_url)
    if m_date:
        d8 = m_date.group(3)
        sale_date = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
    else:
        sale_date = ""

    # Hammer: Global Auction frequently doesn't expose hammer publicly post-sale.
    # If we can find it, great; else use estimate midpoint as proxy.
    hammer = None
    m_h = re.search(r"(?:Hammer|Sold|Realised)[^<]{0,30}?([\d,]+(?:\.\d+)?)", html, re.IGNORECASE)
    if m_h:
        try:
            hammer = float(m_h.group(1).replace(",", ""))
        except ValueError:
            pass
    if not hammer and est_low and est_high:
        hammer = round((est_low + est_high) / 2, 2)

    return {
        "artist_name_raw": artist_raw,
        "artwork_title": artwork,
        "dimensions": dimensions,
        "sale_date": sale_date,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "currency": currency,
    }


# ---- Main crawl entry ------------------------------------------------------

def crawl(conn, catalog_urls=None, delay=1.0, verbose=True, filter_vn=True, use_playwright=True):
    """Crawl Global Auction catalogs. catalog_urls = list of /event/.../products URLs.
    If None, uses DEFAULT_CATALOG_URLS."""
    catalog_urls = catalog_urls or DEFAULT_CATALOG_URLS
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    total_inserted = 0
    from datetime import datetime
    for i, cat in enumerate(catalog_urls, 1):
        run_started = datetime.utcnow().isoformat() + "Z"
        if verbose:
            print(f"\n  [{i}/{len(catalog_urls)}] {cat}", flush=True)
        if use_playwright:
            lots = list_lots_via_playwright(cat)
        else:
            lots = list_lots_from_catalog(cat)
        if verbose:
            print(f"    found {len(lots)} lots", flush=True)
        date_min = date_max = None

        # Auction title from URL slug
        m = re.match(r"https://global\.auction/event/\d{4}/\d{1,2}/([a-z0-9\-]+)/(\d+)/products", cat)
        slug = m.group(1) if m else ""
        auction_id = m.group(2) if m else ""
        # Best-effort human title
        sale_title = (slug.replace("-", " ").title()[:120]) or "Global Auction"
        sale_page_url = cat

        inserted = 0
        for lot_path in lots:
            lot_url = "https://global.auction" + lot_path if lot_path.startswith("/") else lot_path
            try:
                r = requests.get(lot_url, headers=HEADERS, timeout=20)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            parsed = parse_lot_page(r.text, lot_url)
            if not parsed or not parsed.get("artist_name_raw"):
                continue
            if filter_vn and not _is_vietnamese(parsed["artist_name_raw"], vn_catalog, exclusions):
                continue
            if not parsed.get("hammer_price"):
                continue
            # Skip fakes
            text = (parsed["artwork_title"] + " " + parsed["artist_name_raw"]).lower()
            if re.search(r"\b(d'?apr[eè]s|after|copy|copie|reproduction|estampe|print|lithograph)\b", text):
                continue

            rec = {
                "source": "global-auction",
                "source_url": lot_url,
                "sale_page_url": sale_page_url,
                "lot_number": (re.search(r"/lot/(\d+)/", lot_url) or [None, ""])[1] if False else (re.search(r"/lot/(\d+)/", lot_url).group(1) if re.search(r"/lot/(\d+)/", lot_url) else ""),
                "auction_title": f"Global Auction — {sale_title}",
                "sale_date": parsed["sale_date"],
                "sale_location": "Singapore",
                "artist_name_raw": parsed["artist_name_raw"],
                "artwork_title": parsed["artwork_title"],
                "medium": "",
                "dimensions": parsed["dimensions"],
                "year": "",
                "estimate_low": parsed["estimate_low"],
                "estimate_high": parsed["estimate_high"],
                "hammer_price": parsed["hammer_price"],
                "price_with_premium": None,
                "currency": parsed["currency"],
                "status": "estimate",  # midpoint, not real hammer
                "provenance": "",
                "raw_snapshot": (parsed["artist_name_raw"] + " | " + parsed["artwork_title"])[:300],
            }
            insert_sale_result(conn, rec)
            inserted += 1
            sd = parsed.get("sale_date") or ""
            if sd:
                if date_min is None or sd < date_min: date_min = sd
                if date_max is None or sd > date_max: date_max = sd
            time.sleep(delay)
        conn.commit()
        log_crawl_run(conn, "global-auction", target_slug=cat, started_at=run_started,
                      lots_scanned=len(lots), lots_inserted=inserted,
                      sale_date_min=date_min, sale_date_max=date_max,
                      status="ok", note=sale_title[:120])
        if verbose:
            print(f"    {inserted} VN inserted", flush=True)
        total_inserted += inserted
    return total_inserted
