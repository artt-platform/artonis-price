"""Detect 'fake attribution' lots — works that are NOT confirmed by
the named artist (attributed, after, circle of, workshop of, copy).

In the auction market, an 'Attributed to' or 'Cercle de' lot is NOT
the artist's confirmed work — uncertain provenance, often a fraction
of authenticated-piece price.  Including them in a per-artist median
skews the index downward.

Industry convention (Artprice / Artnet / MutualArt): exclude
attribution lots from artist price stats.  This module centralises
the detection so every crawler (Invaluable, Millon, Bonhams,
Dawsons, etc.) applies the same rule.

Match against a URL slug or a plain string ('after Bui Xuan Phai',
'Cercle de Lebadang').
"""
import re


# URL-slug pattern.  Conservative — only patterns that UNAMBIGUOUSLY
# indicate attribution.  'circle-of' is excluded because 'circle-of-life'
# is a common title slug.  'after' and 'workshop-of' likewise unsafe
# in URL form.  Those go in the TEXT pattern only with stricter context.
_URL_MARKERS = (
    "attr", "attrib", "attributed",
    "attributed-to", "attr-to",
    "d-apres", "dapres", "d-après",
    "copy",
    "atelier-de", "cercle-de", "entourage-de", "ecole-de",
)

_URL_RE = re.compile(
    r"(?:^|/|[-_])(?:" + "|".join(re.escape(m) for m in _URL_MARKERS) + r")(?:[-_/]|$)",
    re.IGNORECASE,
)

# Text pattern — explicit phrases at START of artist_name or title.
# Anchored to ^ or sentence start to avoid 'after hours' / 'circle of life'.
_TEXT_RE = re.compile(
    r"(?:^|^\s*)(?:attributed to|attr\.?\s+to|"
    r"d['’ ]?apr[eè]s|"
    r"atelier de|cercle de|entourage de|école de|"
    r"copie d['’]apr[eè]s)\s+[A-ZÀ-Ÿ]",
    re.IGNORECASE,
)


def is_attribution(slug_or_url: str, text: str = "") -> bool:
    """True when the URL slug OR the description text indicates the
    lot is an attribution / copy / after-style work.

    Pass URL or slug as `slug_or_url`; optionally pass title/desc as
    `text` for descriptions that don't reach the URL.
    """
    if slug_or_url:
        # Pick path portion if it's a full URL
        m = re.search(r"://[^/]+(/.*)", slug_or_url)
        path = m.group(1) if m else slug_or_url
        if _URL_RE.search(path):
            return True
    if text and _TEXT_RE.search(text):
        return True
    return False
