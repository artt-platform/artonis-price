"""Single source of truth for 'we own this house directly'.

Used by aggregator crawlers (Invaluable, Drouot platform-discovery)
to SKIP lots whose underlying auction house has its own direct crawler.

Policy: direct source = canonical.  Aggregator crawlers are only for
houses we don't have a direct path to.  Without this rule the same lot
ends up in DB twice (Austin Auction $1200 via direct + $500 via
Invaluable mid-estimate proxy) and price stats get noised.

When adding a new direct-source crawler, add ALL its case-insensitive
sale_location / auction_house spelling variants here.
"""

# Each entry is the spelling that appears on Invaluable's lot detail
# 'Auction House' link.  Lower-cased + trimmed match.  Include all
# punctuation/trailing-digit variants you see in the wild ('Bonhams 3').
#
# Policy: include a house here ONLY when its direct crawler has actually
# seeded data.  If we exclude an Invaluable upstream whose direct crawler
# is broken or hasn't run yet, we lose lots with no fallback.  Houses
# with 0 (shapiro, dawsons, global_auction) or barely-tested (john_moran 2,
# larasati 3) direct lots stay OFF this list until their direct crawler
# proves robust.
DIRECT_OWNED_HOUSE_NAMES = {
    # Top international houses — well-tested, hundreds of lots each
    "bonhams",
    "bonhams 3",
    "christie's",
    "christies",
    "sotheby's",
    "sothebys",
    "phillips",
    # France — hundreds of lots from direct
    "aguttes",
    "tajan",
    "artcurial",
    "drouot",
    "osenat",
    "gros-delettrez",
    "gros & delettrez",
    "gros and delettrez",
    "gros delettrez",
    "millon",
    # Invaluable surfaces Millon vente 2120 (and other Vietnamese Millon
    # sales) under the upstream label 'Asium' — same lots, same hammers,
    # same dim, often even same image_phash.  Operator caught 5 Pham
    # Hau dup pairs 2026-06-27 from sale 2019-06-14.  Aliased here so
    # the Invaluable crawler skips them like any other Millon lot.
    "asium",
    # Asia
    "ravenel",
    # Vietnam
    "le auction",
    "lê auction",
    # US regional with direct crawler — verified ≥10 lots
    "everard auctions and appraisals",
    "everard",
    "akiba galleries",
    "lawsons",
    # Asia regional with direct crawler — verified ≥10 lots (50 inserted 2026-06-24)
    "33 auction",
    # Newer direct crawlers — kept OFF until proven coverage:
    #   "shapiro", "shapiro auctions llc", "shapiro auctioneers"  (0 direct lots)
    #   "dawsons", "dawsons auctioneers"                          (0 direct lots)
    #   "global auction"                                          (0 direct lots)
    #   "john moran", "john moran auctioneers"                    (only 2 direct)
    #   "larasati", "larasati auctioneers"                        (only 3 direct)
    #   "austin auction gallery", "austin auction"                (only 2 direct)
    #   "joshua kodner"                                           (only 6 direct)
}


def is_direct_owned(house_name: str) -> bool:
    """True when the named auction house has its own direct crawler.

    Strips trailing-digit variants ('Bonhams 3' → 'bonhams') and
    matches case-insensitively against DIRECT_OWNED_HOUSE_NAMES.
    """
    if not house_name:
        return False
    s = house_name.strip().lower()
    if s in DIRECT_OWNED_HOUSE_NAMES:
        return True
    # Strip trailing variant suffix
    import re
    base = re.sub(r"\s+\d+$", "", s)
    return base in DIRECT_OWNED_HOUSE_NAMES


# Auction houses we explicitly EXCLUDE from any crawl path —
# typically because the operator flagged them as low-trust (fake
# paintings, lots re-listed multiple sales with no buyer, mis-
# attribution at scale, etc.).  Different from `is_direct_owned`:
# direct-owned houses ARE crawled (just via our own path).  Excluded
# houses are SKIPPED everywhere.
EXCLUDED_HOUSES = {
    # 2026-06-25 — operator: "nhiều tranh giả, nhiều tranh đăng đi
    # đăng lại nhiều phiên mà ko ai mua".  All Cadmore lots in our
    # DB had hammer=None (passed every sale).
    "cadmore auctions",
    "cadmore",
}


def is_excluded(house_name: str) -> bool:
    """True when the house should be SKIPPED across all crawl paths
    (low-trust / fake-painting concerns)."""
    if not house_name:
        return False
    s = house_name.strip().lower()
    if s in EXCLUDED_HOUSES:
        return True
    import re
    base = re.sub(r"\s+\d+$", "", s)
    return base in EXCLUDED_HOUSES
