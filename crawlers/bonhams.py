"""Bonhams crawler via Typesense search API (discovered from their front-end).
Endpoint: api01.bonhams.com/search-proxy/multi_search
API key is public (embedded in front-end JS)."""
import re
import time
import requests
from urllib.parse import quote

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run

API_URL = "https://api01.bonhams.com/search-proxy/multi_search?use_cache=true&enable_lazy_filter=true"
API_KEY = "7YZqOyG0twgst4ACc2VuCyZxpGAYzM0weFTLCC20FQY"
HEADERS = {
    "content-type": "application/json",
    "x-typesense-api-key": API_KEY,
    "user-agent": "Mozilla/5.0",
}

# Keywords that indicate a fake, copy, print, or attributed-to-but-not-authentic work
FAKE_MARKERS = [
    "d'après", "d'apres", "après", "apres",      # after = copy
    "attribué à", "attribue a", "attributed to",  # attributed to = not confirmed
    "école de", "ecole de", "school of",           # "school of" = not by the artist
    "style de", "style of", "in the style of",     # stylistic imitation
    "entourage de", "entourage of",                # circle of
    "suiveur de", "follower of",                   # follower
    "copie", "copy", "copy after",                 # copy
    "reproduction", "print", "lithograph", "giclée", "giclee",
    "estampe", "poster", "affiche",                # prints
    "photographie d'après", "photograph after",
    "manière de", "maniere de", "manner of",       # manner of
    "d'apres le pho", "d'apres mai thu",
]


def is_fake_or_copy(title, description=""):
    """Return True if title/description indicates a non-authentic work."""
    combined = (title + " " + description).lower()
    for marker in FAKE_MARKERS:
        if marker in combined:
            return True
    return False


def search_bonhams(query, per_page=100, max_pages=5, status_filter="SOLD", department=None):
    """Search Bonhams Typesense API for lots matching query.
    We intentionally do NOT exclude catalogDesc/footnotes so we get medium + dimensions."""
    all_hits = []
    for page in range(1, max_pages + 1):
        filter_parts = []
        if status_filter:
            filter_parts.append(f"(status:=[`{status_filter}`])")
        if department:
            filter_parts.append(f"(department.code:=[`{department}`])")
        filter_by = " && ".join(filter_parts) if filter_parts else ""

        body = {
            "searches": [{
                "collection": "lots",
                "filter_by": filter_by,
                "query_by": "title",
                "sort_by": "hammerTime.timestamp:desc",
                "page": page,
                "per_page": per_page,
                "q": query,
            }]
        }
        try:
            r = requests.post(API_URL, headers=HEADERS, json=body, timeout=30)
            if r.status_code != 200:
                return all_hits, f"HTTP {r.status_code}"
            data = r.json()
        except Exception as e:
            return all_hits, f"request error: {e}"

        result = data.get("results", [{}])[0]
        hits = result.get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)
        found = result.get("found", 0)
        if len(all_hits) >= found:
            break
    return all_hits, None


