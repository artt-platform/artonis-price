"""Millon auction house crawler — uses /createurs/{artist-slug} pages which list past sales.
Tested: cloudscraper bypasses their bot protection. Data is SSR and structured."""
import time
import re
import cloudscraper
from bs4 import BeautifulSoup

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, clean_artist_name, log_crawl_run


def _fetch_lot_details(scraper, lot_url):
    """Fetch single lot detail page. Returns dict(title, medium, dimensions, year, birth, death).

    Millon FR lot pages render the title in <h3> / div.sub-title and the artist header
    in <h1> / div.title-large, e.g.:
        <h1>JOSEPH INGUIMBERTY (1896-1971)</h1>
        <h3>Réunion au village du lotus d'or, 1936</h3>
    Meta description holds medium + dimensions + provenance.
    """
    try:
        r = scraper.get(lot_url, timeout=20)
        if r.status_code != 200:
            return {}
        soup = BeautifulSoup(r.text, "html.parser")

        # Title from <h3> or sub-title div
        title = ""
        sub = soup.find(class_="sub-title") or soup.find("h3")
        if sub:
            title = clean_text(sub.get_text(" ", strip=True))

        # Split trailing ", YYYY" from title → artwork year
        title_year = ""
        if title:
            m_ty = re.search(r"^(.+?)[,\-–]\s*(\d{4})\s*$", title)
            if m_ty:
                title = m_ty.group(1).strip().rstrip(",")
                title_year = m_ty.group(2)
            title = title.strip(' "“”«»')

        # Artist + birth/death from <h1> or title-large
        birth_year = death_year = None
        artist_header = soup.find(class_="title-large") or soup.find("h1")
        if artist_header:
            htext = clean_text(artist_header.get_text(" ", strip=True))
            m_yr = re.search(r"\((\d{4})\s*[-–]\s*(\d{4})\)", htext)
            if m_yr:
                birth_year, death_year = int(m_yr.group(1)), int(m_yr.group(2))
            else:
                m_b = re.search(r"\((\d{4})\s*[-–]?\s*\)", htext)
                if m_b:
                    birth_year = int(m_b.group(1))

        # Medium + dimensions from meta description
        meta = soup.find("meta", {"name": "description"})
        desc = meta.get("content", "") if meta else ""
        m_dim = re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm", desc, re.IGNORECASE)
        dims = f"{m_dim.group(1).replace(',','.')} x {m_dim.group(2).replace(',','.')} cm" if m_dim else ""
        # Medium: first phrase after "Lot N - " up to "Signé" / dimensions
        medium = ""
        m_med = re.search(r"Lot\s+\d+\s*[-–]\s*([^0-9]+?)(?:\s+Sign[eé]|\s+\d+[.,]?\d*\s*[x×]|\s+\()",
                          desc, re.IGNORECASE)
        if m_med:
            medium = clean_text(m_med.group(1))[:150]

        return {
            "title_detail": title,
            "year": title_year,
            "medium": medium,
            "dimensions": dims,
            "birth_year": birth_year,
            "death_year": death_year,
            "meta_desc": desc,
        }
    except Exception:
        return {}

BASE = "https://www.millon.com"

_FR_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


def _parse_fr_date(text):
    m = re.search(r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
                  text or "", re.IGNORECASE)
    if m:
        return f"{int(m.group(3))}-{_FR_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


# Cache catalog meta per slug across a crawl session to avoid re-fetching
_CATALOG_META_CACHE = {}


_FR_MONTHS_ABBR = {
    "jan": 1, "janv": 1, "janvier": 1,
    "fév": 2, "fev": 2, "févr": 2, "fevr": 2, "février": 2, "fevrier": 2,
    "mar": 3, "mars": 3, "avr": 4, "avri": 4, "avril": 4, "mai": 5,
    "jui": 6, "juin": 6, "juil": 7, "juill": 7, "juillet": 7,
    "aoû": 8, "aou": 8, "août": 8, "aout": 8,
    "sep": 9, "sept": 9, "septembre": 9,
    "oct": 10, "octo": 10, "octobre": 10,
    "nov": 11, "novembre": 11, "déc": 12, "dec": 12, "décembre": 12, "decembre": 12,
}


