"""Heritage Auctions crawler — search-by-artist strategy.

Heritage (ha.com) is the largest US auctioneer; its Asian Art and Fine Art
departments occasionally feature Vietnamese masters (Lê Phổ, Mai Trung Thứ,
Vũ Cao Đàm, Lebadang). All past results are public, but ha.com sits behind
DataDome (geo.captcha-delivery.com) — every page below the homepage returns
HTTP 403 with a captcha interstitial until the client solves a JS challenge.

DataDome blocking observed in this environment (screening 2026-06-03):

* `requests` + browser headers      → 403 (datadome interstitial)
* `cloudscraper.create_scraper()`   → 403 (cloudscraper handles Cloudflare, not DataDome)
* `curl_cffi.requests` (chrome120)  → 403 (TLS impersonation isn't enough)
* `playwright` headless (Chromium)  → 403 (`navigator.webdriver` is one of many signals)
* `playwright` headless + stealth + real Chrome channel → 403 with `'t':'fe'`
  (fingerprint enforcement triggered even with valid datadome cookie from
  homepage warm-up)

So although the parser logic in this file is complete, in practice
`crawl()` will fetch 0 lots from CI/headless runs. To make it work live:

1. Provide a session-cookie file (`HA_COOKIES_JSON` env var pointing at JSON
   `[{name, value, domain, path}…]` exported from a real browser session
   AFTER a human has visited ha.com and passed the captcha) — those cookies
   include `datadome=…` which is accepted for ~60 minutes.
2. OR route fetches through a residential proxy + DataDome solver (e.g.
   Bright Data Web Unlocker, ScrapingBee, Zyte) by setting `HA_PROXY_URL`.
3. OR run Playwright HEADFUL on an interactive machine, solve the captcha
   once, save storage_state, and reuse it.

Heritage URL patterns observed:

* Past auction archives  → /c/auction-archives.zx
* Search results         → /c/search-results.zx?Ntt=<query>&N=<facet_ids>
* Lot detail (legacy)    → /c/item.zx?saleNo=<sale>&lotNo=<lot>
* Lot detail (current)   → /itm/<dept>/<subcat>/<slug>/a/<sale>-<lot>.s
* Fine Art subdomain     → https://fineart.ha.com (same backend)
* Sale homepage          → /c/auction-home.zx?saleNo=<sale> (robots-disallowed)

Strategy: search-by-artist is more reliable than crawling Asian Art sales
because Heritage's Asian Art catalogs are mostly Chinese/Japanese works
with sparse VN content scattered across many sales. Searching by name
short-circuits straight to relevant lots regardless of sale.

Currency = USD throughout.
"""
import json
import os
import re
import time
import html as _html_lib
from datetime import datetime
from urllib.parse import quote_plus, urljoin

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run


# ───── Fetcher ──────────────────────────────────────────────────────────────
# We layer three fetchers — try the cheapest first, fall back to Playwright.

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    _CLOUDSCRAPER_OK = True
except Exception:
    import requests
    _scraper = requests.Session()
    _CLOUDSCRAPER_OK = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


# Vietnamese artists we want Heritage results for. Ordered by likelihood of
# appearance in Heritage catalogs (US-collected Indochine pieces).
SEED_ARTIST_QUERIES = [
    "Le Pho",
    "Mai Trung Thu",
    "Mai Thu",
    "Vu Cao Dam",
    "Lebadang",
    "Le Ba Dang",
    "Pham Hau",
    "Nguyen Gia Tri",
    "Bui Xuan Phai",
    "Nguyen Phan Chanh",
    "Tran Phuc Duyen",
    "Alix Ayme",
]


# Some Asian-art sale URLs that historically carry VN lots (kept as a hint
# for the alt browse-strategy). Heritage sale numbers monotonically increase
# (~5000 = 2010, ~8000 = 2020). Each sale's archive is at the URL below.
SEED_SALE_URLS = [
    "https://fineart.ha.com",
    "https://www.ha.com/c/search-results.zx?N=51+790+231+232&type=fineart-paintings-and-sculpture",
    "https://www.ha.com/c/search-results.zx?N=51+790+231+232+232027",  # Asian fine art facet
]


# ───── Optional Playwright fallback ─────────────────────────────────────────

