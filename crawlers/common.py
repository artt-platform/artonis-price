"""Shared utilities for auction crawlers: currency conversion, dimension parsing, DB insert."""
import re
import sys
from pathlib import Path

# Allow crawlers to import the main app module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from artonis_price_mvp import (
    upsert_artist, parse_dimensions, compute_area_and_price_per_m2, now_iso,
    VND_TO_USD_RATE, clean_text,
)

__all__ = ["parse_amount", "parse_date", "insert_sale_result", "to_usd", "clean_text",
           "FX_TO_USD", "clean_artist_name", "log_crawl_run"]


def log_crawl_run(conn, source, target_slug=None, started_at=None, finished_at=None,
                  lots_scanned=0, lots_inserted=0, sale_date_min=None, sale_date_max=None,
                  status="ok", note=None):
    """Append a row to crawl_runs. Crawlers call this after processing each catalog/target.
    Idempotent on (source, target_slug, finished_at) — re-running overwrites the latest entry."""
    from artonis_price_mvp import now_iso
    started_at = started_at or now_iso()
    finished_at = finished_at or now_iso()
    conn.execute(
        """insert into crawl_runs(source, target_slug, started_at, finished_at,
            lots_scanned, lots_inserted, sale_date_min, sale_date_max, status, note)
           values (?,?,?,?,?,?,?,?,?,?)""",
        (source, target_slug, started_at, finished_at,
         lots_scanned, lots_inserted, sale_date_min, sale_date_max, status, note),
    )
    conn.commit()


_NÉ_EN_RE = re.compile(r"\s*\(\s*n[ée]\s+en\s+(\d{4})\s*\)?", re.IGNORECASE)
_XX_RE = re.compile(r"\s*\(?\s*(?:xxe?|xx\s*eme|xxeme|xx\s+si[eè]cle|xxe?\s*si[eè]cle)\s*\)?", re.IGNORECASE)
_SLASH_YEARS_RE = re.compile(r"\s*\(\s*(\d{4})\s*/\s*\d{2,4}\s*[-–]\s*(\d{4})\s*\)")
_C_YEARS_RE = re.compile(r"\s*\(\s*c\.?\s*(\d{4})\s*[-–]\s*(\d{4})\s*\)", re.IGNORECASE)  # (C.1914-1976)
_ACTIF_CIRCA_RE = re.compile(r"\s*\(\s*actif\s+circa\s+(\d{4})\s*[-–]\s*(\d{4})\s*\)", re.IGNORECASE)  # (actif circa 1930-1955)
# Plain "(1920-2002)" — Gros & Delettrez writes it this way at the end of every artist label.
_PLAIN_YEARS_RE = re.compile(r"\s*\(\s*(\d{4})\s*[-–]\s*(\d{4})\s*\)")
# Plain "(1920)" — birth year only when artist still alive
_PLAIN_BIRTH_RE = re.compile(r"\s*\(\s*(\d{4})\s*\)")
_LEADING_STAR_RE = re.compile(r"^\*+\s*")
_TRAILING_PUNCT_RE = re.compile(r"[,;:.\-_/\\]+$")