def parse_lot(doc):
    """Parse one Bonhams lot document → sale_result record dict."""
    title_full = clean_text(doc.get("title", ""))
    styled = clean_text(doc.get("styledDescription", ""))
    styled_plain = re.sub(r"<[^>]+>", " ", styled)
    styled_plain = re.sub(r"\s+", " ", styled_plain).strip()

    catalog_desc_raw = doc.get("catalogDesc", "") or ""
    catalog_desc_plain = re.sub(r"<[^>]+>", " ", catalog_desc_raw)
    catalog_desc_plain = re.sub(r"\s+", " ", catalog_desc_plain).strip()

    footnotes_raw = doc.get("footnotes", "") or ""
    footnotes_plain = re.sub(r"<[^>]+>", " ", footnotes_raw)
    footnotes_plain = re.sub(r"\s+", " ", footnotes_plain).strip()

    # Extract artist name: usually ALL CAPS before "(YYYY-YYYY)" OR in "firstLine" styled div
    artist = ""
    birth_year = death_year = None
    m_artist = re.search(r"^([A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-']{2,60}?)\s*\(", styled_plain)
    if m_artist:
        artist = clean_text(m_artist.group(1))
    m_years = re.search(r"\((?:[^,)]*,\s*)?(\d{4})\s*[-–]\s*(\d{4})\)", styled_plain)
    if m_years:
        birth_year, death_year = int(m_years.group(1)), int(m_years.group(2))
    else:
        m_birth = re.search(r"\(\s*b\.\s*(\d{4})\s*\)", styled_plain, re.IGNORECASE)
        if m_birth:
            birth_year = int(m_birth.group(1))

    # Artwork title: inside <i>TITLE</i> if present
    m_ititle = re.search(r"<i>([^<]+)</i>", styled)
    artwork_title = clean_text(m_ititle.group(1)) if m_ititle else ""
    if not artwork_title:
        m_ititle2 = re.search(r"<i>([^<]+)</i>", catalog_desc_raw)
        if m_ititle2:
            artwork_title = clean_text(m_ititle2.group(1))
    if not artwork_title:
        # Fallback: take text after year-paren
        m_title_fb = re.search(r"\(\d{4}(?:[-–]\d{4})?\)\s*(.+)", styled_plain)
        if m_title_fb:
            artwork_title = clean_text(m_title_fb.group(1))[:150]
    if not artwork_title and doc.get("slug"):
        # Last-resort fallback: derive title from URL slug (drops artist-prefix + year markers)
        slug = doc.get("slug", "")
        suffix_pat = re.compile(
            r"(?:vietnamese-born-\d{4}|vietnamese-b\d{4}(?:-\d{4})?|vietnamese-b-\d{4}(?:-\d{4})?"
            r"|vietnamese-\d{4}(?:-\d{4})?|frenchamerican-\d{4}(?:-\d{4})?|french-\d{4}(?:-\d{4})?"
            r"|born-\d{4}|b\d{4}(?:-\d{4})?|b-\d{4}(?:-\d{4})?|\d{4}-\d{4})"
        )
        m_suf = suffix_pat.search(slug)
        after = slug[m_suf.end():].lstrip("-") if m_suf else ""
        if after:
            after = re.sub(r"-(?:19|20)\d{2}$", "", after)  # trailing creation year
            words = after.replace("-", " ").split()
            small = {"a","an","and","of","the","in","on","with","to","at","for","du","de","la","le","et"}
            artwork_title = " ".join(
                (w[:1].upper() + w[1:].lower()) if (i == 0 or w.lower() not in small) else w.lower()
                for i, w in enumerate(words)
            )

    # Dimensions: prefer from catalogDesc (richer), fallback to title.
    # Bonhams writes several formats across vintages:
    #   A) "73 by 61 cm"                       — newer, unit at end
    #   B) "39cm x 64cm"                       — older, unit on both numbers
    #   C) "52.9cm (20 13/16in) x 74.4cm (...)" — newer, inches in parens after each cm value
    #   D) "26 x 40 (10 1/4 x 15 3/4 in)"      — outer pair has no unit but inches in parens (assume cm)
    #   E) "36 x 28 1/4 in"                    — inch-only (convert × 2.54 → cm)
    # Try cm patterns first (A/B/C/D), then inch fallback (E).
    dimensions = ""
    for src in (catalog_desc_plain, title_full):
        # A/B/C: cm explicit on at least the second number, optional cm/parens between
        m_cm = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(?:cm)?\s*(?:\([^)]*(?:in|inch|inches)[^)]*\))?\s*"
            r"(?:[x×]|by)\s*"
            r"(\d+(?:[.,]\d+)?)\s*cm",
            src, re.IGNORECASE,
        )
        if m_cm:
            dimensions = f"{m_cm.group(1).replace(',','.')} x {m_cm.group(2).replace(',','.')} cm"
            break
        # D: bare numbers + (… in) — outer is cm because inch in parens is the imperial twin
        m_paren = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(?:[x×]|by)\s*(\d+(?:[.,]\d+)?)\s*"
            r"\(\s*\d+(?:\s+\d+/\d+)?\s*(?:[x×]|by)\s*\d+(?:\s+\d+/\d+)?\s*(?:in|inch|inches)\s*\)",
            src, re.IGNORECASE,
        )
        if m_paren:
            dimensions = f"{m_paren.group(1).replace(',','.')} x {m_paren.group(2).replace(',','.')} cm"
            break
        # E: inch-only — convert to cm
        m_in = re.search(
            r"(\d+)(?:\s+(\d+)/(\d+))?\s*(?:[x×]|by)\s*(\d+)(?:\s+(\d+)/(\d+))?\s*(?:in|inch|inches)\b",
            src, re.IGNORECASE,
        )
        if m_in:
            def _to_cm(whole, num, den):
                v = int(whole) + (int(num) / int(den) if num and den else 0)
                return round(v * 2.54, 1)
            w = _to_cm(m_in.group(1), m_in.group(2), m_in.group(3))
            h = _to_cm(m_in.group(4), m_in.group(5), m_in.group(6))
            dimensions = f"{w} x {h} cm"
            break

    # Medium: usually 1-2 lines before dimensions in catalogDesc
    medium = ""
    if catalog_desc_plain:
        # Look for English medium pattern: "[medium] NN x NN cm"
        m_med = re.search(
            r"\b((?:oil|acrylic|watercolou?r|ink|gouache|pencil|charcoal|pastel|lacquer|silk|gold|eggshell|canvas|wood|paper|panel|mixed media)[^<]{0,120}?)\s*\d+(?:[.,]\d+)?\s*(?:[x×]|by)",
            catalog_desc_plain, re.IGNORECASE
        )
        if m_med:
            medium = clean_text(m_med.group(1))[:150]
        if not medium:
            # Try French: "huile sur toile", "laque", etc.
            m_med2 = re.search(
                r"\b((?:huile|acrylique|aquarelle|encre|laque|gouache|pastel|fusain|soie|bois|papier|toile|carton)[^<]{0,120}?)\s*\d+(?:[.,]\d+)?\s*[x×]",
                catalog_desc_plain, re.IGNORECASE
            )
            if m_med2:
                medium = clean_text(m_med2.group(1))[:150]

    # Price + currency
    price = doc.get("price") or {}
    hammer = price.get("hammerPrice")
    premium = price.get("hammerPremium")
    est_low = price.get("estimateLow")
    est_high = price.get("estimateHigh")
    currency = (doc.get("currency") or {}).get("iso_code", "EUR")

    # Sale info
    hammer_time = (doc.get("hammerTime") or {}).get("datetime", "")
    sale_date = hammer_time[:10] if hammer_time else ""
    country = (doc.get("country") or {}).get("name", "")
    dept = (doc.get("department") or {}).get("name", "")
    auction_id = doc.get("auctionId", "")
    brand = doc.get("brand", "").title()  # e.g. "Cornette" or "Bonhams"
    lot_no = (doc.get("lotNo") or {}).get("full", "") or str(doc.get("lotId", ""))

    # Source URL
    slug = doc.get("slug", "")
    source_url = f"https://www.bonhams.com/auction/{auction_id}/lot/{lot_no}/{slug}/" if auction_id else ""

    # Auction house display
    auction_house = "Bonhams"
    if brand and brand.lower() != "bonhams":
        auction_house = f"Bonhams / {brand}"

    # Extract year of creation from catalogDesc (e.g. "circa 1955", "1966")
    year_str = ""
    m_year = re.search(r"(?:circa\s+)?(\d{4})(?:\s|<|$)", catalog_desc_plain)
    if m_year:
        y = int(m_year.group(1))
        if 1800 <= y <= 2025:
            year_str = str(y)

    # Provenance from footnotes
    provenance = ""
    if footnotes_plain:
        m_prov = re.search(r"Provenance\s*(.+?)(?:Exhibit|Literature|$)", footnotes_plain, re.IGNORECASE)
        if m_prov:
            provenance = clean_text(m_prov.group(1))[:500]

    sale_page_url = f"https://www.bonhams.com/auction/{auction_id}/" if auction_id else ""
    return {
        "source": "bonhams",
        "source_url": source_url,
        "sale_page_url": sale_page_url,
        "lot_number": lot_no,
        "auction_title": f"{auction_house} — {dept}" if dept else auction_house,
        "sale_date": sale_date,
        "sale_location": country,
        "artist_name_raw": artist,
        "artwork_title": artwork_title,
        "medium": medium,
        "dimensions": dimensions,
        "year": year_str,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": premium,
        "currency": currency,
        "status": doc.get("status", "").lower(),
        "provenance": provenance,
        "raw_snapshot": title_full[:500],
        "birth_year": birth_year,
        "death_year": death_year,
    }


