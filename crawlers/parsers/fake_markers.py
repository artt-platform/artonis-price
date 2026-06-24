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


# UNAMBIGUOUS attribution markers — these slug fragments are ALWAYS
# attribution (no real title contains 'attributed-to' or 'd-apres').
_UNAMBIG_URL_MARKERS = (
    "attr", "attrib", "attributed",
    "attributed-to", "attr-to",
    "d-apres", "dapres", "d-après",
    "copie-de",
    "atelier-de", "cercle-de", "entourage-de", "ecole-de",
)

# Ambiguous markers — match REAL titles too ('Circle of Life', 'After
# hours').  Require the marker to be IMMEDIATELY followed by a VN
# artist's family name in the URL slug.  That distinguishes
# 'manner-of-bui-xuan-phai' (attribution) from 'circle-of-life' (title).
_AMBIG_MARKERS = (
    "after", "circle-of", "manner-of", "workshop-of",
    "follower-of", "school-of",
)
# VN family names + mononym artists from the catalog.  When a marker
# is directly followed by one of these, we're confident it's attribution.
_VN_NAME_PREFIX = (
    "nguyen", "tran", "le", "pham", "vu", "bui", "dao", "ho", "hoang",
    "huynh", "phan", "doan", "lam", "truong", "duong", "dinh", "mai",
    "cao", "dang", "ly", "ngo", "tang", "thai", "hong",
    # Mononyms / variants
    "lebadang", "le-pho", "mai-thu", "mai-trung-thu", "bui-xuan-phai",
    "vu-cao-dam", "nguyen-gia-tri", "alix-ayme",
)

_UNAMBIG_URL_RE = re.compile(
    r"(?:^|/|[-_])(?:" + "|".join(re.escape(m) for m in _UNAMBIG_URL_MARKERS) + r")(?:[-_/]|$)",
    re.IGNORECASE,
)
_AMBIG_URL_RE = re.compile(
    r"(?:^|/|[-_])(?:" + "|".join(re.escape(m) for m in _AMBIG_MARKERS) +
    r")-(?:" + "|".join(re.escape(n) for n in _VN_NAME_PREFIX) + r")(?:[-_/]|$)",
    re.IGNORECASE,
)

# Text pattern — phrases in raw description / title.  Two flavours:
# unambiguous markers anywhere (text uses spaces; 'attributed to' or
# 'd'après' never appears mid-sentence by accident) + ambiguous
# markers only when followed by a Capitalized name.
_TEXT_UNAMBIG_RE = re.compile(
    r"\b(?:attributed to|attr\.?\s+to|"
    r"d['’ ]?apr[eè]s|"
    r"atelier de|cercle de|entourage de|école de|"
    r"copie d['’]apr[eè]s)\b",
    re.IGNORECASE,
)
# Ambiguous text markers require the next token to be a known VN
# family name (capitalised).  Without this, 'Circle of Life' would
# trip — Life is capitalized in title case.
_VN_FAMILY_CAPS = (
    "Bui", "Cao", "Dang", "Dao", "Dinh", "Doan", "Duong", "Ho",
    "Hoang", "Hong", "Huynh", "Lam", "Le", "Mai", "Ngo", "Nguyen",
    "Pham", "Phan", "Tang", "Thai", "Tran", "Truong", "Vu", "Ly",
    # Foreign-name artists working in VN
    "Alix", "Joseph", "Victor",
    # Mononym
    "Lebadang",
)
_TEXT_AMBIG_RE = re.compile(
    r"\b(?i:after|circle of|manner of|workshop of|follower of|school of)"
    r"\s+(?:" + "|".join(re.escape(n) for n in _VN_FAMILY_CAPS) + r")\b",
)


def is_attribution(slug_or_url: str, text: str = "") -> bool:
    """True when the URL slug OR the description text indicates the
    lot is an attribution / copy / after-style work.

    URL form: unambiguous markers (attr, dapres, atelier-de…) match
    anywhere; ambiguous markers (after, circle-of, manner-of, workshop-
    of, follower-of, school-of) match ONLY when directly followed by a
    VN family-name token — distinguishes 'circle-of-life' (title) from
    'manner-of-bui-xuan-phai' (attribution).

    Text form: same split between unambiguous phrases ('attributed to',
    "d'après") and ambiguous ones that require a Capitalized name after.
    """
    if slug_or_url:
        m = re.search(r"://[^/]+(/.*)", slug_or_url)
        path = m.group(1) if m else slug_or_url
        if _UNAMBIG_URL_RE.search(path):
            return True
        if _AMBIG_URL_RE.search(path):
            return True
    if text:
        if _TEXT_UNAMBIG_RE.search(text):
            return True
        if _TEXT_AMBIG_RE.search(text):
            return True
    return False
