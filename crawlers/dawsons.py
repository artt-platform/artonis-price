"""Dawsons Auctioneers (UK) — direct crawler, no Playwright needed.

URL conventions:
  Sale listing (sold filter):  /auction/search/?au={au_id}&sd=2&page={n}
  Lot detail:                  /auction/lot/{slug}/?lot={lot_id}&au={au_id}

The search page returns up to 48 lots per page with lot links of the
form `/auction/lot/{slug}/?lot=NNN&au=NNN`.  The lot detail page is
plain HTML with:
  - <h1 class="lot-title cat-N">         → artwork title + artist hint
  - <div class="lot-desc">               → full description
                                           (medium, dimensions, provenance)
  - <div class="estimate">               → 'Estimate £L - £H'
  - Realised price on detail page (no separate API call needed).

Discovery filter — same 2-pass approach as Bonhams / Bidwizard:
  Pass 1: VN catalog slug keyword in lot URL slug.
  Pass 2: 'vietnamese' literal — catches anonymous/new artists.

A quick au-range scan (50-360) on 2026-06-24 surfaced ~10-15 real VN
art lots after filtering false positives ('Moorcroft Anna LILY' style
slug-fragment hits).  Strict pass1 prevents those.
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except Exception:
    import requests
    _scraper = requests.Session()

from crawlers.common import insert_sale_result, log_crawl_run, clean_text


BASE = "https://www.dawsonsauctions.co.uk"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

# au id range to probe.  Dawsons IDs auctions sequentially; ~50 is the
# oldest sale with art content, ~360 is current (June 2026).
AU_MIN, AU_MAX = 50, 360


def _load_vn_catalog():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG
    return VN_ARTIST_CATALOG


def _build_keywords(catalog):
    """Slug-form artist keywords from VN catalog, full names only."""
    kws = set()
    for normalized in catalog:
        if not normalized or len(normalized) < 5:
            continue
        slug = normalized.replace(" ", "-")
        if len(slug) >= 6:
            kws.add(slug)
    # Extra slugs not in catalog
    kws.update(("lebadang", "le-thiet-cuong", "viet-dung-hong",
                "than-binh-nguyen", "nguyen-tu-nghiem"))
    return kws


_PASS2_RE = re.compile(r"(?:^|-)(vietnamese?|vietnam-war)(?:-|$)")
_FAKE_MARKERS_RE = re.compile(
    r"(?:^|-)(?:after|attrib|attributed|circle-of|workshop-of|"
    r"manner-of|follower-of|school-of|copy)(?:-|$)"
)


def _fetch(url):
    try:
        r = _scraper.get(url, headers=HEADERS, timeout=20)
    except Exception as e:
        return None, str(e)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.text, None


def _list_lot_urls_for_auction(au):
    """Return list of (slug, lot_id) for VN-matching sold lots in this
    auction.  Walks search pages until no new lots appear."""
    catalog = _load_vn_catalog()
    kws = _build_keywords(catalog)
    seen = set()
    results = []
    for page in range(1, 30):
        url = f"{BASE}/auction/search/?au={au}&sd=2&page={page}"
        text, err = _fetch(url)
        if err:
            break
        lot_links = re.findall(
            r'/auction/lot/([a-z0-9-]+)/[^"]*\?lot=(\d+)', text
        )
        new_links = [(s, l) for s, l in lot_links if (s, l) not in seen]
        if not new_links:
            break
        seen.update(new_links)
        for slug, lot_id in new_links:
            if _FAKE_MARKERS_RE.search(slug):
                continue
            slug_norm = re.sub(r"^lot-\d+-+", "", slug)
            if any(kw in slug_norm for kw in kws) or _PASS2_RE.search(slug_norm):
                results.append((slug, lot_id))
        time.sleep(0.4)
    return results


_LOT_TITLE_RE = re.compile(
    r'<h1[^>]*class="lot-title[^"]*"[^>]*>([\s\S]+?)</h1>',
    re.IGNORECASE,
)
_LOT_DESC_RE = re.compile(
    r'<div[^>]*class="lot-desc"[^>]*>([\s\S]+?)</div>\s*</div>',
    re.IGNORECASE,
)
_ESTIMATE_RE = re.compile(
    r'<div[^>]*class="estimate"[^>]*>[\s\S]*?'
    r'(?:&pound;|£)\s*([\d,]+)\s*[-–]\s*'
    r'(?:&pound;|£)\s*([\d,]+)',
    re.IGNORECASE,
)
_DIM_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:cm\s*)?(?:x|by|×)\s*(\d+(?:\.\d+)?)\s*cm',
    re.IGNORECASE,
)
_HAMMER_RE = re.compile(
    r'(?:Hammer\s*price|Sold\s*for|Realised|Realized)[^<]*'
    r'(?:&pound;|£)\s*([\d,]+)',
    re.IGNORECASE,
)
_PROV_RE = re.compile(
    r'<strong>\s*Provenance\s*:?\s*</strong>\s*(?:<br\s*/?>)?\s*([\s\S]+?)(?:</p>|<p>)',
    re.IGNORECASE,
)
_AUCTION_TITLE_RE = re.compile(
    r'<(?:h2|h3|h4)[^>]*class="[^"]*(?:auction|sale)-title[^"]*"[^>]*>([^<]+)</(?:h2|h3|h4)>',
    re.IGNORECASE,
)


def _strip_html(s):
    if not s: return ""
    import html as html_lib
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'</p>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html_lib.unescape(s).replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def parse_lot_detail(text, slug, lot_id, au, verbose=False):
    """Parse one /auction/lot/ page → record dict (without sale_date / hammer
    yet for upcoming).  Returns None when title can't be extracted."""
    title_m = _LOT_TITLE_RE.search(text)
    desc_m = _LOT_DESC_RE.search(text)
    est_m = _ESTIMATE_RE.search(text)
    if not title_m or not desc_m:
        if verbose:
            print(f"    skip {lot_id}: no title/desc", flush=True)
        return None
    raw_title = _strip_html(title_m.group(1))
    desc_html = desc_m.group(1)
    desc = _strip_html(desc_html)

    # Artist + artwork title — UK catalogues use one of these patterns:
    #   "Than Binh Nguyen (b.1954) Vietnamese, Mother & Child, oil on canvas..."
    #   "Jennifer Lee (b.1956), 'Tall dark with ridge', a large stoneware..."
    # The slug echoes the artist: 'lot-N---artist-name-bDDDD-title...'
    artist_name = ""
    artwork_title = ""
    m_artist = re.match(
        r"^([A-Z][A-Za-z .'\-]+?)\s*\(\s*b\.\s*\d{4}\)\s*(?:[A-Z][a-z]+,?\s*)?",
        desc,
    )
    if m_artist:
        artist_name = m_artist.group(1).strip()
        rest = desc[m_artist.end():]
        # Quoted title 'Title' or "Title"
        m_qtitle = re.match(r"['\"']([^'\"]{2,150}?)['\"']", rest)
        if m_qtitle:
            artwork_title = m_qtitle.group(1).strip()
        else:
            # Unquoted: text up to next comma (',' followed by lowercase ='medium' or 'signed')
            m_utitle = re.match(
                r"([A-Z][^,]{2,80}?)(?=,\s*(?:oil|watercolour|watercolor|ink|gouache|lacquer|"
                r"acrylic|signed|dated|circa|pencil|charcoal|a\s+(?:large|small|fine)))",
                rest, re.IGNORECASE,
            )
            if m_utitle:
                artwork_title = m_utitle.group(1).strip().rstrip(",.;")
    # Fall back: derive artist from slug if structured 'lot-N---artist-name-bYYYY-...'
    if not artist_name:
        slug_clean = re.sub(r"^lot-\d+-+", "", slug)
        m_slug = re.match(r"([a-z]+(?:-[a-z]+){0,4})-b\d{4}", slug_clean)
        if m_slug:
            artist_name = " ".join(w.capitalize() for w in m_slug.group(1).split("-"))
    # Fall back: use raw_title prefix
    if not artwork_title:
        artwork_title = raw_title[:200]

    # Medium + dimensions from description
    dim_m = _DIM_RE.search(desc)
    dims = ""
    if dim_m:
        dims = f"{dim_m.group(1)} x {dim_m.group(2)} cm"

    # Medium — common UK terms inline in description
    medium = ""
    for kw in ("oil on canvas", "oil on board", "oil on panel",
               "watercolour on paper", "ink on paper", "lacquer on wood",
               "lithograph", "etching", "screenprint", "gouache", "acrylic"):
        if kw in desc.lower():
            medium = kw
            break

    estimate_low = estimate_high = None
    if est_m:
        try:
            estimate_low = int(est_m.group(1).replace(",", ""))
            estimate_high = int(est_m.group(2).replace(",", ""))
        except ValueError:
            pass

    hammer = None
    h_m = _HAMMER_RE.search(text)
    if h_m:
        try:
            hammer = float(h_m.group(1).replace(",", ""))
        except ValueError:
            pass

    prov = ""
    p_m = _PROV_RE.search(desc_html)
    if p_m:
        prov = _strip_html(p_m.group(1))[:1000]

    # Auction title (sale name) — separate API not required; HTML has it
    sale_title = ""
    at_m = _AUCTION_TITLE_RE.search(text)
    if at_m:
        sale_title = clean_text(at_m.group(1))[:200]
    if not sale_title:
        # Fall back to a generic 'Dawsons Auction {au}'
        sale_title = f"Dawsons Auction {au}"

    return {
        "source": "dawsons",
        "source_url": f"{BASE}/auction/lot/{slug}/?lot={lot_id}&au={au}",
        "sale_page_url": f"{BASE}/auction/search/?au={au}&sd=2",
        "lot_number": "",
        "auction_title": sale_title,
        "sale_date": "",     # Dawsons doesn't surface sale date in detail HTML reliably
        "sale_location": "Maidenhead, UK",
        "artist_name_raw": artist_name,
        "artwork_title": artwork_title,
        "medium": medium,
        "dimensions": dims,
        "year": "",
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "hammer_price": hammer,
        "price_with_premium": None,
        "currency": "GBP",
        "status": "sold" if hammer else "passed",
        "provenance": prov,
        "raw_snapshot": desc[:500],
        "catalog_description": desc[:2000],
    }