def _parse_day_of_sale(dos_text, is_past, og_img_src):
    """Parse Millon's <div class='day-of-sale'> text (e.g. 'sam 12 oct 2024', 'sam 11 Avr').
    - If year present → use it.
    - If no year + upcoming sale → use current year.
    - If no year + past sale → try to infer year from og:image path (/files/YYYY-MM/…).
    """
    if not dos_text:
        return ""
    m = re.search(r"(\d{1,2})\s+([a-zéèûôîâç]+\.?)\s+(20\d{2})", dos_text, re.IGNORECASE)
    year = None
    if m:
        d = int(m.group(1))
        mo_name = m.group(2).rstrip(".").lower()
        year = int(m.group(3))
    else:
        m2 = re.search(r"(\d{1,2})\s+([a-zéèûôîâç]+\.?)(?:\s|$)", dos_text, re.IGNORECASE)
        if not m2:
            return ""
        d = int(m2.group(1))
        mo_name = m2.group(2).rstrip(".").lower()
        if not is_past:
            from datetime import datetime
            year = datetime.now().year
        else:
            m_og = re.search(r"/files/(20\d{2})-\d{2}/", og_img_src or "")
            if m_og:
                year = int(m_og.group(1))
            else:
                return ""
    mo = _FR_MONTHS_ABBR.get(mo_name) or _FR_MONTHS_ABBR.get(mo_name[:4]) or _FR_MONTHS_ABBR.get(mo_name[:3])
    if not mo or not year:
        return ""
    try:
        from datetime import date as _date
        _date(year, mo, d)
    except ValueError:
        return ""
    return f"{year}-{mo:02d}-{d:02d}"


def _fetch_catalog_meta(scraper, slug):
    """Returns (title, sale_date) from the catalog main page.
    Title comes from <h1> / .title-large. Date comes from <div class='day-of-sale'>."""
    if slug in _CATALOG_META_CACHE:
        return _CATALOG_META_CACHE[slug]
    try:
        r = scraper.get(f"https://www.millon.com/catalogue/{slug}", timeout=20)
        if r.status_code != 200:
            _CATALOG_META_CACHE[slug] = ("", "")
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        title_el = soup.find(class_="title-large") or soup.find("h1")
        title = clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
        dos = soup.find(class_="day-of-sale")
        dos_text = dos.get_text(" ", strip=True) if dos else ""
        is_past = "Adjugé" in r.text
        og = soup.find("meta", {"property": "og:image"})
        og_src = og.get("content", "") if og else ""
        sale_date = _parse_day_of_sale(dos_text, is_past, og_src)
        _CATALOG_META_CACHE[slug] = (title, sale_date)
        return title, sale_date
    except Exception:
        return "", ""

# Known Vietnamese / Indochine-school artists on Millon
VN_ARTIST_SLUGS = [
    "le-pho",
    "vu-cao-dam",
    "mai-trung-thu",
    "le-thi-luu",
    "nguyen-phan-chanh",
    "nguyen-gia-tri",
    "to-ngoc-van",
    "alix-ayme",
    "joseph-inguimberty",
    "nam-son",
    "pham-hau",
    "le-quoc-loc",
    "nguyen-tu-nghiem",
    "bui-xuan-phai",
    "duong-bich-lien",
    "nguyen-sang",
]


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True}
    )


