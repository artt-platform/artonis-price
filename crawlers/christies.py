"""Christie's crawler — parses embedded JSON lot data from lot pages.
Each lot page contains DOM of ~50 related lots with full price data."""
import re
import time
import requests
from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run

BASE = "https://www.christies.com.cn"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# Known Christie's lot URLs for VN artists (seed set — expand as needed)
SEED_LOT_URLS = [
    "https://www.christies.com.cn/en/Lot/lot-6240213",  # Le Pho - Portrait Ngo Manh Duc (sale 15619)
]

# Known Christie's SALE pages (past auctions with price_realised data).
# Pattern: /en/auction/{slug-SALEID}/. Each page contains 50-100 lots with full data.
SEED_SALE_URLS = [
    # ─── 2017 ─── Asian 20th Century Art (HK) ───
    "https://www.christies.com/en/auction/asian-20th-century-art-day-sale-26538/",
    "https://www.christies.com/en/auction/asian-20th-century-art-evening-sale-26539/",
    # ─── 2019 ─── (Spring & Autumn HK) ───
    "https://www.christies.com/en/auction/asian-20th-century-contemporary-art-evening-sale-27459/",  # May 2019
    "https://www.christies.com/en/auction/20th-century-contemporary-art-day-sale-27460/",            # May 2019
    "https://www.christies.com/en/auction/asian-20th-century-art-day-sale-27461/",                   # Nov 2019
    "https://www.christies.com/en/auction/20th-century-contemporary-art-morning-session-27462/",     # Nov 2019
    # ─── 2020 ─── (postponed COVID) ───
    "https://www.christies.com/en/auction/20th-century-art-day-sale-17793/",                         # Jul 2020 HK
    "https://www.christies.com/en/auction/20th-century-21st-century-art-evening-sale-18404/",        # Dec 2020 HK
    "https://www.christies.com/en/auction/20th-century-art-day-sale-18405/",                         # Dec 2020 HK
    # ─── 2021 ─── (Spring & Autumn HK) ───
    "https://www.christies.com/en/auction/20th-and-21st-century-art-evening-sale-27997/",            # May 2021 HK
    "https://www.christies.com/en/auction/20th-and-21st-century-art-morning-session-27995/",         # May 2021 HK Day
    "https://www.christies.com/en/auction/20th21st-century-art-evening-sale-27998/",                 # Dec 2021 HK
    "https://www.christies.com/en/auction/20th-century-art-day-sale-27996/",                         # Dec 2021 HK Day
    # ─── 2022 ─── (Spring & Autumn HK) ───
    "https://www.christies.com/en/auction/20th-21st-century-art-evening-sale-21393-hgk/",            # May 2022
    "https://www.christies.com/en/auction/20th-century-art-day-sale-21394-hgk/",                     # May 2022
    "https://www.christies.com/en/auction/20th-century-art-day-sale-21646-hgk/",                     # Dec 2022
    # ─── 2023 ─── (Spring & Autumn HK) ───
    "https://www.christies.com/en/auction/20th-21st-century-art-evening-sale-22107-hgk/",            # May 2023
    "https://www.christies.com/en/auction/20th-century-art-day-sale-22108-hgk/",                     # May 2023
    "https://www.christies.com/en/auction/20th-century-art-day-sale-29788/",                         # Nov 2023 HK
    # ─── 2024 ─── (Spring & Autumn HK — new HQ) ───
    "https://www.christies.com/en/auction/20th-21st-century-evening-sale-29882/",                    # Sep 2024 HK
    "https://www.christies.com/en/auction/21st-century-day-sale-29884/",                             # Sep 2024 HK
    # ─── 2025 ─── ───
    "https://www.christies.com/en/auction/20th-21st-century-art-evening-sale-24142-hgk/",            # Mar 2025
    "https://www.christies.com/en/auction/a-quest-for-eternity-the-philippe-damas-collection-24143-hgk/",  # Mar 2025 — 51 VN works
    "https://www.christies.com/en/auction/20th-century-day-sale-30624/",                             # Sep 2025 HK
    # ─── 2026 ─── ───
    "https://www.christies.com/en/auction/20th-century-day-sale-30850/",
    "https://www.christies.com/en/auction/20th-century-contemporary-art-evening-sale-30849/",
]


