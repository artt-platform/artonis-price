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
DIRECT_OWNED_HOUSE_NAMES = {
    # Top international houses
    "bonhams",
    "bonhams 3",
    "christie's",
    "christies",
    "sotheby's",
    "sothebys",
    "phillips",
    # France
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
    # Asia
    "ravenel",
    "larasati auctioneers",
    "larasati",
    # Vietnam
    "le auction",
    "lê auction",
    # US regional with direct crawler
    "everard auctions and appraisals",
    "everard",
    "austin auction gallery",
    "austin auction",
    "joshua kodner",
    "akiba galleries",
    "lawsons",
    "john moran auctioneers",
    "john moran",
    "shapiro auctions llc",
    "shapiro auctioneers",
    "shapiro",
    # UK
    "dawsons auctioneers",
    "dawsons",
    # Misc with direct
    "global auction",
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