def clean_artist_name(name):
    """Strip notation suffixes that turn equivalent names into duplicates.
    Returns (clean_name, birth_year). Birth-year sentinel -20 means '20th century'.

    Recognised patterns:
      'NAME (né en 1943)'      → ('NAME', 1943)
      'NAME (Né en 1937)'      → ('NAME', 1937)
      'NAME (NÉ EN 1929)'      → ('NAME', 1929)
      'NAME (XXe siècle)'      → ('NAME', -20)
      'NAME (XXe)' / '(XX)'    → ('NAME', -20)
      'NAME (1919/22-2016)'    → ('NAME', 1919)  # death=2016 returned via second tuple slot
      '* NAME'                 → ('NAME', None)
    """
    if not name:
        return "", None
    s = name
    birth = None
    death = None

    # Leading * / asterisks
    s = _LEADING_STAR_RE.sub("", s).strip()

    # Slash-years notation (1919/22-2016)
    m = _SLASH_YEARS_RE.search(s)
    if m:
        birth = int(m.group(1))
        try:
            death = int(m.group(2))
        except (TypeError, ValueError):
            death = None
        s = _SLASH_YEARS_RE.sub("", s).strip()

    # (C.YYYY-YYYY) — circa notation
    m = _C_YEARS_RE.search(s)
    if m:
        if birth is None:
            birth = int(m.group(1))
        if death is None:
            death = int(m.group(2))
        s = _C_YEARS_RE.sub("", s).strip()

    # (actif circa YYYY-YYYY) — active period (no birth/death known)
    m = _ACTIF_CIRCA_RE.search(s)
    if m:
        if birth is None:
            birth = -20  # century-only marker
        s = _ACTIF_CIRCA_RE.sub("", s).strip()

    # (Né en YYYY)
    m = _NÉ_EN_RE.search(s)
    if m:
        if birth is None:
            birth = int(m.group(1))
        s = _NÉ_EN_RE.sub("", s).strip()

    # Plain (YYYY-YYYY) — most common at G&D / Bonhams
    m = _PLAIN_YEARS_RE.search(s)
    if m:
        if birth is None:
            birth = int(m.group(1))
        if death is None:
            death = int(m.group(2))
        s = _PLAIN_YEARS_RE.sub("", s).strip()

    # Plain (YYYY) — birth-only
    m = _PLAIN_BIRTH_RE.search(s)
    if m:
        if birth is None:
            birth = int(m.group(1))
        s = _PLAIN_BIRTH_RE.sub("", s).strip()

    # (XXe), (XXe siècle), 'XX' suffix
    if _XX_RE.search(s):
        if birth is None:
            birth = -20  # century-only sentinel matching fmtYears
        s = _XX_RE.sub("", s).strip()

    # Final whitespace + trailing punctuation
    s = re.sub(r"\s+", " ", s).strip()
    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    return s, birth

# Rough FX rates (USD per 1 unit of foreign currency)
# Update these periodically; for MVP static rates are OK
FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.28,
    "HKD": 0.128,
    "SGD": 0.75,
    "CHF": 1.10,
    "AUD": 0.65,
    "VND": 1.0 / VND_TO_USD_RATE,
}


def to_usd(amount, currency):
    """Convert amount in given currency to USD using FX_TO_USD. Returns (usd_amount, currency_used)."""
    if amount is None or not currency:
        return None, currency
    rate = FX_TO_USD.get(currency.upper())
    if rate is None:
        return None, currency
    return round(amount * rate, 2), currency


def parse_amount(text, default_currency="EUR"):
    """Parse a price string like '20 000 €', '€1,500', '$12,500', 'HKD 180,000' → (amount, currency)."""
    if not text:
        return None, default_currency
    t = clean_text(text)
    currency = default_currency
    low = t.lower()
    if "$" in t or "usd" in low:
        currency = "USD"
    elif "€" in t or "eur" in low:
        currency = "EUR"
    elif "£" in t or "gbp" in low:
        currency = "GBP"
    elif "hk$" in low or "hkd" in low:
        currency = "HKD"
    elif "chf" in low:
        currency = "CHF"
    elif "sgd" in low or "s$" in low:
        currency = "SGD"
    elif "vn" in low or "đ" in t:
        currency = "VND"
    # Extract digits, handling European-style "20 000" or "20.000" or "20,000"
    cleaned = re.sub(r"[^\d.,\s]", "", t).strip()
    cleaned = cleaned.replace(" ", "")
    if "," in cleaned and "." in cleaned:
        # Assume European: period = thousands, comma = decimal
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        # "20,000" → thousands; "20,5" → decimal
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        parts = cleaned.split(".")
        # "20.000" with all 3-digit groups = thousands separator
        if all(len(p) == 3 for p in parts[1:]):
            cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned), currency
    except ValueError:
        return None, currency


