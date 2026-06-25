"""Aguttes crawler — Paris auction house with dedicated 'Peintres d'Asie' Vietnam-art series.

Discovery: /api/artisio/get-all-auctions returns past auctions (use status=completed).
Lots:      /api/artisio/get-lots?auction_uuid=... returns full lot details with hammer_price.
Auth:      The API requires same-origin requests, so we use Playwright to issue calls
           from inside a loaded aguttes.com page context.
"""
import re
import time
import json
import sys
import html as _html_lib
from pathlib import Path

import requests

from crawlers.common import insert_sale_result, clean_text, clean_artist_name, log_crawl_run


_LOT_PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
}


_ARTIST_HEADER_RE = re.compile(r"^[^\d(]{2,80}\([^)]*\b(?:1[89]|20)\d{2}\b[^)]*\)?\s*$")
_MEDIUM_KWS_FR = ("huile", "aquarelle", "encre", "laque", "gouache", "pastel", "fusain",
                   "sanguine", "crayon", "soie", "acrylique", "mine de plomb", "lithographie",
                   "estampe", "bronze", "sculpture", "fixé sous verre", "tempera", "technique mixte")


def _strip_html_lib(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_lib.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def _fetch_lot_page_fields(lot_url):
    """Fetch an Aguttes lot detail HTML and parse __NEXT_DATA__.dynamic_fields.fr.
    Returns (artwork_title, provenance, expertise).

    Title sources, in priority:
      1. dynamic_fields.fr.sub_title (clean HTML <p>title</p>)
      2. description's line-after-artist-header (handles 'ARTIST (né en YYYY)' format)
    """
    try:
        r = requests.get(lot_url, headers=_LOT_PAGE_HEADERS, timeout=20)
    except Exception:
        return "", "", ""
    if r.status_code != 200:
        return "", "", ""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.DOTALL)
    if not m:
        return "", "", ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return "", "", ""
    df = data.get("props", {}).get("pageProps", {}).get("lot", {}).get("dynamic_fields", {}) or {}
    fr = df.get("fr", {}) if isinstance(df, dict) else {}

    sub_title = _strip_html_lib(fr.get("sub_title") or "")
    title = sub_title if (sub_title and 2 < len(sub_title) < 200) else ""

    # If sub_title is empty, parse description text for line-after-artist-header
    if not title:
        desc_plain = _strip_html_lib(fr.get("description") or "")
        lines = [l.strip() for l in desc_plain.split("\n") if l.strip()]
        for idx, line in enumerate(lines):
            if _ARTIST_HEADER_RE.match(line):
                for cand in lines[idx + 1: idx + 4]:
                    cand = cand.strip(" *,;:.")
                    if not cand or len(cand) < 2 or len(cand) > 200:
                        continue
                    low = cand.lower()
                    if any(kw in low for kw in _MEDIUM_KWS_FR):
                        continue
                    if re.search(r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*cm", cand, re.IGNORECASE):
                        continue
                    if re.match(r"^(sign|certificat|provenance|exposition|bibliographie|cette\s+oeuvre|un\s+rapport)",
                                cand, re.IGNORECASE):
                        continue
                    title = cand[:200]
                    break
                break

    # Provenance: dynamic_fields.fr.provenance OR "PROVENANCE" block in description
    provenance = _strip_html_lib(fr.get("provenance") or "")
    if not provenance:
        desc_plain = _strip_html_lib(fr.get("description") or "")
        m_p = re.search(r"PROVENANCE\s*\n?(.+?)(?:\n\s*(?:EXPOSITION|BIBLIOGRAPHIE|LITERATURE)|$)",
                        desc_plain, re.IGNORECASE | re.DOTALL)
        if m_p:
            provenance = m_p.group(1).strip()
    expertise = _strip_html_lib(fr.get("expertise_information") or "")
    return title, provenance[:2000], expertise[:2000]


# ---- helpers ----------------------------------------------------------------

def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def _i18n(field, lang="fr"):
    """Aguttes API returns multi-locale objects: {fr: '…', en: '…'}."""
    if isinstance(field, dict):
        return field.get(lang) or field.get("en") or next(iter(field.values()), "")
    return field or ""


def _parse_artist_and_years(title_html, description_html=None):
    """Title HTML is like '<p>Lê Phổ&nbsp;(1907 - 2001)</p>'. Returns (artist, b, d).
    Vietnamese names use diacritics (đ, ổ, ấ, ữ, ...) outside Latin-1 — accept any non-ASCII letter.
    Also tolerates Aguttes typos like 'ALIX AYME ( (1894 - 1989)' (double-open-paren).

    When the paren-year regex misses AND a description is supplied, fall
    back to Claude Haiku via crawlers.llm_parser.llm_artist_fallback.
    Same pattern as crawlers/drouot.py — see SPEC §10 LLM fallback layer.
    """
    plain = _strip_html(title_html)
    # Normalise extra whitespace + collapse "( (" → "("
    plain = re.sub(r"\(\s+\(", "(", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    # Char class: any letter (incl. CJK/diacritics), space, hyphen, dot, apostrophe
    m = re.match(r"([^\d()]{2,80}?)\s*\(\s*(\d{4})\s*[-–]?\s*(\d{4})?\s*\)", plain)
    if m:
        artist = clean_text(m.group(1))
        # Strip leading "Entourage de", "Atelier de", "École de", "Attribué à", "D'après"
        artist = re.sub(r"^(entourage de|atelier de|école de|attribué à|d'?après|after)\s+",
                        "", artist, flags=re.IGNORECASE).strip()
        return artist, int(m.group(2)), int(m.group(3)) if m.group(3) else None
    # No paren-year — try LLM fallback if description is available.
    # Aguttes occasionally publishes titles in free-form prose (no paren)
    # for new artists; LLM extracts artist + birth/death from the rich
    # bilingual catalog description.
    if description_html:
        try:
            from crawlers.llm_parser import llm_artist_fallback
            desc_plain = _strip_html(description_html)
            a, _t, by, dy = llm_artist_fallback(desc_plain, raw_title=plain)
            if a:
                return a, by, dy
        except Exception:
            pass
    return clean_text(plain), None, None


# Patterns that indicate this is NOT an authentic single-artist artwork
_FAKE_PREFIX_RE = re.compile(
    r"^(entourage de|atelier de|école de|attribué à|d'?après|after\s+|circle of|follower of|"
    r"manner of|style of|copy after|cours de|reproduction|copie)\s",
    re.IGNORECASE,
)
_NON_ART_TITLE_RE = re.compile(
    r"^(ordre|décoration|décret|monnaie|coin|médaille|medal|imperial seal|sceau impérial|"
    r"vase|bol|coupe|tasse|plat|jarre|brûle\-?parfum|vietnam,?\s*xviii|vietnam,?\s*xix|"
    r"vietnam,?\s*xx)",
    re.IGNORECASE,
)


_MEDIUM_HINTS = (
    "huile", "aquarelle", "encre", "laque", "gouache", "pastel", "fusain",
    "sanguine", "crayon", "soie", "acrylique", "mine de plomb", "lithographie",
    "estampe", "bronze", "sculpture",
)


def _looks_like_medium(line):
    return any(kw in line.lower() for kw in _MEDIUM_HINTS)


def _looks_like_dimensions(line):
    return bool(re.search(r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*cm", line, re.IGNORECASE))


def _parse_artwork_title(description_html, artist_header_text=""):
    """Aguttes embeds the artwork title in the description right AFTER the artist header.
    Format examples:
      'ALIX AYMÉ (1894 - 1989) Grande maternité Encre et couleurs sur soie...'
      'ALIX AYMÉ (1894 - 1989) * Maternité Crayon sur papier...'
      '<p>Artiste header</p><p>* Maternité, circa 1960</p><p>Encre...</p>'

    Strategy:
      1. Italic <i>...</i> wins
      2. «…» quote wins
      3. Strip artist header from description, then take first line that's NOT medium/dim
    """
    raw = description_html or ""
    m = re.search(r"<i[^>]*>([^<]+)</i>", raw)
    if m:
        return clean_text(m.group(1))
    plain = _strip_html(raw)
    m_q = re.search(r"[«\"“]([^»\"”]{2,120})[»\"”]", plain)
    if m_q:
        return clean_text(m_q.group(1))

    # Strip the leading artist header so what remains is medium/title/dims/prose
    stripped = plain
    if artist_header_text:
        # Build a flexible regex matching the artist line (escape diacritics)
        ah = re.escape(artist_header_text.strip())
        stripped = re.sub(r"^\s*" + ah + r"\s*", "", stripped).strip()
    # Also drop any leading "ARTIST (years)" header that might still be present
    stripped = re.sub(r"^[^()]{2,60}\(\s*\d{4}\s*[-–]?\s*\d{0,4}\s*\)\s*", "", stripped).strip()

    # Walk through lines (split on <p>/<br> already handled by _strip_html newlines)
    lines = [l.strip().lstrip("* ").strip() for l in stripped.split("\n") if l.strip()]
    if not lines:
        # Fallback: take first chunk from <p> tag
        first_p = re.search(r"<p[^>]*>([^<]+)</p>", raw)
        if first_p:
            cand = clean_text(first_p.group(1))
            cand = re.sub(r"^\*+\s*", "", cand)
            if cand and not _looks_like_medium(cand) and not _looks_like_dimensions(cand):
                return cand[:140]
        # Single-line description: split on first medium keyword
        for kw in _MEDIUM_HINTS:
            idx = stripped.lower().find(kw)
            if idx > 4:
                cand = stripped[:idx].strip().strip(" *,")
                cand = re.sub(r"^\*+\s*", "", cand)
                if cand and 2 < len(cand) < 140:
                    return cand
        return ""

    for cand in lines[:3]:
        if not cand or len(cand) < 2 or len(cand) > 200:
            continue
        if _looks_like_medium(cand):
            # If a medium keyword appears at the START, this is a pure medium line
            # (e.g. "Encre et couleurs sur soie") — skip, don't extract a fake title.
            low = cand.lower()
            earliest = min((low.find(kw) for kw in _MEDIUM_HINTS if low.find(kw) >= 0),
                           default=-1)
            if earliest <= 4:
                continue
            # Title appears BEFORE the medium phrase (e.g. "Femme du peuple Huile sur toile")
            pre = cand[:earliest].strip(" *,")
            if pre and 2 < len(pre) < 140:
                return pre
            continue
        if _looks_like_dimensions(cand):
            continue
        if re.match(r"^(sign[eé]|provenance|exposition|bibliographie|cette\s+oeuvre)",
                    cand, re.IGNORECASE):
            continue
        return cand[:140]
    return ""


# Note: dimension + medium extraction moved to crawlers/parsers/ shared
# module (SPEC §10).  The old _parse_dimensions / _parse_medium helpers
# are kept as thin wrappers for tests and any caller that imports them.
def _parse_dimensions(description_html):
    """Legacy wrapper — prefer parsers.parse_dim() which returns
    (width_cm, height_cm, area_m2, dim_str)."""
    from crawlers.parsers import parse_dim
    plain = _strip_html(description_html)
    _, _, _, dim_str = parse_dim(plain, source="aguttes")
    return dim_str


def _parse_medium(description_html):
    """Legacy wrapper — prefer parsers.extract_medium()."""
    from crawlers.parsers import extract_medium
    plain = _strip_html(description_html)
    med = extract_medium(plain)
    if med:
        return med
    # Aguttes-specific French keyword fallback for terms not in the shared list.
    m = re.search(
        r"((?:Huile|Aquarelle|Encre|Laque|Gouache|Pastel|Fusain|Sanguine|"
        r"Crayon|Soie|Acrylique|Mine\s+de\s+plomb)[^.\n,]{0,80})",
        plain, re.IGNORECASE,
    )
    if m:
        return clean_text(m.group(1))[:120]
    return ""


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


# ---- API access via Playwright (same-origin) --------------------------------

def _open_session():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    )
    page = ctx.new_page()
    page.goto("https://www.aguttes.com/ventes/ventes-passees", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    return pw, browser, page


def _api_get(page, path):
    """Call an Aguttes /api/* endpoint from inside the page (same-origin)."""
    return page.evaluate(
        f"async () => {{ const r = await fetch({path!r}); return await r.json(); }}"
    )


def discover_vn_auctions(page, status="completed"):
    """Return all Aguttes past auctions (paginated by year-range to avoid offset bugs).
    Filters to auctions whose title mentions Peintres d'Asie / Vietnam / Indochine."""
    all_auctions = {}
    ranges = [
        ("2026-01-01", "2026-12-31"),
        ("2025-01-01", "2025-12-31"),
        ("2024-01-01", "2024-12-31"),
        ("2023-01-01", "2023-12-31"),
        ("2022-01-01", "2022-12-31"),
        ("2021-01-01", "2021-12-31"),
        ("2020-01-01", "2020-12-31"),
        ("2019-01-01", "2019-12-31"),
        ("2018-01-01", "2018-12-31"),
        ("2017-01-01", "2017-12-31"),
        ("2010-01-01", "2016-12-31"),
    ]
    for da, db in ranges:
        path = (
            f"/api/artisio/get-all-auctions?ordering=-start_date&is_private=false"
            f"&date_after={da}&date_before={db}&limit=200&status={status}"
        )
        result = _api_get(page, path)
        for a in (result.get("results") or []):
            all_auctions[a["uuid"]] = a
    vn = []
    for a in all_auctions.values():
        title_str = json.dumps(a.get("title", {}), ensure_ascii=False).lower()
        if any(k in title_str for k in ["peintres d", "vietnam", "indochin"]):
            vn.append(a)
    return vn


def fetch_lots(page, auction_uuid):
    """Return all lots for an auction. The API caps per request — paginate via offset."""
    all_lots = []
    for offset in range(0, 2000, 200):
        path = f"/api/artisio/get-lots?limit=200&offset={offset}&is_private=false&auction_uuid={auction_uuid}"
        result = _api_get(page, path)
        chunk = result.get("results") or []
        if not chunk:
            break
        all_lots.extend(chunk)
        if len(chunk) < 200:
            break
    return all_lots


# ---- main crawl entry -------------------------------------------------------

def crawl(conn, verbose=True, filter_vn=True):
    """Crawl Aguttes Peintres d'Asie + Vietnam auctions, insert VN lots."""
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    pw, browser, page = _open_session()
    try:
        if verbose:
            print("  [aguttes] discovering VN/Peintres d'Asie auctions…", flush=True)
        auctions = discover_vn_auctions(page)
        if verbose:
            print(f"  [aguttes] found {len(auctions)} VN-themed auctions", flush=True)

        total_inserted = 0
        for i, a in enumerate(auctions, 1):
            auction_uuid = a["uuid"]
            run_started = __import__("datetime").datetime.utcnow().isoformat() + "Z"
            sale_date = (a.get("start_date") or "")[:10]
            sale_title_fr = _i18n(a.get("title"), "fr")
            full_title = f"Aguttes — {sale_title_fr}"
            sale_page_url = f"https://www.aguttes.com/catalogue/{auction_uuid}"
            cur_field = a.get("currency") or "EUR"
            currency = _i18n(cur_field, "fr") if isinstance(cur_field, dict) else cur_field
            if not currency or len(currency) > 4:
                currency = "EUR"

            try:
                lots = fetch_lots(page, auction_uuid)
            except Exception as e:
                if verbose:
                    print(f"  [{i}/{len(auctions)}] {sale_title_fr[:50]}: ERR {e}", flush=True)
                log_crawl_run(conn, "aguttes", target_slug=auction_uuid,
                              started_at=run_started, status="error", note=str(e)[:200])
                continue

            inserted = 0
            for lot in lots:
                if lot.get("status") != "sold" and not lot.get("hammer_price"):
                    continue
                hammer = lot.get("hammer_price")
                try:
                    hammer_f = float(hammer)
                except (TypeError, ValueError):
                    hammer_f = 0.0
                if hammer_f <= 0:
                    continue

                title_html = lot.get("title", {})
                desc_html = lot.get("description", {})
                title_fr = _i18n(title_html, "fr")
                desc_fr = _i18n(desc_html, "fr")

                # Reject non-art lots (medals, decorations, antique vases without named artist)
                title_plain = _strip_html(title_fr)
                if _NON_ART_TITLE_RE.match(title_plain):
                    continue
                # Reject "Entourage de / Atelier de / D'après" — these are not authentic works
                if _FAKE_PREFIX_RE.match(title_plain):
                    continue

                artist, b_yr, d_yr = _parse_artist_and_years(title_fr, description_html=desc_fr)
                if not artist:
                    continue
                # Strip variant suffixes (Né en, XXe, *, slash-years) so future inserts merge
                artist, alt_birth = clean_artist_name(artist)
                if alt_birth and not b_yr:
                    b_yr = alt_birth
                if not artist:
                    continue
                if filter_vn and not _is_vietnamese(artist, vn_catalog, exclusions):
                    continue

                artwork_title = _parse_artwork_title(desc_fr, _strip_html(title_fr))
                # Use shared parsers (SPEC §10) — get (w,h,area,dim_str) not just string
                from crawlers.parsers import parse_dim, extract_medium
                desc_plain = _strip_html(desc_fr)
                width_cm, height_cm, area_m2, dimensions = parse_dim(desc_plain, source="aguttes")
                medium = extract_medium(desc_plain)
                # Aguttes-specific French keyword fallback when shared list misses
                if not medium:
                    m_med = re.search(
                        r"((?:Huile|Aquarelle|Encre|Laque|Gouache|Pastel|Fusain|Sanguine|"
                        r"Crayon|Soie|Acrylique|Mine\s+de\s+plomb)[^.\n,]{0,80})",
                        desc_plain, re.IGNORECASE,
                    )
                    if m_med:
                        medium = clean_text(m_med.group(1))[:120]
                # Fake/copy filter — universal gate also catches this in
                # common.py:insert_sale_result, but rejecting here saves
                # the artist upsert + downstream processing.
                check_text = (artwork_title + " " + artist + " " + desc_plain[:200]).lower()
                if re.search(r"\b(d'?apr[eè]s|copy|copie|reproduction|estampe|print|lithograph)\b", check_text):
                    continue

                lot_url = f"https://www.aguttes.com/lot/catalogue-{auction_uuid}/{lot['uuid']}"

                # Enrich from lot detail page: dynamic_fields.fr has clean sub_title + provenance.
                # API list-call gives only artist header in `title`; lot page is the source of truth.
                # ALWAYS prefer page_title (canonical sub_title) over desc-parsed title — desc
                # parsing sometimes yields fragments of the medium line (e.g. "Encre et couleurs sur").
                page_title, page_provenance, _expertise = _fetch_lot_page_fields(lot_url)
                if page_title:
                    artwork_title = page_title

                # Try to extract creation year from sub_title or description
                year_str = ""
                m_y = re.search(r"(?:circa|vers|c\.)\s*(\d{4})", (page_title + " " + desc_fr), re.IGNORECASE)
                if m_y:
                    year_str = m_y.group(1)
                else:
                    m_y2 = re.search(r"\b(19\d{2}|20[0-2]\d)\b", page_title)
                    if m_y2:
                        year_str = m_y2.group(1)

                # Strip bilingual block from provenance (defensive; Aguttes
                # is FR-only so usually no-op, but shared utility is cheap).
                from crawlers.parsers import strip_bilingual
                page_provenance = strip_bilingual(page_provenance)

                rec = {
                    "source": "aguttes",
                    "source_url": lot_url,
                    "sale_page_url": sale_page_url,
                    "lot_number": str(lot.get("lot_no") or ""),
                    "auction_title": full_title,
                    "sale_date": sale_date,
                    "sale_location": "Paris",
                    "artist_name_raw": artist,
                    "artwork_title": artwork_title,
                    "medium": medium,
                    "dimensions": dimensions,
                    "width_cm": width_cm,
                    "height_cm": height_cm,
                    "area_m2": area_m2,
                    "catalog_description": desc_plain[:2000],
                    "year": year_str,
                    "estimate_low": float(lot["low"]) if lot.get("low") else None,
                    "estimate_high": float(lot["high"]) if lot.get("high") else None,
                    "hammer_price": hammer_f,
                    "price_with_premium": None,
                    "currency": currency,
                    "status": "sold",
                    "provenance": page_provenance,
                    "raw_snapshot": (title_fr + " | " + _strip_html(desc_fr)[:300])[:500],
                }
                # Pace lot-page fetches to avoid rate-limiting Aguttes
                time.sleep(0.3)
                insert_sale_result(conn, rec)
                inserted += 1
            conn.commit()
            log_crawl_run(conn, "aguttes", target_slug=auction_uuid, started_at=run_started,
                          lots_scanned=len(lots), lots_inserted=inserted,
                          sale_date_min=sale_date, sale_date_max=sale_date,
                          status="ok", note=sale_title_fr[:120])
            if verbose:
                print(f"  [{i}/{len(auctions)}] {sale_date} {sale_title_fr[:55]:55s}: {len(lots)} lots / {inserted} VN inserted", flush=True)
            total_inserted += inserted
            time.sleep(0.4)
        return total_inserted
    finally:
        browser.close()
        pw.stop()
