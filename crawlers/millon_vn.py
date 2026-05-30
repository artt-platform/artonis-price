"""Millon Vietnam (millon-vietnam.com) crawler — uses their public API.
Endpoint: api.millon-vietnam.com/api/products
Returns all lots across all catalogs (past + upcoming). Filter by has-hammer-price for past sales."""
import re
import time
import requests
from bs4 import BeautifulSoup

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run

API_BASE = "https://api.millon-vietnam.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_all_catalogs():
    """Get all catalogs. Returns list of {id, code, name, status, startDate, endDate}."""
    r = requests.get(f"{API_BASE}/catalog?limit=100", headers=HEADERS, timeout=20)
    return r.json().get("data", {}).get("rows", [])


def fetch_all_products(limit=2000):
    """Get all lot products across all catalogs."""
    r = requests.get(f"{API_BASE}/products?limit={limit}", headers=HEADERS, timeout=30)
    return r.json().get("data", {}).get("rows", [])


def _strip_html(html):
    if not html:
        return ""
    plain = re.sub(r"<br\s*/?>", "\n", html)
    plain = re.sub(r"</?(p|div|h\d|span)[^>]*>", "\n", plain)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"&nbsp;", " ", plain)
    plain = re.sub(r"&amp;", "&", plain)
    return re.sub(r"\n{2,}", "\n", plain).strip()


_MEDIUM_KWS = (
    "huile", "acrylique", "aquarelle", "encre", "laque", "gouache", "pastel",
    "fusain", "soie", "mixed", "acrylic", "oil on", "ink on", "watercolor",
    "sơn dầu", "sơn mài", "mực", "bột màu", "màu nước", "lụa", "giấy", "tranh",
    "toile", "panneau", "papier",
)


def _looks_like_medium(line):
    low = line.lower()
    return any(kw in low for kw in _MEDIUM_KWS)


def _is_artist_header(line):
    cleaned = re.sub(r"^[Ⓟ⒫⒤ⒽⒾⓅⓁⓇ]+\s*", "", line).strip()
    return bool(re.match(r"^[A-ZÀ-Ỹ][A-ZÀ-Ỹa-zà-ÿ\-\.\s']{2,60}?\s*\([^)]*\d{4}", cleaned))


