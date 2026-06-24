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
