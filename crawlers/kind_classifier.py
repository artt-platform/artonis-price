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


SCULPT_DESC_KWS = (
    'sans le socle', 'without the base', 'with base', 'avec socle',
    ' socle ', 'pedestal', 'patiné', 'patinated',
)


def classify_kind(medium, fallback='painting', description=None, dimensions=None):
    """Return 'painting' / 'drawing' / 'print' / 'sculpture'.

    Sculpture is detected in 3 layers (any one is sufficient):
      1. Material keyword in medium (bronze, terracotta, lead, etc.).
      2. 3D-only markers in description (socle, pedestal, patiné).
      3. Single-axis dim ('H 50.5 cm') with a wood/mixed medium — single
         axis implies 3D even when the material phrase reads paint-on-support
         (e.g. Lebadang 'Personnage', mixed media on wood, H 50.5 cm sans le
         socle — kind=painting before, now correctly sculpture).
    """
    if not medium:
        return fallback
    m = medium.lower()
    has_paint = any(kw in m for kw in PAINT_KWS)
    has_sculpt = any(kw in m for kw in SCULPT_KWS)
    has_print = any(kw in m for kw in PRINT_KWS)
    has_draw = any(kw in m for kw in DRAW_KWS)
    if description:
        d = description.lower()
        if any(kw in d for kw in SCULPT_DESC_KWS):
            has_sculpt = True
    if dimensions:
        dl = dimensions.strip()
        if (dl.startswith('H ') or dl.startswith('L ') or dl.startswith('W ')) \
                and ('wood' in m or 'bois' in m or 'mixed media' in m):
            has_sculpt = True
    if has_print:
        return 'print'
    if has_sculpt:
        return 'sculpture'
    if has_draw and not has_paint:
        return 'drawing'
    if has_paint:
        return 'painting'
    return fallback
