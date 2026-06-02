"""Larasati Auctioneers crawler — Singapore/Indonesia SEA art house.

Larasati's website (`https://www.larasati.com`) is a static PHP shell. The auction
listings (`media.php?module=auctions&sub=22&ss={ss}`) link to **catalog PDFs hosted
on Google Drive** — there are NO per-lot HTML pages. Catalog PDFs contain
well-structured lot blocks (lot#, ARTIST, nationality, title, medium/dims, estimate),
which pdfplumber can extract reliably; the only "Sales Result" file with hammer prices
hosted on `larasati.com/result/` (one PDF for Bali 20-Jul-2019) does not list artists.

VN content at Larasati is sparse (≈7 lots across 29 known catalogs as of 2026-06-02):
  - London 11-Nov-2020   3 lots (Đỗ Quang Em, Đặng Xuân Hoà, Lê Phổ) — GBP
  - London 08-Nov-2019   2 lots (Lê Phổ, Vũ Cao Đàm)                — GBP
  - Singapore 07-Feb-2026 2 lots (Nguyễn Thanh Bình)                — SGD

Because hammer prices are not exposed, lots are stored with `status="estimate"` and
`hammer_price = midpoint(estimate_low, estimate_high)` — same convention as
`crawlers/global_auction.py`. Currencies vary by sale city:
  - Singapore sales: SGD (primary, with US$ aside)
  - Jakarta/Bali sales: IDR (primary, with S$/US$ aside)
  - London sales: GBP (primary, with US$/IDR aside)

Lot block format (after pdfplumber extraction; case may be inconsistent due to font
glyphs — "Lê Phổ" can render as "le PHo" or "LE PHO"):
    {lot_number}              # 3 digits, e.g. "907" or "819"
    {ARTIST NAME}             # often uppercase, may be mixed-case
    ({birth} - {death}, {nationality})        or  (b. {year}, {nationality})
    {title}
    [{year};] {medium}; {W} x {H} cm
    {signature}
    {CURRENCY} {est_low} - {est_high}
    [other currency conversions]
    [Provenance: ...]

Cloudscraper isn't strictly required for larasati.com (HTTP fine) but Google Drive
direct downloads play nicest with a Chrome-like session.
"""
import io
import re
import time
import unicodedata
from datetime import datetime

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except Exception:
    import requests
    _scraper = requests.Session()

try:
    import pdfplumber
    _HAVE_PDFPLUMBER = True
except Exception:
    _HAVE_PDFPLUMBER = False

