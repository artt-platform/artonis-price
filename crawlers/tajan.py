"""Tajan crawler — Paris auction house running on the Invaluable catalog platform.

Discovery: tajan.com publishes a sitemap (/hksn-preview-sale-sitemap.xml) listing all
           /fr/auction/{slug}/ WordPress pages, one per sale. We filter to Asian /
           Indochina / Modern art sales.
Catalog:   Each /fr/auction/{slug}/ page embeds a `/auction-catalog/{cat-slug}_{REF}`
           link. The catalog page exposes a JSON endpoint with all lots:
           /api/catalog/{REF}/lots?size=500 → {auctionLotUserItemViewList:[{itemView:{…}}, …]}
           and the catalog HTML contains data-catalogData with sale title, date, location.
Auth:      Cloudscraper bypasses the 403 Cloudflare guard — no Playwright needed.
"""
import re
import sys
import time
import json
import html as _html
from pathlib import Path
from datetime import datetime

import cloudscraper

from crawlers.common import (
    insert_sale_result, clean_text, clean_artist_name, log_crawl_run,
)


BASE = "https://www.tajan.com"
SITEMAP_URL = f"{BASE}/hksn-preview-sale-sitemap.xml"

# Sale-slug keywords that flag a Tajan auction as potentially containing Vietnamese art.
# We intentionally keep this broad (Asia + Modern + Impressionist) — the per-lot VN-catalog
# filter is the authoritative gate.
_AUCTION_SLUG_KEYWORDS = (
    "asie", "asia", "asian", "asiatique", "indochin",
    "art-moderne", "art-modern", "moderne",
    "impressionniste", "impressionist",
    "tresor", "treasures", "orient",
)

# Hard-coded seed sales known to contain Vietnamese / Indochina-school lots.
# Used as a fallback if the sitemap is unreachable or as a quick smoke test.
SEED_SALE_URLS = [
    "https://www.tajan.com/fr/auction/2614-arts-dasie/",      # 2026 / upcoming
    "https://www.tajan.com/fr/auction/2542-arts-dasie/",      # 2025-12
    "https://www.tajan.com/fr/auction/2521-arts-dasie/",
    "https://www.tajan.com/fr/auction/2529-arts-dasie/",
    "https://www.tajan.com/fr/auction/2504-asian-arts/",
    "https://www.tajan.com/fr/auction/2447-arts-dasie/",
    "https://www.tajan.com/fr/auction/2448-arts-dasie/",
    "https://www.tajan.com/fr/auction/2425-arts-dasie/",
    "https://www.tajan.com/fr/auction/2418-arts-dasie/",
    "https://www.tajan.com/fr/auction/3244-arts-dasie/",
    "https://www.tajan.com/fr/auction/3235-arts-dasie/",
    "https://www.tajan.com/fr/auction/3219-arts-dasie/",
    "https://www.tajan.com/fr/auction/3216-arts-dasie/",
    "https://www.tajan.com/fr/auction/2400-arts-dasie/",
    "https://www.tajan.com/fr/auction/2542-arts-dasie/",
    # Modern / impressionniste sales — VN masters sometimes slot here
    "https://www.tajan.com/fr/auction/2501-art-impressionniste-moderne/",
    "https://www.tajan.com/fr/auction/2603-art-impressionniste-moderne/",
    "https://www.tajan.com/fr/auction/2609-art-impressionniste-moderne/",
    "https://www.tajan.com/fr/auction/2401-art-moderne/",
    "https://www.tajan.com/fr/auction/2405-art-moderne/",
    "https://www.tajan.com/fr/auction/3229-art-moderne/",
    "https://www.tajan.com/fr/auction/3238-art-moderne/",
    # ─── Historical expansion (discovered 2026-06-15 via Google + Tajan archive) ──
    # Older slug-first URLs (pre-2019 era):
    "https://www.tajan.com/en/auction/asian-art-1407/",
    "https://www.tajan.com/fr/auction/orient-1406/",
    "https://www.tajan.com/fr/auction/arts-dorient/",                            # sale 2105
    "https://www.tajan.com/fr/auction/art-moderne-et-contemporain/",             # sale 2128
    "https://www.tajan.com/fr/auction/art-moderne-2142/",
    "https://www.tajan.com/fr/auction/art-contemporain/",                        # sale 2145
    "https://www.tajan.com/fr/auction/arts-dorient-2147/",
    "https://www.tajan.com/en/auction/2414-oriental-arts/",
    "https://www.tajan.com/fr/auction/2433-art-contemporain/",
    "https://www.tajan.com/en/auction/2439-oriental-arts/",
    "https://www.tajan.com/fr/auction/3205-art-impressionniste-et-moderne/",
    # NB: pre-2020 Tajan sales (2016-2019) mostly archived on tajan.auction.fr
    # subdomain with 5-digit IDs; not reachable via current tajan.com pattern.
    # That would need a separate crawler.
]


