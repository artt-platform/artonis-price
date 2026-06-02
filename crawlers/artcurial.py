"""Artcurial crawler — Paris auction house with regular 'Art d'Asie' / 'Impressionniste & Moderne'
sales featuring Indochine masters (Mai Trung Thứ, Lê Phổ, Vũ Cao Đàm, Lebadang, Inguimberty, …).

Discovery:  /sitemap/sales-{YEAR}-fr.xml — yearly XMLs listing every /ventes/{ref}/lots/{N}-a URL
            plus the /ventes/vente-fr-{techId}-{slug} sale-landing URLs. The slug is filtered for
            Asian/Modern/Orient keywords; the landing redirects to /ventes/{ref}.
Sale meta:  Each lot page embeds `__NUXT_DATA__` (a flat-array Nuxt 3 SSR payload) containing both
            the lot record AND the parent sale record (name, vacations[0].address.city, effectiveDate).
Lot data:   Same `__NUXT_DATA__` carries:
              status               'SOLD' / 'WITHDRAWN' / …
              low / high           estimate range (EUR)
              adjudicationPrice    hammer price (EUR)
              finalPrice           buyer total (premium inclusive, EUR)
              currency             always 'EUR'
              artist               full Artist dict (fullName, nationalities[*].alphaTwo, years)
              descriptions.FRENCH  titleWithHtml, technique, signature, origin (provenance), …
              properties           {height, heightUnit, width, widthUnit} in cm
Auth:       Cloudscraper handles Cloudflare. No Playwright needed.
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


BASE = "https://www.artcurial.com"

# Yearly sitemaps cover 2011 onwards on artcurial.com.
# We default to 2018+ — older sales have sparse VN content and many missing prices.
DEFAULT_YEARS = list(range(2018, datetime.utcnow().year + 1))

# Sale-slug keywords flagging a sale as potentially containing Vietnamese / Indochina art.
# The per-lot VN-catalog filter is the authoritative gate; here we cast a wide net.
_SALE_KEYWORDS = (
    "asie", "asia", "asian", "asiatique", "indochin", "indochina",
    "art-moderne", "art-modern", "moderne",
    "impressionniste", "impressionist",
    "orient", "orientalisme",
)


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True}
    )


# ---- discovery --------------------------------------------------------------

_SITEMAP_CACHE = {}


def _get_yearly_sitemap(scraper, year):
    """Cached fetch of /sitemap/sales-{YEAR}-fr.xml (returns raw XML or '').

    Each yearly sitemap is ~1MB and contains both sale-slug URLs and lot URLs;
    we hit them twice per run (discovery + lot-URL lookup) so caching is a 2×
    win over the wire.
    """
    if year in _SITEMAP_CACHE:
        return _SITEMAP_CACHE[year]
    url = f"{BASE}/sitemap/sales-{year}-fr.xml"
    try:
        r = scraper.get(url, timeout=30)
        text = r.text if r.status_code == 200 else ""
    except Exception:
        text = ""
    _SITEMAP_CACHE[year] = text
    return text


def discover_sale_urls(scraper, years=None):
    """Return a list of canonical sale URLs (e.g. /ventes/4426) by walking the
    yearly sitemaps and filtering to Asian/Modern/Orient slugs.

    The sitemap slug carries a `techId` that doesn't always equal the final `ref`
    (e.g. techId=4036 → ref=4426). We avoid a per-sale redirect probe by reading
    BOTH the slug URLs (for filtering) AND the lot URLs (which contain the final
    `ref`) from the same sitemap, then mapping techId→ref via slug-adjacency.
    Each sitemap groups all URLs for a sale together, so a slug is followed
    in-file by its lot URLs sharing the canonical ref.
    """
    years = years or DEFAULT_YEARS
    resolved = []
    seen = set()

    for yr in years:
        text = _get_yearly_sitemap(scraper, yr)
        if not text:
            continue

        # Walk URLs in order; remember most-recent matching slug and assign its
        # tech_id to the next /ventes/{ref}/lots/ ref we see.
        pending_slug = None
        for m in re.finditer(
            r"/ventes/(?:vente-fr-(\d+)-([\w\-]+)|(\d+)/lots/[\w\-]+)",
            text,
        ):
            tech_id, slug, lot_ref = m.group(1), m.group(2), m.group(3)
            if slug:
                # Slug URL — flag if it matches a target keyword
                if any(kw in slug for kw in _SALE_KEYWORDS):
                    pending_slug = (tech_id, slug)
                else:
                    pending_slug = None
            elif lot_ref and pending_slug is not None:
                # First lot URL after a matching slug → bind the canonical ref
                canonical = f"{BASE}/ventes/{lot_ref}"
                if canonical not in seen:
                    seen.add(canonical)
                    resolved.append((yr, int(lot_ref), canonical))
                pending_slug = None

    # Newest first
    resolved.sort(key=lambda t: (-t[0], -t[1]))
    return [u for _, _, u in resolved]


def get_sale_lot_urls(scraper, sale_url):
    """Return the full list of /ventes/{ref}/lots/{N}-a URLs for a given sale.

    The /ventes/{ref} page itself only embeds the first 20 lots; the yearly
    sitemap is the only public source containing all lot URLs for a sale.
    Sitemaps are cached in-process — discover_sale_urls warms them up first.
    """
    m = re.search(r"/ventes/(\d+)", sale_url)
    if not m:
        return []
    ref = m.group(1)
    # Try years newest-first since most sales are recent
    for yr in DEFAULT_YEARS[::-1] + list(range(2011, DEFAULT_YEARS[0])):
        xml = _get_yearly_sitemap(scraper, yr)
        if not xml:
            continue
        lots = sorted(set(re.findall(rf"(/ventes/{ref}/lots/[\w\-]+)", xml)))
        if lots:
            return [f"{BASE}{p}" for p in lots]
    return []


# ---- Nuxt payload helpers ---------------------------------------------------

def _parse_nuxt_data(html_text):
    """Extract and JSON-decode the `__NUXT_DATA__` flat array from a lot/sale page.
    Returns the decoded list, or None on failure."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.+?)</script>',
                  html_text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(_html.unescape(m.group(1)))
    except Exception:
        return None