from crawlers.common import insert_sale_result, log_crawl_run


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Known past auctions (Google Drive catalog PDF IDs + sale title).
# Discovered via media.php?module=auctions&sub=22&ss=21..24 on 2026-06-02.
# Ranked roughly by date desc / known VN-content first.
SEED_SALES = [
    # ---- High-priority: known to contain VN artists ----
    ("1iPLTzS6j72VMB63B_Io2-GkFagq48tnq", "Singapore - 07 February 2026"),  # 2 VN
    ("1iibbqvq1-IlMN0sDT8exPow57Qulxa-m", "London - 11 November 2020"),     # 3 VN
    ("1AeVbolDYqw8DaJ9PG-TVw-fOLPtjhewY", "London - 08 November 2019"),     # 2 VN
    # ---- Other recent sales (no known VN yet — included for new catalogs) ----
    ("1JdiTxYJeoVf47cEaaR0fBXa9SsVrtm4z", "Singapore - 19 April 2026"),
    ("1o2eBYjr-FV2-KK5ue7yQID3zfe3E8VcB", "Singapore - 14 March 2026"),
    ("1ZHFdjdKFtegX0pE2uj6CR5B9wDVzTw7V", "Singapore - 08 November 2025"),
    ("1N2rmTin-Tty_adwQMV6zztA8rVDw1UU8", "Singapore - 04 October 2025"),
    ("1UKiTaphaa3NLOo6laAVU1KAnLoIeTI1b", "Singapore - 22 August 2025"),
    ("1LtWkgE58RY2IJ_7wNacu0T09zyyLomoo", "Singapore - 12 July 2025"),
    ("1n7ea5z1Jgxq6G_-g4ixGSg__wfgu253S", "Singapore - 01 June 2025"),
    ("1XhYo4B3_iKoVorjWExzF8ydxxF4u4TtR", "Jakarta - 17 May 2026"),
    ("10WEag9VBRd6ulG39A1dsmBPULfHLpFcF", "Jakarta - 12 December 2025"),
    ("1iD5lmOnLltnJ9fuGf9UGqdzkiz7TT7_L", "Bali - 24 January 2026"),
    ("18cDaqoZqTQgJB-oFn2RDaDYD78bqfcfK", "Bali - 21 June 2025"),
    ("1HcPtBirClCwh9DpjZWE_1Flk_IK2bzlu", "Bali - 01 June 2024"),
    ("1e9yDywJ_kic_1q5i2KQ30MYgN7ScySbQ", "Bali - 04 November 2023"),
    ("13zvTSw-7rz-8fwH_qMjV7R2o0DxXGZy4", "Jakarta - 19 March 2023"),
    ("16KUzy9jfLFi_u_jOkz8WDe3JcqnkFkWP", "London - 19 November 2022"),
    ("1VJF3B_PvxL7UNHC3GPZp3_7ftuje0kQo", "Bali - 25 September 2022"),
    ("1FhT5ANsijGqR5isJ9kKr_iUNm-qAPrB5", "Bali - 26 February 2022"),
    ("1tb-GOr2-VQwtjKWGWYcqKgHK79IAVypI", "London - 31 October 2021"),
    ("19A_WNoXuEgUMAeKVa8rt94cuwoq9VTeq", "Bali - 18 December 2021"),
    ("1uxa557GL8M5SmWlhbThSGn2l4s9iw-gw", "Bali - 26 September 2021"),
    ("13rprjVPRggVFrf33QiXky14202mhyE7f", "Jakarta - 28 January 2021"),
    ("1F5banG7NjxPlH-bEWpPQwIgvpLmgiVoo", "Jakarta - 03 October 2020"),
    ("1A4vyb7PF-SGBreAGKHiUjmM2SPpue-SD", "Jakarta - 22 August 2020"),
    ("1H-QFkeU0toAnlcn-B5y2LmfF0nSlvxNs", "Jakarta - 25 July 2020"),
    ("1IhxsEMwPBQQ6cfo38RssQDkeeXlZQsvE", "Jakarta - 20 June 2020"),
    ("1xe59qEu7u8U9qWfbAgMPQsQ6j5wcP2Kf", "London - 09 November 2018"),
]


# ---- Helpers --------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_sale_meta(title):
    """Parse a title like 'Singapore - 07 February 2026' into (city, ISO date).
    Returns (sale_location, sale_date)."""
    city = "Singapore"
    sale_date = ""
    m = re.match(r"\s*([A-Za-z]+)\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", title)
    if m:
        city = m.group(1).strip().title()
        day = int(m.group(2))
        mon = _MONTHS.get(m.group(3).strip().lower())
        year = int(m.group(4))
        if mon:
            sale_date = f"{year:04d}-{mon:02d}-{day:02d}"
    return city, sale_date


def _city_to_currency(city):
    """Primary currency by saleroom (matches catalog text). Defaults to SGD."""
    c = (city or "").lower()
    if "london" in c:
        return "GBP"
    if "jakarta" in c or "bali" in c:
        return "IDR"
    return "SGD"