def parse_date(text):
    """Parse date like '2018/06/20', 'June 20, 2018', '20 juin 2018' → ISO 'YYYY-MM-DD'."""
    if not text:
        return ""
    t = clean_text(text)
    # ISO-ish
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # Day Month Year (French/English)
    months = {
        "jan": 1, "fév": 2, "feb": 2, "mar": 3, "avr": 4, "apr": 4, "mai": 5, "may": 5,
        "juin": 6, "jun": 6, "juil": 7, "jul": 7, "aou": 8, "aug": 8, "sep": 9,
        "oct": 10, "nov": 11, "dec": 12, "déc": 12,
    }
    m = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(\d{4})", t)
    if m:
        month = months.get(m.group(2).lower()[:3])
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    # US format: "Jan. 25, 2026" / "September 28, 2025"
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        month = months.get(m.group(1).lower()[:3])
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
    # Year only
    m = re.search(r"\b(19|20)\d{2}\b", t)
    if m:
        return m.group(0) + "-01-01"
    return ""


_PAINTING_MEDIUM_KWS = (
    "oil", "huile", "acrylique", "acrylic", "watercolour", "watercolor", "aquarelle",
    "ink", "encre", "gouache", "pastel", "fusain", "sanguine", "crayon", "mine de plomb",
    "pencil",
    "paper", "papier", "canvas", "toile", "soie", "silk", "panel", "panneau",
    "board", "masonite", "cardboard", "isorel",
    "lacquer", "laque", "sơn mài", "son mai",
    "tempera", "oeuf",
)
# Print-specific mediums — checked BEFORE painting kws so prints get their own kind.
_PRINT_MEDIUM_KWS = (
    "lithograph", "lithographie", "litho",
    "estampe", "gravure", "engraving",
    "screenprint", "silkscreen", "sérigraphie",
    "etching", "eau-forte", "aquatint", "aquatinte",
    "woodcut", "linocut", "monotype",
    "pochoir",
    "reproduction sur",     # "reproduction sur soie/papier" = copy on silk/paper
    "reproduction en couleur",
    "tirage",
    "impression sur",       # "impression sur soie/papier" = print on silk/paper
    "impression en couleur", # "Impression en couleurs sur soie" — variant phrasing
    "imprimé sur", "imprime sur",
    "giclée", "giclee",
)
# Title hints that flag a lot as print/reproduction even when medium says otherwise
_TITLE_PRINT_KWS = (
    "d'après", " dapres", "after mai thu", "after le pho",
    "édition limitée", "édition de", "numbered",
    "limited edition", "epreuve d'artiste", "épreuve",
    "print on silk", "print on paper",
    "impression sur soie", "impression sur papier",
    "tirage limité", "tirage de tête",
    "exemplaire numéroté",
)
# Edition fraction "342/450" — only flag as print if denom ≥100 (avoids dates like 05/03)
_EDITION_NUM_RE = re.compile(r'\b\d{1,3}\s*/\s*\d{3,4}\b')

_SCULPTURE_MATERIAL_KWS = (
    "bronze", "terracotta", "terre cuite", "terre-cuite",
    "marbre", "marble", "grès", "cuivre", "fonte",
    "porcelain", "porcelaine",
    "plâtre", "platre", "plaster",
    "cire perdue",
    "sculpture en", "sculpté", "statuette en",
)
_TITLE_SCULPTURE_KWS = ("tượng",)

# Medals / commemorative objects — distinct from sculpture and from fine art
_MEDAL_KWS = (
    "medal", "medallion", "médaille", "medaille",
    "issued by unesco", "commemorative coin",
)

# 3D lacquer OBJECTS only — 2D paravents (folding screens) keep kind=painting
# since their painted surface is measurable for $/m².
_LACQUER_OBJECT_KWS = (
    "shallow dish", "lacquer dish", "lacquer box", "music box",
    "cigar box", "cigar and music",
    "lacquer cabinet",
    "boîte en laque", "boite en laque",
    "coupe en laque", "plat en laque",
)

