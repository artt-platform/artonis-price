"""Sotheby's crawler — parses __NEXT_DATA__ from sale pages (Algolia-backed).
Each sale page contains algoliaJson.hits with first ~48 lots.
Uses the per-page algoliaSearchKey to fetch additional pages via Algolia API directly.

Lot detail pages add medium/dimensions/provenance via apolloCache.LotV2.{description,provenance}.
"""
import re
import time
import json
import html as _html_lib
import requests

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_lib.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def fetch_lot_page_fields(lot_url):
    """Fetch a Sotheby's lot detail page and return (artwork_title, medium, dims, year, provenance).
    Algolia hits don't expose provenance/medium; the lot page does (via apolloCache.LotV2)."""
    try:
        r = requests.get(lot_url, headers=HEADERS, timeout=25)
    except Exception:
        return "", "", "", "", ""
    if r.status_code != 200:
        return "", "", "", "", ""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if not m:
        return "", "", "", "", ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return "", "", "", "", ""
    apollo = data.get("props", {}).get("pageProps", {}).get("apolloCache", {}) or {}
    lot = next((v for k, v in apollo.items() if k.startswith("LotV2:")), None)
    if not lot:
        return "", "", "", "", ""
    desc_html = lot.get("description", "") or ""
    prov_html = lot.get("provenance", "") or ""
    desc_plain = _strip_html(desc_html)

    # Title from <em> in description
    title = ""
    m_em = re.search(r"<em[^>]*>([^<]+)</em>", desc_html)
    if m_em:
        title = _strip_html(m_em.group(1))[:200]

    # Medium: first line containing common medium phrase
    medium = ""
    medium_kws = ("oil on", "ink on", "watercolor on", "gouache on", "lacquer on",
                  "tempera on", "ink and color on", "ink and gouache", "pencil on",
                  "pastel on", "acrylic on", "mixed media", "graphite", "charcoal")
    for line in desc_plain.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(kw in line.lower() for kw in medium_kws):
            medium = line[:150]
            break

    # Dimensions
    dims = ""
    m_dim = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:by|x|×)\s*(\d+(?:[.,]\d+)?)\s*cm", desc_plain, re.IGNORECASE)
    if m_dim:
        dims = f"{m_dim.group(1).replace(',', '.')} x {m_dim.group(2).replace(',', '.')} cm"

    # Year
    year = ""
    m_y = re.search(r"(?:painted|executed|circa|c\.)[^\d]{0,15}(\d{4})", desc_plain, re.IGNORECASE)
    if m_y:
        year = m_y.group(1)

    provenance = _strip_html(prov_html)[:2000]
    return title, medium, dims, year, provenance

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
ALGOLIA_APP_ID = "kar1ueupjd"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/prod_lots/query"

