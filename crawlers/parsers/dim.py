"""Dimension parsing — convention-aware across auction houses.

Most catalogs write the dim as 'A x B cm' but the order varies:
  - Sothebys, Bonhams, Aguttes, Drouot, Gros-Delettrez, Tajan,
    Artcurial, Millon, Osenat, Invaluable, Le Auction: H × W
  - Christies, Phillips: W × H
  - English text often uses 'by' instead of 'x' ('50.6 by 65.1 cm').
  - French text uses 'x' or '×'.
  - Some use 'cm x cm' separator ('74.5 cm x 94 cm').

parse_dim() takes the raw text + source key and returns the canonical
(width_cm, height_cm, area_m2, display_str).  Order normalised to
W × H in the display string ('width x height cm').

For sculptures with single-axis dims ('H 50.5 cm'), returns w=None,
h=None — caller decides.  Single-axis is a 3D signal handled by the
kind classifier.
"""
import re


HW_FIRST_SOURCES = frozenset({
    "bonhams", "sothebys", "aguttes", "drouot", "gros-delettrez",
    "gros_delettrez", "tajan", "artcurial", "millon", "millon_vn",
    "osenat", "invaluable", "le_auction",
})


_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:cm\s*)?(?:x|by|×)\s*(\d+(?:[.,]\d+)?)\s*cm",
    re.IGNORECASE,
)

# Inch fallback — '50 x 60"' / '50" x 60"' / '50 by 60 in' / '40 x 30 inches'.
# Used by US regional houses that Invaluable mirrors.  Caller decides
# whether to apply HW_FIRST.
_DIM_INCH_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:[\"″]|inches?|in)?\s*(?:x|by|×)\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:[\"″]|inches?|\bin\b)",
    re.IGNORECASE,
)


# ─── Labelled dim formats ────────────────────────────────────────────
#
# When the catalog text uses explicit width/height labels, the convention
# (H×W vs W×H) is encoded IN THE TEXT, not in the source.  These
# patterns ALWAYS return (w_cm, h_cm) in canonical order regardless of
# HW_FIRST_SOURCES.  Use parse_dim_smart() to try them in priority order.

_FRAC_NUM = r"\d+(?:\s+\d+/\d+)?(?:[.,]\d+)?"

# '48"h, 96"w' / '48 h x 96 w' — Cadmore / Litchfield convention.
# Captures (h_inches, w_inches).  Always returns inches → cm.
_HW_INCH_LABEL_RE = re.compile(
    rf'({_FRAC_NUM})\s*["″]?\s*h\b[\s,]+'
    rf'({_FRAC_NUM})\s*["″]?\s*w\b',
    re.IGNORECASE,
)

# 'H. 60 cm - L. 100,5 cm' / 'H 60 x L 100.5' — French Hauteur/Largeur.
# H = hauteur (height), L = largeur (width).  Captures (h_cm, w_cm).
_HL_CM_LABEL_RE = re.compile(
    rf'\bh(?:auteur)?\.?\s*({_FRAC_NUM})\s*(?:cm)?[\s\-–,xX×]+'
    rf'l(?:argeur)?\.?\s*({_FRAC_NUM})\s*cm',
    re.IGNORECASE,
)

# '198cm high, 250cm wide' / '198 in high 250 in wide' — Invaluable
# lacquer panels (BHH).  Captures (h_value, unit, w_value).
_HEIGHT_WIDE_LABEL_RE = re.compile(
    rf'({_FRAC_NUM})\s*(cm|in|inches?|["″])\s*high[\s,]+'
    rf'({_FRAC_NUM})\s*(?:cm|in|inches?|["″])?\s*wide',
    re.IGNORECASE,
)


def _to_float(s: str) -> float:
    """Convert '48 1/2' / '60,5' / '100.5' to float.  Raises ValueError."""
    s = s.strip()
    # Mixed fraction '48 1/2'
    if " " in s and "/" in s:
        whole, frac = s.split(" ", 1)
        num, den = frac.split("/")
        return float(whole) + float(num) / float(den)
    if "/" in s:
        num, den = s.split("/")
        return float(num) / float(den)
    return float(s.replace(",", "."))


