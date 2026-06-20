"""Roseberys London crawler — discovers past sales via sitemap, parses lot blocks
from sale-page HTML (no JS rendering required).

URL pattern: /bidding/A{4-digit}-{slug}-{auctioneer-id}
Lot block: <div class="lotListing card sml row">LOT N {Artist}, {nationality} {dates} -
           {title}; {medium}, {dims}... Estimate: £X - £Y Price Realised: £Z View Lot</div>

Per-sale yield is limited to ~13 "highlight" lots in current HTML; full lot lists
require lot-page fetches. We capture the highlights now and revisit if needed.
"""
import re
import time
from datetime import datetime, timezone
import cloudscraper
from bs4 import BeautifulSoup

from crawlers.common import insert_sale_result, clean_artist_name, log_crawl_run


BASE = "https://www.roseberys.co.uk"
GBP_USD = 1.27  # approximate; refresh when running annual stats

# Vietnamese name fragments — used to filter lot blocks before insert.
_VN_FRAGMENTS = (
    "vietnam", "vietnamese", "lebadang", "le ba dang", "le pho", "mai trung",
    "vu cao dam", "le thi luu", "nguyen ", "tran ", "bui ", "duong bich",
    "pham hau", "pham an hai", "dinh quan", "dang xuan", "dao hai phong",
    "le thanh son", "hong viet dung", "thanh chuong", "tran luu hau",
    "to ngoc van", "tran van can", "phan chanh", "ho huu thu",
)

# Roseberys lot block parser. Group order:
#   1 lot_no, 2 artist, 3 nationality, 4 dates (YYYY or YYYY-YYYY),
#   5 title, 6 medium+dims, 7 est_low, 8 est_high, 9 realised
_LOT_RE = re.compile(
    r"LOT\s+(\d+)\s+(.+?),\s*([A-Za-z\-/]+)\s+(\d{4}(?:[-–]\d{4})?)\s*-\s*"
    r"(.+?);\s*(.+?)\.{0,3}\s+Estimate:\s*£([\d,]+)\s*[-–]\s*£([\d,]+)\s+"
    r"(?:Price Realised:\s*£([\d,]+)|(Unsold|Withdrawn|Lot Withdrawn))",
    re.IGNORECASE,
)

# Strip trailing fuzz from medium+dimensions
_DIM_RE = re.compile(r"(.+?),\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(cm|in)\b")


def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True}
    )


def list_past_sales(scraper=None):
    """Pull all past-sale URLs from Roseberys sitemap."""
    scraper = scraper or _make_scraper()
    urls = set()
    for n in (1, 2):
        r = scraper.get(f"{BASE}/sitemap/sitemap{n}.xml", timeout=30)
        if r.status_code != 200:
            continue
        urls.update(re.findall(r"/bidding/(A\d+[\-a-z0-9]+)", r.text))
    return sorted(urls)