def _strip_accents(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.replace("Đ", "D").replace("đ", "d")


def _norm_artist(raw):
    """Normalize an artist label from a catalog block.

    Larasati's catalogs list names in two forms:
      (a) 'FAMILY, GIVEN'  →  flip to 'GIVEN FAMILY'   (most common, all-caps)
      (b) 'GIVEN FAMILY'   →  keep as-is

    PDF text extraction often produces inconsistent case due to font kerning —
    e.g. "le PHo" instead of "LE PHO", "vU Cao dam" for "VU CAO DAM". We
    therefore uppercase every artist label and then Title Case it. This is
    safe because Larasati only ever prints artist names in ALL CAPS in the
    catalog — any mixed case is a PDF extraction artefact.
    """
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", raw).strip(" ,.—-")
    # Drop trailing page-number noise: e.g. "Adrien Jean Le Mayeur De"
    s = re.sub(r"\s+\d{1,4}\s*$", "", s)
    # Flip "FAMILY, GIVEN" → "GIVEN FAMILY"
    if "," in s and s.count(",") == 1:
        a, b = [p.strip() for p in s.split(",", 1)]
        if a and b:
            s = f"{b} {a}"
    # Normalize case: uppercase then title-case — kills PDF kerning artefacts
    s = s.upper()
    s = s.title()
    # Re-uppercase Roman-numeral / initial particles that title() mangles
    s = re.sub(r"\bIi\b", "II", s)
    s = re.sub(r"\bIii\b", "III", s)
    return s


def _download_catalog(gd_id, timeout=180):
    """Download a Google Drive PDF by file ID. Returns bytes or None."""
    url = f"https://drive.google.com/uc?export=download&id={gd_id}"
    try:
        r = _scraper.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200 or len(r.content) < 50_000:
        return None
    # Sanity check: PDFs start with %PDF
    if not r.content[:4] == b"%PDF":
        return None
    return r.content


def _extract_pdf_text(pdf_bytes):
    """Return concatenated text from every page, with explicit page breaks."""
    if not _HAVE_PDFPLUMBER:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
        return "\n\f\n".join(pages)
    except Exception:
        return ""


# ---- Lot-block parser ------------------------------------------------------

# Match every "({yyyy} - {yyyy}, NATIONALITY)" or "(b. yyyy, NATIONALITY)" tag.
# This is the most reliable anchor for a lot block in Larasati catalogs.
_NAT_TAG_RE = re.compile(
    r"\(\s*"
    r"(?:b\.\s*(?P<by>\d{4})|"
    r"(?P<birth>\d{4})\s*-\s*(?P<death>\d{4}))"
    r"\s*,\s*(?P<nat>[A-Za-z][A-Za-z \-/]+)\s*\)",
    re.IGNORECASE,
)

# Dimension lines come in many forms. We tolerate "W x H cm" / "W × H cm".
_DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|×)\s*(\d+(?:\.\d+)?)\s*cm", re.IGNORECASE)

# Medium tokens we care about — used to flag a "title; medium; dims" line.
_MEDIUM_KWS = (
    "oil on", "ink on", "watercolour on", "watercolor on", "gouache on",
    "lacquer on", "tempera on", "ink and", "pencil on", "pastel on",
    "acrylic on", "mixed media", "charcoal on",
    "woodcut", "lithograph", "etching", "silkscreen",
    "print on silk", "print on paper",
)

# Currency aliases inside catalog estimate lines.
_CUR_TOKENS = (
    ("SGD", "SGD"), ("S$", "SGD"),
    ("US$", "USD"), ("USD", "USD"),
    ("£", "GBP"), ("GBP", "GBP"),
    ("Rp", "IDR"), ("IDR", "IDR"),
    ("HK$", "HKD"), ("HKD", "HKD"),
    ("€", "EUR"), ("EUR", "EUR"),
)


