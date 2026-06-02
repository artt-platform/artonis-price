"""Phillips crawler — parses server-rendered lot tiles from /auction/{SALE_ID} pages.

Phillips publishes Seldon (their design system) lot-tile components inline in HTML:
each tile is wrapped in `<a data-testid="lot-{lot_id}-object-tile">` and carries the
artist, work title, lot number, estimate, and (for closed sales) the "Sold For" price.
All lots fit on one page — no pagination needed. Sale-level metadata
(name, date, location, currency) lives in JSON-LD `<script type="application/ld+json">`.

Phillips' edge blocks plain `requests` with HTTP 403 after a few calls, so we use
`cloudscraper` for the gentle TLS-fingerprint bypass. Playwright is NOT required for
either the auction listing or the lot detail pages — the HTML is fully server-rendered.

Per-lot detail enrichment (medium / dimensions / year) comes from
`https://www.phillips.com/detail/{slug}/{lot_id}`, inside the
`pah-lot-cataloguing-section` block.

VN art at Phillips: HK office's "20th Century & Contemporary Art" Day/Evening sales
(HK0104**) and "Faces Places" online sales (HK0906**) carry Lê Phổ, Mai Trung Thứ,
Vũ Cao Đàm, Lebadang, Bùi Xuân Phái, Phạm Hậu, Nguyễn Gia Trí. NY/London ≈ zero VN.
Seeds are ranked by VN lot count (screening date 2026-06-02).
"""
import re
import time
import json
import html as _html_lib
from datetime import datetime

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
    _USING_CLOUDSCRAPER = True
except Exception:
    import requests
    _scraper = requests.Session()
    _USING_CLOUDSCRAPER = False

from crawlers.common import parse_amount, insert_sale_result, clean_text, log_crawl_run


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Known past Phillips sales with Vietnamese art (screened for VN content).
# Pattern: HK010*/HK0104** = 20th Century & Contemporary Art Day/Evening Sale,
#          HK0906** = Faces Places online sales (Mai Thu silk prints).
# Ranked by VN lot count (screening date 2026-06-02). All HK office; HKD currency.
SEED_SALE_URLS = [
    "https://www.phillips.com/auction/HK010418",  # 5 VN — 2018-11-25 (Lê Phổ, Vu Cao Dam, Mai Thu)
    "https://www.phillips.com/auction/HK010925",  # 5 VN — Faces Places online
    "https://www.phillips.com/auction/HK090622",  # 7 VN — 2022-04-20 Faces Places online (Mai Thu prints)
    "https://www.phillips.com/auction/HK010118",  # 4 VN — Lê Phổ, Mai Thu, Pham Hau, Nguyễn Gia Trí
    "https://www.phillips.com/auction/HK010423",  # 4 VN — Lê Phổ, Mai Thu
    "https://www.phillips.com/auction/HK010222",  # 3 VN — Lê Phổ, Mai Thu, Vu Cao Dam
    "https://www.phillips.com/auction/HK010221",  # 3 VN — Lê Phổ, Mai Thu
    "https://www.phillips.com/auction/HK010225",  # 2 VN — Lê Phổ
    "https://www.phillips.com/auction/HK010219",  # 2 VN — Mai Thu
    "https://www.phillips.com/auction/HK010223",  # 2 VN — Mai Thu
    "https://www.phillips.com/auction/HK010424",  # 1 VN — Lê Phổ (Evening Sale)
    "https://www.phillips.com/auction/HK010419",  # 1 VN — Lê Phổ
    "https://www.phillips.com/auction/HK010421",  # 1 VN — Bùi Xuân Phái
    "https://www.phillips.com/auction/HK010224",  # 1 VN — Vu Cao Dam
    "https://www.phillips.com/auction/HK010326",  # 1 VN — Mai Thu
    "https://www.phillips.com/auction/HK010120",  # 1 VN — Mai Thu
]