def parse_sale_page(html):
    """Yield lot dicts for each .lotListing block on a sale-results page."""
    soup = BeautifulSoup(html, "html.parser")
    sale_title = ""
    t = soup.find("title")
    if t:
        sale_title = re.sub(r"\s*\|\s*Roseberys.*$", "", t.get_text(strip=True)).strip()[:200]
    # Sale date from page: look for "Sale Date: 5 February 2024" or similar
    sale_date = ""
    page_text = soup.get_text(" ", strip=True)
    m_date = re.search(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        page_text,
    )
    if m_date:
        try:
            sale_date = datetime.strptime(
                f"{m_date.group(1)} {m_date.group(2)} {m_date.group(3)}", "%d %B %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    for el in soup.find_all("div", class_=re.compile(r"\blotListing\b")):
        text = el.get_text(" ", strip=True)
        m = _LOT_RE.search(text)
        if not m:
            continue
        lot_no, artist, nationality, dates, title, medium_dims, est_low, est_high, realised, unsold = m.groups()

        m_yr = re.match(r"(\d{4})(?:[-–](\d{4}))?", dates)
        birth = int(m_yr.group(1)) if m_yr else None
        death = int(m_yr.group(2)) if m_yr and m_yr.group(2) else None

        medium = medium_dims.strip()
        width_cm = height_cm = None
        m_dim = _DIM_RE.search(medium_dims)
        if m_dim:
            medium = m_dim.group(1).strip()
            try:
                width_cm = float(m_dim.group(2))
                height_cm = float(m_dim.group(3))
                if m_dim.group(4) == "in":
                    width_cm *= 2.54
                    height_cm *= 2.54
            except ValueError:
                pass

        hammer = int(realised.replace(",", "")) if realised else None
        status = "sold" if hammer else ("withdrawn" if unsold else "unsold")
        yield {
            "lot_number": lot_no,
            "artist_raw": artist.strip(),
            "nationality": nationality.strip(),
            "birth_year": birth,
            "death_year": death,
            "artwork_title": title.strip(),
            "medium": medium,
            "width_cm": round(width_cm, 2) if width_cm else None,
            "height_cm": round(height_cm, 2) if height_cm else None,
            "area_m2": round(width_cm * height_cm / 10000, 4) if (width_cm and height_cm) else None,
            "estimate_low": int(est_low.replace(",", "")),
            "estimate_high": int(est_high.replace(",", "")),
            "hammer_price": hammer,
            "currency": "GBP",
            "price_usd": round(hammer * GBP_USD, 2) if hammer else None,
            "status": status,
            "sale_title": sale_title,
            "sale_date": sale_date,
        }


def _looks_vn(text):
    low = text.lower()
    return any(frag in low for frag in _VN_FRAGMENTS)


def crawl(conn, sale_urls=None, delay=2.5, verbose=True, max_pages=400):
    """Fetch each past sale, parse lots, insert VN-matching ones."""
    scraper = _make_scraper()
    if sale_urls is None:
        if verbose:
            print("  [roseberys] discovering past sales...", flush=True)
        slugs = list_past_sales(scraper)
        sale_urls = [f"{BASE}/bidding/{s}" for s in slugs]
        if verbose:
            print(f"  [roseberys] found {len(sale_urls)} past sales", flush=True)
    sale_urls = sale_urls[:max_pages]

    total_inserted = 0
    for i, sale_url in enumerate(sale_urls, 1):
        run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = scraper.get(sale_url, timeout=30)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(sale_urls)}] ERR {e}", flush=True)
            log_crawl_run(conn, "roseberys", target_slug=sale_url[-60:],
                          started_at=run_started, status="error", note=str(e)[:200])
            time.sleep(delay)
            continue
        if r.status_code != 200:
            log_crawl_run(conn, "roseberys", target_slug=sale_url[-60:],
                          started_at=run_started, status="error",
                          note=f"HTTP {r.status_code}")
            time.sleep(delay)
            continue

        inserted_this = 0
        sale_date_min = sale_date_max = None
        for lot in parse_sale_page(r.text):
            full_text = f"{lot['artist_raw']} {lot['nationality']}"
            if not _looks_vn(full_text):
                continue
            artist_raw, _ = clean_artist_name(lot["artist_raw"])
            if not artist_raw:
                continue
            ppm2 = (
                round(lot["price_usd"] / lot["area_m2"], 2)
                if (lot["price_usd"] and lot["area_m2"])
                else None
            )
            rec = {
                "source": "roseberys",
                "source_url": f"{sale_url}#lot-{lot['lot_number']}",
                "sale_page_url": sale_url,
                "lot_number": lot["lot_number"],
                "auction_title": f"Roseberys — {lot['sale_title']}"[:300],
                "sale_date": lot["sale_date"] or None,
                "sale_location": "London",
                "artist_name_raw": artist_raw,
                "artwork_title": lot["artwork_title"][:300],
                "medium": lot["medium"][:200],
                "dimensions": (
                    f"{lot['width_cm']}x{lot['height_cm']} cm"
                    if lot["width_cm"]
                    else ""
                ),
                "width_cm": lot["width_cm"],
                "height_cm": lot["height_cm"],
                "area_m2": lot["area_m2"],
                "year": "",
                "estimate_low": lot["estimate_low"],
                "estimate_high": lot["estimate_high"],
                "hammer_price": lot["hammer_price"],
                "currency": lot["currency"],
                "status": lot["status"],
                "raw_snapshot": (lot["artist_raw"] + " | " + lot["artwork_title"])[:500],
            }
            # Persist via the same helper the rest of the crawlers use
            insert_sale_result(conn, rec)
            inserted_this += 1
            if lot["sale_date"]:
                sd = lot["sale_date"]
                if sale_date_min is None or sd < sale_date_min:
                    sale_date_min = sd
                if sale_date_max is None or sd > sale_date_max:
                    sale_date_max = sd
        conn.commit()
        log_crawl_run(
            conn, "roseberys",
            target_slug=sale_url[-60:],
            started_at=run_started,
            sale_date_min=sale_date_min,
            sale_date_max=sale_date_max,
            lots_inserted=inserted_this,
            status="ok",
        )
        total_inserted += inserted_this
        if verbose and inserted_this:
            print(f"  [{i}/{len(sale_urls)}] {sale_url[-50:]}: +{inserted_this} VN lots", flush=True)
        time.sleep(delay)
    return total_inserted
