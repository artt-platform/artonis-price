"""Ravenel Art Group crawler — Taipei-based auction house with HK presence.

Ravenel's English site (`https://www.ravenel.com/en/...`) exposes a clean JSON API
for past-sale lots that is *not* protected by a CDN challenge — plain HTTP works,
no Playwright needed. cloudscraper is used only for soft TLS-fingerprint parity.

Past sales are listed inline in the catalogue page HTML:
    https://www.ravenel.com/en/cata/hisCata/882f4163-b8e5-458a-9e35-662911d540b8
  (`882f4163-...` = English-language category UID, found via `/cata/e`)
The page embeds `var transactionList = [...]` — a JS literal containing every
historical English-language sale with `auctionUidStr`, `auctionName`, `themeName`,
`transactionUrl` (PDF results) and language code.

Lots for one sale (English catalog) come from:
    GET /rest/auc/lots/{auctionUid}?orderBy=lotSn&language=en&pageIndex=N&pageSize=100
Response: `{code, text, total, data: [lot, ...]}` with each lot carrying
`name` (artwork title), `sn` (lot number), `artistList[0].{name, race, simpleInfo}`,
`material`, `dimension`, `year`, plus `estimateCurrencyValue` and
`finalCurrencyValue` arrays in TWD / HKD / USD. TWD is primary for Taipei sales,
HKD for Hong Kong sales — we use the per-sale currency derived from auctionName.

VN content at Ravenel is sparse (~30 lots across 12 sales out of 75 English
catalogs as of 2026-06-02). Most VN lots sit in the smaller "Select: Modern &
Contemporary Art" sales (2018-2020, ~80-100 lots each), with named contributors:
TRAN Luu Hau, Trung Nguyen, Lam Nguyen, DAO Hai Phong, Boi Tran, Thanh Binh
Nguyen, plus singular appearances by Lê Phổ (HK 2014) and Bùi Xuân Phái (2023).

No per-lot HTML detail fetch is required — the lots API already returns
material/dimensions/year. Sale-date metadata is not in any API response; we
derive it from auctionName via the Spring=early-June / Autumn=early-December
convention Ravenel has used since 1999.
"""
import re
import time
import json
import html as _html_lib
from datetime import datetime

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except Exception:
    import requests
    _scraper = requests.Session()

