"""Shared parsing utilities for all auction-house crawlers.

The old approach: each crawler had its own copy of the dim regex, the
medium keyword list, the bilingual-strip logic.  Sothebys used
'X by Y cm', Christies used 'X x Y cm', and adding 'by' to one place
didn't fix the other.  Same for medium keywords and provenance cleanup.

This module centralises those.  Each crawler imports the utility,
passes its source key for convention-aware behaviour (e.g. H × W vs
W × H), and gets back consistent output.

Modules:
  dim.py         — parse_dim(text, source) → (w_cm, h_cm, area_m2, dim_str)
  medium.py      — extract_medium(text) → str
  provenance.py  — strip_bilingual(prov) → str
  artist_match.py — match_to_catalog(name) → canonical name | None
"""
from .dim import parse_dim, HW_FIRST_SOURCES
from .medium import extract_medium
from .provenance import strip_bilingual
from .artist_match import match_to_catalog, load_catalog

__all__ = [
    'parse_dim', 'HW_FIRST_SOURCES',
    'extract_medium',
    'strip_bilingual',
    'match_to_catalog', 'load_catalog',
]