def _deref(arr, idx, depth=0, max_depth=12):
    """Resolve a Nuxt-flat-array index. Nuxt stores nested values by integer index
    into the same top-level list — so {'foo': 42} means foo's value is arr[42].
    Recurse up to max_depth to fully materialise."""
    if not isinstance(idx, int) or idx < 0 or idx >= len(arr):
        return idx
    if depth > max_depth:
        return arr[idx]
    v = arr[idx]
    if isinstance(v, dict):
        return {k: _deref(arr, vv, depth + 1, max_depth) for k, vv in v.items()}
    if isinstance(v, list):
        return [_deref(arr, x, depth + 1, max_depth) for x in v]
    return v


def _find_lot_dict(arr):
    """Locate the lot dict inside the flat Nuxt array. Identified by presence of
    'adjudicationPrice' key (every lot has this even when null)."""
    for i, v in enumerate(arr):
        if isinstance(v, dict) and "adjudicationPrice" in v:
            return _deref(arr, i)
    return None


def _find_sale_dict(arr):
    """Locate the parent sale dict (carries name, vacations, effectiveDate)."""
    for i, v in enumerate(arr):
        if isinstance(v, dict) and "totalLots" in v:
            return _deref(arr, i)
    return None


# ---- HTML / text cleanup ----------------------------------------------------

def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


# Patterns that flag a lot as inauthentic — these aren't single-artist primary-market works
_FAKE_PREFIX_RE = re.compile(
    r"^(?:ATTRIBU[ÉE](?:\s+[ÀA])?|D['’]APR[ÈE]S|APR[ÈE]S|ENTOURAGE\s+DE|"
    r"ATELIER\s+DE|ÉCOLE\s+DE|ECOLE\s+DE|CIRCLE\s+OF|FOLLOWER\s+OF|"
    r"MANNER\s+OF|STYLE\s+OF|COPIE|REPRODUCTION)\s+",
    re.IGNORECASE,
)


# ---- sale-meta parsing ------------------------------------------------------

def _parse_sale_meta(sale_dict):
    """Extract sale_date (YYYY-MM-DD), sale_city, sale_name from a sale dict.

    `effectiveDate` is encoded as ['Date', 'ISO-8601'] — a custom Nuxt serial wrapper.
    `vacations[0].address.city` is something like 'Artcurial, Paris' — keep just the city.
    """
    sale_name = clean_text(sale_dict.get("name") or "")
    eff = sale_dict.get("effectiveDate")
    iso = ""
    if isinstance(eff, list) and len(eff) >= 2 and isinstance(eff[1], str):
        iso = eff[1]
    sale_date = ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if m:
        sale_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    sale_city = "Paris"
    vacations = sale_dict.get("vacations") or []
    if vacations and isinstance(vacations[0], dict):
        addr = (vacations[0].get("address") or {})
        city = addr.get("city") or ""
        # Strip leading 'Artcurial, ' / 'Hôtel, ' prefixes
        city = re.sub(r"^(Artcurial|Hôtel|H[oô]tel)\s*[,]?\s*", "", city).strip()
        if city:
            sale_city = city
    return sale_date, sale_city, sale_name


