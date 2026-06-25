"""Drouot crawler — Paris-based aggregator covering 1000+ regional French auction houses.

Discovery: paginate /en/c/43/asian-art?page=N to enumerate Asian-art sales
           (cat 43). Each sale URL is /en/v/{saleId}-{slug}.
Lots:      Sale-detail HTML embeds a SvelteKit data island with `data:{lots:[{…}, …]}`
           — JS literal (not strict JSON) with unquoted keys and `void 0`. Parsed via
           targeted regex per field. Each lot's `result` field holds the hammer price
           (0 if not yet sold / upcoming sale).
Aggregation: Drouot aggregates many houses (Aguttes, Cornette, Millon, Tessier…).
           We skip lots whose `auctioneerSlug` matches a house that ships its own
           dedicated Artonis crawler to avoid duplicate rows.
Auth:      Cloudscraper handles Cloudflare; no Playwright required.
"""
import re
import sys
import time
import json
import html as _html
from pathlib import Path
from datetime import datetime, timezone

import cloudscraper

from crawlers.common import (
    insert_sale_result, clean_text, clean_artist_name, log_crawl_run,
)


BASE = "https://drouot.com"

# Attribution-prefix detection — these lots are NOT original works by the
# named artist (workshop pieces, after-prints, attributed, etc.). Skip them
# so they don't appear under the artist's profile.
_ATTRIBUTION_RE = re.compile(
    r"(?:^|-)("
    r"after|d-apres|attribue|attribue-a|et-son-atelier|et-atelier|"
    r"atelier-de|ecole-de|entourage-de|cours-de|cercle-de|"
    r"circle-of|follower-of|school-of|manner-of|in-the-manner-of"
    r")(?:-|$)",
    re.IGNORECASE,
)
_ATTRIBUTION_RAW_RE = re.compile(
    r"^\s*("
    r"after\s+|d['’']apr[èe]s|attribu[eé]\s+[àa]|attribu(?:ted|é)\s+to|"
    r"atelier\s+de\s+|école\s+de\s+|ecole\s+de\s+|entourage\s+de\s+|"
    r"cercle\s+de\s+|cours\s+de\s+|"
    r"circle\s+of\s+|follower\s+of\s+|school\s+of\s+|"
    r"in\s+the\s+manner\s+of\s+|manner\s+of\s+"
    r")",
    re.IGNORECASE,
)

# Drouot's Asian Art category id; covers Indochine / Chinese / Japanese / Korean lots.
ASIAN_ART_CAT = 43

# Full-text keywords that surface Vietnamese-related sales BEYOND the Asian Art
# category — catches contemporary VN artists (Trương Tân etc.) sold by small
# Paris houses (Planète des Arts, Beaussant-Lefèvre…) under "Modern" or
# "Paintings" headers.
VN_SEARCH_KEYWORDS = [
    "vietnam",      # broadest, ~40 hits — includes some noise (wine, militaria)
    "vietnamese",
    "vietnamien",
    "indochine",
    "indochinois",
    "tonkin",
    "saigon",
    "hanoi",
]

# Auctioneer slugs already covered by their own Artonis crawler. Drouot re-lists
# their sales (since most member houses also operate independently); skip to avoid
# inserting the same lot twice via different `source`.
_SKIP_AUCTIONEERS = {
    "aguttes",
    "millon",
    "millon-riviera",       # Millon's Côte d'Azur sales (same house)
    "millon-paris",
    "millon-cs",
    "cornette-de-saint-cyr",
    "cornette",
    "cornettedesaintcyr",
    "tajan",
    "gros-and-delettrez",
    "gros-delettrez",
    "gros-et-delettrez",
    "artcurial",            # we have direct crawlers/artcurial.py
    "osenat",               # direct
}


# Houses where regional/partner variants share the parent name — match by
# stem so 'millon-2025-spring' / 'millon-online' / 'millon-hk' all skip.
_SKIP_AUCTIONEER_STEMS = ("millon", "aguttes", "cornette", "tajan",
                          "gros-delettrez", "gros-et-delettrez",
                          "artcurial", "osenat")


def _is_skip_auctioneer(slug: str) -> bool:
    """True when the auctioneer is one of our direct-crawler houses
    (including regional/seasonal slug variants)."""
    if not slug:
        return False
    s = slug.lower()
    if s in _SKIP_AUCTIONEERS:
        return True
    return any(s == st or s.startswith(st + "-") for st in _SKIP_AUCTIONEER_STEMS)


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True}
    )


# ---- inline SvelteKit JS-literal parsing -----------------------------------
# Drouot's pages embed app data as a JS object literal (not JSON) — keys are
# unquoted and `void 0` appears. We extract each lot block via balanced-brace
# walking from the `lots:[` array, then pull fields with targeted regex.

