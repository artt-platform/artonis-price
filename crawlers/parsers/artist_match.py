"""Strict catalog match for artist names.

Used by aggregator/search-based crawlers (auction_catalog_platform,
Invaluable card scraper, Dawsons, etc.) to validate a candidate
artist name BEFORE inserting.  The Lawsons/Akiba Phase 2 lesson:
naive regex extracts 'Pair of Vintage' or 'Four' as artist names if
not gated.

Three-level match:
  1. Normalised direct match against catalog.
  2. Word-sort match (handles 'Viet Dung Hong' ↔ 'Hong Viet Dung').
  3. Mononym whole-word substring (≥ 6 chars) — 'Hoi Lebadang'
     contains catalog 'lebadang'.
"""
import re
import sys
import unicodedata
from pathlib import Path


def _normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize('NFD', name)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def load_catalog() -> set:
    """Load VN_ARTIST_CATALOG as a set of normalized names."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG
    return set(VN_ARTIST_CATALOG)


_NOISE_TAIL_RE = re.compile(
    r"\s+(?:vietnamese?|vietnam|french|chinese|american|british|"
    r"french-vietnamese|vietnamese-french)[\s,.\-]*.*$",
    re.IGNORECASE,
)
_BIO_TAIL_RE = re.compile(r"\s+b\s+\d{4}.*$", re.IGNORECASE)


def match_to_catalog(raw_name: str, catalog: set | None = None) -> str | None:
    """Return canonical normalized name from catalog when raw_name maps,
    else None.

    Strips trailing nationality / bio noise ('Le Pho (FRENCH-VIETNAMESE,
    B. 1907-2001)' → 'le pho') then tries direct match → word-sort →
    mononym substring.
    """
    if not raw_name:
        return None
    if catalog is None:
        catalog = load_catalog()
    norm = _normalize(raw_name)
    norm = _NOISE_TAIL_RE.sub("", norm)
    norm = _BIO_TAIL_RE.sub("", norm).strip()
    if not norm or len(norm) < 4:
        return None
    # Direct match
    if norm in catalog:
        return norm
    # Word-sort match
    sorted_norm = " ".join(sorted(norm.split()))
    for cand in catalog:
        if " ".join(sorted(cand.split())) == sorted_norm:
            return cand
    # Whole-word mononym substring (min 6 chars to avoid 'le' matching everything)
    padded = " " + norm + " "
    for cand in catalog:
        if len(cand) < 6:
            continue
        if (" " + cand + " ") in padded:
            return cand
    return None