# Vietnamese-diaspora artists commonly found at Bonhams
VN_QUERIES = [
    "Le Pho", "Mai Thu", "Mai Trung Thu", "Vu Cao Dam", "Le Thi Luu",
    "Nguyen Gia Tri", "To Ngoc Van", "Nguyen Phan Chanh", "Nguyen Sang",
    "Nguyen Tu Nghiem", "Bui Xuan Phai", "Duong Bich Lien", "Luong Xuan Nhi",
    "Nam Son", "Alix Ayme", "Joseph Inguimberty", "Nguyen Tien Chung",
    "Pham Hau", "Tran Van Can", "Hoang Tich Chu", "Le Ba Dang", "Le Quoc Loc",
    "Nguyen Thi Hop", "Pham Luc", "Dinh Q Le", "Tran Luu Hau",
]


def build_expanded_queries():
    """Return the union of VN_QUERIES + names from VN_ARTIST_CATALOG, de-duplicated.
    Each query is the first 2-3 words of the catalog name (enough to be distinctive)."""
    vn_catalog, _ = _load_vn_catalog()
    seen = {q.lower().replace(" ", ""): q for q in VN_QUERIES}
    for norm in vn_catalog:
        # Take up to 3 first words, title-cased
        q = " ".join(norm.split()[:3]).title()
        key = q.lower().replace(" ", "")
        if key not in seen and len(q) >= 5:
            seen[key] = q
    return list(seen.values())