_LOT_FIELD_PATTERNS = {
    "id":             re.compile(r"(?<![A-Za-z_])id:\s*(\d+)"),
    "num":            re.compile(r"(?<![A-Za-z_])num:\s*(\d+)"),
    "lowEstim":       re.compile(r"(?<![A-Za-z_])lowEstim:\s*([\d.]+)"),
    "highEstim":      re.compile(r"(?<![A-Za-z_])highEstim:\s*([\d.]+)"),
    "result":         re.compile(r"(?<![A-Za-z_])result:\s*([\d.]+)"),
    "currencyId":     re.compile(r'currencyId:\s*"([A-Z]{3})"'),
    "saleId":         re.compile(r"(?<![A-Za-z_])saleId:\s*(\d+)"),
    "auctioneerId":   re.compile(r"(?<![A-Za-z_])auctioneerId:\s*(\d+)"),
    "date":           re.compile(r"(?<![A-Za-z_])date:\s*(\d+)"),
    "slug":           re.compile(r'(?<![A-Za-z_])slug:\s*"([^"]+)"'),
    "saleStatus":     re.compile(r'saleStatus:\s*"([A-Z_]+)"'),
}


def _unescape_js_string(s):
    """Convert a JS double-quoted string body to Python text (handle \\n, \\", \\u escapes)."""
    return (
        s.replace("\\n", "\n")
         .replace('\\"', '"')
         .replace("\\/", "/")
         .replace("\\\\", "\\")
    )


def _extract_description(block):
    """Pull a JS double-quoted description string, handling escapes."""
    m = re.search(r'description:\s*"((?:[^"\\]|\\.)*)"', block, re.DOTALL)
    if not m:
        return ""
    return _unescape_js_string(m.group(1))


def _split_lots(lots_array_text):
    """Walk a `[{…},{…},…]` JS-literal array and yield each top-level object body
    (without the enclosing braces). Brace-aware so nested {…} inside lots don't
    break the split."""
    s = lots_array_text
    n = len(s)
    i = 0
    # Expect opening '['
    while i < n and s[i] != "[":
        i += 1
    if i >= n:
        return
    i += 1  # past [
    while i < n:
        # Skip whitespace / commas
        while i < n and s[i] in " \t\n,":
            i += 1
        if i >= n or s[i] == "]":
            return
        if s[i] != "{":
            return
        # Brace-aware walk, ignoring braces inside double-quoted strings
        depth = 0
        start = i
        in_str = False
        esc = False
        while i < n:
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield s[start + 1 : i]  # body without outer braces
                        i += 1
                        break
            i += 1


def _extract_sale_data_block(html_text):
    """Find the SvelteKit script tag containing the sale-page data and return it.
    Drouot puts the dehydrated app state in the script that holds
    `data:{lots:[…],…}`; the schema.org product script (first <script>) and the
    Cloudflare challenge stub (last <script>) are skipped automatically."""
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text, re.DOTALL)
    for s in scripts:
        if "data:{lots:[" in s and "saleId:" in s:
            return s
    return ""


def _extract_lots_array(script_text):
    """Find the `lots:[…]` chunk inside the sale-page script and return its text."""
    i = script_text.find("data:{lots:[")
    if i < 0:
        i = script_text.find("lots:[{")
        if i < 0:
            return ""
    # Move to the [
    j = script_text.find("[", i)
    if j < 0:
        return ""
    # Brace-walk to closing ]
    n = len(script_text)
    k = j + 1
    depth = 1
    in_str = False
    esc = False
    while k < n and depth > 0:
        c = script_text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
        k += 1
    return script_text[j:k]


def _extract_sale_info(script_text):
    """Pull sale-level info (title, city, auctioneerSlug, sale date epoch).

    On a Drouot /v/ page, the sale object is keyed by `saleSlug:` and includes
    sibling fields title, address{city}, status, auctioneerCard{link{auctioneerSlug,
    auctioneerName}}, plus a schedules array. We anchor on `saleSlug:` then scan
    a neighbourhood window for the typed fields (cheaper and more robust than
    trying to walk to the outermost enclosing brace, since the parent may be
    layered inside several arrays).
    """
    info = {"title": "", "city": "", "auctioneer_slug": "", "auctioneer_name": "", "date": 0}
    # Anchor on saleSlug:"...". The sale "title:" field appears AFTER saleSlug,
    # while auctioneerCard appears BEFORE. Search a generous window both sides.
    m_anchor = re.search(r'saleSlug:"[^"]+"', script_text)
    if not m_anchor:
        return info
    pos = m_anchor.start()
    win = script_text[max(0, pos - 2500): pos + 2500]
    # Sale title — the first title:"…" AFTER saleSlug inside the window
    m_title = re.search(r'saleSlug:"[^"]+"[^"]*?,(?:[^{}\[\]]*?,)*?title:"((?:[^"\\]|\\.)*)"', win)
    if m_title:
        info["title"] = _html.unescape(_unescape_js_string(m_title.group(1)))
    else:
        # Fallback: look at structuredData -> name:"…" which mirrors the title
        m_sd = re.search(r'"@type":"Event",name:"((?:[^"\\]|\\.)*)"', win)
        if m_sd:
            info["title"] = _html.unescape(_unescape_js_string(m_sd.group(1)))
    # Auctioneer — read from the structuredData.Organizer block (which lives
    # next to saleSlug). It has the same name + slug as auctioneerCard.link but
    # without the multi-kilobyte payload-settings noise in between.
    m_org = re.search(
        r'Organizer:\{"@type":"Organization",name:"((?:[^"\\]|\\.)*)",'
        r'url:"https?://[^"]+/auctioneer/\d+/([a-z0-9\-]+)"',
        win,
    )
    if m_org:
        info["auctioneer_name"] = _unescape_js_string(m_org.group(1))
        info["auctioneer_slug"] = m_org.group(2)
    # City — first address{…city:"…"} in window
    m_city = re.search(r'address:\{[^}]*?city:"([^"]+)"', win)
    if m_city:
        info["city"] = m_city.group(1)
    # Sale date — Drouot serialises startDate two ways:
    #  - epoch seconds (integer)        e.g. startDate:1781082000
    #  - ISO-8601 string (Heritage etc.) e.g. startDate:"2026-06-05T14:50:00.000Z"
    m_sd = re.search(r'startDate:(?:(\d+)|"([^"]+)")', win)
    if m_sd:
        if m_sd.group(1):
            try:
                info["date"] = int(m_sd.group(1))
            except ValueError:
                pass
        elif m_sd.group(2):
            # Store as ISO-8601 string; caller converts to YYYY-MM-DD
            info["date_iso"] = m_sd.group(2)
    return info