def crawl(conn, au_min=AU_MIN, au_max=AU_MAX, delay=0.6, verbose=True):
    run_started = datetime.utcnow().isoformat() + "Z"
    total_inserted = 0
    scanned = 0
    for au in range(au_min, au_max + 1):
        try:
            vn_lots = _list_lot_urls_for_auction(au)
        except Exception as e:
            if verbose:
                print(f"  au={au}: list err {e}", flush=True)
            continue
        if not vn_lots:
            continue
        if verbose:
            print(f"  au={au}: {len(vn_lots)} VN candidate lots", flush=True)
        for slug, lot_id in vn_lots:
            url = f"{BASE}/auction/lot/{slug}/?lot={lot_id}&au={au}"
            text, err = _fetch(url)
            scanned += 1
            if err:
                continue
            rec = parse_lot_detail(text, slug, lot_id, au, verbose=verbose)
            if not rec:
                continue
            if not rec["artist_name_raw"]:
                continue
            insert_sale_result(conn, rec)
            total_inserted += 1
            if verbose:
                print(f"    + {rec['artist_name_raw'][:25]:<25} | {rec['artwork_title'][:30]:<30} | "
                      f"{rec['hammer_price'] or '—'} {rec['currency']}", flush=True)
            time.sleep(delay)
        time.sleep(delay)
    conn.commit()
    log_crawl_run(
        conn, "dawsons", target_slug=f"au={au_min}-{au_max}",
        started_at=run_started,
        lots_scanned=scanned, lots_inserted=total_inserted,
        status="ok",
        note=f"Dawsons UK direct crawl, au range {au_min}-{au_max}",
    )
    if verbose:
        print(f"\n  Dawsons total: {total_inserted} inserted, {scanned} VN candidates fetched", flush=True)
    return total_inserted
