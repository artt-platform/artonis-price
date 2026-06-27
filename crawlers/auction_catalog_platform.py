"""Generic crawler for the /auction-catalog/ + /auction-lot/ WordPress
platform shared by 5 houses with VN content:

  - bid.joshuakodner.com   (WordPress + Invaluable plugin — Joshua Kodner)
  - live.akibagalleries.com (WordPress + Invaluable plugin — Akiba)
  - www.lawsons.com.au     (WordPress + custom auction plugin — Lawsons)
  - www.johnmoran.com      (WordPress + custom auction plugin — John Moran)
  - www.shapiroauctions.com (WordPress + custom auction plugin — Shapiro)

All five share the same URL conventions:
  Search:      {host}/search-results?query=vietnamese&past=1
  Lot detail:  {host}/auction-lot/{slug}_{alphanumID}

Pages are React-rendered — cloudscraper sees empty shells, Playwright
sees prices and titles.  Estimated VN coverage (search query 'vietnamese'):
  Joshua  17 lots
  Akiba   33 lots
  Lawsons 33 lots (mostly indigenous Australian, few VN)
  Moran    9 lots
  Shapiro 31 lots
                    ─── ~120 total ───

Why crawl direct when Invaluable already aggregates 2 of these
(Joshua + Akiba use Invaluable plugin)?  Invaluable archive 404s
shortly after a sale closes; the houses' own sites retain past lots
for years.  The 'Joshua via Invaluable' + 'Akiba via Invaluable'
counts in our DB sit at 2 each — vs 17 + 33 actually visible direct.

Periodic batch sweep via .github/workflows/auction_catalog_sweep.yml
keeps Cloudflare cooldowns separating runs.
"""
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawlers.common import insert_sale_result, log_crawl_run


HOUSES = {
    # key: (label, host, default_location, currency)
    "joshua_kodner":   ("Joshua Kodner",     "https://bid.joshuakodner.com",     "Florida, USA",      "USD"),
    "akiba_galleries": ("Akiba Galleries",   "https://live.akibagalleries.com",  "Pennsylvania, USA", "USD"),
    "lawsons":         ("Lawsons",           "https://www.lawsons.com.au",       "Sydney, Australia", "AUD"),
    "john_moran":      ("John Moran",        "https://www.johnmoran.com",        "Los Angeles, USA",  "USD"),
    "shapiro":         ("Shapiro Auctions",  "https://www.shapiroauctions.com",  "New York, USA",     "USD"),
    # Added 2026-06-24: 33 Auction (Singapore + Jakarta) — strong VN
    # coverage.  Same WP + Invaluable plugin pattern as Joshua/Akiba.
    # Search returned 60 candidate VN lots vs 12 via Invaluable mirror
    # — huge gap to close (SPEC §14.1 audit).
    "auction_33":      ("33 Auction",        "https://www.33auction.com",        "Singapore",         "SGD"),
}

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/'
      '605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15')

# Strict 2-pass discovery same as Bonhams + Dawsons.  Strip the URL ID
# tail (_AB12...) before checking the slug; otherwise random hex matches
# nothing useful.
_FAKE_MARKERS_RE = re.compile(
    r"(?:^|-)(?:after|attrib|attributed|circle-of|workshop-of|"
    r"manner-of|follower-of|school-of|copy)(?:-|$)"
)