# ---- field parsers ---------------------------------------------------------

# Drouot member-house descriptions wrap the artist heading in several styles:
#   UPPER CASE:    "ALIX AYMÉ (1894 - 1989)"                    (Cornette / Aguttes)
#   Title-Case:    "Le Pho (1907-2001)"                         (Millon)
#   With nat'lty:  "Le Pho (French/Vietnamese, 1907-2001)"      (Heritage Auctions)
#   Born-only:     "Nguyen Trung (born 1940)"                   (Tessier-Sarrou)
#   "né en":       "Pham Luc (né en 1943)"                      (French houses)
# The regex captures the name + birth/death years; intermediate prose like
# "French/Vietnamese," is allowed inside the parens.
_ARTIST_HEADER_RE = re.compile(
    r"^([A-ZÀ-ÿ][A-ZÀ-ÿa-zà-ÿ\-' \.]{2,60})\s*"
    r"\(\s*(?:[A-Za-zà-ÿ/, \-]*?(?:n[ée]\s+en|born)?\s*)?"
    r"(\d{4})(?:\s*[-–]\s*(\d{4}))?\s*\)",
)
# Title-cased "Le Pho (…1907-2001)" variant — first char is upper, then lower
_ARTIST_HEADER_TC_RE = re.compile(
    r"^([A-Z][a-zà-ÿ][A-Za-zà-ÿ\-' \.]{1,60})\s*"
    r"\(\s*(?:[A-Za-zà-ÿ/, \-]*?(?:n[ée]\s+en|born)?\s*)?"
    r"(\d{4})(?:\s*[-–]\s*(\d{4}))?\s*\)",
)
_DIM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm", re.IGNORECASE)
_DIM_IN_RE = re.compile(r"(\d+(?:[ /]?\d+/\d+)?)\s*[\"”]\s*[x×]\s*(\d+(?:[ /]?\d+/\d+)?)\s*[\"”]")

_MEDIUM_HINTS = (
    "huile", "aquarelle", "encre", "laque", "gouache", "pastel", "fusain",
    "sanguine", "crayon", "soie", "acrylique", "lithographie", "estampe",
    "oil", "watercolour", "watercolor", "ink", "lacquer", "pencil", "silk",
    "tempera", "technique mixte", "mixed media", "ink wash",
)