_DEFAULT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
}


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True}
    )


# ---- discovery --------------------------------------------------------------

def discover_sale_urls(scraper):
    """Pull /hksn-preview-sale-sitemap.xml and return WP auction-page URLs
    likely to contain Vietnamese / Indochina-school art."""
    try:
        r = scraper.get(SITEMAP_URL, timeout=25)
        if r.status_code != 200:
            return list(SEED_SALE_URLS)
    except Exception:
        return list(SEED_SALE_URLS)
    urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
    keepers = []
    seen = set()
    for u in urls:
        low = u.lower()
        # Only French pages (en/ pages mirror the same catalog, would double-count)
        if "/fr/auction/" not in low:
            continue
        if not any(kw in low for kw in _AUCTION_SLUG_KEYWORDS):
            continue
        if u in seen:
            continue
        seen.add(u)
        keepers.append(u)
    return keepers


def extract_catalog_ref(scraper, sale_page_url):
    """Fetch a /fr/auction/{slug}/ page and return (catalog_slug, catalog_ref)
    extracted from the embedded /auction-catalog/{slug}_{REF} link.
    Returns (None, None) on failure."""
    try:
        r = scraper.get(sale_page_url, timeout=25)
    except Exception:
        return None, None
    if r.status_code != 200:
        return None, None
    m = re.search(r"/auction-catalog/([A-Za-z0-9\-]+)_([A-Z0-9]{8,16})", r.text)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def fetch_catalog_meta(scraper, catalog_ref, catalog_slug):
    """Get sale title, ISO date, and city from data-catalogData on the catalog page.
    Returns dict with keys: title, sale_date (YYYY-MM-DD), city."""
    url = f"{BASE}/auction-catalog/{catalog_slug}_{catalog_ref}"
    try:
        r = scraper.get(url, timeout=25)
    except Exception:
        return {"title": "", "sale_date": "", "city": "Paris"}
    if r.status_code != 200:
        return {"title": "", "sale_date": "", "city": "Paris"}
    m = re.search(r'data-catalogData\s*=\s*"([^"]+)"', r.text)
    if not m:
        return {"title": "", "sale_date": "", "city": "Paris"}
    try:
        data = json.loads(_html.unescape(m.group(1)))
    except Exception:
        return {"title": "", "sale_date": "", "city": "Paris"}
    title = clean_text(data.get("catalogTitle") or "")
    iso = data.get("eventLocalDateIso8601") or data.get("eventDateIso8601") or ""
    # eventLocalDateIso8601 looks like '2020-12-07T14:00+01:00[CET]' — keep date only
    m_d = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    sale_date = f"{m_d.group(1)}-{m_d.group(2)}-{m_d.group(3)}" if m_d else ""
    city = (data.get("location") or {}).get("addressCity") or "Paris"
    return {"title": title, "sale_date": sale_date, "city": city}


def fetch_catalog_lots(scraper, catalog_ref):
    """Pull all lots in one catalog via /api/catalog/{REF}/lots?size=500.
    Returns a list of itemView dicts (the API yields up to 1000 per request,
    well above any Tajan sale's lot count)."""
    url = f"{BASE}/api/catalog/{catalog_ref}/lots?size=500"
    try:
        r = scraper.get(url, timeout=40, headers=_DEFAULT_HEADERS)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    items = (data.get("_embedded") or {}).get("auctionLotUserItemViewList") or []
    return [it.get("itemView") or {} for it in items if isinstance(it, dict)]


# ---- parsing helpers --------------------------------------------------------

def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


_ARTIST_HEADER_RE = re.compile(
    r"^([^\d(]{2,80})\s*\(\s*(\d{4})\s*[-–]?\s*(\d{4})?\s*[-–]?\s*\??\s*\)",
)

# Fake / non-authentic indicators
_FAKE_PREFIX_RE = re.compile(
    r"^(?:ATTRIBU[ÉE](?:\s+[ÀA])?|D['’]APR[ÈE]S|APR[ÈE]S|ENTOURAGE\s+DE|"
    r"ATELIER\s+DE|ÉCOLE\s+DE|ECOLE\s+DE|CIRCLE\s+OF|FOLLOWER\s+OF|"
    r"MANNER\s+OF|STYLE\s+OF|COPIE|REPRODUCTION)\s+",
    re.IGNORECASE,
)