def _fetch_playwright(url, timeout_sec=30):
    """Headless Chrome + stealth, with optional stored session cookies.
    Returns (html, error_str)."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as e:
        return None, f"playwright unavailable: {e}"

    storage_state_path = os.environ.get("HA_STORAGE_STATE")  # path to JSON
    cookies_path = os.environ.get("HA_COOKIES_JSON")          # path to JSON list
    proxy_url = os.environ.get("HA_PROXY_URL")
    launch_kwargs = {"headless": True}
    try:
        launch_kwargs["channel"] = "chrome"
    except Exception:
        pass
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}

    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(**launch_kwargs)
            ctx_kwargs = {
                "viewport": {"width": 1440, "height": 900},
                "user_agent": HEADERS["User-Agent"],
                "locale": "en-US",
            }
            if storage_state_path and os.path.exists(storage_state_path):
                ctx_kwargs["storage_state"] = storage_state_path
            ctx = browser.new_context(**ctx_kwargs)

            if cookies_path and os.path.exists(cookies_path):
                try:
                    with open(cookies_path) as f:
                        cookies = json.load(f)
                    ctx.add_cookies(cookies)
                except Exception as ce:
                    pass

            page = ctx.new_page()
            r = page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            page.wait_for_timeout(3000)
            if r and r.status == 403:
                browser.close()
                return None, "HTTP 403 (DataDome)"
            html = page.content()
            browser.close()
            return html, None
    except Exception as e:
        return None, f"playwright err: {e}"


def _fetch(url, timeout=30, use_playwright_fallback=True):
    """GET with cloudscraper; fall back to Playwright on 403/blank.
    Returns (html, err)."""
    try:
        r = _scraper.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        if use_playwright_fallback:
            return _fetch_playwright(url, timeout_sec=timeout)
        return None, f"request err: {e}"
    if r.status_code == 200 and "captcha-delivery.com" not in r.text:
        return r.text, None
    if use_playwright_fallback:
        html, err = _fetch_playwright(url, timeout_sec=timeout)
        if html and "captcha-delivery.com" not in html:
            return html, None
        return None, err or f"HTTP {r.status_code} (DataDome)"
    return None, f"HTTP {r.status_code}"


# ───── Parsers ──────────────────────────────────────────────────────────────

def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_lib.unescape(s).replace("\xa0", " ").replace("’", "'")
    return re.sub(r"[ \t]+", " ", s).strip()


_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
_LOT_HREF_RE = re.compile(
    r'href="(/(?:itm/[^"]+/a/\d+-\d+\.s(?:\?[^"]*)?|c/item\.zx\?[^"]*saleNo=\d+[^"]*lotNo=\d+[^"]*))"',
    re.IGNORECASE,
)
_SALE_LOT_RE = re.compile(r"(?:saleNo=(\d+)[^a-z]*lotNo=(\d+))|/a/(\d+)-(\d+)\.s")


def _extract_lot_links_from_search(html_text, base="https://www.ha.com"):
    """Pull every distinct /itm/.../a/<sale>-<lot>.s or /c/item.zx?...
    lot URL out of a search-results page. Returns ordered list of full URLs."""
    seen = set()
    out = []
    for m in _LOT_HREF_RE.finditer(html_text):
        href = _html_lib.unescape(m.group(1))
        full = urljoin(base, href)
        # Strip tracking params
        full = re.sub(r"[?&](type|ic\d?|sourceLink)=[^&]*", "", full).rstrip("?&")
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _parse_lot_page(html_text, lot_url):
    """Parse a single Heritage lot detail page. Returns sale_result-record-shaped dict
    or (None, err). Heritage lot pages carry an `og:title`, structured JSON-LD, plus
    a sidebar with 'Sold for: $X' / 'Estimate $X-Y' lines."""
    # Try JSON-LD first (Heritage sometimes embeds Product schema)
    artist = ""
    artwork = ""
    sale_date = ""
    sale_location = ""
    auction_name = ""
    hammer = None
    est_low = est_high = None
    status = "passed"
    medium = ""
    dims = ""
    year = ""

    for ld_match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.+?)</script>',
        html_text, re.DOTALL,
    ):
        try:
            data = json.loads(ld_match.group(1))
        except Exception:
            continue
        if isinstance(data, list):
            data_list = data
        else:
            data_list = [data]
        for d in data_list:
            t = d.get("@type", "") if isinstance(d, dict) else ""
            if t in ("Product", "VisualArtwork", "CreativeWork"):
                artwork = artwork or d.get("name", "")
                creator = d.get("creator") or d.get("author") or {}
                if isinstance(creator, list) and creator:
                    creator = creator[0]
                if isinstance(creator, dict):
                    artist = artist or creator.get("name", "")
                elif isinstance(creator, str):
                    artist = artist or creator
                offers = d.get("offers") or {}
                if isinstance(offers, dict):
                    price = offers.get("price")
                    if price and not hammer:
                        try:
                            hammer = float(str(price).replace(",", ""))
                            status = "sold"
                        except ValueError:
                            pass
                if not medium and d.get("artMedium"):
                    medium = d["artMedium"]
            elif t == "Event":
                auction_name = auction_name or d.get("name", "")
                sd = d.get("startDate", "")
                mm = re.match(r"(\d{4}-\d{2}-\d{2})", sd)
                if mm and not sale_date:
                    sale_date = mm.group(1)
                loc = d.get("location") or {}
                if isinstance(loc, dict) and not sale_location:
                    sale_location = loc.get("name", "") or (loc.get("address") or {}).get("addressLocality", "")

    # Fallback: regex against the visible markup
    if not artist:
        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_text)
        if m:
            og = _html_lib.unescape(m.group(1))
            # Heritage og:title is usually "Artist (years) Title - Lot #X | Auction Title"
            artist = og.split(" - ")[0].split("|")[0].strip()
    if not artwork:
        m = re.search(r"<h1[^>]*>(.+?)</h1>", html_text, re.DOTALL)
        if m:
            artwork = _strip_html(m.group(1))[:300]
    if hammer is None:
        # "Sold for: $1,200" / "Realized: $1,200" / "Hammer Price: $1,200"
        m = re.search(r"(?:Sold for|Realized|Hammer Price)\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)",
                      html_text, re.IGNORECASE)
        if m:
            hammer = float(m.group(1).replace(",", ""))
            status = "sold"
    # Estimate range — "Estimate: $1,000 - $1,500"
    m = re.search(
        r"Estimate\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)\s*[-–]\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        html_text, re.IGNORECASE,
    )
    if m:
        est_low = float(m.group(1).replace(",", ""))
        est_high = float(m.group(2).replace(",", ""))
    # Sale date — "Auction Date: Jan 15, 2024" / nearby in markup
    if not sale_date:
        m = re.search(
            r"(?:Auction Date|Sale Date)\s*[:>]\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
            html_text, re.IGNORECASE,
        )
        if m:
            sale_date = parse_date(m.group(1))
    # Auction title near top
    if not auction_name:
        m = re.search(r"<title>([^<]+)</title>", html_text)
        if m:
            t = _html_lib.unescape(m.group(1))
            t = t.split(" | Heritage Auctions")[0].split(" | HA.com")[0]
            auction_name = t.strip()
    # Cataloging details — Heritage prints them inline like:
    #   "Oil on canvas, 24 x 18 in. (61 x 45.7 cm), 1945."
    body_m = re.search(r'<div[^>]+(?:item-description|catalog|lot-description)[^>]*>(.+?)</div>',
                       html_text, re.DOTALL | re.IGNORECASE)
    blob = _strip_html(body_m.group(1)) if body_m else _strip_html(html_text[:8000])
    if not medium:
        for kw in ("oil on", "ink on", "watercolor on", "watercolour on", "gouache on",
                   "lacquer on", "tempera on", "ink and color on", "ink and gouache",
                   "pencil on", "pastel on", "acrylic on", "mixed media",
                   "charcoal on", "lithograph", "silkscreen", "etching"):
            i = blob.lower().find(kw)
            if i >= 0:
                # Capture the medium line — up to next period/newline
                tail = blob[i:i + 200]
                end = min((tail.find(c) for c in (".", "\n") if tail.find(c) > 0),
                          default=len(tail))
                medium = tail[:end].strip()[:200]
                break
    if not dims:
        m = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(?:x|by|×)\s*(\d+(?:[.,]\d+)?)\s*cm",
            blob, re.IGNORECASE,
        )
        if m:
            dims = f"{m.group(1).replace(',', '.')} x {m.group(2).replace(',', '.')} cm"
        else:
            # Inches → convert
            m = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:x|by|×)\s*(\d+(?:\.\d+)?)\s*in\.?",
                blob, re.IGNORECASE,
            )
            if m:
                w_in = float(m.group(1)); h_in = float(m.group(2))
                dims = f"{round(w_in * 2.54, 1)} x {round(h_in * 2.54, 1)} cm"
    if not year:
        m = re.search(r"(?:painted|executed|circa|c\.)[^\d]{0,15}(\d{4})", blob, re.IGNORECASE)
        if m:
            year = m.group(1)
        else:
            m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", blob)
            if m:
                year = m.group(1)

    # Sale + lot numbers from the URL
    lot_num = ""
    sm = _SALE_LOT_RE.search(lot_url)
    if sm:
        # Either (saleNo, lotNo) groups or (sale, lot) in /a/{sale}-{lot}.s
        sale_no = sm.group(1) or sm.group(3) or ""
        lot_num = sm.group(2) or sm.group(4) or ""

    if not artist:
        return None, "no artist parsed"

    rec = {
        "source": "heritage",
        "source_url": lot_url,
        "sale_page_url": "",  # filled by crawl()
        "lot_number": lot_num,
        "auction_title": f"Heritage — {auction_name}" if auction_name else "Heritage Auctions",
        "sale_date": sale_date,
        "sale_location": sale_location or "Dallas",
        "artist_name_raw": artist,
        "artwork_title": artwork,
        "medium": medium,
        "dimensions": dims,
        "year": year,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": None,
        "currency": "USD",
        "status": status if hammer else "passed",
        "provenance": "",
        "raw_snapshot": f"{artist} | {artwork[:100]}"[:300],
    }
    return rec, None


# ───── VN filter (mirrors phillips.py) ──────────────────────────────────────

def _load_vn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(__import__("sys").modules.keys()):
        if "vn_artist_catalog" in m:
            del __import__("sys").modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def is_vietnamese(artist_name, catalog, exclusions):
    from artonis_price_mvp import normalize_key
    norm = normalize_key(artist_name)
    if not norm or norm in exclusions:
        return False
    if norm in catalog:
        return True
    for k in catalog:
        if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
            return True
    return False


# ───── Main entry ───────────────────────────────────────────────────────────

def _search_url_for(query, page=1):
    """Build a Heritage search URL filtered to Fine Art categories.
    The N facet 51+790+231+232 narrows to 'Fine Art / Paintings & Sculpture / Paintings'.
    Ntk=SI_Titles searches title+artist; Nty=1 = match-any."""
    base = "https://www.ha.com/c/search-results.zx"
    params = (
        f"?N=51+790+231+232"
        f"&Ntk=SI_Titles-Desc"
        f"&Ntt={quote_plus(query)}"
        f"&Nty=1"
        f"&type=fineart-paintings-and-sculpture"
    )
    if page > 1:
        params += f"&No={(page - 1) * 24}"
    return base + params


def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Heritage Auctions by searching each Vietnamese artist name.

    Args:
      conn: sqlite3 connection (caller sets row_factory).
      sale_urls: optional override — list of search-result OR lot URLs to scan.
                 If None, iterates SEED_ARTIST_QUERIES.
      delay: seconds between page fetches (lot fetches add 0.5s).
      verbose: print progress.
      filter_vn: when True, drop lots whose artist isn't in vn_artist_catalog.
      max_pages: hard cap on total search-result pages visited per run.

    Returns:
      (total_inserted, total_scanned) tuple.

    DataDome blocker: if ha.com returns its captcha interstitial, every fetch
    fails and the function returns (0, 0) with a logged 'error' run. Set the
    HA_COOKIES_JSON / HA_STORAGE_STATE / HA_PROXY_URL env vars to plumb in a
    valid session — see module docstring.
    """
    if filter_vn:
        vn_catalog, exclusions = _load_vn()
    else:
        vn_catalog, exclusions = {}, set()

    # Build the set of search-result URLs to walk. If caller passed explicit
    # sale_urls (could be search pages OR lot URLs), use those as-is.
    if sale_urls:
        seed_urls = list(sale_urls)
    else:
        seed_urls = [_search_url_for(q, page=1) for q in SEED_ARTIST_QUERIES]

    seed_urls = seed_urls[:max_pages]
    total_inserted = 0
    total_scanned = 0
    blocked_count = 0

    for seed in seed_urls:
        run_started = datetime.utcnow().isoformat() + "Z"

        # If this looks like a lot URL, parse it directly.
        if "/item.zx" in seed or re.search(r"/a/\d+-\d+\.s", seed):
            text, err = _fetch(seed)
            if err or not text:
                if verbose:
                    print(f"  ERR {seed[-60:]}: {err}")
                blocked_count += 1
                log_crawl_run(conn, "heritage", target_slug=seed,
                              started_at=run_started, status="error",
                              note=str(err)[:200])
                continue
            rec, perr = _parse_lot_page(text, seed)
            total_scanned += 1
            if perr or not rec:
                if verbose:
                    print(f"  PARSE {seed[-60:]}: {perr}")
                log_crawl_run(conn, "heritage", target_slug=seed,
                              started_at=run_started, lots_scanned=1,
                              status="ok", note=(perr or "")[:200])
                continue
            if filter_vn and not is_vietnamese(rec["artist_name_raw"], vn_catalog, exclusions):
                log_crawl_run(conn, "heritage", target_slug=seed,
                              started_at=run_started, lots_scanned=1,
                              status="ok", note="non-VN artist")
                continue
            if not rec.get("hammer_price"):
                log_crawl_run(conn, "heritage", target_slug=seed,
                              started_at=run_started, lots_scanned=1,
                              status="ok", note="no hammer price")
                continue
            insert_sale_result(conn, rec)
            conn.commit()
            total_inserted += 1
            log_crawl_run(conn, "heritage", target_slug=seed,
                          started_at=run_started, lots_scanned=1,
                          lots_inserted=1, status="ok",
                          sale_date_min=rec.get("sale_date") or None,
                          sale_date_max=rec.get("sale_date") or None)
            if verbose:
                print(f"  + {rec['artist_name_raw']}: ${rec['hammer_price']:.0f}")
            time.sleep(delay)
            continue

        # Otherwise treat as a search-results page (browse strategy)
        text, err = _fetch(seed)
        if err or not text:
            if verbose:
                print(f"  ERR {seed[-80:]}: {err}")
            blocked_count += 1
            log_crawl_run(conn, "heritage", target_slug=seed,
                          started_at=run_started, status="error",
                          note=str(err)[:200])
            time.sleep(delay)
            continue

        lot_links = _extract_lot_links_from_search(text)
        if verbose:
            print(f"  {seed[-80:]}: {len(lot_links)} lot links")

        inserted_this = 0
        for lot_url in lot_links:
            total_scanned += 1
            ltext, lerr = _fetch(lot_url, timeout=25)
            if lerr or not ltext:
                if verbose:
                    print(f"    ERR lot {lot_url[-60:]}: {lerr}")
                blocked_count += 1
                time.sleep(0.5)
                continue
            rec, perr = _parse_lot_page(ltext, lot_url)
            if perr or not rec:
                time.sleep(0.5)
                continue
            rec["sale_page_url"] = seed
            if filter_vn and not is_vietnamese(rec["artist_name_raw"], vn_catalog, exclusions):
                continue
            if re.search(
                r"\b(d'?apr[eè]s|after\s+(mai|le\s+pho|vu\s+cao)|copie|copy of|reproduction)\b",
                rec["artwork_title"] + " " + rec["artist_name_raw"],
                re.IGNORECASE,
            ):
                continue
            if not rec.get("hammer_price"):
                continue
            insert_sale_result(conn, rec)
            inserted_this += 1
            time.sleep(0.5)
        conn.commit()
        total_inserted += inserted_this
        log_crawl_run(conn, "heritage", target_slug=seed,
                      started_at=run_started, lots_scanned=len(lot_links),
                      lots_inserted=inserted_this, status="ok",
                      note=f"search seed (blocked_lots={0})")
        if verbose:
            print(f"  → {inserted_this} VN lots inserted from this seed")
        time.sleep(delay)

    if verbose:
        if blocked_count and total_inserted == 0:
            print(
                "\n  Heritage: every fetch blocked by DataDome — set "
                "HA_COOKIES_JSON or HA_PROXY_URL (see crawlers/heritage.py docstring)."
            )
        print(f"\n  Heritage total inserted: {total_inserted}, scanned: {total_scanned}")
    return total_inserted, total_scanned