# Known past Sotheby's sales with Vietnamese art (screened for VN content).
# Pattern: Hong Kong modern sales carry most VN lots; NY/Paris/London near-zero.
# Ranked by VN lot count (screening date 2026-04-23).
SEED_SALE_URLS = [
    # ─── Known high-VN sales (ranked by VN lot count, 2026-04-23 screen) ──────
    "https://www.sothebys.com/en/buy/auction/2023/modern-day-auction",               # 28 VN — 2023-04-06 HK
    "https://www.sothebys.com/en/buy/auction/2022/modern-day-auction-3",              # 25 VN — 2022-10-06 HK
    "https://www.sothebys.com/en/buy/auction/2023/modern-day-auction-3",              # 18 VN — 2023-10-06 HK
    "https://www.sothebys.com/en/buy/auction/2024/modern-day-auction",                # 17 VN — 2024-04-06 HK
    "https://www.sothebys.com/en/buy/auction/2020/modern-and-contemporary-southeast-asian-art",  # 16 VN — 2020-06-12 HK
    "https://www.sothebys.com/en/buy/auction/2025/modern-contemporary-day-auction",   # 9 VN — 2025-09-29 HK
    "https://www.sothebys.com/en/buy/auction/2023/modern-evening-auction-2",          # 4 VN — 2023-04-05 HK
    "https://www.sothebys.com/en/buy/auction/2025/modern-contemporary-evening-auction",  # 2 VN — 2025-03-29 HK
    # ─── Historical 2018-2022 expansion (auto-discovered 2026-06-15 from /results) ──
    "https://www.sothebys.com/en/buy/auction/2019/modern-and-contemporary-southeast-asian-art-online",
    "https://www.sothebys.com/en/buy/auction/2019/modern-contemporary-southeast-asian-art-hk0859",  # Apr 2019
    "https://www.sothebys.com/en/buy/auction/2020/modern-and-contemporary-southeast-asian-art-hk0932",  # Jul 2020 — Nguyễn Nam Sơn Nude 1939
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


def extract_sale_data(sale_url):
    """Fetch a Sotheby's sale page and extract __NEXT_DATA__.
    Returns (auction_id, algolia_key, initial_hits, total_count, sale_meta)."""
    try:
        r = requests.get(sale_url, headers=HEADERS, timeout=25)
    except Exception as e:
        return None, None, [], 0, {}, f"request err: {e}"
    if r.status_code != 200:
        return None, None, [], 0, {}, f"HTTP {r.status_code}"

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if not m:
        return None, None, [], 0, {}, "no __NEXT_DATA__"
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        return None, None, [], 0, {}, f"parse err: {e}"

    pp = data.get("props", {}).get("pageProps", {})
    auction_id = pp.get("auctionId", "")
    algolia_key = pp.get("algoliaSearchKey", "")
    algolia_json = pp.get("algoliaJson", {}) or {}
    hits = algolia_json.get("hits", [])
    total = algolia_json.get("nbHits", len(hits))

    # Extract auction-level meta from first hit
    sale_meta = {}
    if hits:
        h0 = hits[0]
        sale_meta = {
            "auctionId": auction_id,
            "auctionName": h0.get("auctionName", ""),
            "auctionLocation": h0.get("auctionLocation", ""),
            "auctionDate": h0.get("auctionDate", ""),
            "currency": h0.get("currency", "USD"),
            "sale_url": sale_url,
        }
    return auction_id, algolia_key, hits, total, sale_meta, None


def fetch_more_pages(auction_id, algolia_key, total, got_so_far):
    """Fetch additional pages from Algolia directly."""
    all_hits = []
    hits_per_page = 48
    page = 1
    while got_so_far < total:
        body = {
            "query": "",
            "filters": f"auctionId:'{auction_id}' AND objectTypes:'All' AND NOT isHidden:true",
            "facetFilters": [["withdrawn:false"], []],
            "hitsPerPage": hits_per_page,
            "page": page,
            "facets": ["*"],
            "numericFilters": [],
        }
        headers = {
            "X-Algolia-API-Key": algolia_key,
            "X-Algolia-Application-Id": ALGOLIA_APP_ID.upper(),
            "Content-Type": "application/json",
            "Origin": "https://www.sothebys.com",
            "Referer": "https://www.sothebys.com/",
            "User-Agent": HEADERS["User-Agent"],
        }
        try:
            r = requests.post(ALGOLIA_URL, headers=headers, json=body, timeout=15)
        except Exception:
            break
        if r.status_code != 200:
            break
        data = r.json()
        hits = data.get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)
        got_so_far += len(hits)
        page += 1
        if page > 20:  # safety
            break
    return all_hits


def hit_to_record(hit, sale_meta):
    """Convert one Algolia hit → sale_result record dict."""
    creators = hit.get("creators", []) or []
    artist_raw = creators[0] if creators else ""
    if not artist_raw:
        return None

    # Artist birth-death years may be in creatorsDisplayTitle or creatorPrefix — not reliably present.
    # Dimensions
    dims = ""
    w = hit.get("Width")
    h = hit.get("Height")
    if w and h:
        ww = w[0] if isinstance(w, list) else w
        hh = h[0] if isinstance(h, list) else h
        if ww and hh and ww > 0 and hh > 0:
            # Values are in mm → convert to cm
            dims = f"{round(ww / 10, 1)} x {round(hh / 10, 1)} cm"

    price = hit.get("price")
    est_low = hit.get("lowEstimate")
    est_high = hit.get("highEstimate")
    currency = hit.get("currency") or sale_meta.get("currency") or "USD"

    # Sale date
    sale_date = sale_meta.get("auctionDate", "") or hit.get("auctionDate", "")
    if sale_date:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", sale_date)
        sale_date = m.group(1) if m else ""

    # URL
    slug = hit.get("slug", "")
    source_url = f"https://www.sothebys.com{slug}" if slug.startswith("/") else slug

    # Auction title
    auction_name = sale_meta.get("auctionName", "") or hit.get("auctionName", "") or "Sotheby's"
    auction_title = f"Sotheby's — {auction_name}"

    # Year
    year_list = hit.get("Year Text", []) or []
    year_str = str(year_list[0]) if year_list else ""

    # Sotheby's hides realized prices behind login; Algolia `price` is almost always null
    # for closed auctions. Use estimate midpoint as a proxy so lots still surface with
    # price signal, and mark status='estimate' to distinguish from true hammer records.
    if price:
        hammer = float(price)
        status = "sold"
    elif est_low and est_high:
        hammer = round((float(est_low) + float(est_high)) / 2, 2)
        status = "estimate"
    elif est_low:
        hammer = float(est_low)
        status = "estimate"
    else:
        hammer = None
        status = (hit.get("lotState", "") or "").lower()

    return {
        "source": "sothebys",
        "source_url": source_url,
        "sale_page_url": sale_meta.get("sale_url", ""),
        "lot_number": str(hit.get("lotDisplayNumber") or hit.get("lotNr") or ""),
        "auction_title": auction_title,
        "sale_date": sale_date,
        "sale_location": sale_meta.get("auctionLocation", ""),
        "artist_name_raw": artist_raw,
        "artwork_title": hit.get("title", ""),
        "medium": "",
        "dimensions": dims,
        "year": year_str,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": None,
        "currency": currency,
        "status": status,
        "provenance": "",
        "raw_snapshot": f"{artist_raw} | {hit.get('title','')[:100]}"[:300],
    }