def fetch_artist_page(slug, scraper=None):
    """Fetch one artist's price page. Returns list of sale records."""
    scraper = scraper or _make_scraper()
    url = f"{BASE}/createurs/{slug}"
    try:
        r = scraper.get(url, timeout=25)
    except Exception as e:
        return None, f"request error: {e}"
    if r.status_code == 404:
        return None, "404 not found"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"

    soup = BeautifulSoup(r.text, "html.parser")
    records = []

    # Get the whole page text in a structured way
    page_text = soup.get_text(" ", strip=True)

    # Build a map: lot_number → source_url (from <a href="/catalogue/...lotN...">)
    lot_urls = {}
    for link_el in soup.select("a[href*='/catalogue/']"):
        href = link_el.get("href", "")
        m_lot = re.search(r"lot(\d+)-", href)
        if m_lot:
            lot_urls[m_lot.group(1)] = (BASE + href) if href.startswith("/") else href

    # Find all sale rows in page text. "Vendu le" date is optional (some entries have no date).
    # Also tolerate provenance symbols (Ⓗ, Ⓟ, Ⓘ) between the date and artist name.
    row_re = re.compile(
        r"Adjug[eé]\s+[àa]\s*([\d\s.,]+\s*(?:€|\$|£|HKD|USD|EUR))"         # (1) price
        r"\s*(?:Vendu\s+le\s+([\d/\-.]+))?"                                 # (2) optional date
        r"\s*[Ⓗ⒫⒤Ⓟ]?\s*"                                                   # optional symbol
        r"([A-ZÀ-Ÿ][A-ZÀ-Ÿ\s\-]{2,50}?)\s*\(\s*(\d{4})"                    # (3,4) artist + birth year
        r"(?:\s*-\s*(\d{4}))?\s*\)"                                         # (5) death year
        r"\s*(.*?)\s*Lot\s+(\d+)",                                          # (6,7) title + lot
        re.IGNORECASE,
    )

    for m in row_re.finditer(page_text):
        amount, currency = parse_amount(m.group(1), default_currency="EUR")
        sale_date = parse_date(m.group(2)) if m.group(2) else ""
        artist = m.group(3).strip().title()
        title = clean_text(m.group(6))
        # Strip enclosing quotes on title
        title = title.strip('"“”')
        lot_number = m.group(7)
        source_url = lot_urls.get(lot_number, "")

        records.append({
            "source": "millon",
            "source_url": source_url,
            "lot_number": lot_number,
            "auction_title": "",
            "sale_date": sale_date,
            "sale_location": "Paris",
            "artist_name_raw": artist,
            "artwork_title": title,
            "hammer_price": amount,
            "currency": currency,
            "status": "sold",
            "raw_snapshot": m.group(0)[:500],
        })
    return records, None


def list_vn_past_catalogs(scraper=None, max_pages=25):
    """Return all Millon catalogs filed under the Vietnam department (1113).
    This is the authoritative, Millon-curated list — do NOT fall back to keyword matching
    on all past sales (which mixed in unrelated Orientalism/Asian-art catalogs)."""
    scraper = scraper or _make_scraper()
    all_cats = set()
    for page in range(max_pages):
        qs = "op=submit&f%5B0%5D=department%3A1113"
        url = f"https://www.millon.com/catalogue/ventes-passees?{qs}"
        if page > 0:
            url += f"&page={page}"
        try:
            r = scraper.get(url, timeout=20)
            if r.status_code != 200:
                break
            cats = set(re.findall(r"/catalogue/(vente\d+-[a-z0-9\-]+)", r.text))
            new = cats - all_cats
            if not new:
                break
            all_cats.update(cats)
        except Exception:
            break
    return sorted(all_cats)