def _load_vn_catalog():
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
        # Force fresh import in case catalog was updated
        for mod in list(sys.modules.keys()):
            if "vn_artist_catalog" in mod:
                del sys.modules[mod]
        from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
        return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    except ImportError:
        return {}, set()


def is_vietnamese_artist(name, vn_catalog, exclusions):
    """Strict check: artist name must match catalog and not be in exclusions."""
    from artonis_price_mvp import normalize_key
    norm = normalize_key(name)
    if not norm:
        return False
    if norm in exclusions:
        return False
    # Exact match
    if norm in vn_catalog:
        return True
    # Prefix match (catalog key is prefix of name, or vice versa, both at word boundary)
    for key in vn_catalog:
        if norm == key or norm.startswith(key + " ") or key.startswith(norm + " "):
            return True
    return False


def crawl_by_department(conn, department_code, delay=0.5, verbose=True, skip_fake=True, filter_vn=True, max_pages=20):
    """Pull ALL lots in a Bonhams department (e.g. PIC-SEA, ORI-SEA).
    If filter_vn=True, keep only lots matching Vietnamese artists in our catalog."""
    vn_catalog, exclusions = ({}, set())
    if filter_vn:
        vn_catalog, exclusions = _load_vn_catalog()

    if verbose:
        print(f"  [dept={department_code}] fetching all pages...")
    hits, err = search_bonhams("*", per_page=250, max_pages=max_pages, status_filter="SOLD", department=department_code)
    if err:
        return 0, err
    if verbose:
        print(f"  [dept={department_code}] {len(hits)} total hits")

    inserted = 0
    skipped_fake = 0
    skipped_nonvn = 0
    for h in hits:
        rec = parse_lot(h.get("document", {}))
        if not rec.get("hammer_price") or not rec.get("artist_name_raw"):
            continue
        if skip_fake and is_fake_or_copy(rec["artwork_title"], rec.get("raw_snapshot", "")):
            skipped_fake += 1
            continue
        if filter_vn and vn_catalog and not is_vietnamese_artist(rec["artist_name_raw"], vn_catalog, exclusions):
            skipped_nonvn += 1
            continue
        insert_sale_result(conn, rec)
        inserted += 1
    conn.commit()
    if verbose:
        print(f"  [dept={department_code}] inserted={inserted} fake_skipped={skipped_fake} non_vn_skipped={skipped_nonvn}")
    time.sleep(delay)
    return inserted, None


def crawl_all(conn, queries=None, delay=1, verbose=True, skip_fake=True, filter_vn=True):
    """Crawl Bonhams API for all given artist queries, insert into sale_results."""
    queries = queries or VN_QUERIES
    vn_catalog, exclusions = ({}, set())
    if filter_vn:
        vn_catalog, exclusions = _load_vn_catalog()
    total = 0
    skipped = 0
    from datetime import datetime
    for q in queries:
        run_started = datetime.utcnow().isoformat() + "Z"
        hits, err = search_bonhams(q, per_page=100, max_pages=3, status_filter="SOLD")
        if err:
            if verbose:
                print(f"  [{q}] error: {err}")
            log_crawl_run(conn, "bonhams", target_slug=f"query:{q}", started_at=run_started,
                          status="error", note=str(err)[:200])
            time.sleep(delay)
            continue
        n_inserted = 0
        date_min = date_max = None
        for h in hits:
            rec = parse_lot(h.get("document", {}))
            if not rec.get("hammer_price") or not rec.get("artist_name_raw"):
                continue
            if skip_fake and is_fake_or_copy(rec["artwork_title"], rec.get("raw_snapshot", "")):
                skipped += 1
                continue
            q_clean = q.lower().replace(" ", "")
            if q_clean not in rec["artist_name_raw"].lower().replace(" ", ""):
                continue
            if filter_vn and vn_catalog and not is_vietnamese_artist(rec["artist_name_raw"], vn_catalog, exclusions):
                continue
            insert_sale_result(conn, rec)
            n_inserted += 1
            sd = rec.get("sale_date") or ""
            if sd:
                if date_min is None or sd < date_min: date_min = sd
                if date_max is None or sd > date_max: date_max = sd
        conn.commit()
        log_crawl_run(conn, "bonhams", target_slug=f"query:{q}", started_at=run_started,
                      lots_scanned=len(hits), lots_inserted=n_inserted,
                      sale_date_min=date_min, sale_date_max=date_max, status="ok")
        if verbose:
            print(f"  [{q}] {n_inserted} lots inserted (from {len(hits)} hits)")
        total += n_inserted
        time.sleep(delay)
    if verbose:
        print(f"\nSkipped {skipped} fake/copy lots")
    return total