def _load_vn_catalog():
    """Return the VN catalog as a set of normalized names (single source
    of truth from data/vn_artist_catalog.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG
    return set(VN_ARTIST_CATALOG)


def _load_vn_kws():
    """Catalog names slugified, for URL pattern pre-filtering."""
    kws = set()
    for normalized in _load_vn_catalog():
        if not normalized or len(normalized) < 5:
            continue
        slug = normalized.replace(" ", "-")
        if len(slug) >= 6:
            kws.add(slug)
    kws.update(("lebadang", "le-thiet-cuong", "viet-dung-hong",
                "than-binh-nguyen", "nguyen-tu-nghiem", "hoi-lebadang"))
    return kws


import re as _re


def _normalize_artist(name: str) -> str:
    """Match upsert_artist's normalize_key — lowercase, accents stripped,
    alphanumeric-only, space-separated."""
    import unicodedata
    if not name:
        return ""
    s = unicodedata.normalize('NFD', name)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = _re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return _re.sub(r"\s+", " ", s).strip()


def _match_catalog_artist(raw_name: str, catalog: set) -> str | None:
    """Return the canonical normalized name from catalog if raw_name maps,
    else None.  Tries: direct match, word-sorted match, common VN
    Western-order reversal."""
    if not raw_name:
        return None
    norm = _normalize_artist(raw_name)
    # Strip parenthetical bio that the catalog page leaks into name
    norm = _re.sub(r"\s+(?:vietnamese?|vietnam|french|chinese|american|british).*$", "", norm)
    norm = _re.sub(r"\s+b\s+\d{4}.*$", "", norm)  # 'b 1907 2001' tail
    norm = norm.strip()
    if not norm or len(norm) < 4:
        return None
    if norm in catalog:
        return norm
    # Word-sort match — handles 'Viet Dung Hong' ↔ 'Hong Viet Dung'
    sorted_norm = " ".join(sorted(norm.split()))
    for cand in catalog:
        if " ".join(sorted(cand.split())) == sorted_norm:
            return cand
    # Whole-word substring (mononym match) — 'Hoi Lebadang' contains
    # catalog 'lebadang'.  Minimum 6 chars + whole-word boundary to
    # avoid 'le' matching everything.
    norm_words = norm.split()
    norm_padded = " " + norm + " "
    for cand in catalog:
        if len(cand) < 6:
            continue
        if (" " + cand + " ") in norm_padded:
            return cand
    return None


def _parse_lot_page(page, url, currency, host_label, host_location):
    """Render a /auction-lot/{slug}_{ID} page and extract record dict."""
    try:
        page.goto(url, timeout=30000, wait_until='domcontentloaded')
        # Wait for prices to materialise — they render after a JS API call.
        try:
            page.wait_for_selector('text=/(?:Estimate|Sold|Hammer)/i', timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        body = page.inner_text('body')
    except Exception:
        return None
    if len(body) < 200:
        return None

    # Title — 'Lot N: Vietnamese ...' or just 'Title' depending on house
    title = ""
    h1 = page.locator('h1').first
    try:
        if h1.count() > 0:
            title = h1.inner_text().strip()
    except Exception:
        pass
    # Preserve raw H1 for artist extraction — title gets cleaned (bio
    # prefix stripped, pipe-split) but artist extraction needs the raw
    # form to read 'DANG XUAN HOA | Self Portrait' → 'Dang Xuan Hoa'.
    raw_h1 = title
    # Strip leading 'Lot 338:' from Joshua/Akiba layout
    m_lot = re.match(r'^Lot\s+\d+\s*[:.\-]\s*(.+)$', title, re.IGNORECASE)
    lot_no = ""
    if m_lot:
        lot_no_m = re.match(r'^Lot\s+(\d+)', title, re.IGNORECASE)
        if lot_no_m: lot_no = lot_no_m.group(1)
        title = m_lot.group(1).strip()
    # Strip artist bio prefix.  Common patterns in WP/Invaluable-
    # mirrored sites:
    #   1. ARTIST (NATIONALITY, DATES) ACTUAL_TITLE
    #   2. ARTIST (DATES, NATIONALITY) ACTUAL_TITLE  (Akiba uses this)
    #   3. ARTIST NATIONALITY, DATES (no parens — bio-only)
    title = re.sub(
        r"^[A-Z][A-Za-z .'\-]+?\s*\((?:French[-\s]+)?Vietnamese?(?:[-\s]+French)?,?\s*"
        r"(?:b\.?\s*)?\d{4}(?:\s*[-–]\s*\d{4})?\)\s*",
        "", title,
    )
    title = re.sub(
        r"^[A-Z][A-Za-z .'\-]+?\s*\(\s*(?:b\.?\s*)?\d{4}(?:\s*[-–]\s*\d{4})?,?\s*"
        r"(?:French[-\s]+)?Vietnamese?(?:[-\s]+French)?\s*\)\s*",
        "", title,
    )
    title = re.sub(
        r"^[A-Z][A-Za-z .'\-]+\s+(?:French[-\s]+)?Vietnamese?(?:[-\s]+French)?,?\s+"
        r"(?:b\.?\s*)?\d{4}(?:\s*[-–]\s*\d{4})?",
        "", title,
    ).strip()
    # 33 Auction pipe layout: 'DANG XUAN HOA | Self Portrait' → 'Self Portrait'
    if '|' in title:
        parts = title.split('|', 1)
        if len(parts) == 2 and len(parts[1].strip()) >= 3:
            title = parts[1].strip()
    if len(title) < 3:
        title = ""

    # Estimate — accept $, £, €, or 3-letter ISO codes (SGD, HKD, USD, etc.).
    # 33 Auction renders 'Estimate: SGD\xa05,000 - SGD\xa07,000' (NBSP after code).
    est_low = est_high = None
    m_est = re.search(
        r'Estimate[:\s]*(?:[\$£€]|[A-Z]{3})?\s*([\d,]+)\s*[-–]\s*(?:[\$£€]|[A-Z]{3})?\s*([\d,]+)',
        body, re.IGNORECASE,
    )
    if m_est:
        try:
            est_low = int(m_est.group(1).replace(',', ''))
            est_high = int(m_est.group(2).replace(',', ''))
        except ValueError:
            pass

    # Hammer / Sold for — same currency-code tolerance.
    hammer = None
    m_sold = re.search(
        r'(?:Sold(?:\s+for)?|Hammer\s*price|Realised|Realized|Winning\s*bid)'
        r'[:\s]*(?:[\$£€]|[A-Z]{3})?\s*([\d,]+(?:\.\d+)?)',
        body, re.IGNORECASE,
    )
    if m_sold:
        try:
            hammer = float(m_sold.group(1).replace(',', ''))
        except ValueError:
            pass

    # Description — usually after 'Description' label
    description = ""
    m_desc = re.search(
        r'Description[:\s]*\n([\s\S]{30,1500}?)(?:\n\n|Auction Details|Estimate:|$)',
        body, re.IGNORECASE,
    )
    if m_desc:
        description = m_desc.group(1).strip()[:2000]

    # Dim + medium via shared parsers (SPEC §10).
    from crawlers.parsers import parse_dim, extract_medium
    width_cm, height_cm, area_m2, dims = parse_dim(description or body, source="invaluable")
    haystack = (description or "") + " " + (title or "")
    medium = extract_medium(haystack)
    # Local fallback list for medium phrases not in the shared list
    # (silkscreen variants, embossed lithograph, watercolor on paper).
    if not medium:
        for kw in ("oil on canvas", "oil on board", "oil on panel",
                   "watercolour on paper", "watercolor on paper",
                   "ink on paper", "lacquer on wood", "lithograph",
                   "etching", "screenprint", "silkscreen", "gouache",
                   "acrylic", "mixed media", "embossed lithograph",
                   "watercolor on paper painting"):
            if kw in haystack.lower():
                medium = kw
                break

    # Artist — extract from title or h1.  Title patterns by host:
    #   Joshua/Akiba: "Tran Luu Hau Vietnamese, 1928-2020"  → "Tran Luu Hau"
    #   33 Auction:   "DANG XUAN HOA | Self Portrait"       → "Dang Xuan Hoa"
    #   Lawsons/JM/Shapiro: "Vietnamese Style Oil Painting" → "" (anonymous)
    artist = ""
    # Use raw_h1 (preserved before title cleaning) since title may have
    # had the artist prefix stripped.
    src_title = raw_h1 or title
    # Pattern A: '<NAME> Vietnamese' / '<NAME> French Vietnamese'
    m_art = re.match(
        r"^([A-Z][A-Za-z .'\-]+?)\s+(?:French\s+)?(?:Vietnamese|Vietnam)\b",
        src_title,
    )
    if m_art:
        artist = m_art.group(1).strip()
    # Pattern B: '<NAME> | <Title>' (33 Auction layout)
    elif '|' in src_title:
        artist_part = src_title.split('|', 1)[0].strip()
        if 2 < len(artist_part) < 50:
            # Title-case if all caps ('DANG XUAN HOA' → 'Dang Xuan Hoa')
            if artist_part == artist_part.upper() and ' ' in artist_part:
                artist_part = ' '.join(w.capitalize() for w in artist_part.lower().split())
            artist = artist_part
    # Pattern C: '<NAME>, <Title>' (legacy)
    elif src_title and ',' in src_title:
        artist_part = src_title.split(',')[0].strip()
        if 2 < len(artist_part) < 40 and artist_part.split()[0][0].isupper():
            artist = artist_part

    if not artist:
        # Skip anonymous folk-art lots — no artist extracted
        return None

    # STRICT catalog gate: artist must match VN_ARTIST_CATALOG.  Without
    # this, regex parses 'Pair of Vintage Vietnamese Ceramic Elephants'
    # → artist = 'Pair of'.  Lesson from Lawsons/Akiba contamination:
    # never trust regex extraction for artist identity; always validate.
    catalog = _load_vn_catalog()
    canonical = _match_catalog_artist(artist, catalog)
    if not canonical:
        return None
    # Promote the raw name to the canonical catalog spelling so
    # upsert_artist hits the right row.
    artist = canonical.title()

    # Sale name from URL parent-catalog reference (in body text)
    sale_name = host_label
    m_sale = re.search(r"Auction:\s*([^\n]{5,200})", body, re.IGNORECASE)
    if m_sale:
        sale_name = m_sale.group(1).strip()

    # Sale date — operator-flagged 2026-06-28: 33Auction page exposes
    # a clear 'Auction details … <SaleName> Month DD, YYYY' block.
    # Same pattern works for Joshua Kodner / Akiba layouts.
    sale_date = None
    _MONTHS = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
               "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}
    m_date = re.search(
        r'(' + '|'.join(_MONTHS) + r')\s+(\d{1,2}),\s+(\d{4})',
        body,
    )
    if m_date:
        sale_date = f"{m_date.group(3)}-{_MONTHS[m_date.group(1)]:02d}-{int(m_date.group(2)):02d}"

    # Hammer + currency from 'Sold: XXX 5,800.00' line that 33Auction
    # renders when the lot actually sold.  Overrides the upstream
    # parser's blanket hammer=None.
    m_hammer = re.search(r'Sold:\s*([A-Z]{3})\s*([\d,]+(?:\.\d+)?)', body)
    if m_hammer and not hammer:
        try:
            hammer = float(m_hammer.group(2).replace(",", ""))
            currency = m_hammer.group(1)
        except ValueError:
            pass

    # Image — WP-Invaluable-plugin pages embed the lot photo from
    # image.invaluable.com/housePhotos/<host_slug>/<batch>/<seq>/...
    # 33Auction (operator 2026-06-27 Bui Huu Hung Mandarin Daughter)
    # is the canonical case; Joshua Kodner / Akiba / Lawsons share
    # the same plugin and the same CDN.  Pick the first non-static
    # housePhotos URL — variations after the first are just additional
    # angles of the same lot.
    image_url = None
    try:
        img_el = page.locator('img[src*="image.invaluable.com/housePhotos"]').first
        if img_el and img_el.count() > 0:
            image_url = img_el.get_attribute('src')
    except Exception:
        pass

    return {
        "source": "auction_catalog",
        "via_platform": "auction_catalog",
        "source_url": url,
        "sale_page_url": "",
        "lot_number": lot_no,
        "auction_title": sale_name[:200],
        "sale_date": sale_date,
        "sale_location": host_location,
        "artist_name_raw": artist,
        "artwork_title": title[:200],
        "medium": medium,
        "dimensions": dims,
        "year": "",
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": None,
        "currency": currency,
        "image_url": image_url,
        "status": "sold" if hammer else "passed",
        "provenance": "",
        "raw_snapshot": description[:500],
        "catalog_description": description[:2000],
    }


def _list_vn_lot_urls(page, host, kws, verbose=False):
    """Load /search-results?query=vietnamese&past=1, scroll if needed,
    return list of (slug, lot_id, full_url)."""
    url = f"{host}/search-results?query=vietnamese&past=1"
    try:
        page.goto(url, timeout=30000, wait_until='domcontentloaded')
        page.wait_for_selector('a[href*="/auction-lot/"]', timeout=15000)
    except Exception as e:
        if verbose:
            print(f"    search err: {e}", flush=True)
        return []
    # Try to scroll bottom to lazy-load remaining lots
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)
    # Extract distinct lot URLs
    links = page.locator('a[href*="/auction-lot/"]').all()
    seen = set()
    out = []
    for a in links:
        href = a.get_attribute('href') or ''
        m = re.search(r'/auction-lot/([a-z0-9-]+)_([A-Za-z0-9]{6,15})', href)
        if not m:
            continue
        slug, lot_id = m.group(1), m.group(2)
        if lot_id in seen:
            continue
        seen.add(lot_id)
        # Filter false positives (fake/copy markers)
        if _FAKE_MARKERS_RE.search(slug):
            continue
        full = href if href.startswith('http') else (host + href)
        out.append((slug, lot_id, full))
    return out


def crawl_house(conn, house_key, max_lots=None, delay=0.5, verbose=True):
    """Crawl one house from the HOUSES dict.  Returns count inserted."""
    label, host, location, currency = HOUSES[house_key]
    from playwright.sync_api import sync_playwright

    run_started = datetime.utcnow().isoformat() + "Z"
    kws = _load_vn_kws()
    inserted = 0
    scanned = 0

    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        ctx = b.new_context(user_agent=UA, viewport={'width': 1920, 'height': 1080})
        page = ctx.new_page()

        if verbose:
            print(f"  {label}: listing VN lots…", flush=True)
        lot_urls = _list_vn_lot_urls(page, host, kws, verbose=verbose)
        if verbose:
            print(f"  {label}: {len(lot_urls)} candidate lots", flush=True)

        if max_lots:
            lot_urls = lot_urls[:max_lots]

        for slug, lot_id, url in lot_urls:
            ctx2 = b.new_context(user_agent=UA, viewport={'width': 1920, 'height': 1080})
            p2 = ctx2.new_page()
            rec = _parse_lot_page(p2, url, currency, label, location)
            ctx2.close()
            scanned += 1
            if rec is None:
                continue
            rec["source"] = house_key
            insert_sale_result(conn, rec)
            inserted += 1
            if verbose:
                print(f"    + {rec['artist_name_raw'][:25]:<25} | "
                      f"{rec['artwork_title'][:35]:<35} | "
                      f"{rec['hammer_price'] or '—'} {rec['currency']}", flush=True)
            time.sleep(delay)

        ctx.close()
        b.close()

    conn.commit()
    log_crawl_run(
        conn, house_key, target_slug="search?query=vietnamese&past=1",
        started_at=run_started,
        lots_scanned=scanned, lots_inserted=inserted,
        status="ok",
        note=f"{label} direct crawl",
    )
    if verbose:
        print(f"  {label}: {inserted} inserted, {scanned} scanned", flush=True)
    return inserted


def crawl(conn, **kwargs):
    """Entry point — sweep all 5 houses in sequence."""
    total = 0
    for k in HOUSES:
        try:
            total += crawl_house(conn, k, **kwargs)
        except Exception as e:
            print(f"  {k}: ERR {type(e).__name__}: {e}", flush=True)
    return total


# Per-house entry points (so crawl_and_sync.py rows can target individual houses)
def crawl_joshua(conn, **kw):  return crawl_house(conn, "joshua_kodner", **kw)
def crawl_akiba(conn, **kw):   return crawl_house(conn, "akiba_galleries", **kw)
def crawl_lawsons(conn, **kw): return crawl_house(conn, "lawsons", **kw)
def crawl_moran(conn, **kw):   return crawl_house(conn, "john_moran", **kw)
def crawl_shapiro(conn, **kw): return crawl_house(conn, "shapiro", **kw)
def crawl_33auction(conn, **kw): return crawl_house(conn, "auction_33", **kw)
