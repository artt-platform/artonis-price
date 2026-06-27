"""Osenat crawler — Fontainebleau / Versailles auction house.

Discovery: /resultats-ventes-passees?year=YYYY lists every catalogue per year.
           Catalog URL: /catalogue/{SALE_ID}-{slug}
Catalog:   Paginated via ?offset=N&max=50. Each lot card has:
             - /lot/{SALE_ID}/{LOT_ID}-{slug} link
             - <div class="sale-flash">Résultat : NN NNN EUR</div> for hammer
             - <span class='lotnum'>N</span>
Lot detail: /lot/{SALE_ID}/{LOT_ID}-{slug}
             - <div class="fiche_lot_description" id="lotDesc-{LOT_ID}">FULL DESC</div>
             - <div class="estimAff4">LOW - HIGH EUR</div>
             - Detail page does NOT carry hammer — must be taken from catalog card.
Auth:      Plain cloudscraper works (no 403 / no Cloudflare challenge).

Major VN finds observed in test:
  - Lê Thy 2024 lacquer panel — €5,040 (155462)
  - Vũ Cao Đàm 2019 silk — €15,000+
  - Nguyễn Gia Trí cited at €365K hammer (specific sale tbd)
"""
import re
import sys
import time
import html as _html
from pathlib import Path
from datetime import datetime

import cloudscraper

from crawlers.common import (
    insert_sale_result, clean_text, clean_artist_name, log_crawl_run,
    parse_amount,
)


BASE = "https://www.osenat.com"
YEAR_URL = BASE + "/resultats-ventes-passees?year={year}"
CATALOG_URL = BASE + "/catalogue/{sid}?lang=fr&offset={off}&max=50"

# Sale-slug keywords flagging sales likely to have VN art. Broad on purpose;
# per-lot artist filter is authoritative.
_SALE_SLUG_KEYWORDS = (
    "asie", "asian", "indochin", "asiatique", "extra-europ", "extra-euro",
    "art-moderne", "art-modern", "moderne", "contemporain",
    "tableaux", "peintres", "tableau",
    "curiosit", "vente-prestige", "oriental",
)


_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True}
    )


# ---- discovery --------------------------------------------------------------

def discover_sale_urls(scraper, years=None):
    """For each year, fetch /resultats-ventes-passees?year=YYYY and pull /catalogue/{ID}-{slug}
    links whose slug matches an Asia/Modern keyword. Returns list of (sale_id, slug)."""
    if years is None:
        # Default: 2016 → current. Pre-2016 Osenat archive is sparse for VN content.
        # The "Tableaux modernes" + "Art moderne et contemporain" Versailles sales
        # are the main source of Lê Phổ/Mai Thứ/Vũ Cao Đàm at this house — covered
        # by _SALE_SLUG_KEYWORDS filter below.
        cur = datetime.utcnow().year
        years = list(range(2016, cur + 1))
    seen = set()
    keepers = []
    for year in years:
        try:
            r = scraper.get(YEAR_URL.format(year=year), timeout=30)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        for sid, slug in re.findall(r"/catalogue/(\d+)-([^\"\?#]+)", r.text):
            if sid in seen:
                continue
            low = slug.lower()
            if not any(kw in low for kw in _SALE_SLUG_KEYWORDS):
                continue
            seen.add(sid)
            keepers.append((sid, slug, year))
    return keepers


# ---- catalog page parsing ---------------------------------------------------

_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def _parse_fr_date(text):
    m = re.search(
        r"(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|"
        r"août|aout|septembre|octobre|novembre|décembre|decembre)\s+(20\d{2})",
        text, re.IGNORECASE,
    )
    if not m:
        return ""
    day = int(m.group(1))
    month = _FR_MONTHS.get(m.group(2).lower())
    if not month:
        return ""
    return f"{m.group(3)}-{month:02d}-{day:02d}"