def parse_lot_description(html, name):
    """Parse description HTML + name → dict(artist, title, medium, dimensions, year).

    Millon-Vietnam description layout (one artist per lot):
        Line 1: "TẠ TỴ (1922–2004)"          ← artist header
        Line 2: "Ánh đèn building, 1960"      ← title (optionally with artwork year)
        Line 3: "Sơn dầu trên vải"            ← medium
        Line 4: "Ký tên..."                    ← signature (optional)
        Line 5: "75 × 60 cm"                   ← dimensions
    Prose description may follow.
    """
    plain = _strip_html(html)
    lines = [l.strip() for l in plain.split("\n") if l.strip()]

    # Artist + years
    artist = ""
    birth_year = death_year = None
    artist_line_idx = -1
    for i, line in enumerate(lines[:3]):
        cleaned = re.sub(r"^[Ⓟ⒫⒤ⒽⒾⓅⓁⓇ]+\s*", "", line).strip()
        m = re.match(r"([A-ZÀ-ỸÀ-ÿ][A-ZÀ-ỸÀ-ÿa-zà-ÿ\-\.\s']{2,60}?)\s*\(", cleaned)
        if m:
            artist = clean_text(m.group(1))
            artist_line_idx = i
            m_yr = re.search(r"\((\d{4})\s*[-–]\s*(\d{4})\)", cleaned)
            if m_yr:
                birth_year, death_year = int(m_yr.group(1)), int(m_yr.group(2))
            else:
                m_ne = re.search(r"n[ée]\s+en\s+(\d{4})", cleaned, re.IGNORECASE)
                if m_ne:
                    birth_year = int(m_ne.group(1))
                else:
                    m_b = re.search(r"\b(b\.|sinh n[aă]m)\s*(\d{4})", cleaned, re.IGNORECASE)
                    if m_b:
                        birth_year = int(m_b.group(2))
            break
    if not artist and name:
        m2 = re.match(r"[Ⓟ⒫⒤ⒽⒾⓅⓁⓇ]*\s*([A-ZÀ-Ỹ][A-ZÀ-Ỹa-zà-ÿ\s\-\.\']{2,60}?)\s*\(", name)
        if m2:
            artist = clean_text(m2.group(1))

    # Title extraction — in order of preference:
    # (a) <h3> element in description (Millon sometimes renders titles here)
    # (b) 1st line after artist header that isn't a medium/dimension/signature
    # (c) italic <i> text
    # (d) quoted text in any line
    # (e) name field, stripped of artist prefix; or used as-is if it's a standalone title
    # (f) fallback: first non-artist/non-dimension line even if medium-like
    title = ""
    title_year = ""

    # (a) <h3>TITLE</h3>
    m_h3 = re.search(r"<h3[^>]*>(.+?)</h3>", html or "", re.DOTALL)
    if m_h3:
        h3_text = _strip_html(m_h3.group(1)).replace("\n", " ").strip()
        if h3_text and len(h3_text) < 200 and not _is_artist_header(h3_text):
            title = h3_text

    # (b) line-based scan
    if not title and artist_line_idx >= 0:
        for cand in lines[artist_line_idx + 1: artist_line_idx + 4]:
            if not cand or len(cand) < 3 or len(cand) > 200:
                continue
            if _looks_like_medium(cand):
                continue
            if re.search(r"\d+\s*[x×]\s*\d+\s*cm", cand, re.IGNORECASE):
                continue
            if re.search(r"^(ký tên|signé|signed|sign[eé]e|estampill)", cand, re.IGNORECASE):
                continue
            if cand.count(".") > 1 or len(cand) > 120:
                continue
            title = cand
            break

    if not title:
        m_i = re.search(r"<i[^>]*>([^<]+)</i>", html or "")
        if m_i:
            title = clean_text(m_i.group(1))
    if not title:
        for line in lines:
            m_q = re.search(r"[«\"“]([^»\"”]{3,100})[»\"”]", line)
            if m_q:
                title = clean_text(m_q.group(1))
                break

    # (e) name field
    if not title and name:
        nm = re.sub(r"^[Ⓟ⒫⒤ⒽⒾⓅⓁⓇ]+\s*", "", name).strip()
        m_n = re.match(r"[A-ZÀ-Ỹ][A-ZÀ-Ỹa-zà-ÿ\s\-\.\']{2,60}?\s*\([^)]*\)\s*(.+)$", nm)
        if m_n:
            title = clean_text(m_n.group(1))
        elif not _is_artist_header(nm) and 2 < len(nm) < 120:
            # name itself looks like a standalone title (e.g. "Hộp gỗ sơn mài")
            title = clean_text(nm)

    # Split trailing ", YYYY" (or " – YYYY") from title → artwork year
    if title:
        m_ty = re.search(r"^(.+?)[,\-–]\s*(\d{4})\s*$", title)
        if m_ty:
            title = m_ty.group(1).strip().rstrip(",")
            title_year = m_ty.group(2)
        title = title.strip(' "“”«»')

    # Dimensions
    dimensions = ""
    m_dim = re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm", plain, re.IGNORECASE)
    if m_dim:
        dimensions = f"{m_dim.group(1).replace(',','.')} x {m_dim.group(2).replace(',','.')} cm"

    # Medium
    medium = ""
    for line in lines[artist_line_idx + 1: artist_line_idx + 6] if artist_line_idx >= 0 else lines[:6]:
        if _looks_like_medium(line) and len(line) < 200:
            medium = clean_text(line)[:150]
            break

    # Artwork year (NOT the artist birth year).
    # Order: title suffix → "sáng tác năm YYYY" / "cr[eé]é[e]? en YYYY" → first year NOT matching birth/death.
    year_str = title_year
    if not year_str:
        m_st = re.search(r"(?:sáng t[aá]c|cr[eé]é[e]?|peint|ex[eé]cut[eé]?)[^\d]{0,20}(\d{4})",
                         plain, re.IGNORECASE)
        if m_st:
            year_str = m_st.group(1)
    if not year_str:
        for m_y in re.finditer(r"\b(19\d{2}|20[0-2]\d)\b", plain):
            y = int(m_y.group(1))
            if y == birth_year or y == death_year:
                continue
            year_str = m_y.group(1)
            break

    return {
        "artist": artist,
        "title": title,
        "medium": medium,
        "dimensions": dimensions,
        "year": year_str,
        "birth_year": birth_year,
        "death_year": death_year,
    }


def _is_vietnamese(artist_raw, vn_catalog, exclusions):
    """Check if artist is in VN catalog (strict). Allow partial match on prefix."""
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