def _fetch(url, timeout=30):
    """GET with cloudscraper (falls back to requests if unavailable)."""
    try:
        r = _scraper.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        return None, f"request err: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.text, None


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_lib.unescape(s).replace("\xa0", " ").replace("’", "'")
    return re.sub(r"[ \t]+", " ", s).strip()


def _parse_price(label):
    """Parse 'HK$1,841,500' / 'HK$20,000–30,000' → (low, high, currency)."""
    if not label:
        return None, None, None
    label = label.replace("–", "-").replace("—", "-").replace("−", "-")
    cur = "HKD"
    if "HK$" in label or "HKD" in label:
        cur = "HKD"
    elif "US$" in label or "USD" in label or label.startswith("$"):
        cur = "USD"
    elif "£" in label or "GBP" in label:
        cur = "GBP"
    elif "CHF" in label:
        cur = "CHF"
    elif "€" in label or "EUR" in label:
        cur = "EUR"
    nums = re.findall(r"[\d,]+", label)
    nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").isdigit()]
    if len(nums) >= 2:
        return nums[0], nums[1], cur
    if len(nums) == 1:
        return nums[0], None, cur
    return None, None, cur


_LOT_TILE_RE = re.compile(r'data-testid="lot-(\d+)-object-tile"')


def parse_sale_page(text, sale_url):
    """Parse a Phillips /auction/{SALE_ID} HTML page.
    Returns (sale_meta, [lot_record_minus_enrichment...], err)."""
    # Sale-level metadata from JSON-LD Event block
    sale_meta = {"sale_url": sale_url, "currency": "HKD", "location": "Hong Kong"}
    m = re.search(
        r'<script type="application/ld\+json">\s*(\{[^<]*?"@type":"Event"[^<]+?\})\s*</script>',
        text, re.DOTALL,
    )
    if m:
        try:
            ld = json.loads(m.group(1))
            sale_meta["auction_name"] = ld.get("name", "")
            sd = ld.get("startDate", "")
            mm = re.match(r"(\d{4}-\d{2}-\d{2})", sd)
            sale_meta["sale_date"] = mm.group(1) if mm else ""
            loc = (ld.get("location") or {}).get("name", "")
            if loc:
                sale_meta["location"] = loc
            offers = ld.get("offers") or {}
            cur = offers.get("priceCurrency") if isinstance(offers, dict) else None
            if cur:
                sale_meta["currency"] = cur
        except Exception:
            pass
    # Fallbacks
    if not sale_meta.get("auction_name"):
        mt = re.search(r"<title>([^<]+)</title>", text)
        sale_meta["auction_name"] = _html_lib.unescape(mt.group(1)).split(" | ")[0].strip() if mt else ""
    if not sale_meta.get("sale_date"):
        m_sd = re.search(r'"startDate":"(\d{4}-\d{2}-\d{2})', text)
        if m_sd:
            sale_meta["sale_date"] = m_sd.group(1)
    if not sale_meta.get("currency"):
        m_cur = re.search(r'"priceCurrency":"([^"]+)"', text)
        if m_cur:
            sale_meta["currency"] = m_cur.group(1)

    # Slice text into per-lot chunks by lot-tile anchor positions
    positions = [(m.start(), m.group(1)) for m in _LOT_TILE_RE.finditer(text)]
    if not positions:
        return sale_meta, [], "no lot tiles"
    positions.append((len(text), None))
    records = []
    for i in range(len(positions) - 1):
        start, lot_id = positions[i]
        end, _ = positions[i + 1]
        chunk = text[start:end]
        artist_m = re.search(r'object-tile__maker[^>]*>.*?<span>([^<]+)</span>', chunk, re.DOTALL)
        if not artist_m:
            continue
        artist_raw = _html_lib.unescape(artist_m.group(1)).strip()
        title_m = re.search(r'object-tile__title[^>]*>.*?<span>([^<]+)</span>', chunk, re.DOTALL)
        artwork_title = _html_lib.unescape(title_m.group(1)).strip() if title_m else ""
        lotnum_m = re.search(r'object-tile__lot-number[^>]*>(\d+)<', chunk)
        lot_number = lotnum_m.group(1) if lotnum_m else ""
        est_m = re.search(
            r'object-tile__estimate__label.*?seldon-detail__value[^>]*>\s*<span[^>]*>([^<]+)</span>',
            chunk, re.DOTALL,
        )
        estimate_label = _html_lib.unescape(est_m.group(1)).strip() if est_m else ""
        sold_m = re.search(
            r'bid-snapshot__sold.*?seldon-detail__value[^>]*>\s*<span[^>]*>([^<]+)</span>',
            chunk, re.DOTALL,
        )
        sold_label = _html_lib.unescape(sold_m.group(1)).strip() if sold_m else ""
        href_m = re.search(r'href="(/detail/[^"]+)"', chunk)
        if href_m:
            from urllib.parse import quote
            lot_path = href_m.group(1)
            # Path may contain raw UTF-8 (e.g. /detail/lê-phổ/122737); requote safely
            lot_url = "https://www.phillips.com" + quote(lot_path, safe="/:%")
        else:
            lot_url = f"https://www.phillips.com/detail/{lot_id}"

        est_low, est_high, est_cur = _parse_price(estimate_label)
        sold_low, _, sold_cur = _parse_price(sold_label)
        currency = sold_cur or est_cur or sale_meta.get("currency", "HKD")

        if sold_low:
            hammer = sold_low
            status = "sold"
        else:
            hammer = None
            status = "passed"

        auction_name = sale_meta.get("auction_name", "") or "Phillips"
        auction_title = f"Phillips — {auction_name}"

        rec = {
            "source": "phillips",
            "source_url": lot_url,
            "sale_page_url": sale_url,
            "lot_number": lot_number,
            "auction_title": auction_title,
            "sale_date": sale_meta.get("sale_date", ""),
            "sale_location": sale_meta.get("location", "Hong Kong"),
            "artist_name_raw": artist_raw,
            "artwork_title": artwork_title,
            "medium": "",
            "dimensions": "",
            "year": "",
            "estimate_low": est_low,
            "estimate_high": est_high,
            "hammer_price": hammer,
            "price_with_premium": None,
            "currency": currency,
            "status": status,
            "provenance": "",
            "raw_snapshot": f"{artist_raw} | {artwork_title[:100]}"[:300],
            "_phillips_lot_id": lot_id,
        }
        records.append(rec)
    return sale_meta, records, None