def _parse_est_amount(num_str):
    """'1,200' / '54.040.000' / '2 826' → float. Returns None on fail."""
    if not num_str:
        return None
    s = num_str.strip()
    # IDR style: 54.040.000  → drop periods
    if s.count(".") >= 2 and "," not in s:
        s = s.replace(".", "")
    # Western: 1,200 or 1,200.50
    s = s.replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _extract_estimate(block_text, primary_currency):
    """Find best primary estimate within the lot block.
    Priority: primary_currency-matching token > any token.
    Returns (low, high, currency_used)."""
    candidates = []
    for token, code in _CUR_TOKENS:
        for m in re.finditer(
            re.escape(token) + r"\s*([\d.,\s]+?)\s*[-–]\s*([\d.,\s]+)",
            block_text,
        ):
            low = _parse_est_amount(m.group(1))
            high = _parse_est_amount(m.group(2))
            if low and high and high >= low and low >= 100:
                candidates.append((code, low, high, m.start()))
    if not candidates:
        return None, None, None
    # Prefer primary currency, then earliest in block (closest to top of lot)
    primary = [c for c in candidates if c[0] == primary_currency]
    pick = primary[0] if primary else candidates[0]
    return pick[1], pick[2], pick[0]


def _extract_lot_block(text, tag_start, tag_end, next_tag_start):
    """Given the text and the positions of the current nationality tag and the next,
    walk backward to find lot # + artist line, and forward to gather metadata.

    Real lot blocks have this exact shape (top→bottom):
        {lot_number}            ← 3-digit number on its own line
        {ARTIST NAME}           ← uppercase-dominant
        ({...nationality})      ← the tag we just matched
    Essays/biographies also contain "(b. YYYY, nationality)" tags mid-paragraph,
    but they lack the 3-digit-only line above. We use that as our gate.

    Returns dict or None if the structure doesn't look like a lot.
    """
    # --- Walk backward from tag_start to find lot # and artist name ---
    pre = text[max(0, tag_start - 300):tag_start]
    pre_lines = [ln.strip() for ln in pre.split("\n") if ln.strip()]
    if len(pre_lines) < 2:
        return None

    # Required: a lone 3-digit lot# line within the last 4 lines before the tag.
    lot_number = ""
    lot_idx = -1
    for k in range(1, min(5, len(pre_lines) + 1)):
        ln = pre_lines[-k]
        if re.fullmatch(r"\d{3}", ln):
            lot_number = ln
            lot_idx = len(pre_lines) - k
            break
    if not lot_number:
        return None

    # Artist line = first non-empty line AFTER the lot# line (typically just below).
    # If the artist label wrapped to two lines (e.g. "VU CAO" / "DAM"), accept up
    # to two lines between the lot# and the tag.
    artist_parts = pre_lines[lot_idx + 1:]
    if not artist_parts:
        return None
    # Skip lines that are pure numbers (page footer noise) between artist and tag
    artist_parts = [p for p in artist_parts if not re.fullmatch(r"\d{1,4}", p)]
    if not artist_parts:
        return None
    artist_raw = " ".join(artist_parts[:2])  # cap at 2 lines to avoid noise

    # --- Look ahead for title / medium / dims / estimate ---
    block_end = next_tag_start if next_tag_start > tag_end else min(len(text), tag_end + 1200)
    forward = text[tag_end:block_end]
    fwd_lines = [ln.strip() for ln in forward.split("\n") if ln.strip()]

    title = ""
    medium = ""
    dimensions = ""
    year = ""
    # Skip page-footer numbers like "18" sitting between lots
    cleaned = [ln for ln in fwd_lines if not re.fullmatch(r"\d{1,4}", ln)]
    # First non-noise line that doesn't contain a price is the title
    for ln in cleaned[:6]:
        low = ln.lower()
        if any(c in low for c in ("s$", "us$", "rp", "£", "hk$", "sgd", "idr", "gbp")):
            break
        if any(kw in low for kw in _MEDIUM_KWS) or _DIM_RE.search(ln):
            break
        if not title:
            title = ln
        else:
            # Allow two-line titles (rare); concatenate
            title = (title + " " + ln).strip()

    # Title commonly has trailing junk like "1122" or "20" (page numbers OCR'd in).
    title = re.sub(r"\s+\d{1,4}\s*$", "", title).strip()

    # Pull medium/dims/year from any of the next ~10 lines
    for ln in cleaned[:10]:
        low = ln.lower()
        if not medium and any(kw in low for kw in _MEDIUM_KWS):
            medium = ln[:220]
        if not dimensions:
            md = _DIM_RE.search(ln)
            if md:
                dimensions = f"{md.group(1)} x {md.group(2)} cm"
        if not year:
            my = re.search(r"\b(?:painted in|executed in|circa|c\.)\s*(\d{4})", ln, re.IGNORECASE)
            if my:
                year = my.group(1)
            else:
                # "1964; watercolour on paper" — year prefix
                my2 = re.match(r"^(\d{4})\s*;\s*", ln)
                if my2:
                    year = my2.group(1)

    return {
        "lot_number": lot_number,
        "artist_name_raw": _norm_artist(artist_raw),
        "title": title,
        "medium": medium,
        "dimensions": dimensions,
        "year": year,
        "block_text": text[tag_start:block_end],
    }