# ---- VN filtering -----------------------------------------------------------

def _load_vn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_dict, artist_raw, vn_catalog, exclusions):
    """Two-channel VN match:
      1) Strong signal: artist.nationalities[*].alphaTwo == 'VN' (Artcurial flags this explicitly)
      2) Fallback signal: artist_raw normalises into VN_ARTIST_CATALOG (covers Inguimberty,
         Alix Aymé etc. — French-school Indochine masters lacking VN nationality flag)
    """
    if artist_dict:
        for nat in (artist_dict.get("nationalities") or []):
            if isinstance(nat, dict) and (nat.get("alphaTwo") or "").upper() == "VN":
                # Catalog exclusion still wins (e.g. someone explicitly flagged non-VN)
                from artonis_price_mvp import normalize_key
                if normalize_key(artist_raw) in exclusions:
                    return False
                return True
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


# ---- lot-record extraction --------------------------------------------------

def _build_artist_label(artist_dict):
    """Return a normalised artist label like 'Mai Trung Thứ' or 'Lê Phổ' from the
    Artist dict. Falls back to party.firstName + party.lastName if name fields empty.

    Artcurial stores some legacy artist records as ALL CAPS ('LE PHO', 'INGUIMBERTY').
    We title-case those so downstream artist-table dedup works (LE PHO and Le Pho
    must map to the same artist_id)."""
    if not artist_dict:
        return ""
    party = artist_dict.get("party") or {}
    raw = clean_text(party.get("fullName") or "")
    if not raw:
        first = clean_text(party.get("firstName") or "")
        last = clean_text(party.get("lastName") or "")
        if first or last:
            raw = f"{first} {last}".strip()
    if not raw:
        raw = clean_text(artist_dict.get("name") or "")
    if not raw:
        return ""
    # All-caps → Title Case (preserves diacritics; .title() handles them in py3).
    # Keep mixed-case names intact (Mai Trung Thứ, Vũ Cao Đàm, etc.).
    if raw == raw.upper() and any(c.isalpha() for c in raw):
        raw = raw.title()
    return raw


def _parse_dimensions_from_properties(props):
    """properties dict has {height, heightUnit, width, widthUnit} — return 'W x H cm'."""
    if not isinstance(props, dict):
        return ""
    h = props.get("height")
    w = props.get("width")
    hu = (props.get("heightUnit") or "CM").upper()
    wu = (props.get("widthUnit") or "CM").upper()
    if not (h and w) or hu != "CM" or wu != "CM":
        return ""
    try:
        hf, wf = float(h), float(w)
    except (TypeError, ValueError):
        return ""
    if hf <= 0 or wf <= 0:
        return ""
    # Normalise to "W x H cm" (matches common.parse_dimensions expectations)
    def _fmt(x):
        return str(int(x)) if x == int(x) else f"{x:.1f}"
    return f"{_fmt(wf)} x {_fmt(hf)} cm"


def _parse_dimensions_from_text(*texts):
    """Fallback: scan signature/comment/technique for 'NN x NN cm' literal."""
    blob = " ".join(_strip_html(t) for t in texts if t)
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm",
        blob, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1).replace(',', '.')} x {m.group(2).replace(',', '.')} cm"
    return ""


def _parse_year(*texts):
    """Look for a 4-digit 19xx/20xx in title/signature, preferring the FIRST one."""
    blob = " ".join(_strip_html(t) for t in texts if t)
    # Title patterns like 'Mère et enfant au fruit - 1970' or 'Femme à la fleur - 1956'
    m = re.search(r"[-–]\s*(1[89]\d{2}|20[0-2]\d)\b", blob)
    if m:
        return m.group(1)
    m = re.search(r"\b(1[89]\d{2}|20[0-2]\d)\b", blob)
    if m:
        return m.group(1)
    return ""