# Medium phrases the cataloging block uses (lower-cased haystack)
_MEDIUM_KWS = (
    "oil on", "ink on", "watercolor on", "watercolour on", "gouache on",
    "lacquer on", "tempera on", "ink and color on", "ink and gouache",
    "pencil on", "pastel on", "acrylic on", "mixed media", "graphite",
    "charcoal on", "print on silk", "print on paper", "impression sur",
    "silkscreen", "lithograph", "etching", "screenprint",
)


def fetch_lot_detail(lot_url):
    """Fetch a Phillips lot detail page and extract (medium, dims, year) from
    the `pah-lot-cataloguing-section` block. Returns ("", "", "") on any failure."""
    text, err = _fetch(lot_url, timeout=25)
    if err or not text:
        return "", "", ""
    m = re.search(
        r'pah-lot-cataloguing-section[^"]*"[^>]*id="lot-cataloging-section"[^>]*>(.*?)(?:</section>)',
        text, re.DOTALL,
    )
    if not m:
        return "", "", ""
    raw = m.group(1)
    # Pull each <span>…</span> as a logical catalog line
    lines = []
    for sp in re.finditer(r"<span[^>]*>([^<]+)</span>", raw):
        line = _html_lib.unescape(sp.group(1)).replace("’", "'").strip()
        if line:
            lines.append(line)
    medium = ""
    dims = ""
    year = ""
    for line in lines:
        low = line.lower()
        if not medium and any(kw in low for kw in _MEDIUM_KWS):
            medium = line[:200]
            continue
        if not dims:
            m_dim = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:x|by|×)\s*(\d+(?:\.\d+)?)\s*cm",
                line, re.IGNORECASE,
            )
            if m_dim:
                dims = f"{m_dim.group(1)} x {m_dim.group(2)} cm"
                continue
        if not year:
            m_y = re.search(
                r"(?:painted|executed|circa|c\.)[^\d]{0,15}(\d{4})", line, re.IGNORECASE
            )
            if m_y:
                year = m_y.group(1)
    return medium, dims, year