def _load_vn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _pick_catalog(product_created_at, catalogs, max_days=60):
    """Match a product's createdAt to the closest catalog by startDate (within max_days)."""
    from datetime import datetime
    if not product_created_at:
        return None
    try:
        ca = datetime.fromisoformat(product_created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    best = None; best_dist = None
    for c in catalogs:
        sd = c.get("startDate") or ""
        if not sd:
            continue
        try:
            cd = datetime.fromisoformat(sd.replace("Z", "+00:00"))
        except ValueError:
            continue
        dist = abs((ca - cd).total_seconds())
        if best_dist is None or dist < best_dist:
            best = c; best_dist = dist
    if best_dist is not None and best_dist < max_days * 86400:
        return best
    return None


def crawl(conn, limit=2000, verbose=True, filter_vn=True, include_estimates=True):
    """Fetch all lots from Millon Vietnam API, insert VN lots.

    Millon Vietnam publishes estimates but rarely publishes hammer prices in their public API.
    Status 8 = past auctions. We use midpoint of estimate as hammer proxy (marked `estimate_only`)
    for past auctions without explicit hammer.

    Products are mapped to their originating catalog by date proximity of createdAt ↔ startDate,
    so sale_date reflects the actual auction day (not the product update day).
    """
    vn_catalog, exclusions = _load_vn()
    catalogs = fetch_all_catalogs()
    if verbose:
        print(f"  Fetched {len(catalogs)} catalogs")

    products = fetch_all_products(limit=limit)
    if verbose:
        print(f"  Fetched {len(products)} lot products")

    inserted = 0
    skipped_fake = 0
    skipped_nonvn = 0
    skipped_no_price = 0
    from datetime import datetime
    run_started = datetime.utcnow().isoformat() + "Z"
    date_min = date_max = None
    for p in products:
        status = p.get("status")
        hammer = p.get("price")
        est_low = p.get("lowEstimatePrice")
        est_high = p.get("hightEstimatePrice")

        # Use hammer if present, else estimate midpoint for past auctions
        price_source = "hammer"
        effective_price = hammer
        if not effective_price and status == 8 and include_estimates and est_low:
            # Status 8 = past, use estimate midpoint
            effective_price = (est_low + (est_high or est_low)) / 2
            price_source = "estimate_only"
        if not effective_price:
            skipped_no_price += 1
            continue

        info = parse_lot_description(p.get("description", ""), p.get("name", ""))
        if not info["artist"]:
            continue
        if filter_vn and not _is_vietnamese(info["artist"], vn_catalog, exclusions):
            skipped_nonvn += 1
            continue
        # Fake/copy check
        if re.search(r"\b(d'?apr[eè]s|copy|copie|r[eé]production|estampe|print|lithograph)\b",
                     info["title"] + " " + info["artist"], re.IGNORECASE):
            skipped_fake += 1
            continue

        cat = _pick_catalog(p.get("createdAt"), catalogs)
        auction_title = f"Millon Vietnam — {cat['name']}" if cat and cat.get("name") else "Millon Vietnam"
        sale_date = (cat["startDate"] or "")[:10] if cat and cat.get("startDate") else (p.get("createdAt") or "")[:10]

        rec = {
            "source": "millon",  # millon-vietnam.com is Millon's VN-team front-end, not a separate house
            "source_url": f"https://millon-vietnam.com/product/{p['code']}",
            "sale_page_url": "https://millon-vietnam.com/",
            "lot_number": str(p.get("lot", "")),
            "auction_title": auction_title,
            "sale_date": sale_date,
            "sale_location": "Hà Nội / Paris",
            "artist_name_raw": info["artist"].title() if info["artist"].isupper() else info["artist"],
            "artwork_title": info["title"],
            "medium": info["medium"],
            "dimensions": info["dimensions"],
            "year": info["year"],
            "estimate_low": est_low,
            "estimate_high": est_high,
            "hammer_price": effective_price,
            "price_with_premium": None,
            "currency": "EUR",
            "status": price_source,  # "hammer" or "estimate_only"
            "provenance": "",
            "raw_snapshot": (p.get("name", "") + " | " + _strip_html(p.get("description", ""))[:300])[:500],
        }
        insert_sale_result(conn, rec)
        inserted += 1
        if sale_date:
            if date_min is None or sale_date < date_min: date_min = sale_date
            if date_max is None or sale_date > date_max: date_max = sale_date
    conn.commit()
    log_crawl_run(conn, "millon", target_slug="millon-vietnam.com:api/products",
                  started_at=run_started, lots_scanned=len(products), lots_inserted=inserted,
                  sale_date_min=date_min, sale_date_max=date_max, status="ok",
                  note=f"Millon-VN team site (skipped_no_price={skipped_no_price}, nonvn={skipped_nonvn})")
    if verbose:
        print(f"  inserted={inserted}, skipped_no_price={skipped_no_price}, skipped_nonvn={skipped_nonvn}, skipped_fake={skipped_fake}")
    return inserted