def parse_catalog_results(scraper, catalog_slug):
    """Fetch /catalogue/{slug}/resultat page and extract lot records with hammer prices.
    Returns list of dict records."""
    url = f"https://www.millon.com/catalogue/{catalog_slug}/resultat"
    records = []
    seen_lots = set()
    for page in range(1, 10):  # paginate within catalog
        page_url = url if page == 1 else f"{url}?page={page}"
        try:
            r = scraper.get(page_url, timeout=25)
            if r.status_code != 200:
                break
        except Exception:
            break
        text = r.text
        # Each lot card: "Adjugé à ... X € ... LOT N ... /lot{N}-artist-YYYY-YYYY"
        # Use finditer with non-greedy match
        lot_pattern = re.compile(
            r"Adjug[eé]\s+[àa]\s*[^\d]*([\d\s.,]+)\s*(?:€|EUR)"        # hammer price
            r".*?/catalogue/" + re.escape(catalog_slug) + r"/(lot(\d+)-[^\"'\s>]+)",
            re.IGNORECASE | re.DOTALL,
        )
        page_new = 0
        for m in lot_pattern.finditer(text):
            price_raw = m.group(1)
            lot_path = m.group(2)
            lot_num = m.group(3)
            if lot_num in seen_lots:
                continue
            seen_lots.add(lot_num)
            amount, currency = parse_amount(price_raw, default_currency="EUR")
            if amount is None:
                continue
            # Extract artist from slug (after lotN-)
            m_art = re.match(r"lot\d+-([a-z0-9\-]+?)(?:-\d{4})?$", lot_path)
            slug_artist = m_art.group(1) if m_art else ""
            # Title + years will come from lot detail page
            records.append({
                "slug": lot_path,
                "lot_number": lot_num,
                "hammer_price": amount,
                "currency": currency,
                "slug_artist": slug_artist,
                "lot_url": f"https://www.millon.com/catalogue/{catalog_slug}/{lot_path}",
            })
            page_new += 1
        if page_new == 0:
            break
    return records