def _load_vn():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def is_vietnamese(artist_name, catalog, exclusions):
    from artonis_price_mvp import normalize_key
    norm = normalize_key(artist_name)
    if not norm or norm in exclusions:
        return False
    if norm in catalog:
        return True
    for k in catalog:
        if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
            return True
    return False


def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=True, max_pages=200):
    """Crawl Phillips sale pages and extract all VN lots.

    Args:
      conn: sqlite3 connection (row_factory set by caller).
      sale_urls: list of /auction/{SALE_ID} URLs. Defaults to SEED_SALE_URLS.
      delay: seconds between sale-page fetches (lot-detail fetches add 0.4s extra).
      verbose: print progress to stdout.
      filter_vn: when True, drop lots whose artist isn't in vn_artist_catalog.
      max_pages: hard cap on number of sale URLs visited per run.
    Returns:
      total_inserted (int).
    """
    sale_urls = sale_urls or SEED_SALE_URLS
    sale_urls = sale_urls[:max_pages]
    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    total_inserted = 0
    skipped_nonvn = 0
    for sale_url in sale_urls:
        run_started = datetime.utcnow().isoformat() + "Z"
        text, err = _fetch(sale_url, timeout=30)
        if err or not text:
            if verbose:
                print(f"  ERR {sale_url[-40:]}: {err}")
            log_crawl_run(conn, "phillips", target_slug=sale_url, started_at=run_started,
                          status="error", note=str(err)[:200])
            continue
        sale_meta, records, perr = parse_sale_page(text, sale_url)
        if perr and verbose:
            print(f"  WARN {sale_url[-40:]}: {perr}")
        inserted_this = 0
        for rec in records:
            if filter_vn and not is_vietnamese(rec["artist_name_raw"], vn_catalog, exclusions):
                skipped_nonvn += 1
                continue
            # Skip explicit copies/reproductions
            blob = (rec["artwork_title"] + " " + rec["artist_name_raw"])
            if re.search(r"\b(d'?apr[eè]s|after\s+(mai|le\s+pho|vu\s+cao)|copie|copy of|reproduction)\b",
                         blob, re.IGNORECASE):
                continue
            # Phillips publishes hammer for sold lots; passed lots have no price → skip
            if not rec.get("hammer_price"):
                continue
            # Enrich from lot detail page (medium / dimensions / year)
            medium, dims, year = fetch_lot_detail(rec["source_url"])
            if medium:
                rec["medium"] = medium
            if dims:
                rec["dimensions"] = dims
            if year:
                rec["year"] = year
            # Drop internal-only field before insert
            rec.pop("_phillips_lot_id", None)
            insert_sale_result(conn, rec)
            inserted_this += 1
            time.sleep(0.4)  # pace lot-detail fetches
        conn.commit()
        sale_date = sale_meta.get("sale_date", "") or None
        log_crawl_run(conn, "phillips", target_slug=sale_url, started_at=run_started,
                      lots_scanned=len(records), lots_inserted=inserted_this,
                      sale_date_min=sale_date, sale_date_max=sale_date,
                      status="ok", note=(sale_meta.get("auction_name", "") or "")[:120])
        if verbose:
            print(f"  {sale_url[-40:]}: {len(records)} lots, {inserted_this} VN inserted")
        total_inserted += inserted_this
        time.sleep(delay)
    if verbose:
        print(f"\n  Phillips total inserted: {total_inserted}, skipped non-VN: {skipped_nonvn}")
    return total_inserted