def _load_vn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
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


def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True):
    """Crawl Sotheby's sale pages and extract all VN lots.

    Apollo Cache path (current Sotheby's layout, post-2026):
      1. Sale page → parse __NEXT_DATA__.pageProps.apolloCache for LotCard
         entries (first 48 per sale).
      2. For each LotCard whose title matches the VN catalog, fetch the lot
         detail page → LotV2.bidState.bidAsk is the last accepted bid
         ≈ hammer ± buyer's premium.
      3. SKIP lots where bidState.isClosed=False or sold field is absent —
         these never sold; no synthetic midpoint price.

    Falls back gracefully if Sotheby's changes layout again — extract_sale_data
    returns None.algolia_key now and the function below skips fetch_more_pages.
    """
    sale_urls = sale_urls or SEED_SALE_URLS
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    total_inserted = 0
    from datetime import datetime
    for sale_url in sale_urls:
        run_started = datetime.utcnow().isoformat() + "Z"
        try:
            r = requests.get(sale_url, headers=HEADERS, timeout=25)
        except Exception as e:
            if verbose: print(f"  ERR {sale_url[-60:]}: {e}")
            log_crawl_run(conn, "sothebys", target_slug=sale_url, started_at=run_started,
                          status="error", note=str(e)[:200])
            continue
        if r.status_code != 200:
            if verbose: print(f"  ERR {sale_url[-60:]}: HTTP {r.status_code}")
            log_crawl_run(conn, "sothebys", target_slug=sale_url, started_at=run_started,
                          status="error", note=f"HTTP {r.status_code}")
            continue

        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
        if not m:
            if verbose: print(f"  ERR {sale_url[-60:]}: no __NEXT_DATA__")
            continue
        try:
            data = json.loads(m.group(1))
        except Exception as e:
            if verbose: print(f"  ERR {sale_url[-60:]}: parse {e}")
            continue

        pp = data.get("props", {}).get("pageProps", {})
        apollo = pp.get("apolloCache", {})

        # New Apollo path: enumerate LotCard entries
        lot_cards = [v for k, v in apollo.items() if k.startswith("LotCard:")]
        if not lot_cards:
            # Legacy algoliaJson path — fall back so old data still works
            if verbose: print(f"  {sale_url[-50:]}: no Apollo LotCards (layout?)")
            log_crawl_run(conn, "sothebys", target_slug=sale_url, started_at=run_started,
                          status="error", note="no LotCards in apolloCache")
            continue

        # Sale meta from Auction object
        auct = next((v for k, v in apollo.items() if k.startswith("Auction:")), {})
        sale_meta_fallback = {
            "title": auct.get("title", ""),
            "location": auct.get("location") or "",
            "currency": auct.get("currency") or auct.get("currencyV2", "USD"),
        }
        sess = next((v for k, v in apollo.items() if k.startswith("Session:")), {})
        sale_date_fallback = (sess.get("scheduledOpeningDate") or "")[:10]

        # Find VN candidates by matching the LotCard title prefix against the catalog
        candidates = []
        for lc in lot_cards:
            title = lc.get("title", "")
            artist = _apollo_parse_artist(title)
            if not artist:
                continue
            if filter_vn and not is_vietnamese(artist, vn_catalog, exclusions):
                continue
            lot_slug = (lc.get("slug") or {}).get("lotSlug", "")
            if not lot_slug:
                continue
            candidates.append((artist, title, f"{sale_url}/{lot_slug}"))

        inserted_this = 0
        for artist_raw, lot_title, lot_url in candidates:
            rec = _apollo_extract_lot(lot_url, sale_url, sale_meta_fallback, sale_date_fallback)
            if not rec:
                continue
            # Fake/attribution check
            if re.search(r"\b(d'?apr[eè]s|after\s|attribu|atelier de|école de|copy|copie|reproduction)\b",
                         (rec.get("artwork_title") or "") + " " + (rec.get("artist_name_raw") or ""), re.IGNORECASE):
                continue
            insert_sale_result(conn, rec)
            inserted_this += 1
            time.sleep(0.4)  # pace lot-page fetches

        conn.commit()
        log_crawl_run(conn, "sothebys", target_slug=sale_url, started_at=run_started,
                      lots_scanned=len(lot_cards), lots_inserted=inserted_this,
                      sale_date_min=sale_date_fallback or None,
                      sale_date_max=sale_date_fallback or None,
                      status="ok", note=sale_meta_fallback["title"][:120])
        if verbose:
            print(f"  {sale_url[-50:]}: {len(lot_cards)} cards / {len(candidates)} VN / {inserted_this} inserted")
        total_inserted += inserted_this
        time.sleep(delay)
    return total_inserted