def _parse_artist_and_title(description):
    """Parse the artist header + artwork title from a Drouot lot description.

    Drouot lots come from many member houses, so we see two main shapes:
      Multi-line:  'ARTIST (1907-2001)\\nTitle\\nMedium\\nDimensions'
      Single-line: 'Le Pho (French/Vietnamese, 1907-2001) Title Title Oil on board 18 x 13"…'

    Returns (artist_raw, artwork_title, birth_year, death_year).
    """
    if not description:
        return "", "", None, None
    lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
    if not lines:
        return "", "", None, None
    head = lines[0]
    m = _ARTIST_HEADER_RE.match(head) or _ARTIST_HEADER_TC_RE.match(head)
    if not m:
        # Drouot post-2026 redesign uses 'LASTNAME, Firstname [Title]'
        # without paren-years for many member houses (observed on Roldan,
        # Camille Chabroux, etc.).  Lat-1 chars handled; the strict VN
        # catalog gate downstream rejects non-VN matches so we can be
        # liberal here.
        # Capture ALLCAPS lastname before the comma — drop the firstname
        # token because Drouot's new format puts EITHER 'Western firstname'
        # OR 'title' after the comma; we can't disambiguate locally.
        # Strategy: use LASTNAME (Title-cased) as the artist.  Strict VN
        # catalog gate (_is_vietnamese) will reject Western LASTNAMEs
        # ('Gómez Canle', 'Le Parc') so we don't insert junk.  VN names
        # ('Pham Hau', 'Nguyen Gia Tri') match by direct catalog entry.
        m_comma = re.match(
            r"^([A-ZÀ-ÿ]{2,}(?:\s+[A-ZÀ-ÿ]{2,}){0,2}),\s+",  # LAST (1-3 words),
            head,
        )
        if not m_comma:
            # Last-resort fallback: ask Claude Haiku to extract
            # artist + title.  Pre-filter dodges Asian Art antique
            # lots ('CHINA, Qing dynasty / glazed jar...').  See
            # crawlers/llm_parser.py for cost (~$0.0014/lot) and
            # validation rules.  Returns ('','',None,None) on any
            # failure / low-confidence, so behavior is unchanged
            # when LLM is disabled / API key absent.
            try:
                from crawlers.llm_parser import llm_artist_fallback
                a, t, b, d = llm_artist_fallback(description)
            except Exception:
                a, t, b, d = '', '', None, None
            if not a:
                return "", "", None, None
            return a, t, b, d
        artist = clean_text(m_comma.group(1).strip().title())
        b_yr = d_yr = None
        m = m_comma   # m.end() now points just after 'LASTNAME, '
    else:
        artist = clean_text(m.group(1))
        b_yr = int(m.group(2)) if m.group(2) else None
        d_yr = int(m.group(3)) if m.group(3) else None

    # Look for the title in lines AFTER the header (multi-line case) or after the
    # ")" on the SAME line (single-line / Heritage-style case).
    title = ""

    # Single-line case — head has more text after the closing ")"
    after_close = head[m.end():].strip()
    if after_close:
        # Title is everything up to a medium keyword or a dimension token
        cand = after_close
        # Trim at first medium keyword (case-insensitive)
        low = cand.lower()
        cut_at = len(cand)
        for kw in _MEDIUM_HINTS:
            idx = low.find(kw)
            if 4 < idx < cut_at:
                cut_at = idx
        # Also stop at dimension hint
        m_dim = _DIM_RE.search(cand) or _DIM_IN_RE.search(cand)
        if m_dim and m_dim.start() > 4:
            cut_at = min(cut_at, m_dim.start())
        cand = cand[:cut_at].strip(" .,;:-")
        if 2 < len(cand) < 200:
            title = cand

    if not title:
        for ln in lines[1:5]:
            low = ln.lower()
            if any(kw in low for kw in _MEDIUM_HINTS):
                continue
            if _DIM_RE.search(ln) or _DIM_IN_RE.search(ln):
                continue
            if re.match(r"^(sign|certificat|provenance|exposition|bibliograph|\d+\s*(cm|x))",
                        ln, re.IGNORECASE):
                continue
            if len(ln) < 2 or len(ln) > 200:
                continue
            title = ln
            break
    return artist, title[:200], b_yr, d_yr


def _parse_medium(description):
    """Pick out the medium phrase. We always snip from the FIRST medium keyword
    forward (rather than returning whole lines) so single-line descriptions from
    Heritage/Drouot don't include the title, dimensions, or signature note."""
    low = description.lower()
    for kw in _MEDIUM_HINTS:
        idx = low.find(kw)
        if idx < 0:
            continue
        # Walk forward up to ~80 chars or until a hard stop (period, newline,
        # next dimension token, or "Signed").
        end = idx + len(kw)
        tail = description[end : end + 80]
        m_stop = re.search(r"[.\n]|\d+(?:[.,]\d+)?\s*[x×]|Signed|\(", tail)
        if m_stop:
            end += m_stop.start()
        else:
            end += len(tail)
        return clean_text(description[idx:end])[:120]
    return ""


def _parse_dimensions(description):
    """Prefer cm dimensions. If only inch dims are present (e.g. 18 x 13 inches),
    convert to cm so downstream price/m² is consistent across houses.

    Drouot is HW_FIRST_SOURCES in artonis_price_mvp.parse_dimensions, so
    the stored 'A x B cm' is interpreted as 'H x W cm' downstream.  Keep
    that convention when emitting labelled-format results — labelled
    formats encode W and H explicitly, so we re-order to 'H x W' here
    before storing.
    """
    # First try labelled formats — French Hauteur/Largeur, HW inch labels,
    # height/wide.  Drouot French lots use 'H. 60 cm - L. 100,5 cm' which
    # the plain _DIM_RE used to miss entirely.
    from crawlers.parsers import parse_dim_labelled
    w_lab, h_lab, _area, _disp = parse_dim_labelled(description)
    if w_lab is not None:
        # Re-emit as 'H x W cm' so downstream HW_FIRST interpretation
        # extracts back the correct (W, H) tuple.
        return f"{h_lab:g} x {w_lab:g} cm"

    # Plain 'N x N cm' — original logic preserved.
    m = _DIM_RE.search(description)
    if m:
        return f"{m.group(1).replace(',', '.')} x {m.group(2).replace(',', '.')} cm"
    # Heritage often writes "18 x 13 inches (45.7 x 33.0 cm)" — the cm form
    # is normally captured above. Last resort: parse plain "18 x 13 inches".
    m_in = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*inches?", description, re.IGNORECASE)
    if m_in:
        try:
            w = float(m_in.group(1)) * 2.54
            h = float(m_in.group(2)) * 2.54
            return f"{w:.1f} x {h:.1f} cm"
        except ValueError:
            pass
    return ""