def parse_dim(text: str, source: str = "") -> tuple:
    """Extract (width_cm, height_cm, area_m2, display_str) from `text`.

    Returns (None, None, None, "") when nothing parseable.  Sanitises
    width/height to [1, 1000] cm.  Source key drives H × W vs W × H
    convention — see HW_FIRST_SOURCES.
    """
    if not text:
        return (None, None, None, "")
    m = _DIM_RE.search(text)
    if not m:
        return (None, None, None, "")
    try:
        a = float(m.group(1).replace(',', '.'))
        b = float(m.group(2).replace(',', '.'))
    except ValueError:
        return (None, None, None, "")
    if not (1 <= a <= 1000 and 1 <= b <= 1000):
        return (None, None, None, "")
    if source in HW_FIRST_SOURCES:
        height_cm, width_cm = a, b
    else:
        width_cm, height_cm = a, b
    area = round(width_cm * height_cm / 10000, 4)
    disp = f"{width_cm:g} x {height_cm:g} cm"
    return (width_cm, height_cm, area, disp)


def parse_dim_labelled(text: str) -> tuple:
    """Try labelled dim formats — labels tell us which is W and H,
    so the result IGNORES source convention.

    Returns (w_cm, h_cm, area_m2, display_str) or (None, None, None, "")
    when no labelled pattern matches.

    Patterns tried in priority order:
      1. '48"h, 96"w' (Cadmore / Litchfield inch H/W labels)
      2. 'H. 60 cm - L. 100,5 cm' (French Hauteur/Largeur)
      3. '198cm high, 250cm wide' (Invaluable lacquer panels)

    Both width and height sanitised to [1, 1000] cm.
    """
    if not text:
        return (None, None, None, "")

    # 1) Inch H/W labels
    m = _HW_INCH_LABEL_RE.search(text)
    if m:
        try:
            h_in = _to_float(m.group(1))
            w_in = _to_float(m.group(2))
            w_cm, h_cm = round(w_in * 2.54, 2), round(h_in * 2.54, 2)
            if 1 <= w_cm <= 1000 and 1 <= h_cm <= 1000:
                area = round(w_cm * h_cm / 10000, 4)
                return (w_cm, h_cm, area, f"{w_cm:g} x {h_cm:g} cm")
        except ValueError:
            pass

    # 2) French H./L. cm labels
    m = _HL_CM_LABEL_RE.search(text)
    if m:
        try:
            h_cm = round(_to_float(m.group(1)), 2)
            w_cm = round(_to_float(m.group(2)), 2)
            if 1 <= w_cm <= 1000 and 1 <= h_cm <= 1000:
                area = round(w_cm * h_cm / 10000, 4)
                return (w_cm, h_cm, area, f"{w_cm:g} x {h_cm:g} cm")
        except ValueError:
            pass

    # 3) 'Nh high, Nw wide' — unit may be cm or in; both share unit
    m = _HEIGHT_WIDE_LABEL_RE.search(text)
    if m:
        try:
            h_val = _to_float(m.group(1))
            w_val = _to_float(m.group(3))
            unit = m.group(2).lower()
            if unit.startswith("in") or unit in ('"', "″"):
                h_val *= 2.54
                w_val *= 2.54
            w_cm, h_cm = round(w_val, 2), round(h_val, 2)
            if 1 <= w_cm <= 1000 and 1 <= h_cm <= 1000:
                area = round(w_cm * h_cm / 10000, 4)
                return (w_cm, h_cm, area, f"{w_cm:g} x {h_cm:g} cm")
        except ValueError:
            pass

    return (None, None, None, "")


def parse_dim_smart(text: str, source: str = "") -> tuple:
    """Try labelled formats first; fall back to plain parse_dim().

    Use this in aggregator crawlers (Invaluable, Drouot) where the text
    comes from many upstream houses with mixed conventions — labelled
    patterns disambiguate themselves and the source-convention fallback
    only fires when nothing labelled matches.

    For direct crawlers with consistent format use parse_dim() directly.
    """
    res = parse_dim_labelled(text)
    if res[0] is not None:
        return res
    res = parse_dim(text, source)
    if res[0] is not None:
        return res
    # Inch fallback — direct crawlers rarely hit this but Invaluable's
    # US-regional uppstreams (Litchfield etc.) sometimes use inch-only.
    m = _DIM_INCH_RE.search(text)
    if m:
        try:
            a = float(m.group(1).replace(",", "."))
            b = float(m.group(2).replace(",", "."))
            w_in, h_in = (b, a) if source in HW_FIRST_SOURCES else (a, b)
            w_cm, h_cm = round(w_in * 2.54, 2), round(h_in * 2.54, 2)
            if 1 <= w_cm <= 1000 and 1 <= h_cm <= 1000:
                area = round(w_cm * h_cm / 10000, 4)
                return (w_cm, h_cm, area, f"{w_cm:g} x {h_cm:g} cm")
        except ValueError:
            pass
    return (None, None, None, "")