def _build_record(lot, sale, lot_url, sale_url):
    """Translate a fully-deref'd lot dict + sale dict into the canonical
    insert_sale_result record. Returns None if the lot isn't a sold artwork."""
    if not lot or lot.get("status") != "SOLD":
        return None
    if lot.get("publishLot") is False:
        return None
    if lot.get("publishResult") is False:
        # Sale-level "publishResult: false" is read off sale dict; lots themselves
        # don't carry that flag. Still defensively skip.
        pass

    hammer = lot.get("adjudicationPrice")
    try:
        hammer_f = float(hammer or 0)
    except (TypeError, ValueError):
        hammer_f = 0.0
    if hammer_f <= 0:
        return None

    desc = (lot.get("descriptions") or {}).get("FRENCH") or {}
    title_fr = _strip_html(desc.get("titleWithHtml") or "")
    medium = clean_text(desc.get("technique") or "")
    signature = desc.get("signature") or ""
    origin = desc.get("origin") or ""           # provenance lives here
    comment = desc.get("comment") or ""
    bibliography = desc.get("bibliography") or ""

    artist_dict = lot.get("artist") or {}
    artist_raw = _build_artist_label(artist_dict)
    if not artist_raw:
        # Fall back to shortDescription's leading caps line ("MAI TRUNG THU 1906 - 1980 …")
        sd = _strip_html(desc.get("shortDescription") or "")
        m = re.match(r"^([A-ZÀ-Þ][A-ZÀ-Þ\s\-]{2,60})\s+\d{4}", sd)
        if m:
            artist_raw = clean_text(m.group(1).title())
    if not artist_raw:
        return None

    # Strip notation suffixes ("Né en YYYY" / "(1906-1980)") so duplicates merge
    artist_clean, _alt_birth = clean_artist_name(artist_raw)
    if not artist_clean:
        return None

    # Reject "After / Atelier de / D'après" pseudo-attributions
    title_for_check = (title_fr + " " + (desc.get("shortDescription") or "")).strip()
    if _FAKE_PREFIX_RE.match(title_for_check):
        return None

    # Reject prints / reproductions — these aren't comparable to primary paintings
    blob = (title_fr + " " + medium + " " + _strip_html(comment) + " " + _strip_html(signature)).lower()
    if re.search(r"\b(d['’]apr[èe]s|estampe|lithograph|sérigraphie|gravure|"
                 r"impression sur (?:soie|papier)|reproduction sur|tirage limit|"
                 r"épreuve d['’]artiste|epreuve d'artiste)\b", blob):
        return None

    dimensions = _parse_dimensions_from_properties(lot.get("properties"))
    if not dimensions:
        dimensions = _parse_dimensions_from_text(signature, comment, medium)

    year = _parse_year(title_fr, signature, comment)
    provenance = _strip_html(origin)[:2000]

    # Sale-level metadata
    sale_date, sale_city, sale_name = _parse_sale_meta(sale or {})

    currency = lot.get("currency") or "EUR"
    est_low = lot.get("low")
    est_high = lot.get("high")
    final_price = lot.get("finalPrice")    # buyer total (premium-inclusive)
    try:
        premium_total = float(final_price) if final_price else None
    except (TypeError, ValueError):
        premium_total = None

    return {
        "source": "artcurial",
        "source_url": lot_url,
        "sale_page_url": sale_url,
        "lot_number": f"{lot.get('index') or ''}{lot.get('subIndex') or ''}".strip() or "",
        "auction_title": f"Artcurial — {sale_name}" if sale_name else "Artcurial",
        "sale_date": sale_date,
        "sale_location": sale_city,
        "artist_name_raw": artist_clean,
        "artwork_title": title_fr[:200],
        "medium": medium[:140],
        "dimensions": dimensions,
        "year": year,
        "estimate_low": float(est_low) if est_low else None,
        "estimate_high": float(est_high) if est_high else None,
        "hammer_price": hammer_f,
        "price_with_premium": premium_total,
        "currency": currency,
        "status": "sold",
        "provenance": provenance,
        "raw_snapshot": (title_fr + " | " + _strip_html(comment)[:300])[:500],
    }, artist_dict, artist_raw


def fetch_lot(scraper, lot_url):
    """Return ({lot_dict, sale_dict, ...}, html_text) for a lot URL."""
    r = scraper.get(lot_url, timeout=25)
    if r.status_code != 200:
        return None, None
    arr = _parse_nuxt_data(r.text)
    if not arr:
        return None, r.text
    lot = _find_lot_dict(arr)
    sale = _find_sale_dict(arr)
    return {"lot": lot, "sale": sale}, r.text