def fetch_catalog_meta(scraper, sale_id):
    """First page of catalog gives title, date, location."""
    url = CATALOG_URL.format(sid=sale_id, off=0)
    try:
        r = scraper.get(url, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.text
    title = ""
    m = re.search(r"<h1[^>]*>(.+?)</h1>", html, re.DOTALL)
    if m:
        title = clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
    sale_date = _parse_fr_date(html)
    # Detect city from address blocks (Fontainebleau / Versailles are the two Osenat venues)
    city = "Fontainebleau"
    if re.search(r"versailles", html, re.IGNORECASE):
        if html.lower().count("versailles") >= html.lower().count("fontainebleau"):
            city = "Versailles"
    return {"title": title, "sale_date": sale_date, "city": city, "first_html": html}


def iter_catalog_pages(scraper, sale_id, first_html=None, max_pages=20):
    """Yield each page of a catalog's HTML in order."""
    for page_idx in range(max_pages):
        off = page_idx * 50
        if page_idx == 0 and first_html:
            yield first_html
            continue
        try:
            r = scraper.get(CATALOG_URL.format(sid=sale_id, off=off), timeout=30)
        except Exception:
            return
        if r.status_code != 200:
            return
        # End of pagination: no new lot links
        if not re.search(r"/lot/" + sale_id + r"/(\d+)", r.text):
            return
        yield r.text


_LOT_BLOCK_RE = re.compile(
    r'<a href="(/lot/(\d+)/(\d+)-[^"]+)"[^>]*>.*?'
    r'</a>(?:.*?)'
    r'(?:<div class="sale-flash">\s*(?:R\xc3\xa9sultat|R..sultat|Résultat)\s*:\s*'
    r'<nobr>([^<]+)</nobr>\s*</div>)?',
    re.DOTALL,
)


def extract_lot_cards(html, sale_id):
    """Parse the catalog HTML into per-lot cards.
    Returns list of dicts: {lot_id, lot_url, hammer_text, lot_num, short_desc}.

    The short_desc field is the truncated description rendered in the catalog
    grid (<h2 id="lotDesc-LOTID">) — enough to read the artist header for an
    early VN filter before we fetch the per-lot detail page.

    Strategy: split the HTML on lot URL anchors (the first <a href="/lot/.../"> of
    each card). Within each chunk, look back/forward for sale-flash and lotnum.
    """
    cards = []
    anchors = list(re.finditer(
        r'<a href="(/lot/' + sale_id + r'/(\d+)-[^"]+)"', html,
    ))
    if not anchors:
        return cards
    seen_ids = {}
    boundaries = []  # (lot_id, start_pos, lot_url)
    for m in anchors:
        lot_id = m.group(2)
        if lot_id in seen_ids:
            continue
        seen_ids[lot_id] = m.start()
        boundaries.append((lot_id, m.start(), m.group(1)))
    boundaries.sort(key=lambda x: x[1])

    for i, (lot_id, start, lot_url) in enumerate(boundaries):
        end = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(html)
        block_start = max(boundaries[i - 1][1] if i > 0 else 0, start - 1500)
        chunk = html[block_start:end]

        hammer_text = None
        m_h = re.search(
            r'<div class="sale-flash">\s*R[^<]*?sultat\s*:\s*<nobr>([^<]+)</nobr>',
            chunk,
        )
        if m_h:
            hammer_text = clean_text(m_h.group(1))

        lot_num = None
        m_n = re.search(r"<span class='lotnum'>([^<]+)</span>", chunk)
        if m_n:
            lot_num = clean_text(m_n.group(1))

        short_desc = ""
        m_d = re.search(
            r'<h2 id="lotDesc-' + lot_id + r'">(.+?)</h2>',
            chunk, re.DOTALL,
        )
        if m_d:
            short_desc = _strip_html(m_d.group(1))

        cards.append({
            "lot_id": lot_id,
            "lot_url": BASE + lot_url,
            "hammer_text": hammer_text,
            "lot_num": lot_num,
            "short_desc": short_desc,
        })
    return cards


def _extract_artist_from_short(short_desc):
    """Pull just the artist name from the catalog grid's truncated description.
    Returns "" if the text doesn't start with a parseable artist header."""
    if not short_desc:
        return ""
    first = short_desc.split("\n", 1)[0].strip()
    m = _ARTIST_HEADER_RE.match(first)
    if m:
        return clean_text(m.group(1))
    m = _ARTIST_HEADER_DASH_RE.match(first)
    if m:
        return clean_text(m.group(1))
    # Fallback: take everything before a parenthesis or comma — captures
    # names like "LE THY" when birth years are missing.
    head = re.split(r"[,(–\-—]", first)[0].strip()
    return clean_text(head)


# ---- lot detail page --------------------------------------------------------

_FAKE_PREFIX_RE = re.compile(
    r"^(?:ATTRIBU[ÉE](?:\s+[ÀA])?|D['’]APR[ÈE]S|APR[ÈE]S|ENTOURAGE\s+DE|"
    r"ATELIER\s+DE|[ÉE]COLE\s+DE|CIRCLE\s+OF|FOLLOWER\s+OF|"
    r"MANNER\s+OF|STYLE\s+OF|COPIE|REPRODUCTION)\s+",
    re.IGNORECASE,
)

_NON_ART_TITLE_RE = re.compile(
    r"^(?:[ÉE]COLE\s+VIETNAMIENNE|[ÉE]COLE\s+CHINOISE|[ÉE]COLE\s+INDOCHINOISE|"
    r"CHINE\b|JAPON\b|VIETNAM\b|TIBET\b|MONGOLIE\b|"
    r"GRAND\s+PANNEAU|SUITE\s+DE|VASE|BOL|COUPE|PLAT|JARRE|"
    r"STATUE|STATUETTE|BR[ÛU]LE|SCEAU|CACHET|PARAVENT|"
    r"M[EÉ]DAILLE|ORDRE|D[EÉ]CORATION|MONNAIE)\b",
    re.IGNORECASE,
)

_ARTIST_HEADER_RE = re.compile(
    r"^([^\d(]{2,80})\s*\(\s*(\d{4})\s*[-–]?\s*(\d{4})?\s*[-–]?\s*\??\s*\)",
)

# Osenat sometimes writes "LE PHO – 1907-2001" or "LE PHO - 1907 / 2001"
# (no parens). This covers that variant and also "LE PHO 1907-2001" plain.
_ARTIST_HEADER_DASH_RE = re.compile(
    r"^([^\d(]{2,80}?)\s*[–\-—]?\s*(\d{4})\s*[-–/]\s*(\d{4})\b",
)

_MEDIUM_KWS = (
    "huile", "aquarelle", "encre", "laque", "gouache", "pastel", "fusain",
    "sanguine", "crayon", "soie", "acrylique", "mine de plomb", "lithographie",
    "estampe", "bronze", "tempera", "technique mixte", "panneau",
    "ink", "oil", "watercolour", "watercolor", "lacquer", "silk",
)


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def fetch_lot_detail(scraper, lot_url, lot_id):
    """Return (description_text, estimate_low, estimate_high, currency, est_text) or None."""
    try:
        r = scraper.get(lot_url, timeout=30, headers=_DEFAULT_HEADERS)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.text

    # Full description
    m = re.search(
        r'<div class="fiche_lot_description" id="lotDesc-' + lot_id + r'">(.+?)</div>',
        html, re.DOTALL,
    )
    desc = _strip_html(m.group(1)) if m else ""

    # Estimation
    est_low = est_high = None
    currency = "EUR"
    est_text = ""
    m = re.search(
        r'<div class="estimLabelAff4">Estimation\s*:</div>\s*'
        r'<div class="estimAff4">\s*([^<]+?)\s*</div>',
        html, re.DOTALL,
    )
    if m:
        est_text = clean_text(m.group(1).replace("\n", " "))
        # Patterns: "3000 - 5000 EUR" / "3 000 - 5 000 EUR" / "3000 EUR"
        m2 = re.search(
            r"([\d  .,]+)\s*[-–]\s*([\d  .,]+)\s*([A-Z]{3})?",
            est_text,
        )
        if m2:
            lo, _ = parse_amount(m2.group(1))
            hi, cur = parse_amount(m2.group(2) + " " + (m2.group(3) or "EUR"))
            est_low, est_high = lo, hi
            if cur:
                currency = cur
        else:
            m3 = re.search(r"([\d  .,]+)\s*([A-Z]{3})?", est_text)
            if m3:
                lo, cur = parse_amount(m3.group(1) + " " + (m3.group(2) or "EUR"))
                est_low = lo
                if cur:
                    currency = cur

    # Image — Osenat (like G&D / Nouvel) embeds lot photos on Drouot's
    # CDN: cdn.drouot.com/d/image/lot?size=phare&path=<house>/<auction>/<hash>
    # Pick the first inline <img> that points there.
    image_url = None
    m_img = re.search(
        r'<img[^>]+(?:src|data-src)="(https?://cdn\.drouot\.com/d/image/lot[^"]+)"',
        html,
    )
    if m_img:
        image_url = m_img.group(1).replace("&amp;", "&")

    return {
        "description": desc,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "currency": currency,
        "est_text": est_text,
        "image_url": image_url,
    }


def parse_lot_description(desc_text):
    """Split full description into artist / title / medium / dimensions / year."""
    lines = [l.strip() for l in desc_text.split("\n") if l.strip()]
    out = {"artist": "", "artwork_title": "", "medium": "", "dimensions": "", "year": ""}
    if not lines:
        return out

    first = lines[0]
    artist_match_end = 0  # index inside `first` where the artist header ends
    m = _ARTIST_HEADER_RE.match(first)
    if m:
        out["artist"] = clean_text(m.group(1))
        artist_match_end = m.end()
    else:
        m = _ARTIST_HEADER_DASH_RE.match(first)
        if m:
            out["artist"] = clean_text(m.group(1))
            artist_match_end = m.end()
        else:
            # No "(YYYY-YYYY)" — Osenat sometimes writes "CHINE..." or "LE THY (vers 1948)"
            # We still want VN artist names. Split on COMMA/paren/em-dash (NOT
            # hyphen — French hyphenated names like 'JEAN-MICHEL WILMOTTE' or
            # 'PIERRE LE-TAN' would otherwise truncate to 'JEAN'/'PIERRE LE'
            # and the matcher would then fuzzy-misassign them to VN artists.
            out["artist"] = clean_text(re.split(r"[,(–—]", first)[0])

    # If description is a single long line (older Osenat lots have no <br>),
    # the artwork title and the rest of the metadata are concatenated after the
    # artist header. Split the first line on heading-like markers ("Dim.",
    # "Provenance", "Signé") so we can feed the loop below cleaner candidates.
    if len(lines) == 1 and artist_match_end and artist_match_end < len(first):
        tail = first[artist_match_end:].strip(" .,;:–-—")
        # Cut at the first metadata marker — everything before it is descriptive
        # text about the work; everything after is provenance / dims / notes.
        parts = re.split(
            r"\s+(?=Dim\.|Provenance|Sign[éE]|Non sign|Bibliographie|"
            r"Exposition|Note\s|Expert\s|Cabinet|Estimation)",
            tail, maxsplit=1,
        )
        head = parts[0].strip()
        tail_meta = parts[1] if len(parts) > 1 else ""
        # Drop a leading "(Atelier)" / "(École de)" qualifier — it's metadata,
        # not part of the work title.
        head = re.sub(r"^\(\s*(?:atelier|école|ecole|attribu[ée]|d['’]apr[èe]s)[^)]*\)\s*",
                      "", head, flags=re.IGNORECASE)
        lines = [first[:artist_match_end], head, tail_meta]

    # Artwork title: first try « ... » quoted title (Vietnamese art lots at Osenat use
    # French guillemets to bracket the canonical title, e.g.
    #   « Vue lacustre animée de jonques parmi les brumes »).
    # If absent, fall back to the first substantive line after the artist header.
    quoted = None
    for cand in lines[1:8]:
        c = cand.strip(" *,;:")
        m_q = re.match(r"«\s*(.+?)\s*»", c)
        if m_q:
            quoted = m_q.group(1).strip(" .,;:")
            break
        # Stop at first non-blank line that isn't a quote — quoted title (if any)
        # always comes right after the artist header.
        if c and not c.startswith("("):
            break
    if quoted:
        out["artwork_title"] = quoted[:300]
        # Try to detect year embedded
        m_yr = re.search(r",\s*(\d{4})\s*$", quoted)
        if m_yr:
            out["artwork_title"] = quoted[:m_yr.start()].strip(" .,;:")[:300]
            out["year"] = m_yr.group(1)
    else:
        # Osenat lots without quoted titles: the description IS the title
        # (e.g. "Panneau horizontal en bois laqué représentant une panthère noire…").
        # We keep medium-keyword lines as titles when they are descriptive (≥1 comma
        # or ≥40 chars). We DO still skip pure boiler-plate lines like "Signed",
        # "Provenance", "Dim. NN x NN cm", or short inscription/note fragments.
        for cand in lines[1:8]:
            c = cand.strip(" *,;:")
            if not c:
                continue
            if re.match(r"^(?:dim\.|signed|sign[ée]|provenance|note|bibliographie|"
                        r"r[éE]f[éE]rence|\(grande|\(petit|\(craquelure)", c, re.IGNORECASE):
                continue
            if re.search(r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*cm", c, re.IGNORECASE):
                continue
            if c.startswith("\""):
                continue
            m_yr = re.search(r"^(.+?),\s*(\d{4})\s*$", c)
            if m_yr:
                out["artwork_title"] = m_yr.group(1).strip()[:300]
                out["year"] = m_yr.group(2)
            else:
                out["artwork_title"] = c[:300]
            break

    # Medium line — first line with a medium keyword
    for cand in lines[1:10]:
        low = cand.lower()
        if any(kw in low for kw in _MEDIUM_KWS):
            phrase = re.split(r"[.,(]", cand)[0].strip()
            if 4 < len(phrase) < 220:
                out["medium"] = clean_text(phrase)
            break

    # Dimensions — use the shared smart parser so labelled French
    # 'H. 25 x L. 36 cm' and 'Diamètre : 28 cm' formats catch
    # (Osenat lacquer panels and Tran Phuc Duyên-style plates use
    # these; the prior bespoke regex only handled 'Dim. X x Y cm').
    from crawlers.parsers.dim import parse_dim_smart
    w_cm, h_cm, _area, dim_str = parse_dim_smart(desc_text, source="osenat")
    if dim_str:
        out["dimensions"] = dim_str

    # Year fallback: "vers 1948" anywhere
    if not out["year"]:
        m_y = re.search(r"\bvers\s+(1[89]\d{2}|20[0-2]\d)\b", desc_text, re.IGNORECASE)
        if m_y:
            out["year"] = m_y.group(1)

    return out


# ---- VN filtering -----------------------------------------------------------

def _load_vn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_raw, vn_catalog, exclusions):
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

def crawl(conn, sale_specs=None, years=None, delay=1.2, verbose=True, filter_vn=True, max_pages=80):
    """Crawl Osenat past sales for VN-artist lots.

    Args:
        conn:        sqlite3 connection.
        sale_specs:  optional list of (sale_id, slug, year). If None → auto-discover.
        years:       optional list of years to scan (when sale_specs is None).
        delay:       polite delay between catalog-page fetches.
        verbose:     stdout progress.
        filter_vn:   only insert VN-catalog matches.
        max_pages:   ceiling on sales to crawl.

    Returns (inserted, scanned).
    """
    scraper = _make_scraper()
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    if sale_specs is None:
        if verbose:
            print("  [osenat] discovering sales by year…", flush=True)
        sale_specs = discover_sale_urls(scraper, years=years)
        if verbose:
            print(f"  [osenat] found {len(sale_specs)} candidate sales", flush=True)

    sale_specs = list(sale_specs)[:max_pages]

    total_inserted = 0
    total_scanned = 0

    for i, spec in enumerate(sale_specs, 1):
        sale_id, slug, year = spec
        run_started = datetime.utcnow().isoformat() + "Z"

        meta = fetch_catalog_meta(scraper, sale_id)
        if not meta:
            if verbose:
                print(f"  [{i}/{len(sale_specs)}] {sale_id} {slug}: meta fetch failed", flush=True)
            time.sleep(delay)
            continue

        sale_title = meta["title"] or slug.replace("-", " ").title()
        sale_date = meta["sale_date"]
        sale_city = meta["city"]
        catalog_url = BASE + f"/catalogue/{sale_id}-{slug}"

        inserted_this = 0
        cards_total = 0
        first_html = meta.pop("first_html", None)
        for page_html in iter_catalog_pages(scraper, sale_id, first_html=first_html, max_pages=20):
            cards = extract_lot_cards(page_html, sale_id)
            if not cards:
                break
            cards_total += len(cards)
            first_html = None

            for card in cards:
                total_scanned += 1
                lot_id = card["lot_id"]
                lot_url = card["lot_url"]
                hammer_text = card["hammer_text"]

                # Skip unsold lots — no price signal
                if not hammer_text:
                    continue

                hammer, hammer_cur = parse_amount(hammer_text)
                if not hammer or hammer <= 0:
                    continue

                # Early VN check from catalog short_desc — avoids fetching detail
                # page for tens of thousands of non-VN lots.
                if filter_vn:
                    short_artist = _extract_artist_from_short(card.get("short_desc", ""))
                    if not short_artist or _FAKE_PREFIX_RE.match(short_artist):
                        continue
                    if _NON_ART_TITLE_RE.match(short_artist):
                        continue
                    short_clean, _ = clean_artist_name(short_artist)
                    if not short_clean or not _is_vietnamese(short_clean, vn_catalog, exclusions):
                        continue

                # Fetch detail page for description / estimate
                detail = fetch_lot_detail(scraper, lot_url, lot_id)
                if not detail or not detail["description"]:
                    continue
                time.sleep(0.4)  # polite per-lot delay

                parsed = parse_lot_description(detail["description"])
                artist_raw = parsed["artist"]
                if not artist_raw:
                    continue
                if _FAKE_PREFIX_RE.match(artist_raw):
                    continue
                check_text = (artist_raw + " " + parsed["artwork_title"]).strip()
                if _NON_ART_TITLE_RE.match(check_text):
                    continue

                artist_clean, _alt_birth = clean_artist_name(artist_raw)
                if not artist_clean:
                    continue

                if filter_vn and not _is_vietnamese(artist_clean, vn_catalog, exclusions):
                    continue

                # Reject prints/reproductions
                blob_low = (parsed["artwork_title"] + " " + parsed["medium"] + " "
                            + detail["description"][:500]).lower()
                if re.search(r"\b(d['’]apr[èe]s|copie|reproduction|estampe|"
                             r"impression sur|tirage|lithograph|s[eé]rigraph)\b", blob_low):
                    continue

                currency = hammer_cur or detail["currency"] or "EUR"
                rec = {
                    "source": "osenat",
                    "source_url": lot_url,
                    "sale_page_url": catalog_url,
                    "lot_number": card["lot_num"] or "",
                    "auction_title": f"Osenat — {sale_title}",
                    "sale_date": sale_date,
                    "sale_location": sale_city,
                    "artist_name_raw": artist_clean,
                    "artwork_title": parsed["artwork_title"],
                    "medium": parsed["medium"],
                    "dimensions": parsed["dimensions"],
                    "year": parsed["year"],
                    "estimate_low": detail["estimate_low"],
                    "estimate_high": detail["estimate_high"],
                    "hammer_price": hammer,
                    "price_with_premium": None,  # let common derive from premium_rate_pct
                    "currency": currency,
                    "status": "sold",
                    "provenance": "",
                    # Source of truth = scoped fiche_lot_description div
                    # (fetch_lot_detail already scopes via the div regex).
                    # Older lots in DB inherited page-body noise; this
                    # writes the clean version going forward.
                    "catalog_description": detail["description"][:2000],
                    "image_url": detail.get("image_url"),
                    "raw_snapshot": detail["description"][:500],
                }
                insert_sale_result(conn, rec)
                inserted_this += 1
                if verbose:
                    print(
                        f"      +{artist_clean[:24]:24s} | "
                        f"{(parsed['artwork_title'] or '')[:32]:32s} | "
                        f"{parsed['dimensions']:14s} | "
                        f"{hammer:>10.0f} {currency}",
                        flush=True,
                    )

            time.sleep(delay)

        conn.commit()
        log_crawl_run(
            conn, "osenat", target_slug=f"{sale_id}-{slug}",
            started_at=run_started,
            lots_scanned=cards_total, lots_inserted=inserted_this,
            sale_date_min=sale_date or None, sale_date_max=sale_date or None,
            status="ok", note=sale_title[:120],
        )
        if verbose:
            print(
                f"  [{i}/{len(sale_specs)}] {sale_date or '??????????':10s} "
                f"{sale_title[:50]:50s}: {cards_total:3d} lots / {inserted_this} VN",
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
    # Quick smoke test: just the one known VN-yielding 2024 sale
    inserted, scanned = crawl(
        conn,
        sale_specs=[("155462", "les-curiosites-de-breteuil-art-dasie-afrique-et-extra", 2024)],
        max_pages=1,
    )
    print(f"\nDONE — inserted {inserted}, scanned {scanned}")
    conn.close()