_SALE_INFO_CACHE = {}  # sale_number → (title, date, url)


def fetch_sale_info_from_sale_url(sale_url):
    """Given a known sale_url, fetch the page title and start date."""
    import html as _html
    try:
        r = requests.get(sale_url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return "", ""
        title = date = ""
        m_t = re.search(r"<title>([^<]+)</title>", r.text)
        if m_t:
            raw_title = _html.unescape(m_t.group(1))
            raw_title = raw_title.replace(" | Christie's", "").strip()
            if raw_title and len(raw_title) < 150:
                title = raw_title
        m_d = re.search(r'"start_date":"([\d\-T:Z]+)"', r.text)
        if m_d:
            mm = re.match(r"(\d{4}-\d{2}-\d{2})", m_d.group(1))
            if mm:
                date = mm.group(1)
        return title, date
    except Exception:
        return "", ""


def extract_lots_from_page(url, verbose=False):
    """Fetch a lot page and extract ALL embedded lot objects (the page carries related lots with full price data)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return []
    except Exception:
        return []

    # Christie's serves UTF-8 but doesn't always set the charset header; requests
    # then falls back to Latin-1 which mojibakes French diacritics
    # (é → Ã©, è → Ã¨, etc.). Force UTF-8.
    r.encoding = "utf-8"
    text = r.text
    records = []

    # Extract page-level sale info (if this is a sale page rather than individual lot page)
    page_sale_date = ""
    m_ps = re.search(r'"auction_status":\s*\{\s*"start_date":"([\d\-T:Z]+)"', text)
    if m_ps:
        mm = re.match(r"(\d{4}-\d{2}-\d{2})", m_ps.group(1))
        if mm:
            page_sale_date = mm.group(1)
    page_sale_id = ""
    m_sid = re.search(r'"sale_number":"(\d+)"', text)
    if m_sid:
        page_sale_id = m_sid.group(1)
    # Find the actual public auction URL embedded in lot page
    # Pattern: https://www.christies.com(.cn)?/en/auction/{slug-{PUBLIC_ID}} — NOT /auction/browse-lots
    page_sale_url = ""
    page_sale_title = ""
    for m in re.finditer(r'(https?://[^"\s]*christies[^"\s]*auction/[a-z0-9\-]+\d+)(?:/?["\s,])', text):
        u = m.group(1)
        if "browse-lots" in u or "browse" in u:
            continue
        # Skip if URL doesn't end with a number (slug must have {slug-ID} suffix)
        if not re.search(r"-\d+$", u):
            continue
        page_sale_url = u
        break
    # Derive page title from URL slug as fallback, or fetch
    if page_sale_url:
        slug_match = re.search(r"/auction/([a-z0-9\-]+)-\d+/?$", page_sale_url)
        if slug_match:
            page_sale_title = slug_match.group(1).replace("-", " ").title()
    # If this is actually a sale page (many lots + direct page_sale_id), override with cleaner title
    m_st = re.search(r'"title":"(Asian[^"]+|20th[^"]+|Modern[^"]+|Contemporary[^"]+|Impressionist[^"]+)"', text)
    if m_st:
        page_sale_title = m_st.group(1)

    # Find all lot data blocks. A lot has `"price_realised":"XXX"` with surrounding context.
    # Strategy: find each `"price_realised":` occurrence, backtrack to find object boundaries.
    # Keys we care about: image_alt_text (has artist + years), estimate_low/high, price_realised/_txt
    for m in re.finditer(r'"price_realised":"?([\d.]+)"?', text):
        # Walk back to find the enclosing {
        # Look for the start of the JSON object by scanning back for matching braces
        end = m.end()
        # Find start by searching backward for a `{` that balances
        depth = 0
        start = end
        i = end
        while i > max(0, end - 5000):
            c = text[i]
            if c == '}':
                depth += 1
            elif c == '{':
                if depth == 0:
                    start = i
                    break
                depth -= 1
            i -= 1
        if start == end:
            continue
        # Find the end of object from end forward
        j = end
        depth = 0
        obj_end = end
        while j < min(len(text), end + 5000):
            c = text[j]
            if c == '{':
                depth += 1
            elif c == '}':
                if depth == 0:
                    obj_end = j + 1
                    break
                depth -= 1
            j += 1
        if obj_end == end:
            continue

        blob = text[start:obj_end]

        # Extract fields via regex (safer than JSON.parse due to possible escape issues)
        def extract(key):
            mm = re.search(rf'"{key}":"((?:[^"\\]|\\.)*)"', blob)
            return mm.group(1).encode().decode('unicode_escape') if mm else ""

        def extract_num(key):
            mm = re.search(rf'"{key}":"?([\d.]+)"?', blob)
            return float(mm.group(1)) if mm else None

        # Artist name from title_primary_txt (e.g. "LE THI LUU (1911-1988)")
        artist_info = extract("title_primary_txt") or extract("image_alt_text")
        m_art = re.match(r"^([A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-']+?)\s*\(\s*(\d{4})\s*[-–]?\s*(\d{4})?", artist_info)
        artist_name = m_art.group(1).strip().title() if m_art else ""
        birth_year = int(m_art.group(2)) if m_art else None
        death_year = int(m_art.group(3)) if (m_art and m_art.group(3)) else None

        lot_id = extract("object_id") or extract("lot_id_txt") or extract("id")
        title = extract("title_secondary_txt") or extract("title") or extract("lot_title")
        # Sale info
        sale_id = extract("sale_number") or extract("sale_id") or extract("event_code")
        sale_date_raw = extract("start_date") or extract("sale_date") or extract("event_date") or extract("end_date")
        # Try to find sale_title in the blob (parent section)
        sale_title = extract("sale_title") or extract("event_title")

        currency = extract("currency_txt") or "HKD"
        price = extract_num("price_realised")
        est_low = extract_num("estimate_low")
        est_high = extract_num("estimate_high")
        url_lot = extract("url") or extract("lot_url")

        if not (artist_name and price):
            continue

        source_url = (BASE + url_lot) if url_lot.startswith("/") else (url_lot or f"{BASE}/en/Lot/lot-{lot_id}")
        # Normalize to canonical form so the same lot crawled from multiple URL patterns
        # (/lot/slug-{id}?params, /en/lot/lot-{id}?breadcrumb, /en/lot/lot-{id}) dedupes on insert.
        m_lid = re.search(r"(?:lot-|-|/)(\d{6,8})(?=[/?]|$)", source_url)
        if m_lid:
            source_url = f"https://www.christies.com/en/lot/lot-{m_lid.group(1)}"

        # Parse date format: "2019-11-24T10:00Z" → "2019-11-24"
        sale_date_clean = ""
        if sale_date_raw:
            mm = re.match(r"(\d{4}-\d{2}-\d{2})", sale_date_raw)
            if mm:
                sale_date_clean = mm.group(1)
        # Fallback to page-level date
        if not sale_date_clean and page_sale_date:
            sale_date_clean = page_sale_date

        # Fallback to page-level sale info
        if not sale_id and page_sale_id:
            sale_id = page_sale_id
        if not sale_title and page_sale_title:
            sale_title = page_sale_title

        # Use page-level extracted sale URL + title
        sale_page_url = page_sale_url
        if not sale_title and page_sale_title:
            sale_title = page_sale_title
        # If we have a sale URL but no title, fetch the sale page for clean title
        if sale_page_url and (not sale_title or sale_title == page_sale_title.title()):
            cached = _SALE_INFO_CACHE.get(sale_page_url)
            if cached:
                t_fetch, d_fetch = cached
            else:
                t_fetch, d_fetch = fetch_sale_info_from_sale_url(sale_page_url)
                _SALE_INFO_CACHE[sale_page_url] = (t_fetch, d_fetch)
            if t_fetch:
                sale_title = t_fetch
            if d_fetch and not sale_date_clean:
                sale_date_clean = d_fetch

        # Better auction title: use sale_title if available, else Sale ID, else date
        if sale_title:
            auction_title = f"Christie's — {sale_title}"
        elif sale_id:
            auction_title = f"Christie's — Sale {sale_id}"
        elif sale_date_clean:
            auction_title = f"Christie's — Sale {sale_date_clean}"
        else:
            auction_title = "Christie's"

        # Extract dimensions from JSON blob: "height_cm":"130","width_cm":"90"
        dims = ""
        m_d = re.search(r'"height_cm":"(\d+(?:\.\d+)?)"\s*,\s*"width_cm":"(\d+(?:\.\d+)?)"', blob)
        if m_d:
            dims = f"{float(m_d.group(2))} x {float(m_d.group(1))} cm"

        records.append({
            "source": "christies",
            "source_url": source_url,
            "sale_page_url": sale_page_url,
            "lot_number": lot_id,
            "auction_title": auction_title,
            "sale_date": sale_date_clean,
            "sale_location": "Hong Kong",
            "artist_name_raw": artist_name,
            "artwork_title": title,
            "medium": "",
            "dimensions": dims,
            "year": "",
            "estimate_low": est_low,
            "estimate_high": est_high,
            "hammer_price": price,
            "price_with_premium": None,
            "currency": currency,
            "status": "sold",
            "provenance": "",
            "raw_snapshot": artist_info[:200],
            "birth_year": birth_year,
            "death_year": death_year,
        })
    return records


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


def crawl(conn, seed_urls=None, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Christie's. Combines lot-seed URLs (~50 related lots each) + sale-page URLs (~50-100 lots each)."""
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())
    seed_urls = seed_urls or SEED_LOT_URLS
    sale_urls = sale_urls or SEED_SALE_URLS

    all_records = []
    seen_urls = set()
    # Sale pages first (higher yield per fetch)
    queue = list(sale_urls) + list(seed_urls)

    total = 0
    skipped_nonvn = 0
    from datetime import datetime
    while queue and len(seen_urls) < max_pages:
        url = queue.pop(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        run_started = datetime.utcnow().isoformat() + "Z"
        records = extract_lots_from_page(url)
        if verbose:
            print(f"  [{len(seen_urls)}] {url[-50:]}: {len(records)} lots")
        inserted_this = 0
        date_min = date_max = None
        for rec in records:
            if filter_vn and not is_vietnamese(rec["artist_name_raw"], vn_catalog, exclusions):
                skipped_nonvn += 1
                continue
            if re.search(r"\b(d'?apr[eè]s|copy|copie|reproduction)\b", rec["artwork_title"] + " " + rec["artist_name_raw"], re.IGNORECASE):
                continue
            insert_sale_result(conn, rec)
            inserted_this += 1
            sd = rec.get("sale_date") or ""
            if sd:
                if date_min is None or sd < date_min: date_min = sd
                if date_max is None or sd > date_max: date_max = sd
            if rec["source_url"] not in seen_urls and rec["source_url"] not in queue:
                queue.append(rec["source_url"])
        conn.commit()
        log_crawl_run(conn, "christies", target_slug=url, started_at=run_started,
                      lots_scanned=len(records), lots_inserted=inserted_this,
                      sale_date_min=date_min, sale_date_max=date_max, status="ok")
        total += inserted_this
        time.sleep(delay)
    if verbose:
        print(f"\n  Total inserted: {total}, skipped non-VN: {skipped_nonvn}")
    return total