# ---- main crawl entry -------------------------------------------------------

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Artcurial past sales for Vietnamese / Indochine-master lots.

    Args:
        conn:       sqlite3 connection.
        sale_urls:  list of /ventes/{ref} URLs to crawl. If None, discover via yearly sitemaps.
        delay:      sleep between lot fetches (seconds). Sales add an extra delay × 2 pause.
        verbose:    print progress.
        filter_vn:  only insert lots whose artist is VN (nationality or catalog match).
        max_pages:  cap on number of SALES processed (safety limit).

    Returns (inserted, scanned).
    """
    scraper = _make_scraper()
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    if sale_urls is None:
        if verbose:
            print("  [artcurial] discovering sale URLs from yearly sitemaps…", flush=True)
        sale_urls = discover_sale_urls(scraper)
        if verbose:
            print(f"  [artcurial] sitemaps yielded {len(sale_urls)} Asian/Modern/Orient sales",
                  flush=True)
    sale_urls = list(sale_urls)[:max_pages]

    total_inserted = 0
    total_scanned = 0

    for i, sale_url in enumerate(sale_urls, 1):
        run_started = datetime.utcnow().isoformat() + "Z"
        m = re.search(r"/ventes/(\d+)", sale_url)
        ref = m.group(1) if m else sale_url

        lot_urls = get_sale_lot_urls(scraper, sale_url)
        if not lot_urls:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] sale {ref}: no lot URLs in sitemap",
                      flush=True)
            log_crawl_run(conn, "artcurial", target_slug=ref, started_at=run_started,
                          status="error", note="no lot urls in sitemap")
            time.sleep(delay)
            continue

        inserted_this = 0
        sale_name = ""
        sale_date = ""
        sale_meta_logged = False

        for j, lot_url in enumerate(lot_urls, 1):
            total_scanned += 1
            try:
                payload, _html_text = fetch_lot(scraper, lot_url)
            except Exception as e:
                if verbose and j == 1:
                    print(f"      lot {lot_url} ERR {e}", flush=True)
                time.sleep(delay)
                continue
            if not payload or not payload.get("lot"):
                time.sleep(delay)
                continue

            lot = payload["lot"]
            sale = payload["sale"] or {}
            if not sale_meta_logged:
                sale_date, _sale_city, sale_name = _parse_sale_meta(sale)
                sale_meta_logged = True

            built = _build_record(lot, sale, lot_url, sale_url)
            if built is None:
                time.sleep(delay)
                continue
            rec, artist_dict, artist_raw = built

            if filter_vn and not _is_vietnamese(artist_dict, artist_raw, vn_catalog, exclusions):
                time.sleep(delay)
                continue

            insert_sale_result(conn, rec)
            # Commit eagerly so partial progress survives a long-running crawl being
            # interrupted (Artcurial sales can have 200+ lots × ~1.5s each).
            conn.commit()
            inserted_this += 1
            if verbose:
                print(
                    f"      +{rec['artist_name_raw'][:22]:22s} | "
                    f"{(rec['artwork_title'] or '')[:30]:30s} | "
                    f"{rec['dimensions']:14s} | "
                    f"{rec['hammer_price']:>8.0f} {rec['currency']}",
                    flush=True,
                )
            time.sleep(delay)

        conn.commit()
        log_crawl_run(
            conn, "artcurial", target_slug=ref, started_at=run_started,
            lots_scanned=len(lot_urls), lots_inserted=inserted_this,
            sale_date_min=sale_date or None, sale_date_max=sale_date or None,
            status="ok", note=sale_name[:120],
        )
        if verbose:
            print(
                f"  [{i}/{len(sale_urls)}] {sale_date or '??????????':10s} "
                f"{(sale_name or 'ref ' + ref)[:50]:50s}: {len(lot_urls):3d} lots / "
                f"{inserted_this} VN inserted",
                flush=True,
            )
        total_inserted += inserted_this
        time.sleep(delay * 2)

    return total_inserted, total_scanned


if __name__ == "__main__":
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    DB = str(Path(__file__).resolve().parent.parent / "data" / "artonis_price_mvp.sqlite")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    inserted, scanned = crawl(conn, max_pages=10)
    print(f"\nDONE — inserted {inserted}, scanned {scanned}")
    conn.close()