def _parse_year(title, description):
    blob = (title or "") + " " + (description or "")
    m = re.search(r"(?:circa|vers|c\.)\s*(\d{4})", blob, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b(1[89]\d{2}|20[0-2]\d)\b", title or "")
    if m:
        return m.group(1)
    return ""


def _load_vn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_raw, vn_catalog, exclusions):
    """True when the parsed artist name maps to a VN-catalog entry.

    Match rules:
      1. Exact normalised match (catalog has the artist exactly).
      2. Input STARTS WITH a catalog entry + space — e.g. catalog has
         'lebadang', input is 'lebadang dang' (compound surname).
    Removed (2026-06-24): catalog-starts-with-input branch.  It let
    single first names ('Henri', 'Jean', 'Pierre') match VN catalog
    entries with the same first name ('Henri Nguyen Quy Kien',
    'Jean Volang') producing false positives — Drouot redesign's
    comma-format parser sometimes returns only the first name when the
    lastname-then-firstname pattern picks up a wrong second word.
    """
    from artonis_price_mvp import normalize_key
    norm = normalize_key(artist_raw)
    if not norm or norm in exclusions:
        return False
    if norm in vn_catalog:
        return True
    # Single-token names (no space) require an EXACT catalog match —
    # else 'Henri' alone matches catalog 'henri nguyen quy kien'.
    if " " not in norm:
        return False
    for k in vn_catalog:
        if norm.startswith(k + " "):
            return True
    return False


# ---- discovery -------------------------------------------------------------

_SALE_URL_RE = re.compile(r'/[a-z]{2}/v/(\d+)-([a-z0-9\-]+?)(?=[?"])')


def discover_asian_sales(scraper, max_pages=20, verbose=False):
    """Discover Drouot sale URLs across multiple discovery paths.

    Discovery paths (UNION — each saleId appears at most once):
      1. /en/auctions/future?categs=43  — upcoming sales tagged Asian Art
      2. /en/auctions/hotel?categs=43   — sales currently at Hôtel Drouot
      3. /en/auctions/future?q=<KEYWORD> for each VN keyword
         (vietnam, vietnamese, vietnamien, indochine, tonkin, saigon, hanoi…).
         This catches sales that *mention* Vietnamese content in title/lots but
         aren't filed under the "Asian Art" category — e.g. Planète des Arts
         selling Trương Tân under "Modern & Contemporary".

    `max_pages` is the TOTAL request budget shared across all paths.
    """
    seen = {}  # saleId -> url
    base_paths = [
        f"/en/auctions/future?categs={ASIAN_ART_CAT}",
        f"/en/auctions/hotel?categs={ASIAN_ART_CAT}",
    ]
    # Each keyword gets its own discovery pass on /auctions/future.
    base_paths.extend(f"/en/auctions/future?q={kw}" for kw in VN_SEARCH_KEYWORDS)
    budget = max(1, int(max_pages))
    for base_path in base_paths:
        if budget <= 0:
            break
        for page in range(1, budget + 1):
            sep = "&" if "?" in base_path else "?"
            url = f"{BASE}{base_path}{sep}page={page}" if page > 1 else f"{BASE}{base_path}"
            try:
                r = scraper.get(url, timeout=30)
            except Exception as e:
                if verbose:
                    print(f"  [drouot] {base_path} p{page} request error: {e}",
                          flush=True)
                break
            budget -= 1
            if r.status_code != 200:
                if verbose:
                    print(f"  [drouot] {base_path} p{page} HTTP {r.status_code}",
                          flush=True)
                break
            r.encoding = "utf-8"
            new = 0
            for m in _SALE_URL_RE.finditer(r.text):
                sid, slug = m.group(1), m.group(2)
                if sid in seen:
                    continue
                seen[sid] = f"{BASE}/en/v/{sid}-{slug}"
                new += 1
            if verbose:
                print(f"  [drouot] {base_path} p{page}: +{new} sales "
                      f"(total {len(seen)})", flush=True)
            if new == 0 or budget <= 0:
                break
            time.sleep(0.4)
    return list(seen.values())


# ---- sale-page lot extraction ---------------------------------------------

def parse_sale_page(html_text, sale_url):
    """Parse a Drouot sale-detail HTML and return (sale_info, lots_list).
    `lots_list` is a list of dicts with raw lot fields.
    """
    script_text = _extract_sale_data_block(html_text)
    if not script_text:
        return {}, []
    sale_info = _extract_sale_info(script_text)
    lots_array_text = _extract_lots_array(script_text)
    if not lots_array_text:
        return sale_info, []
    lots = []
    for body in _split_lots(lots_array_text):
        rec = {}
        for fld, pat in _LOT_FIELD_PATTERNS.items():
            m = pat.search(body)
            if m:
                rec[fld] = m.group(1)
        desc = _extract_description(body)
        rec["description"] = desc
        # Original (French) description preferred when the English one is empty
        m_orig = re.search(r'originalDescription:\s*"((?:[^"\\]|\\.)*)"', body, re.DOTALL)
        if m_orig:
            rec["originalDescription"] = _unescape_js_string(m_orig.group(1))
        lots.append(rec)
    return sale_info, lots