# Drawings / sketches / works on paper sets — different market from finished oil/silk paintings
_DRAWING_KWS = (
    "set of two drawings", "set of three drawings", "set of four drawings",
    "set of five drawings", "set of six drawings", "set of seven drawings",
    "set of 6 drawings", "set of 7 drawings", "set of 8 drawings", "set of 10 drawings",
    "drawings with", "180 sketches", "set of sketches",
    "ink drawing", "pencil drawing", "pencil sketch",
    "dessin à l'encre", "dessin au crayon",
    "carnet de croquis", "carnet de dessins",
    "deux dessins", "trois dessins", "quatre dessins",
    "two drawings", "three drawings", "four drawings", "five drawings", "six drawings",
    "ensemble de deux dessins", "ensemble de trois dessins", "ensemble de quatre dessins",
    "ensemble de cinq dessins", "ensemble de six dessins",
    "étude préparatoire", "etude préparatoire",
)


_EXPLICIT_SCULPTURE_KWS = ("sculpture", "sculpté", "carved", "statuette", "buste en bronze",
                           "buste en plâtre", "tượng", "modelé en", "molded plaster")


def classify_kind(medium, title):
    """Classify a sale_result as one of:
       'painting' (default 2D — works on paper/silk/canvas/lacquer/panel/etc.)
       'sculpture' (3D works in bronze/terracotta/marble/stone; includes 3D lacquer objects)
       'print'    (lithograph/etching/edition-numbered reproductions — multiples)
       'drawing'  (sketch/multi-piece works on paper — no canonical single dim)
       'medal'    (commemorative coins/medals — distinct market)
    Order matters: most-specific first so a lacquer box doesn't get tagged as
    painting just because "laqué" matches.
    """
    m = (medium or "").lower()
    t = (title or "").lower()
    blob = m + " " + t
    # 1) Medals / commemorative objects — keep separate from fine art
    for kw in _MEDAL_KWS:
        if kw in blob:
            return "medal"
    # 2) Explicit sculpture/3D markers — including 3D lacquer-objects (boxes, dishes)
    for kw in _EXPLICIT_SCULPTURE_KWS:
        if kw in blob:
            return "sculpture"
    for kw in _LACQUER_OBJECT_KWS:
        if kw in blob:
            return "sculpture"
    # 3) Drawing / sketch sets — multi-piece works on paper, no canonical single dim
    for kw in _DRAWING_KWS:
        if kw in t:
            return "drawing"
    # 4) Print: medium keyword OR title indicates reproduction
    for kw in _PRINT_MEDIUM_KWS:
        if kw in m:
            return "print"
    for kw in _TITLE_PRINT_KWS:
        if kw in t or kw in m:
            return "print"
    # Edition number fraction "342/450" — only treat as print if denom ≥100
    if _EDITION_NUM_RE.search(blob):
        return "print"
    for kw in _PAINTING_MEDIUM_KWS:
        if kw in m:
            return "painting"
    for kw in _SCULPTURE_MATERIAL_KWS:
        if kw in m:
            return "sculpture"
    for kw in _SCULPTURE_MATERIAL_KWS:
        if kw in t:
            return "sculpture"
    for kw in _TITLE_SCULPTURE_KWS:
        if kw in t:
            return "sculpture"
    return "painting"


# Support-type detection: physical material the work is on. Determines $/m² peer group
# for fair comparison (canvas vs silk vs paper vs lacquer have very different markets).
# Order = priority: lacquer first because "huile sur panneau LAQUÉ" should be lacquer not panel.
# Keywords use regex char class for accents (laqu[eéèê] matches laque/laqué/laquée).
_SUPPORT_PATTERNS = [
    ("lacquer", [(r"laqu[eéèê]", False), (r"lacquer", False),
                 (r"sơn mài", False), (r"son mai", False)]),
    ("silk",    [(r"silk", True), (r"soie", True), (r"lụa", True)]),
    ("canvas",  [(r"canvas", True), (r"toile", True), (r"vải", True)]),
    ("paper",   [(r"paper", True), (r"papier", True), (r"giấy", True),
                 (r"vélin", True), (r"arches", True)]),
    ("panel",   [(r"panel", True), (r"panneau", True), (r"carton", True), (r"board", True),
                 (r"bois", True), (r"isorel", True), (r"masonite", True)]),
    ("metal",   [(r"plate", True), (r"plaque", True), (r"métal", True), (r"metal", True),
                 (r"copper", True), (r"cuivre", True)]),
]