from crawlers.common import insert_sale_result, log_crawl_run


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Known past Ravenel sales with Vietnamese art (screened 2026-06-02 — all 75
# English-language catalogs scanned via /rest/auc/lots and filtered by race
# tag containing "Vietnamese" plus name-catalog containment).
# Ranked by VN lot count.
#
# Each URL is the human-readable lots page: /en/auCal/lots/{auctionUidStr}
# The crawler extracts the trailing UUID and hits the JSON API directly.
SEED_SALE_URLS = [
    # 7 VN — 2019-12 Taipei Select (Trung Nguyen ×3, Lam Nguyen ×2, DAO Hai Phong ×2)
    "https://www.ravenel.com/en/auCal/lots/e6a1a7bb-0f5c-47ce-846e-d5d7ca03c3a6",
    # 7 VN — 2019-06 Taipei Select (Boi Tran, Trung Nguyen ×2, Thanh Binh Nguyen ×2, DAO Hai Phong ×2)
    "https://www.ravenel.com/en/auCal/lots/c60b4dd3-b5ba-4b9a-bc36-af8d7cbd8083",
    # 3 VN — 2020-08 Taipei Select (TRAN Luu Hau, Trung Nguyen ×2)
    "https://www.ravenel.com/en/auCal/lots/89c5c804-b1f2-4280-b3de-80fc652a8333",
    # 3 VN — 2018-12 Taipei Select (Trung Nguyen ×2, Thanh Binh Nguyen)
    "https://www.ravenel.com/en/auCal/lots/febc120d-d964-4b54-90ac-2dfcf89c4b6b",
    # 2 VN — 2010-11 Hong Kong (TRAN Luu Hau, Nguyen Dieu Thuy)
    "https://www.ravenel.com/en/auCal/lots/71acb1d2-386f-4394-b951-38bf661ed2e8",
    # 1 VN — 2023-06 Taipei Select (Bùi Xuân Phái)
    "https://www.ravenel.com/en/auCal/lots/67ff693e-c320-44ba-b6ac-eb0ab53b987d",
    # 1 VN — 2015-11 Hong Kong (TRAN Luu Hau)
    "https://www.ravenel.com/en/auCal/lots/b131a469-742b-4bb3-87cc-32fe5c7c6fb5",
    # 1 VN — 2014-11 Hong Kong (LE PHO)
    "https://www.ravenel.com/en/auCal/lots/c3d7b1a3-8150-4a63-996e-f67e2366dd4f",
]


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*cm", re.IGNORECASE)


def _fetch_json(url, params=None, timeout=30):
    """GET a JSON endpoint. Returns (data, err)."""
    try:
        r = _scraper.get(url, params=params, headers=HEADERS, timeout=timeout)
    except Exception as e:
        return None, f"request err: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), None
    except Exception as e:
        return None, f"json parse: {e}"


def _fetch_text(url, timeout=30):
    try:
        r = _scraper.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        return None, f"request err: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.text, None


def _auction_uid_from_url(url):
    """Extract the UUID at the end of /auCal/lots/{uid} or /cata/hisCata/{uid}."""
    m = _UUID_RE.search(url)
    return m.group(0) if m else None


def _derive_sale_meta(auction_name):
    """Derive (sale_date, sale_location) from an auctionName string like
    'Ravenel Spring Auction 2026 Taipei' or 'Ravenel Autumn Auction 2014 Hong Kong'.

    Ravenel's spring sale typically runs early June (Taipei) or end-May (HK);
    autumn sale typically runs early December (Taipei) or late October (HK).
    Without per-sale calendar data we approximate to the first of the month."""
    if not auction_name:
        return "", "Taipei"
    name = auction_name
    location = "Taipei"
    nl = name.lower()
    if "hong kong" in nl or "hongkong" in nl:
        location = "Hong Kong"
    # Year
    year_m = re.search(r"\b(20\d{2}|19\d{2})\b", name)
    year = year_m.group(1) if year_m else ""
    # Season → month
    month = ""
    if "spring" in nl:
        month = "06" if "taipei" in nl else "05"
    elif "autumn" in nl or "fall" in nl:
        month = "12" if "taipei" in nl else "11"
    elif "summer" in nl:
        month = "08"
    elif "winter" in nl:
        month = "01"
    if year and month:
        return f"{year}-{month}-01", location
    if year:
        return f"{year}-01-01", location
    return "", location


def _parse_price_str(s):
    """'15,500-25,800' → (15500.0, 25800.0) ; '24,742' → (24742.0, None)."""
    if not s:
        return None, None
    s = s.replace(",", "").replace(" ", "").strip()
    if not s:
        return None, None
    # Range
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*[-–~]\s*(-?\d+(?:\.\d+)?)$", s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            return None, None
    try:
        return float(s), None
    except ValueError:
        return None, None


def _pick_currency_value(arr, want_currency):
    """From estimateCurrencyValue / finalCurrencyValue list, pick the entry with
    displayName == want_currency. Returns the entry dict or None."""
    if not arr:
        return None
    for entry in arr:
        if (entry.get("displayName") or "").upper() == want_currency.upper():
            return entry
    return None


def _strip_html_simple(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_lib.unescape(s).replace("\xa0", " ").replace("’", "'")
    return re.sub(r"[ \t]+", " ", s).strip()


def _normalize_dimension(raw):
    """Trim trailing imperial '(37 3/4 x 51 in.)'; collapse whitespace."""
    if not raw:
        return ""
    s = _strip_html_simple(raw)
    # Drop trailing parenthetical that contains "in" (imperial conversion)
    s = re.sub(r"\s*\([^)]*\bin\.?\b[^)]*\)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def lot_to_record(lot, sale_meta):
    """Convert one Ravenel API lot dict → sale_result record dict.
    Returns None when the lot has no usable artist."""
    artists = lot.get("artistList") or []
    if not artists:
        return None
    a = artists[0]
    artist_raw = (a.get("name") or "").strip()
    if not artist_raw:
        return None

    primary_cur = sale_meta.get("currency", "TWD")

    # Pricing — prefer primary currency, fall back to USD
    est_entry = _pick_currency_value(lot.get("estimateCurrencyValue"), primary_cur) \
                or _pick_currency_value(lot.get("estimateCurrencyValue"), "USD")
    fin_entry = _pick_currency_value(lot.get("finalCurrencyValue"), primary_cur) \
                or _pick_currency_value(lot.get("finalCurrencyValue"), "USD")

    est_low, est_high = (None, None)
    cur_used = primary_cur
    if est_entry:
        est_low, est_high = _parse_price_str(est_entry.get("value") or est_entry.get("displayValue"))
        cur_used = est_entry.get("displayName") or cur_used
    hammer = None
    if fin_entry:
        h, _ = _parse_price_str(fin_entry.get("value") or fin_entry.get("displayValue"))
        if h is not None:
            hammer = h
            cur_used = fin_entry.get("displayName") or cur_used

    # Title — Ravenel embeds raw HTML breaks for multi-line titles
    title = _strip_html_simple(lot.get("name") or "")

    # Dimensions / medium / year — pulled from flat fields when present
    medium = (lot.get("material") or "").strip()
    dims_raw = lot.get("dimension") or ""
    dims = _normalize_dimension(dims_raw)
    # If no canonical "WxH cm" left, drop dims so downstream area calc isn't fed garbage
    if dims and not _DIM_RE.search(dims):
        dims = ""
    year = (lot.get("year") or "").strip()

    # NOTE: Ravenel /en/cata/lots/{UUID} per-lot pages return 404 — the site
    # only keeps the parent sale catalog (/en/auCal/lots/{auctionUid}) live.
    # Point source_url at the sale page (with #lot=NN fragment so we can scroll
    # to the relevant lot when their catalog UI supports it).
    sale_url = sale_meta.get("sale_url", "")
    lot_sn = str(lot.get("sn", "") or "")
    source_url = (
        f"{sale_url}#lot-{lot_sn}" if sale_url and lot_sn else sale_url
    )

    status = "sold" if hammer is not None else "passed"

    auction_title = f"Ravenel — {sale_meta.get('auction_name', '')}".strip(" —")

    return {
        "source": "ravenel",
        "source_url": source_url,
        "sale_page_url": sale_meta.get("sale_url", ""),
        "lot_number": str(lot.get("sn", "") or ""),
        "auction_title": auction_title,
        "sale_date": sale_meta.get("sale_date", ""),
        "sale_location": sale_meta.get("sale_location", "Taipei"),
        "artist_name_raw": artist_raw,
        "artwork_title": title[:300],
        "medium": medium[:200],
        "dimensions": dims,
        "year": year,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": None,
        "currency": cur_used or primary_cur,
        "status": status,
        "provenance": "",
        "raw_snapshot": (
            f"{artist_raw} ({a.get('race','')}) | {title[:120]}"
        )[:300],
        "_artist_race": a.get("race", "") or "",
    }


def fetch_sale_lots(auction_uid, verbose=True, page_size=100, max_pages=10):
    """Page through `/rest/auc/lots/{uid}` and return (lots, total).
    Stops after `max_pages` pages or when fewer items than `page_size` are returned."""
    base = f"https://www.ravenel.com/rest/auc/lots/{auction_uid}"
    all_lots = []
    total = 0
    for page in range(max_pages):
        params = {
            "orderBy": "lotSn",
            "fuzzyKeyword": "",
            "priceStart": "",
            "priceStop": "",
            "language": "en",
            "pageIndex": page,
            "pageSize": page_size,
        }
        data, err = _fetch_json(base, params=params)
        if err:
            if verbose:
                print(f"    page {page}: {err}")
            break
        total = data.get("total", 0) or total
        items = data.get("data", []) or []
        all_lots.extend(items)
        if len(items) < page_size:
            break
        if len(all_lots) >= total:
            break
    return all_lots, total


# ---- Sale-meta lookup --------------------------------------------------------

_TRANSACTION_LIST_RE = re.compile(r"var transactionList\s*=\s*(\[.+?\]);", re.DOTALL)


def fetch_sale_index(verbose=False):
    """Fetch the master English-language past-catalogue index.
    Returns a dict { auctionUidStr: {auctionName, themeName, transactionUrl, ...} }."""
    url = "https://www.ravenel.com/en/cata/hisCata/882f4163-b8e5-458a-9e35-662911d540b8"
    text, err = _fetch_text(url)
    if err or not text:
        if verbose:
            print(f"  sale-index: {err}")
        return {}
    m = _TRANSACTION_LIST_RE.search(text)
    if not m:
        return {}
    # Ravenel switched the embedded list from JSON-ish to a Python/JS-literal
    # mix: single-quoted strings, bare `null`/`true`/`false`, `\uXXXX` escapes.
    # naive `replace("'", '"')` corrupts strings with apostrophes
    # ("L'art" → "L"art"). Use ast.literal_eval after normalising JS sigils.
    import ast
    raw = m.group(1)
    raw = re.sub(r'\\u([0-9a-fA-F]{4})', lambda mm: chr(int(mm.group(1), 16)), raw)
    py = (
        raw.replace(':null', ':None')
        .replace(':true', ':True')
        .replace(':false', ':False')
    )
    try:
        data = ast.literal_eval(py)
    except (SyntaxError, ValueError):
        # Old JSON-ish path as last-ditch fallback
        try:
            data = json.loads(raw.replace("'", '"'))
        except Exception:
            return {}
    out = {}
    for d in data:
        uid = d.get("auctionUidStr")
        if uid:
            out[uid] = {
                "auction_name": d.get("auctionName", ""),
                "theme_name": d.get("themeName", ""),
                "transaction_url": d.get("transactionUrl", ""),
                "language": d.get("language", "en"),
            }
    return out


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


_NON_VN_RACES = (
    "japanese", "chinese", "taiwanese", "korean", "thai", "indonesian",
    "filipino", "philipino", "malaysian", "british", "american", "german",
    "italian", "spanish", "french", "swiss", "dutch", "russian",
    "hungarian", "australian", "venezuelan", "argentine", "dominican",
    "hong kong", "latvian",
)


def _is_vietnamese(artist, race, vn_catalog, exclusions):
    """A lot counts as VN when EITHER:
      (a) the artistList[0].race tag explicitly contains "Vietnam" (most reliable
          signal — Ravenel labels lots like 'Vietnamese' or 'Vietnamese-French'); OR
      (b) the artist name normalizes to a key in vn_artist_catalog AND the race
          tag is empty / does not contradict (i.e. doesn't name a different
          nationality). This blocks the 'LY (Japanese)' false-positive that
          would otherwise slip through name-prefix matching on 'ly truc son'.
    """
    from artonis_price_mvp import normalize_key
    race_l = (race or "").lower()
    if "viet" in race_l:
        # Still respect exclusions (rare)
        key = normalize_key(artist)
        if key in exclusions:
            return False
        return True
    # Race contradicts — short-circuit
    if any(nation in race_l for nation in _NON_VN_RACES):
        return False
    key = normalize_key(artist)
    if not key or key in exclusions:
        return False
    if key in vn_catalog:
        return True
    # Multi-token prefix match (safer than single-token)
    if " " in key:
        for k in vn_catalog:
            if key == k or key.startswith(k + " ") or k.startswith(key + " "):
                return True
    return False


# ---- Public entry ----------------------------------------------------------

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Ravenel sale pages and extract VN lots via the lots JSON API.

    Args:
      conn: sqlite3 connection (row_factory set by caller).
      sale_urls: list of `/en/auCal/lots/{auctionUid}` URLs. Defaults to SEED_SALE_URLS.
      delay: seconds between sale-page fetches (lot pagination adds ~0.3s extra).
      verbose: print progress to stdout.
      filter_vn: when True, drop lots whose artist isn't tagged Vietnamese
        and isn't matchable in vn_artist_catalog.
      max_pages: hard cap on number of sale URLs visited per run.

    Returns:
      (total_inserted, total_scanned) — both ints.
    """
    sale_urls = sale_urls or SEED_SALE_URLS
    sale_urls = sale_urls[:max_pages]
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    # Pre-fetch the sale index once so we can resolve auctionName / themeName
    # per UUID. If this fails (network blip) we fall back to deriving meta from
    # the API response and the URL itself.
    sale_index = fetch_sale_index(verbose=verbose)
    if verbose and sale_index:
        print(f"  Ravenel: {len(sale_index)} past-sale entries in index")

    total_inserted = 0
    total_scanned = 0
    skipped_nonvn = 0

    for sale_url in sale_urls:
        run_started = datetime.utcnow().isoformat() + "Z"
        auction_uid = _auction_uid_from_url(sale_url)
        if not auction_uid:
            if verbose:
                print(f"  WARN: no UUID in {sale_url}")
            log_crawl_run(conn, "ravenel", target_slug=sale_url, started_at=run_started,
                          status="error", note="no UUID in URL")
            continue

        meta = sale_index.get(auction_uid, {})
        auction_name = meta.get("auction_name", "")
        sale_date, sale_location = _derive_sale_meta(auction_name)
        sale_currency = "HKD" if "Hong Kong" in (auction_name or "") else "TWD"
        sale_meta = {
            "sale_url": sale_url,
            "auction_name": auction_name,
            "theme_name": meta.get("theme_name", ""),
            "sale_date": sale_date,
            "sale_location": sale_location,
            "currency": sale_currency,
        }

        if verbose:
            print(f"  [{auction_uid[:8]}] {auction_name or '(unknown)'} → "
                  f"{sale_location} {sale_date}")
        lots, total = fetch_sale_lots(auction_uid, verbose=verbose)
        if not lots:
            log_crawl_run(conn, "ravenel", target_slug=sale_url, started_at=run_started,
                          lots_scanned=0, status="error",
                          note=f"no lots returned ({auction_name})"[:200])
            if verbose:
                print(f"    no lots returned (total reported {total})")
            time.sleep(delay)
            continue

        total_scanned += len(lots)
        inserted_this = 0
        for lot in lots:
            rec = lot_to_record(lot, sale_meta)
            if not rec:
                continue
            race = rec.pop("_artist_race", "")
            if filter_vn and not _is_vietnamese(rec["artist_name_raw"], race,
                                                vn_catalog, exclusions):
                skipped_nonvn += 1
                continue
            # Skip explicit copies / reproductions
            blob = rec["artwork_title"] + " " + rec["artist_name_raw"]
            if re.search(r"\b(d'?apr[eè]s|after\s+(le\s+pho|mai|vu\s+cao)|"
                         r"copie|copy of|reproduction)\b", blob, re.IGNORECASE):
                continue
            # Ravenel publishes hammer for sold lots; passed lots have no
            # hammer_price — skip those, matching the pattern in phillips.py.
            if not rec.get("hammer_price"):
                continue
            insert_sale_result(conn, rec)
            inserted_this += 1
        conn.commit()
        log_crawl_run(
            conn, "ravenel", target_slug=sale_url, started_at=run_started,
            lots_scanned=len(lots), lots_inserted=inserted_this,
            sale_date_min=sale_date or None, sale_date_max=sale_date or None,
            status="ok", note=(auction_name or "")[:120],
        )
        if verbose:
            print(f"    {len(lots)} lots scanned, {inserted_this} VN inserted")
        total_inserted += inserted_this
        time.sleep(delay)

    if verbose:
        print(f"\n  Ravenel total: inserted={total_inserted}, "
              f"scanned={total_scanned}, skipped non-VN={skipped_nonvn}")
    return total_inserted, total_scanned