# ---- watchlist: track future sales so we can re-fetch results after sale_date ----
#
# Drouot's site only exposes /auctions/future and /auctions/hotel (currently happening).
# Once a sale becomes past, its URL drops off discovery and the lot data is often
# hidden behind login. To capture results, we save every discovered future-sale URL
# in a local watchlist and re-fetch it after sale_date passes — that's the only
# window where the sale page still serves lots WITH `result` values.

def _ensure_watchlist_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drouot_watchlist (
            url TEXT PRIMARY KEY,
            sale_date TEXT,
            auction_title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            note TEXT
        )
    """)
    conn.commit()


def _watchlist_add(conn, url, sale_date=None, title=None):
    """Idempotent insert. Updates sale_date/title if they were unknown before."""
    conn.execute("""
        INSERT INTO drouot_watchlist (url, sale_date, auction_title)
        VALUES (?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            sale_date = COALESCE(drouot_watchlist.sale_date, excluded.sale_date),
            auction_title = COALESCE(drouot_watchlist.auction_title, excluded.auction_title)
    """, (url, sale_date, title))


def _watchlist_due_for_refetch(conn):
    """Return URLs whose sale_date is past + still pending + not too many attempts.
    Spaces out retries: ≤3 attempts in first 7 days, then weekly after."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT url FROM drouot_watchlist
        WHERE status = 'pending'
          AND sale_date IS NOT NULL
          AND sale_date < ?
          AND (
              attempts < 3
              OR last_checked IS NULL
              OR last_checked < datetime('now','-7 days')
          )
        ORDER BY sale_date DESC
        LIMIT 100
    """, (today,)).fetchall()
    return [r[0] for r in rows]


def _watchlist_mark(conn, url, status, note=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE drouot_watchlist
        SET status = ?, last_checked = ?, attempts = attempts + 1, note = ?
        WHERE url = ?
    """, (status, now, note, url))


# ---- main entry ------------------------------------------------------------

def crawl_refetch_only(conn, **kw):
    """Lightweight watchlist refetch — skip the discovery phase.

    Designed for high-frequency cron (every 3-4h) to catch Drouot
    result data within the narrow ~24h post-close window.  Drouot
    drops lot data soon after sale close; the daily crawl() can miss
    that window if its run time and the sale close time don't align.

    No fresh sales discovered — only sales already in the watchlist
    whose sale_date is past and have < 3 refetch attempts.
    """
    scraper = _make_scraper()
    _ensure_watchlist_table(conn)
    sales = _watchlist_due_for_refetch(conn)
    if not sales:
        if kw.get("verbose", True):
            print("  [drouot/refetch] no watchlist URLs due — skip", flush=True)
        return 0
    if kw.get("verbose", True):
        print(f"  [drouot/refetch] processing {len(sales)} watchlist URLs", flush=True)
    return crawl(conn, sale_urls=sales, **kw)