def parse_catalog(text, sale_meta):
    """Parse all lot blocks in a catalog text. Returns list of records ready
    for `insert_sale_result` (with `hammer_price=midpoint, status='estimate'`)."""
    city = sale_meta["sale_location"]
    primary_cur = _city_to_currency(city)
    sale_date = sale_meta["sale_date"]
    auction_title = sale_meta["auction_title"]
    sale_page_url = sale_meta["sale_page_url"]
    source_url = sale_meta.get("source_url", sale_page_url)

    tags = list(_NAT_TAG_RE.finditer(text))
    records = []
    for i, t in enumerate(tags):
        nat = (t.group("nat") or "").strip().lower()
        next_start = tags[i + 1].start() if i + 1 < len(tags) else len(text)
        parsed = _extract_lot_block(text, t.start(), t.end(), next_start)
        if not parsed or not parsed["artist_name_raw"]:
            continue
        low, high, est_cur = _extract_estimate(parsed["block_text"], primary_cur)
        if not low or not high:
            continue
        currency = est_cur or primary_cur
        hammer = round((low + high) / 2, 2)  # estimate midpoint (no public hammer)
        # sale_results.source_url is UNIQUE — anchor per-lot with #lot=NNN so multiple
        # lots from the same catalog don't collide-overwrite each other.
        lot_source_url = f"{source_url}#lot={parsed['lot_number']}"

        rec = {
            "source": "larasati",
            "source_url": lot_source_url,
            "sale_page_url": sale_page_url,
            "lot_number": parsed["lot_number"],
            "auction_title": auction_title,
            "sale_date": sale_date,
            "sale_location": city,
            "artist_name_raw": parsed["artist_name_raw"],
            "artwork_title": parsed["title"][:300],
            "medium": parsed["medium"],
            "dimensions": parsed["dimensions"],
            "year": parsed["year"],
            "estimate_low": low,
            "estimate_high": high,
            "hammer_price": hammer,
            "price_with_premium": None,
            "currency": currency,
            "status": "estimate",          # midpoint, not real hammer
            "provenance": "",
            "raw_snapshot": (
                f"{parsed['artist_name_raw']} ({nat}) | {parsed['title'][:120]}"
            )[:300],
            "_nationality": nat,
        }
        records.append(rec)
    return records


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


def _is_vietnamese(artist, nationality, vn_catalog, exclusions):
    """An artist counts as VN if the nationality tag says 'Vietnamese' OR
    the normalized name is in the VN catalog (and not in NON_VN_EXCLUSIONS)."""
    from artonis_price_mvp import normalize_key
    nat = (nationality or "").lower()
    if "vietnamese" in nat or "vietnam" in nat:
        # Trust catalog nationality tag — but still respect exclusions
        key = normalize_key(artist)
        if key in exclusions:
            return False
        return True
    key = normalize_key(artist)
    if not key or key in exclusions:
        return False
    if key in vn_catalog:
        return True
    for k in vn_catalog:
        if key == k or key.startswith(k + " ") or k.startswith(key + " "):
            return True
    return False