def crawl_past_catalogs(conn, catalog_slugs=None, delay=1.5, detail_delay=1.2, verbose=True, filter_vn=True, max_catalogs=None):
    """Crawl Millon past auction catalogs (Vietnam-themed), extract all sold lots with hammer prices."""
    scraper = _make_scraper()
    if catalog_slugs is None:
        if verbose: print("  Discovering past catalogs...")
        catalog_slugs = list_vn_past_catalogs(scraper)
        if verbose: print(f"  Found {len(catalog_slugs)} VN-themed past catalogs")
    if max_catalogs:
        catalog_slugs = catalog_slugs[:max_catalogs]

    # Load VN catalog for filtering
    vn_catalog, exclusions = ({}, set())
    if filter_vn:
        try:
            import sys as _sys
            from pathlib import Path
            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
            for m in list(_sys.modules.keys()):
                if "vn_artist_catalog" in m:
                    del _sys.modules[m]
            from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
            vn_catalog = VN_ARTIST_CATALOG
            exclusions = NON_VN_EXCLUSIONS
        except ImportError:
            pass
    from artonis_price_mvp import normalize_key

    total = 0
    from datetime import datetime
    for i, slug in enumerate(catalog_slugs, 1):
        run_started = datetime.utcnow().isoformat() + "Z"
        try:
            records = parse_catalog_results(scraper, slug)
        except Exception as e:
            if verbose: print(f"  [{i}/{len(catalog_slugs)}] {slug[:50]}: ERR {e}")
            log_crawl_run(conn, "millon", target_slug=slug, started_at=run_started,
                          status="error", note=str(e)[:200])
            continue

        inserted_this = 0
        for rec in records:
            # Fetch lot detail for artist name + title + dimensions
            details = _fetch_lot_details(scraper, rec["lot_url"])
            # Skip attribution lots — works "after / d'après / attribué à / et son atelier"
            # are NOT original by the named artist. Detected in URL lot slug
            # (e.g. .../lot12-le-quoc-loc-1918-1987-attribue) or in the meta desc.
            url_low = (rec["lot_url"] or "").lower()
            if re.search(r"-(?:attribue|attribue-a|et-son-atelier|et-atelier|d-apres|atelier-de|ecole-de|entourage-de|cercle-de|cours-de|after)(?:-|$)", url_low):
                continue
            meta = details.get("meta_desc", "")
            if re.search(r"\b(?:Attribu[eé](?:\s+[àa])?|D['’]?Apr[èe]s|Et\s+Son\s+Atelier|Attributed\s+To|After\s+|Circle\s+of|Follower\s+of|Studio\s+of|Workshop\s+of|Manner\s+of)\b",
                         meta, re.IGNORECASE):
                continue
            # Extract artist from meta desc: "Lot N - ARTIST NAME (YYYY-YYYY) Title Medium Dims"
            m_art = re.search(r"Lot\s+\d+\s*[-–]\s*([A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-']{2,50}?)\s*\(", meta)
            artist_raw = m_art.group(1).strip().title() if m_art else rec["slug_artist"].replace("-", " ").title()
            # Strip trailing years/digits from slug-based names
            artist_raw = re.sub(r"\s+(?:1[89]\d{2}|20[0-2]\d)(?:\s+.*)?$", "", artist_raw).strip()
            # Centralised cleanup: (Né en YYYY), (XXe siècle), (1919/22-2016), * prefix
            artist_raw, _alt_birth = clean_artist_name(artist_raw)

            if filter_vn:
                norm = normalize_key(artist_raw)
                if norm in exclusions:
                    continue
                # Accept if in catalog (exact or prefix)
                is_vn = (norm in vn_catalog or
                         any(norm == k or norm.startswith(k + " ") or k.startswith(norm + " ") for k in vn_catalog))
                if not is_vn:
                    continue

            title = details.get("title_detail", "")

            cat_title, cat_date = _fetch_catalog_meta(scraper, slug)
            rec_out = {
                "source": "millon",
                "source_url": rec["lot_url"],
                "sale_page_url": f"https://www.millon.com/catalogue/{slug}/resultat",
                "lot_number": rec["lot_number"],
                "auction_title": f"Millon — {cat_title or slug}",
                "sale_date": cat_date,
                "sale_location": "Paris",
                "artist_name_raw": artist_raw,
                "artwork_title": title,
                "medium": details.get("medium", ""),
                "dimensions": details.get("dimensions", ""),
                "year": details.get("year", ""),
                "estimate_low": None,
                "estimate_high": None,
                "hammer_price": rec["hammer_price"],
                "price_with_premium": None,
                "currency": rec["currency"],
                "status": "sold",
                "raw_snapshot": (meta or rec["slug_artist"])[:500],
            }
            insert_sale_result(conn, rec_out)
            inserted_this += 1
            time.sleep(detail_delay)
        conn.commit()
        # Log run with date range from this catalog's metadata
        cat_meta_title, cat_meta_date = _fetch_catalog_meta(scraper, slug)
        log_crawl_run(conn, "millon", target_slug=slug, started_at=run_started,
                      lots_scanned=len(records), lots_inserted=inserted_this,
                      sale_date_min=cat_meta_date or None, sale_date_max=cat_meta_date or None,
                      status="ok", note=cat_meta_title[:120] if cat_meta_title else None)
        if verbose:
            print(f"  [{i}/{len(catalog_slugs)}] {slug[:55]:<55} {len(records)} lots / {inserted_this} inserted")
        total += inserted_this
        time.sleep(delay)
    return total


def crawl_all(conn, slugs=None, delay=2.5, verbose=True, fetch_details=True, detail_delay=2):
    """Crawl price pages for all given artist slugs, insert into sale_results.
    If fetch_details=True, also fetch each lot page to get dimensions + medium (slower but richer)."""
    slugs = slugs or VN_ARTIST_SLUGS
    scraper = _make_scraper()
    total = 0
    for slug in slugs:
        records, err = fetch_artist_page(slug, scraper)
        if err:
            if verbose:
                print(f"  [{slug}] skipped: {err}")
            time.sleep(delay)
            continue
        if fetch_details:
            for rec in records:
                if rec.get("source_url"):
                    details = _fetch_lot_details(scraper, rec["source_url"])
                    if details.get("dimensions"):
                        rec["dimensions"] = details["dimensions"]
                    if details.get("medium"):
                        rec["medium"] = details["medium"]
                    time.sleep(detail_delay)
        for r in records:
            insert_sale_result(conn, r)
        conn.commit()
        if verbose:
            print(f"  [{slug}] {len(records)} sales inserted")
        total += len(records)
        time.sleep(delay)
    return total