def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Drouot Asian-art sales. Returns (inserted, scanned).

    Args:
      sale_urls:  explicit list of sale URLs to crawl; if None, discovers from
                  /en/c/43/asian-art pagination.
      delay:      seconds to sleep between sale fetches.
      filter_vn:  when True (default), only insert lots whose artist matches the
                  VN catalog (mirrors aguttes/millon behaviour).
      max_pages:  cap on discovery pages (each ~30 sales).
    """
    scraper = _make_scraper()
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())
    _ensure_watchlist_table(conn)

    if sale_urls is None:
        if verbose:
            print("  [drouot] discovering Asian-art sales…", flush=True)
        discovered = discover_asian_sales(scraper, max_pages=max_pages, verbose=verbose)
        if verbose:
            print(f"  [drouot] found {len(discovered)} discovered URLs", flush=True)

        # Pull URLs whose sale_date has passed and we haven't captured results yet.
        # These are sales we tracked earlier as 'future'; now they're past and
        # Drouot still serves lots-with-results for a short window.
        revisit = _watchlist_due_for_refetch(conn)
        if verbose and revisit:
            print(f"  [drouot] re-fetching {len(revisit)} past sales from watchlist", flush=True)

        # Re-fetch FIRST (results most likely to disappear) then fresh discoveries.
        sale_urls = revisit + [u for u in discovered if u not in revisit]

    total_inserted = 0
    total_scanned = 0
    revisit_set = set(_watchlist_due_for_refetch(conn))  # rescan-pass URLs for status updates

    for i, sale_url in enumerate(sale_urls, 1):
        run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = scraper.get(sale_url, timeout=30)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] {sale_url[-60:]}: ERR {e}", flush=True)
            log_crawl_run(conn, "drouot", target_slug=sale_url[-80:],
                          started_at=run_started, status="error", note=str(e)[:200])
            continue
        if r.status_code != 200:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] HTTP {r.status_code}", flush=True)
            log_crawl_run(conn, "drouot", target_slug=sale_url[-80:],
                          started_at=run_started, status="error",
                          note=f"HTTP {r.status_code}")
            time.sleep(delay)
            continue
        # Drouot serves UTF-8 but omits the charset header; cloudscraper falls back
        # to ISO-8859-1 which mangles French/Vietnamese diacritics.
        r.encoding = "utf-8"

        sale_info, lots = parse_sale_page(r.text, sale_url)

        # Pagination — Drouot sales over 100 lots split into ?page=2/3/…
        # Page 1 → 100 lots; subsequent pages → 100 more until a short
        # page (<100) signals the end.  Without this, lots beyond 100
        # silently vanished (e.g. Marambat sale 178740 lot 140 Nguyen
        # Huyen at €? wasn't seen).  Detect: if page 1 returns the cap
        # (100) keep paginating until short page or 10-page safety stop.
        if len(lots) >= 100:
            seen_lot_ids = {lot.get("id") for lot in lots}
            for page in range(2, 11):
                pg_url = f"{sale_url}?page={page}" if "?" not in sale_url else f"{sale_url}&page={page}"
                try:
                    rp = scraper.get(pg_url, timeout=30)
                    rp.encoding = "utf-8"
                except Exception:
                    break
                if rp.status_code != 200:
                    break
                _, page_lots = parse_sale_page(rp.text, pg_url)
                # Stop when a page returns 0 new lots (dedup against seen)
                new_lots = [lot for lot in page_lots if lot.get("id") not in seen_lot_ids]
                if not new_lots:
                    break
                lots.extend(new_lots)
                seen_lot_ids.update(lot.get("id") for lot in new_lots)
                if len(page_lots) < 100:
                    break   # short page → no more
                time.sleep(0.3)

        total_scanned += len(lots)

        # Skip whole sale if auctioneer is one of our dedicated-crawler houses —
        # avoids duplicate rows for the same lot under different `source`.
        auct_slug = (sale_info.get("auctioneer_slug") or "").lower()
        if _is_skip_auctioneer(auct_slug):
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] skip {auct_slug} sale "
                      f"({len(lots)} lots)", flush=True)
            log_crawl_run(conn, "drouot", target_slug=sale_url[-80:],
                          started_at=run_started, lots_scanned=len(lots),
                          lots_inserted=0, status="skip",
                          note=f"auctioneer={auct_slug}")
            time.sleep(delay)
            continue

        sale_date = ""
        if sale_info.get("date"):
            try:
                sale_date = datetime.fromtimestamp(int(sale_info["date"]), tz=timezone.utc) \
                                    .strftime("%Y-%m-%d")
            except (ValueError, OSError):
                sale_date = ""
        if not sale_date and sale_info.get("date_iso"):
            # ISO-8601 string "2026-06-05T14:50:00.000Z"
            sale_date = sale_info["date_iso"][:10]
        sale_city = sale_info.get("city") or "Paris"
        sale_title_full = sale_info.get("title", "").strip() or "Drouot Asian Art Sale"
        # Normalise &amp; HTML entities in sale title
        sale_title_full = _html.unescape(sale_title_full)
        auctioneer_name = sale_info.get("auctioneer_name") or ""
        auction_title = f"Drouot — {sale_title_full}"
        if auctioneer_name:
            auction_title += f" ({auctioneer_name})"

        # Watchlist: record this sale for future re-fetch (idempotent).
        # If sale_date is past + we found result lots, mark resolved at the end.
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _watchlist_add(conn, sale_url, sale_date=sale_date, title=sale_title_full[:200])

        inserted_this = 0
        results_seen = 0  # lots with a non-zero hammer (used to decide watchlist status)
        sale_date_min = sale_date_max = None
        for lot in lots:
            # Drouot lots embed source-URL slug + numeric id
            lot_id = lot.get("id")
            lot_slug = lot.get("slug")
            if not lot_id or not lot_slug:
                continue
            lot_url = f"{BASE}/en/l/{lot_id}-{lot_slug}"

            desc = lot.get("description") or lot.get("originalDescription") or ""
            artist_raw, artwork_title, b_yr, d_yr = _parse_artist_and_title(desc)
            if not artist_raw:
                continue
            artist_raw, _alt_birth = clean_artist_name(artist_raw)
            if not artist_raw:
                continue

            # Skip attribution lots (not original works by the artist):
            #   "after X", "d'après X", "attribué à X", "atelier de X",
            #   "école de X", "entourage de X", "circle of X", etc.
            # Detected in the lot slug ("after-mai-thu", "le-pho-attribue")
            # OR in the raw artist string itself.
            if _ATTRIBUTION_RE.search(lot_slug) or _ATTRIBUTION_RAW_RE.match(artist_raw):
                continue

            if filter_vn and not _is_vietnamese(artist_raw, vn_catalog, exclusions):
                continue

            # Determine status / hammer price
            try:
                result = float(lot.get("result", 0) or 0)
            except ValueError:
                result = 0.0
            try:
                low_est = float(lot.get("lowEstim", 0) or 0) or None
            except ValueError:
                low_est = None
            try:
                high_est = float(lot.get("highEstim", 0) or 0) or None
            except ValueError:
                high_est = None

            currency = lot.get("currencyId", "EUR") or "EUR"

            # Status / hammer policy (2026-06-24 — pre-capture pivot):
            #   - hammer > 0      → status='sold'                  (post-sale visible)
            #   - upcoming sale   → status='estimate_only'         (pre-capture, no fake price)
            #   - past, no hammer → preserve existing OR 'passed'  (don't overwrite estimate→passed)
            #
            # User insight that drove this change: 'Drouot rất nhiều lot
            # VN.  Bạn phải lên chiến lược để lấy trước các lot trước khi
            # đấu.  Sau đó lắng nghe và catch lại sau khi đấu xong'.
            # Drouot drops result data ~24h post-close; pre-capturing
            # while sale is upcoming guarantees we keep the lot metadata
            # even if our refetch misses the result window.
            if result > 0:
                status = "sold"
                hammer = result
                results_seen += 1
            elif sale_date and sale_date >= today_iso:
                # Upcoming sale — insert with estimate-only marker.
                # When watchlist refetches after sale close, hammer (if
                # found) will overwrite this row's status → 'sold'.
                status = "estimate_only"
                hammer = None
            else:
                # Past sale, no hammer in API.  Either truly unsold OR
                # Drouot already hid the result.  Don't downgrade a
                # previously captured estimate_only / sold record.
                existing = conn.execute(
                    "SELECT status FROM sale_results WHERE source_url = ?",
                    (lot_url,),
                ).fetchone()
                if existing and existing[0] in ("sold", "estimate_only"):
                    continue   # preserve prior data
                status = "passed"
                hammer = None

            medium = _parse_medium(desc)
            dimensions = _parse_dimensions(desc)
            year_str = _parse_year(artwork_title, desc)

            # Attribute to underlying auctioneer when known; record drouot as
            # the discovery platform via `via_platform`. Lots without an
            # auctioneer slug stay attributed to "drouot" so we don't lose them.
            actual_source = auct_slug or "drouot"
            via_platform = "drouot" if auct_slug else None

            rec = {
                "source": actual_source,
                "via_platform": via_platform,
                "source_url": lot_url,
                "sale_page_url": sale_url,
                "lot_number": str(lot.get("num") or ""),
                "auction_title": auction_title[:300],
                "sale_date": sale_date,
                "sale_location": sale_city,
                "artist_name_raw": artist_raw,
                "artwork_title": artwork_title,
                "medium": medium,
                "dimensions": dimensions,
                "year": year_str,
                "estimate_low": low_est,
                "estimate_high": high_est,
                "hammer_price": hammer,
                "currency": currency,
                "status": status,
                "raw_snapshot": json.dumps({
                    "organizer_house": auctioneer_name,
                    "auctioneer_slug": auct_slug,
                    "sale_id": lot.get("saleId"),
                    "desc_excerpt": desc[:400],
                }, ensure_ascii=False)[:1500],
            }
            try:
                insert_sale_result(conn, rec)
                inserted_this += 1
                if sale_date:
                    if sale_date_min is None or sale_date < sale_date_min:
                        sale_date_min = sale_date
                    if sale_date_max is None or sale_date > sale_date_max:
                        sale_date_max = sale_date
            except Exception as e:
                if verbose:
                    print(f"    insert err {lot_id}: {e}", flush=True)

        # Watchlist status: a sale is "resolved" once we've captured ≥1 lot with
        # a hammer price (any artist, not just VN — the indicator we care about
        # is whether Drouot is still serving results for this URL).
        if sale_date and sale_date < today_iso:
            if results_seen > 0:
                _watchlist_mark(conn, sale_url, "resolved",
                                note=f"{results_seen} lots with results")
            else:
                # Past sale but no hammer prices.  Get the current attempts
                # count and decide whether to retire the URL.  Observed
                # 2026-06-24: Drouot drops lot data within ~24h of close;
                # after 3 retries we either hit the empty page or the sale
                # simply went unsold.  Either way, no point keeping it
                # pending forever (98-entry queue with 0 resolved).
                attempts_row = conn.execute(
                    "SELECT attempts FROM drouot_watchlist WHERE url = ?",
                    (sale_url,)
                ).fetchone()
                attempts_so_far = (attempts_row[0] if attempts_row else 0)
                if attempts_so_far >= 2:  # this call is attempt #3
                    _watchlist_mark(conn, sale_url, "resolved",
                                    note=f"no results after 3 attempts (lots={len(lots)})")
                else:
                    _watchlist_mark(conn, sale_url, "pending",
                                    note=f"no results yet attempt={attempts_so_far + 1} (lots={len(lots)})")
        conn.commit()
        log_crawl_run(
            conn, "drouot",
            target_slug=sale_url[-80:],
            started_at=run_started,
            lots_scanned=len(lots),
            lots_inserted=inserted_this,
            sale_date_min=sale_date_min,
            sale_date_max=sale_date_max,
            status="ok",
            note=(sale_title_full + (f" / {auctioneer_name}" if auctioneer_name else ""))[:120],
        )
        total_inserted += inserted_this
        if verbose:
            print(
                f"  [{i}/{len(sale_urls)}] {sale_date or '----------'} "
                f"{(auctioneer_name or auct_slug or '?')[:24]:24s} "
                f"{sale_title_full[:40]:40s} "
                f"{len(lots)} lots / {inserted_this} VN inserted",
                flush=True,
            )
        time.sleep(delay)

    return total_inserted, total_scanned