# Anonymous / non-art catalog lots we never want to ingest
_NON_ART_TITLE_RE = re.compile(
    r"^(?:ÉCOLE\s+VIETNAMIENNE|ECOLE\s+VIETNAMIENNE|"
    r"ÉCOLE\s+CHINOISE|ECOLE\s+CHINOISE|ECOLE\s+INDOCHINOISE|"
    r"GRAND\s+PANNEAU|SUITE\s+DE|VASE|BOL|COUPE|PLAT|JARRE|"
    r"STATUE|STATUETTE|BR[ÛU]LE|SCEAU|CACHET|PARAVENT|"
    r"M[EÉ]DAILLE|ORDRE|D[EÉ]CORATION|MONNAIE)\b",
    re.IGNORECASE,
)

_MEDIUM_KWS = (
    "huile", "aquarelle", "encre", "laque", "gouache", "pastel", "fusain",
    "sanguine", "crayon", "soie", "acrylique", "mine de plomb", "lithographie",
    "estampe", "bronze", "tempera", "technique mixte",
    "ink", "oil", "watercolour", "watercolor", "lacquer", "silk",
)


def _parse_artist_and_title(title_field, description_field):
    """Tajan stores the artist header inside the lot 'title' (a truncated copy of
    the description's first line) and the full data inside 'description'.

    The description looks like:
        ARTIST (BIRTH-DEATH)
        ARTWORK TITLE
        Medium phrase
        ...
        DIM. NN x NN CM
        €X-Y

    Returns dict(artist, artwork_title, medium, dimensions, year).
    """
    desc_plain = _strip_html(description_field or "")
    title_plain = _strip_html(title_field or "")

    lines = [l.strip() for l in desc_plain.split("\n") if l.strip()]

    artist = ""
    artwork_title = ""
    medium = ""
    dimensions = ""
    year = ""

    if not lines:
        return {
            "artist": clean_text(title_plain),
            "artwork_title": "",
            "medium": "",
            "dimensions": "",
            "year": "",
        }

    # Line 1 should match "ARTIST (YYYY-YYYY)" — parse name + years
    first = lines[0]
    m = _ARTIST_HEADER_RE.match(first)
    if m:
        artist = clean_text(m.group(1))
    else:
        # Fallback to title_plain or first line
        # Tajan 'title' field is shortened — prefer description's first line
        artist_candidate = first
        # Some lots prefix description with "ATTRIBUÉ À …" etc. — keep for filter
        artist = clean_text(artist_candidate)

    # Line 2 = artwork title (until medium-keyword line)
    for idx, cand in enumerate(lines[1:6], start=1):
        c = cand.strip(" *,;:")
        if not c or len(c) > 200:
            continue
        low = c.lower()
        if any(kw in low for kw in _MEDIUM_KWS):
            continue
        if re.search(r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*cm", c, re.IGNORECASE):
            continue
        if re.match(r"^(?:dim\.?|signed|signé|provenance|note|exhibited|literature|"
                    r"\(.*\)|ink and|oil on|huile sur|encre et)",
                    c, re.IGNORECASE):
            continue
        # Often artwork title ends with ", YYYY" → split
        m_yr = re.search(r"^(.+?),\s*(\d{4})\s*$", c)
        if m_yr:
            artwork_title = m_yr.group(1).strip()
            year = m_yr.group(2)
        else:
            artwork_title = c
        break

    # Medium line — first line containing a medium keyword
    for cand in lines[1:8]:
        low = cand.lower()
        if any(kw in low for kw in _MEDIUM_KWS):
            # Cut at first comma / period to keep the phrase tight
            phrase = re.split(r"[.,(]", cand)[0].strip()
            if 4 < len(phrase) < 200:
                medium = clean_text(phrase)
            break

    # Dimensions: search for NN x NN CM anywhere in description
    m_dim = re.search(
        r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm",
        desc_plain, re.IGNORECASE,
    )
    if m_dim:
        dimensions = f"{m_dim.group(1).replace(',', '.')} x {m_dim.group(2).replace(',', '.')} cm"

    # Year fallback: look for "1943" near artwork_title
    if not year and artwork_title:
        m_y = re.search(r"\b(1[89]\d{2}|20[0-2]\d)\b", artwork_title)
        if m_y:
            year = m_y.group(1)

    return {
        "artist": artist,
        "artwork_title": artwork_title,
        "medium": medium,
        "dimensions": dimensions,
        "year": year,
    }


def _parse_provenance(description_field):
    """Provenance often lives inside the closing <i>…</i> block of the description.
    Pattern: "<i>Provenance <br> - Galerie ... <br> Note <br> ...</i>".
    Returns provenance text (≤2000 chars) or empty string."""
    raw = description_field or ""
    m_i = re.search(r"<i[^>]*>(.+?)</i>", raw, re.DOTALL)
    if not m_i:
        return ""
    inner = _strip_html(m_i.group(1))
    # Extract block under "Provenance" header up to next "Note"/"Information"/"Bibliographie"
    m_p = re.search(
        r"Provenance\s*:?\s*\n?(.+?)(?:\n\s*(?:Note|Bibliographie|Bibliography|"
        r"Exhibited|Exposition|Information\s+importante|Literature)|$)",
        inner, re.IGNORECASE | re.DOTALL,
    )
    if m_p:
        return m_p.group(1).strip()[:2000]
    return ""


# ---- VN filtering -----------------------------------------------------------

def _load_vn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_raw, vn_catalog, exclusions):
    """Match artist_raw against VN_ARTIST_CATALOG (exact + prefix). Mirrors aguttes.py."""
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


