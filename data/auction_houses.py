"""Reference data for auction houses: buyer's premium rates, taxes, locations.
Premium rates are approximate and updated 2024-2025.
Source: each house's published buyer's premium schedule."""

AUCTION_HOUSES = {
    "bonhams": {
        "name": "Bonhams",
        "country": "UK (global offices)",
        "founded": 1793,
        "premium_rate_pct": 28.0,            # 28% on first £400k, sliding down
        "premium_note": "28% up to £400k, 27% £400k–£4m, 21% above; +2% online",
        "vat_pct": 20.0,                      # UK VAT on premium
        "tax_note": "VAT charged on buyer's premium only for UK/EU buyers",
        "vietnamese_art_dept": "Southeast Asian Modern & Contemporary Art",
        "website": "https://www.bonhams.com",
    },
    "sothebys": {
        "name": "Sotheby's",
        "country": "UK/US (global)",
        "founded": 1744,
        "premium_rate_pct": 26.0,
        "premium_note": "26% up to $1M, 21% $1M–$8M, 15% above",
        "vat_pct": 20.0,
        "tax_note": "VAT on premium for UK/EU; US sales tax by state",
        "vietnamese_art_dept": "Modern & Contemporary Southeast Asian Art",
        "website": "https://www.sothebys.com",
    },
    "christies": {
        "name": "Christie's",
        "country": "UK/US (global)",
        "founded": 1766,
        "premium_rate_pct": 26.0,
        "premium_note": "26% up to $1M, 21% $1M–$6M, 14.5% above",
        "vat_pct": 20.0,
        "tax_note": "VAT on premium for UK/EU; US sales tax by state",
        "vietnamese_art_dept": "Asian 20th Century & Contemporary Art",
        "website": "https://www.christies.com",
    },
    "millon": {
        "name": "Millon",
        "country": "France",
        "founded": 1925,
        "premium_rate_pct": 28.0,             # Typical French house
        "premium_note": "~28% TTC (tax included)",
        "vat_pct": 20.0,
        "tax_note": "TVA 20% on premium; droit de suite for artists under 70yrs post-mortem",
        "vietnamese_art_dept": "Arts d'Asie / Art Moderne",
        "website": "https://www.millon.com",
    },
    "invaluable": {
        "name": "Invaluable",
        "country": "US (online platform)",
        "kind": "platform",                    # NOT a house — aggregates lots from many houses
        "founded": 1989,
        "premium_rate_pct": 25.0,
        "premium_note": "Platform: premium set by each underlying house (typical 22-28%)",
        "vat_pct": 0.0,
        "tax_note": "Tax determined by underlying house",
        "vietnamese_art_dept": "Aggregates ~5,000 houses worldwide",
        "website": "https://www.invaluable.com",
    },
    "drouot": {
        "name": "Drouot",
        "country": "France (online platform)",
        "kind": "platform",
        "founded": 1852,
        "premium_rate_pct": 28.0,
        "premium_note": "Platform: each operator-house in Hôtel Drouot sets its own premium (20-30%)",
        "vat_pct": 20.0,
        "tax_note": "TVA 20% on premium; varies per operator",
        "vietnamese_art_dept": "Multi-house Indochine art venue (Aguttes, Cornette, Tessier, …)",
        "website": "https://drouot.com",
    },
    "cornette": {
        "name": "Cornette de Saint-Cyr",
        "country": "France (Bonhams Group since 2022)",
        "founded": 1973,
        "premium_rate_pct": 28.8,
        "premium_note": "24% to €500k, 20% above; +VAT",
        "vat_pct": 20.0,
        "tax_note": "TVA 20% on premium",
        "vietnamese_art_dept": "Arts d'Asie",
        "website": "https://www.bonhams.com",
    },
    "aguttes": {
        "name": "Aguttes",
        "country": "France (Neuilly-sur-Seine)",
        "founded": 1974,
        "premium_rate_pct": 26.0,
        "premium_note": "26% up to €900k, 23% above; +TVA",
        "vat_pct": 20.0,
        "tax_note": "TVA 20% on premium; droit de suite for living artists",
        "vietnamese_art_dept": "Peintres d'Asie — Œuvres Majeures (regular VN-focused sales)",
        "address": "164 bis avenue Charles de Gaulle, 92200 Neuilly-sur-Seine",
        "website": "https://www.aguttes.com",
    },
    "global-auction": {
        "name": "Global Auction",
        "country": "Indonesia/Singapore (HQ Jakarta + offices Singapore, Kuala Lumpur, Hong Kong)",
        "founded": 2004,
        "premium_rate_pct": 22.0,
        "premium_note": "22% flat of hammer price",
        "vat_pct": 11.0,
        "tax_note": "PPN 11% (Indonesia VAT) on premium for buyers in Indonesia",
        "vietnamese_art_dept": "Southeast Asian, Chinese, Modern & Contemporary Art (occasional VN lots — Bui Huu Hung, Le Pho, Đặng Phương Việt, etc.)",
        "address": "Jakarta, Indonesia (Pte Ltd registered Singapore)",
        "website": "https://global.auction",
    },
}


def get_house(source_slug):
    """Return house info dict or None."""
    return AUCTION_HOUSES.get(source_slug.lower())
