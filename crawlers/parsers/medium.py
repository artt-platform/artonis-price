"""Medium keyword extraction from catalog text.

Each crawler used to duplicate this keyword list with slightly
different spellings.  One central list keeps medium values consistent
across sources for grouping in stats.
"""
import re


# Order: most specific first ('lithograph in color' before 'lithograph').
_MEDIUM_KEYWORDS = [
    # English
    "oil on canvas", "oil on board", "oil on panel", "oil on masonite",
    "oil on paper", "oil on silk",
    "watercolour on paper", "watercolor on paper",
    "watercolour on silk", "watercolor on silk",
    "gouache on paper", "gouache on silk", "gouache on board",
    "ink and colour on silk", "ink and color on silk",
    "ink on paper", "ink on silk",
    "lacquer on wood", "lacquer on board", "lacquer on panel",
    "lithograph in color", "lithography on paper",
    "lithograph with embossment", "color lithograph", "lithograph",
    "intaglio etching", "screenprint", "silkscreen", "etching", "engraving",
    "mixed media on canvas", "mixed media on board", "mixed media on wood",
    "mixed media on paper",
    "acrylic on canvas", "acrylic on board", "acrylic on paper",
    "pastel on paper",
    # French (Bonhams/Aguttes/Millon/Drouot)
    "huile sur toile", "huile sur panneau", "huile sur bois",
    "huile sur papier",
    "gouache sur papier", "gouache sur soie",
    "aquarelle sur papier", "aquarelle sur soie",
    "encre et couleurs sur soie", "encre sur papier", "encre sur soie",
    "encre et couleurs sur papier",
    "encre, couleurs et crayon sur papier",
    "encre, couleurs et crayon sur papier de riz",
    "laque sur bois", "laque sur panneau", "laque sur papier",
    "panneau en bois laqué", "panneau laqué",
    "bois laqué polychrome", "bois laqué",
    "technique mixte sur bois", "technique mixte sur papier",
    "technique mixte sur toile",
    "pastel sur papier", "pastel sur carton",
    "fusain et sanguine",
    "aquatinte", "pointe sèche", "eau-forte sur papier",
    # Standalone (lower priority — fall-through after specific phrases)
    "oil paint", "lacquer", "lithograph",
]


def extract_medium(text: str) -> str:
    """Find the first medium keyword in `text` (case-insensitive).
    Returns lowercased phrase, or '' when nothing matches.
    """
    if not text:
        return ""
    t = text.lower()
    for kw in _MEDIUM_KEYWORDS:
        if kw in t:
            return kw
    return ""