# ---- main crawl entry -------------------------------------------------------

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Tajan past sales for Vietnamese-artist lots.

    Args:
        conn:       sqlite3 connection.
        sale_urls:  list of /fr/auction/{slug}/ URLs to crawl. If None, discover from sitemap.
        delay:      sleep between sales (seconds).
        verbose:    print progress.
        filter_vn:  only insert lots whose artist appears in vn_artist_catalog.
        max_pages:  cap on number of sales to crawl (safety limit).

    Returns (inserted, scanned).
    """
    scraper = _make_scraper()
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    if sale_urls is None:
        if verbose:
            print("  [tajan] discovering sale URLs from sitemap…", flush=True)
        sale_urls = discover_sale_urls(scraper)
        if verbose:
            print(f"  [tajan] sitemap yielded {len(sale_urls)} Asian/Modern sales", flush=True)
    sale_urls = list(sale_urls)[:max_pages]

    total_inserted = 0
    total_scanned = 0

    for i, sale_url in enumerate(sale_urls, 1):
        run_started = datetime.utcnow().isoformat() + "Z"
        slug = sale_url.rstrip("/").rsplit("/", 1)[-1]

        catalog_slug, catalog_ref = extract_catalog_ref(scraper, sale_url)
        if not catalog_ref:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] {slug}: no catalog link found", flush=True)
            log_crawl_run(conn, "tajan", target_slug=slug, started_at=run_started,
                          status="error", note="no catalog link")
            time.sleep(delay)
            continue

        meta = fetch_catalog_meta(scraper, catalog_ref, catalog_slug)
        sale_date = meta["sale_date"]
        sale_title = meta["title"] or catalog_slug.replace("-", " ").title()
        sale_city = meta["city"] or "Paris"
        catalog_url = f"{BASE}/auction-catalog/{catalog_slug}_{catalog_ref}"

        try:
            lots = fetch_catalog_lots(scraper, catalog_ref)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] {slug}: lots API ERR {e}", flush=True)
            log_crawl_run(conn, "tajan", target_slug=slug, started_at=run_started,
                          status="error", note=str(e)[:200])
            time.sleep(delay)
            continue

        inserted_this = 0
        for lot in lots:
            total_scanned += 1
            if not lot:
                continue

            title_raw = lot.get("title") or ""
            desc_raw = lot.get("description") or ""

            parsed = _parse_artist_and_title(title_raw, desc_raw)
            artist_raw = parsed["artist"]

            # Reject anonymous / fake / non-art catalog lots up front.
            check_text = (artist_raw + " " + parsed["artwork_title"]).strip()
            if not artist_raw:
                continue
            if _FAKE_PREFIX_RE.match(artist_raw):
                continue
            if _NON_ART_TITLE_RE.match(check_text):
                continue

            # Strip notation suffixes (XXe siècle, Né en, *, etc.) so duplicates merge.
            artist_clean, _alt_birth = clean_artist_name(artist_raw)
            if not artist_clean:
                continue

            if filter_vn and not _is_vietnamese(artist_clean, vn_catalog, exclusions):
                continue

            # Reject prints/reproductions which polluted other crawlers.
            blob_low = (parsed["artwork_title"] + " " + parsed["medium"] + " " + desc_raw).lower()
            if re.search(r"\b(d['’]apr[èe]s|copy|copie|reproduction|estampe|"
                         r"print on|impression sur|tirage|lithograph)\b", blob_low):
                continue

            hammer = lot.get("priceResult")
            is_sold = bool(lot.get("isSold"))
            try:
                hammer_f = float(hammer or 0)
            except (TypeError, ValueError):
                hammer_f = 0.0
            status = "sold" if (is_sold and hammer_f > 0) else "passed"
            if status != "sold":
                # Skip passed lots — no price signal to record.
                continue

            currency = lot.get("currency") or "EUR"
            est_low = lot.get("estimateLow")
            est_high = lot.get("estimateHigh")
            buyer_premium = lot.get("buyersPremium") or 0
            try:
                premium_total = float(hammer_f) + float(buyer_premium)
            except (TypeError, ValueError):
                premium_total = None
            if not buyer_premium:
                # buyersPremium often 0 in the public payload → let common.insert_sale_result
                # derive it from auction_houses.py's premium_rate_pct.
                premium_total = None

            provenance = _parse_provenance(desc_raw)
            lot_ref = lot.get("ref") or ""
            lot_number = lot.get("lotNumber")
            # Build the auction-lot URL.  Tajan changed the format from
            # /lot/{HEX} to /auction-lot/{slug}_{HEX} sometime before
            # 2026-06.  Slug = kebab(artist) + birth-death year if known.
            if lot_ref:
                import unicodedata as _u
                t = _u.normalize("NFD", artist_clean or "")
                t = "".join(c for c in t if _u.category(c) != "Mn")
                t = t.replace("Đ","D").replace("đ","d").lower()
                slug = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
                # Try to recover lifespan from desc_raw or _alt_birth
                # for slug enrichment.  Tajan's slug appends '-YYYY-YYYY'.
                m_yrs = re.search(r"\((\d{4})\s*[-–]\s*(\d{4})\)", desc_raw or "")
                if m_yrs:
                    slug = f"{slug}-{m_yrs.group(1)}-{m_yrs.group(2)}"
                elif _alt_birth:
                    slug = f"{slug}-{_alt_birth}"
                lot_url = f"{BASE}/auction-lot/{slug}_{lot_ref}"
            else:
                lot_url = catalog_url

            rec = {
                "source": "tajan",
                "source_url": lot_url,
                "sale_page_url": catalog_url,
                "lot_number": str(lot_number) if lot_number is not None else "",
                "auction_title": f"Tajan — {sale_title}",
                "sale_date": sale_date,
                "sale_location": sale_city,
                "artist_name_raw": artist_clean,
                "artwork_title": parsed["artwork_title"],
                "medium": parsed["medium"],
                "dimensions": parsed["dimensions"],
                "year": parsed["year"],
                "estimate_low": float(est_low) if est_low else None,
                "estimate_high": float(est_high) if est_high else None,
                "hammer_price": hammer_f,
                "price_with_premium": premium_total,
                "currency": currency,
                "status": status,
                "provenance": provenance,
                "raw_snapshot": _strip_html(desc_raw)[:500],
            }
            insert_sale_result(conn, rec)
            inserted_this += 1
            if verbose:
                print(
                    f"      +{artist_clean[:22]:22s} | "
                    f"{(parsed['artwork_title'] or '')[:30]:30s} | "
                    f"{parsed['dimensions']:14s} | "
                    f"{hammer_f:>8.0f} {currency}",
                    flush=True,
                )

        conn.commit()
        log_crawl_run(
            conn, "tajan", target_slug=slug, started_at=run_started,
            lots_scanned=len(lots), lots_inserted=inserted_this,
            sale_date_min=sale_date or None, sale_date_max=sale_date or None,
            status="ok", note=sale_title[:120],
        )
        if verbose:
            print(
                f"  [{i}/{len(sale_urls)}] {sale_date or '??????????':10s} "
                f"{sale_title[:50]:50s}: {len(lots):3d} lots / {inserted_this} VN inserted",
                flush=True,
            )
        total_inserted += inserted_this
        time.sleep(delay)

    return total_inserted, total_scanned


if __name__ == "__main__":
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    DB = str(Path(__file__).resolve().parent.parent / "data" / "artonis_price_mvp.sqlite")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    inserted, scanned = crawl(conn)
    print(f"\nDONE — inserted {inserted}, scanned {scanned}")
    conn.close()