def detect_support_type(medium, title):
    """Return support_type slug or None when nothing matches."""
    blob = ((medium or "") + " " + (title or "")).lower()
    for support, kws in _SUPPORT_PATTERNS:
        for kw, use_boundary in kws:
            pattern = (r"\b" + kw + r"\b") if use_boundary else kw
            if re.search(pattern, blob):
                return support
    return None


def insert_sale_result(conn, record):
    """Upsert a sale_result row. record must contain at minimum: source, source_url, artist_name_raw.
    artist_id is resolved via upsert_artist if artist_name_raw is provided."""
    artist_id = None
    if record.get("artist_name_raw"):
        artist_id = upsert_artist(conn, record["artist_name_raw"])

    dims = record.get("dimensions", "")
    kind = classify_kind(record.get("medium", ""), record.get("artwork_title", ""))
    # Sculptures don't have meaningful 2D area — keep w/h pairs as parsed but null out area+ppm
    # Prints/drawings/medals get dim+area kept but $/m² nulled later (different market).
    w, h, area, _ = compute_area_and_price_per_m2(dims, record.get("hammer_price") or 0)
    if kind in ("sculpture", "medal"):
        w, area = None, None

    # Hammer price in USD (this is what market benchmarks use)
    price_usd, _ = to_usd(record.get("hammer_price"), record.get("currency", "EUR"))
    if price_usd is None:
        price_usd, _ = to_usd(record.get("price_with_premium"), record.get("currency", "EUR"))
    # Price with buyer's premium in USD (what buyer actually paid)
    premium_usd, _ = to_usd(record.get("price_with_premium"), record.get("currency", "EUR"))
    # If house didn't supply explicit premium-included price, derive from hammer × source's premium rate
    if premium_usd is None and price_usd is not None:
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
            from auction_houses import AUCTION_HOUSES
            rate = (AUCTION_HOUSES.get(record.get("source", "")) or {}).get("premium_rate_pct", 25.0)
            premium_usd = round(price_usd * (1 + rate / 100), 2)
        except Exception:
            premium_usd = None
    # $/m² uses premium-inclusive price (the "real" price buyer paid).
    # Sculptures/prints/drawings/medals: $/m² doesn't reflect market value → set None.
    ppm_basis = premium_usd if premium_usd is not None else price_usd
    ppm_usd = round(ppm_basis / area, 2) if (ppm_basis and area) else None
    if kind in ("print", "drawing", "medal", "sculpture"):
        ppm_usd = None

    support_type = detect_support_type(record.get("medium", ""), record.get("artwork_title", ""))

    conn.execute(
        """
        insert or replace into sale_results(
            source, source_url, sale_page_url, lot_number, auction_title, sale_date, sale_location,
            artist_id, artist_name_raw, artwork_title, medium, dimensions,
            width_cm, height_cm, area_m2, year,
            estimate_low, estimate_high, hammer_price, price_with_premium, currency,
            price_usd, price_with_premium_usd, price_per_m2_usd, status, provenance, raw_snapshot, scraped_at, kind, support_type
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.get("source", ""),
            record.get("source_url", ""),
            record.get("sale_page_url", ""),
            record.get("lot_number", ""),
            record.get("auction_title", ""),
            record.get("sale_date", ""),
            record.get("sale_location", ""),
            artist_id,
            record.get("artist_name_raw", ""),
            record.get("artwork_title", ""),
            record.get("medium", ""),
            dims,
            w, h, area,
            record.get("year", ""),
            record.get("estimate_low"),
            record.get("estimate_high"),
            record.get("hammer_price"),
            record.get("price_with_premium"),
            record.get("currency", ""),
            price_usd,
            premium_usd,
            ppm_usd,
            record.get("status", "sold"),
            record.get("provenance", ""),
            record.get("raw_snapshot", ""),
            now_iso(),
            kind,
            support_type,
        ),
    )