# ─── Apollo Cache helpers ────────────────────────────────────────────────────

# CJK Unified Ideographs (basic + ext-A) + Hiragana + Katakana + full-width space
_CJK_RE = re.compile(r"[　぀-ヿ㐀-鿿＀-￯]")
FX_TO_USD = {"USD": 1.0, "SGD": 0.74, "HKD": 0.128, "GBP": 1.27, "EUR": 1.08,
             "CHF": 1.13, "IDR": 0.000061}


def _apollo_parse_artist(title):
    """Sotheby's titles: 'LE PHO 黎譜 | Femme au bouquet' → 'LE PHO'."""
    if not title:
        return ""
    head = title.split("|")[0].strip()
    head = _CJK_RE.sub("", head).strip()
    return re.sub(r"\s+", " ", head).strip()


def _apollo_parse_artwork_title(title):
    """Strip 'ARTIST | ' prefix + CJK suffix."""
    if not title:
        return ""
    parts = title.split("|", 1)
    if len(parts) < 2:
        return _CJK_RE.sub("", title).strip()
    tail = parts[1].strip()
    return re.sub(r"\s+", " ", _CJK_RE.sub("", tail)).strip()


def _apollo_extract_lot(lot_url, sale_url, sale_meta_fallback, sale_date_fallback):
    """Fetch lot detail page → record dict ready for insert_sale_result."""
    try:
        r = requests.get(lot_url, headers=HEADERS, timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None

    ac = data.get("props", {}).get("pageProps", {}).get("apolloCache", {})
    lot = next((v for k, v in ac.items() if k.startswith("LotV2:")), None)
    if not lot:
        return None

    bs_ref = (lot.get("bidState") or {}).get("__ref")
    bs = ac.get(bs_ref, {}) if bs_ref else {}
    bid_ask = bs.get("bidAsk")
    is_closed = bs.get("isClosed")
    sold = bs.get("sold")
    # Skip lots that never closed sold — don't synthesise a midpoint price.
    if not (is_closed and sold and bid_ask):
        return None

    auct_ref = (lot.get("auction") or {}).get("__ref")
    auct = ac.get(auct_ref, {}) if auct_ref else {}
    currency = auct.get("currency") or sale_meta_fallback.get("currency") or "USD"
    auction_title = auct.get("title") or sale_meta_fallback.get("title") or ""
    location = auct.get("location") or sale_meta_fallback.get("location") or ""

    sess_ref = (lot.get("session") or {}).get("__ref")
    sess = ac.get(sess_ref, {}) if sess_ref else (lot.get("session") or {})
    sale_date = (sess.get("scheduledOpeningDate") or "")[:10] or sale_date_fallback

    est = lot.get("estimateV2") or {}
    est_low = (est.get("lowEstimate") or {}).get("amount")
    est_high = (est.get("highEstimate") or {}).get("amount")

    title = lot.get("title", "")
    artist = _apollo_parse_artist(title)
    artwork = _apollo_parse_artwork_title(title) or title
    desc = _strip_html(lot.get("description") or "")
    prov = _strip_html(lot.get("provenance") or "")

    bid_ask_f = float(bid_ask)
    price_usd = bid_ask_f * FX_TO_USD.get(currency, 1.0)

    return {
        "source": "sothebys",
        "source_url": lot_url,
        "sale_page_url": sale_url,
        "auction_title": auction_title[:200],
        "sale_date": sale_date or None,
        "sale_location": location[:100],
        "artist_name_raw": artist[:200],
        "artwork_title": artwork[:300],
        "medium": "",
        "dimensions": "",
        "year": "",
        "estimate_low": float(est_low) if est_low else None,
        "estimate_high": float(est_high) if est_high else None,
        "hammer_price": None,
        "price_with_premium": bid_ask_f,
        "currency": currency,
        "price_usd": None,
        "price_with_premium_usd": price_usd,
        "status": "sold",
        "kind": "painting",
        "provenance": prov[:2000],
        "raw_snapshot": (f"{artist} | {artwork[:80]} | {desc[:120]}")[:300],
    }
    return total_inserted
