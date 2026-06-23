"""Shared kind classifier — decides if a lot is painting / drawing /
print / sculpture from its medium string.

Several crawlers (Christie's, Sotheby's, Aguttes, Le Auction) default
kind='painting' without checking medium.  That mislabels every
sculpture, lithograph, and charcoal sketch they ingest.  Use this
helper instead of hard-coding kind in the crawler.

Order: print > sculpture > drawing > painting.  Keywords cover both
French and English catalog vocabulary."""

SCULPT_KWS = ('sculpture', 'bronze', 'terracotta', 'terre cuite', 'plâtre', 'plaster',
              'marble sculpture', 'marbre, patine', 'lead sculpture',
              'résine', 'resin sculpture', 'ceramic', 'céramique', 'cast iron')
PRINT_KWS = ('lithograph', 'etching', 'eau-forte', 'screenprint',
             'silkscreen', 'sérigraph', 'serigraph', 'gravure', 'woodcut',
             'estampe', 'engraving', 'intaglio')
DRAW_KWS = ('pencil', 'crayon', 'charcoal', 'fusain', 'sanguine', 'graphite', 'pastel')
PAINT_KWS = ('huile', 'oil', 'gouache', 'aquarelle', 'watercolour', 'watercolor',
             'acrylique', 'acrylic', 'tempera', 'laque', 'lacquer', 'soie',
             'oil on', 'oil paint')


def classify_kind(medium, fallback='painting'):
    """Return 'painting' / 'drawing' / 'print' / 'sculpture' from the
    medium string.  Falls back to `fallback` when medium is empty or
    unrecognised (caller can pass None to leave kind unset)."""
    if not medium:
        return fallback
    m = medium.lower()
    has_paint = any(kw in m for kw in PAINT_KWS)
    has_sculpt = any(kw in m for kw in SCULPT_KWS)
    has_print = any(kw in m for kw in PRINT_KWS)
    has_draw = any(kw in m for kw in DRAW_KWS)
    if has_print:
        return 'print'
    if has_sculpt and not has_paint:
        return 'sculpture'
    if has_draw and not has_paint and not has_sculpt:
        return 'drawing'
    if has_paint:
        return 'painting'
    return fallback