# ---- Public entry ----------------------------------------------------------

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Larasati catalog PDFs and extract lots.

    Args:
      conn: sqlite3 connection (row_factory set by caller).
      sale_urls: list of Google Drive PDF URLs OR list of (gd_file_id, title)
        tuples. Defaults to SEED_SALES.
      delay: seconds between catalog downloads.
      verbose: print progress.
      filter_vn: drop non-Vietnamese lots.
      max_pages: hard cap on number of catalogs visited per run.

    Returns:
      (total_inserted, total_scanned) — both ints.
    """
    if sale_urls is None:
        sales = SEED_SALES[:max_pages]
    else:
        sales = []
        for entry in sale_urls[:max_pages]:
            if isinstance(entry, tuple) and len(entry) == 2:
                sales.append(entry)
                continue
            url = entry
            m = re.search(r"/d/([A-Za-z0-9_\-]+)", url) or re.search(r"id=([A-Za-z0-9_\-]+)", url)
            if not m:
                if verbose:
                    print(f"  WARN: can't parse Drive ID from {url}")
                continue
            sales.append((m.group(1), url))

    if not _HAVE_PDFPLUMBER:
        if verbose:
            print("  ERROR: pdfplumber not installed — `pip install pdfplumber`")
        return 0, 0

    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    total_inserted = 0
    total_scanned = 0
    for gd_id, title in sales:
        run_started = datetime.utcnow().isoformat() + "Z"
        sale_page_url = f"https://www.larasati.com/?module=auctions&sub=22"  # human-readable index
        source_url = f"https://drive.google.com/file/d/{gd_id}/view"
        city, sale_date = _parse_sale_meta(title)
        auction_title = f"Larasati — {title}"

        if verbose:
            print(f"  [{title}] fetching catalog…", flush=True)
        pdf_bytes = _download_catalog(gd_id)
        if not pdf_bytes:
            log_crawl_run(conn, "larasati", target_slug=gd_id, started_at=run_started,
                          status="error", note=f"download failed: {title}"[:200])
            if verbose:
                print(f"    SKIP: download failed")
            continue

        text = _extract_pdf_text(pdf_bytes)
        if not text:
            log_crawl_run(conn, "larasati", target_slug=gd_id, started_at=run_started,
                          status="error", note=f"PDF text extraction failed: {title}"[:200])
            if verbose:
                print(f"    SKIP: PDF parse failed")
            continue

        sale_meta = {
            "sale_location": city,
            "sale_date": sale_date,
            "auction_title": auction_title,
            "sale_page_url": sale_page_url,
            "source_url": source_url,
        }
        records = parse_catalog(text, sale_meta)
        total_scanned += len(records)

        inserted = 0
        for rec in records:
            nat = rec.pop("_nationality", "")
            if filter_vn and not _is_vietnamese(
                rec["artist_name_raw"], nat, vn_catalog, exclusions
            ):
                continue
            # Skip prints / explicit copies
            blob = (rec["artwork_title"] + " " + (rec.get("medium") or "")).lower()
            if re.search(r"\b(d'?apr[eè]s|after\s+(le\s+pho|mai|vu\s+cao)|copy of|reproduction|copie)\b", blob):
                continue
            insert_sale_result(conn, rec)
            inserted += 1
        conn.commit()
        log_crawl_run(
            conn, "larasati", target_slug=gd_id, started_at=run_started,
            lots_scanned=len(records), lots_inserted=inserted,
            sale_date_min=sale_date or None, sale_date_max=sale_date or None,
            status="ok", note=title[:120],
        )
        if verbose:
            print(f"    {len(records)} lots parsed → {inserted} inserted (VN={filter_vn})", flush=True)
        total_inserted += inserted
        time.sleep(delay)

    if verbose:
        print(f"\n  Larasati total: inserted={total_inserted}, scanned={total_scanned}")
    return total_inserted, total_scanned
