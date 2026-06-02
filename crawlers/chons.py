"""Chọn Auction House (Chọn Đấu Giá) crawler — Hà Nội VN art house, founded 2016.

Note on the domain:
    The user-supplied brief references `chonsauction.com`, but DNS for both
    `chonsauction.com` and the historical `chondaugia.vn` (the house's real
    domain per their yellowpages / Facebook listings) is now NXDOMAIN.
    The site went offline after Spring 2021 (the house ran into legal
    issues — see Dân Trí 2017 "đấu giá hay rửa tiền" coverage).

    The catalog is therefore recovered from the Internet Archive (Wayback
    Machine). The crawler:
      1. Discovers archived lot URLs via the Wayback CDX API
         (`http://chondaugia.vn/auction/{slug}-{N}ar`).
      2. Fetches each lot's most recent successful (HTTP 200) snapshot.
      3. Parses both the Vietnamese and English label variants — Chọn's
         page rendered in whichever language the visitor's session was set
         to, so different snapshots used different labels.

    If the house ever republishes a live catalog, callers can pass
    `sale_urls=[...]` directly (parsed by `parse_lot_page` regardless of
    host) and the crawler will hit those URLs without going through the
    archive.

Lot page schema (both languages, identical structure):
    <h4>{auction_title}</h4>
    Kết thúc / Ended : DD/MM/YYYY HH:MM
    <p><b>Tác giả / Author:</b> {artist}</p>
    <p><b>Năm sác tác / Year:</b> {year}</p>
    <p><b>Chất liệu / Material:</b> {medium}</p>
    <p><b>Thể loại / Category:</b> {kind}</p>             # TRANH=painting, Statue=sculpture
    <p><b>Kích thước / Size:</b> {WxH}x cm</p>
    <p><b>Giá ước định / Estimate price:</b> {low} - {high}</p>
    <h5>Giá gõ búa / Hammer price</h5>
    <b class="current-price">${hammer}</b>                # always USD, $-prefixed
"""
import html as _html
import json
import re
import time
import urllib.parse
from datetime import datetime

import requests

from crawlers.common import insert_sale_result, clean_text, log_crawl_run


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
}

# Historical host where Chọn's catalog lived (now NXDOMAIN — only reachable via Wayback)
HIST_HOST = "chondaugia.vn"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"


# ---- Discovery via Wayback CDX ---------------------------------------------

def list_archived_lots(host=HIST_HOST, limit=2000):
    """Query the Wayback CDX API for all archived /auction/{slug}-{N}ar pages.
    Returns list of dicts {slug, lot_id, timestamp, archive_url, original_url}.
    Only HTTP 200 captures are kept; for each lot the most recent capture wins."""
    params = {
        "url": f"{host}/auction/*",
        "output": "json",
        "limit": str(limit),
        "filter": "statuscode:200",
    }
    try:
        r = requests.get(WAYBACK_CDX, params=params, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        rows = r.json()
    except Exception:
        return []
    if not rows or len(rows) < 2:
        return []
    # rows[0] is header
    out = {}
    pat = re.compile(r"/auction/([a-z0-9\-]+?-(\d+)ar)$", re.IGNORECASE)
    for row in rows[1:]:
        try:
            ts, original = row[1], row[2]
        except (IndexError, TypeError):
            continue
        m = pat.search(original)
        if not m:
            continue
        slug = m.group(1)
        lot_id = m.group(2)
        # Keep the most recent timestamp per slug
        prev = out.get(slug)
        if prev is None or ts > prev["timestamp"]:
            out[slug] = {
                "slug": slug,
                "lot_id": lot_id,
                "timestamp": ts,
                "original_url": original.replace("http://", "https://"),
                "archive_url": f"{WAYBACK_BASE}/{ts}/{original}",
            }
    return list(out.values())


# ---- Lot page parsing ------------------------------------------------------

# Bilingual label map: each tuple (vi_label, en_label) → canonical key
_LABEL_MAP = [
    (("Tác giả", "Author"), "artist"),
    (("Năm sác tác", "Năm sáng tác", "Year"), "year"),
    (("Chất liệu", "Material"), "medium"),
    (("Thể loại", "Category"), "kind"),
    (("Kích thước", "Size"), "dimensions"),
    (("Giá ước định", "Estimate price", "Estimate"), "estimate"),
]


def _extract_field(html, labels):
    """Find <p><b>LABEL:</b> VALUE</p> for any label in `labels`. Returns value or ''."""
    for lab in labels:
        # Allow optional whitespace, optional trailing colon variations
        pat = re.escape(lab) + r"\s*:\s*</b>\s*([^<]*)"
        m = re.search(pat, html)
        if m:
            return clean_text(m.group(1))
    return ""


def _parse_dimensions(raw):
    """'100x100x cm' / '90x90x cm' / '100 x 80 cm' → '100 x 100 cm'.
    Chọn writes dims with a trailing 'x' (no depth), drop it."""
    if not raw:
        return ""
    s = raw.replace("×", "x").strip()
    # Strip trailing 'x' before 'cm'
    s = re.sub(r"x\s*(?=cm)", " ", s, flags=re.IGNORECASE)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*cm", s, re.IGNORECASE)
    if m:
        w = m.group(1).replace(",", ".")
        h = m.group(2).replace(",", ".")
        return f"{w} x {h} cm"
    return raw.strip()


def _parse_estimate(raw):
    """'0 - 100.000' / '100 - 3,000' → (low, high). Chọn's estimates are unitless
    integers that line up with hammer (USD): hammers are $600-$700 against
    '0 - 100.000' or '100 - 3,000' estimates — neither obviously VND nor USD.
    Best guess: low estimates ~hundreds → assume same currency as hammer (USD).
    Returns (low_float, high_float) or (None, None)."""
    if not raw:
        return None, None
    m = re.search(r"([\d,.]+)\s*[-–]\s*([\d,.]+)", raw)
    if not m:
        return None, None

    def _num(s):
        # Treat both ',' and '.' as thousands separators when followed by exactly 3 digits;
        # otherwise treat as decimal.
        s = s.strip()
        # If looks like '100.000' or '100,000' → thousand sep
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
            return float(re.sub(r"[.,]", "", s))
        # Otherwise drop commas, keep dots as decimal
        s2 = s.replace(",", "")
        try:
            return float(s2)
        except ValueError:
            return None

    return _num(m.group(1)), _num(m.group(2))


def _parse_hammer(html):
    """<b class="current-price">$700</b> → (700.0, 'USD'). Returns (price, currency)."""
    m = re.search(r'class="current-price"[^>]*>\s*([^<]+?)\s*<', html)
    if not m:
        return None, "USD"
    txt = m.group(1).strip()
    if not txt or txt in ("-", "—"):
        return None, "USD"
    currency = "USD"
    low = txt.lower()
    if "đ" in txt or "vnd" in low or "vnđ" in low:
        currency = "VND"
    elif "$" in txt or "usd" in low:
        currency = "USD"
    # Strip non-digits except separators
    cleaned = re.sub(r"[^\d.,]", "", txt)
    if not cleaned:
        return None, currency
    # Handle both EU/US thousand seps
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", cleaned):
        cleaned = re.sub(r"[.,]", "", cleaned)
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned), currency
    except ValueError:
        return None, currency


def _parse_end_date(html):
    """'Kết thúc : DD/MM/YYYY HH:MM' or 'Ended: DD/MM/YYYY HH:MM' → 'YYYY-MM-DD'."""
    # Try labelled context first
    m = re.search(
        r"(?:Kết thúc|Ended|Ends|End)\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})",
        html, re.IGNORECASE,
    )
    if not m:
        # Fallback: any dd/mm/yyyy near the title
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}", html)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return ""
    return ""


def _strip_wayback(html):
    """Remove the Wayback toolbar / archive comments and decode HTML entities so that
    Vietnamese labels (which the Chọn site renders as numeric entities like
    'T&#225;c giả' for 'Tác giả') match against `_LABEL_MAP` literally."""
    html = re.sub(
        r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?<!--\s*END WAYBACK TOOLBAR INSERT\s*-->",
        "", html, flags=re.DOTALL,
    )
    return _html.unescape(html)


def parse_lot_page(html, lot_url):
    """Parse a Chọn lot detail page → record dict (or None on failure)."""
    if not html:
        return None
    html = _strip_wayback(html)

    fields = {}
    for labels, key in _LABEL_MAP:
        fields[key] = _extract_field(html, labels)

    if not fields.get("artist"):
        return None

    # Artwork title — the <h4> directly above the metadata block.
    # Some snapshots (esp. EN-language captures) render the title block lazily
    # via JS and it never makes it into the WARC; fall back to the URL slug.
    title = ""
    m_h4 = re.search(r"<h4[^>]*>\s*([^<]{2,200}?)\s*</h4>", html)
    if m_h4:
        title = clean_text(m_h4.group(1))
    if not title:
        m_slug = re.search(r"/auction/([a-z0-9\-]+?)-\d+ar(?:[/?#]|$)", lot_url, re.IGNORECASE)
        if m_slug:
            title = m_slug.group(1).replace("-", " ").strip().capitalize()

    sale_date = _parse_end_date(html)
    dims = _parse_dimensions(fields.get("dimensions", ""))
    est_low, est_high = _parse_estimate(fields.get("estimate", ""))
    hammer, currency = _parse_hammer(html)

    # Lot number from URL: .../auction/{slug}-{N}ar
    lot_num = ""
    m_lot = re.search(r"-(\d+)ar(?:[/?#]|$)", lot_url)
    if m_lot:
        lot_num = m_lot.group(1)

    return {
        "artist_name_raw": fields["artist"],
        "artwork_title": title,
        "year": fields.get("year", ""),
        "medium": fields.get("medium", ""),
        "category": fields.get("kind", ""),
        "dimensions": dims,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "currency": currency,
        "sale_date": sale_date,
        "lot_number": lot_num,
    }


# ---- Main crawl entry ------------------------------------------------------

def crawl(conn, sale_urls=None, delay=1.0, verbose=True, filter_vn=False, max_pages=200):
    """Crawl Chọn Auction House lots.

    Args:
        sale_urls: optional list of lot URLs (live or archived). When None, the
            crawler discovers archived lots via the Wayback CDX API.
        delay: seconds between successive lot fetches.
        verbose: print progress.
        filter_vn: False by default — Chọn is a VN-domestic house, virtually all
            consigned works are by Vietnamese artists. Setting True applies the
            same VN-catalog filter the other crawlers use.
        max_pages: cap on number of lot pages to fetch.

    Returns (inserted, scanned).
    """
    run_started = datetime.utcnow().isoformat() + "Z"

    # 1) Resolve lot URLs ----------------------------------------------------
    targets = []  # list of (fetch_url, canonical_lot_url, snapshot_ts_or_None)
    if sale_urls:
        for u in sale_urls:
            targets.append((u, u, None))
    else:
        if verbose:
            print("  Discovering archived Chọn lots via Wayback CDX...", flush=True)
        lots = list_archived_lots()
        if verbose:
            print(f"  found {len(lots)} archived lots in CDX", flush=True)
        for lot in lots:
            targets.append((lot["archive_url"], lot["original_url"], lot["timestamp"]))

    targets = targets[:max_pages]

    # 2) Optional VN filter setup --------------------------------------------
    vn_catalog = exclusions = None
    if filter_vn:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
        for m in list(__import__("sys").modules.keys()):
            if "vn_artist_catalog" in m:
                del __import__("sys").modules[m]
        from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
        from artonis_price_mvp import normalize_key
        vn_catalog, exclusions = VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS

        def _is_vn(name):
            norm = normalize_key(name)
            if not norm or norm in exclusions:
                return False
            if norm in vn_catalog:
                return True
            for k in vn_catalog:
                if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
                    return True
            return False
    else:
        def _is_vn(name):  # noqa: E306
            return True

    # 3) Fetch + parse each lot ---------------------------------------------
    inserted = 0
    scanned = 0
    skipped_no_price = 0
    skipped_no_artist = 0
    skipped_nonvn = 0
    date_min = date_max = None

    for i, (fetch_url, canonical_url, ts) in enumerate(targets, 1):
        scanned += 1
        try:
            r = requests.get(fetch_url, headers=HEADERS, timeout=30)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(targets)}] fetch error: {e}", flush=True)
            continue
        if r.status_code != 200:
            if verbose:
                print(f"  [{i}/{len(targets)}] HTTP {r.status_code}", flush=True)
            continue

        parsed = parse_lot_page(r.text, canonical_url)
        if not parsed:
            skipped_no_artist += 1
            time.sleep(delay)
            continue
        if not parsed.get("hammer_price"):
            skipped_no_price += 1
            time.sleep(delay)
            continue
        if not _is_vn(parsed["artist_name_raw"]):
            skipped_nonvn += 1
            time.sleep(delay)
            continue
        # Fakes / prints / reproductions filter (consistent with other crawlers)
        blob = (parsed["artwork_title"] + " " + parsed["artist_name_raw"]).lower()
        if re.search(r"\b(d'?apr[eè]s|after|copy|copie|r[eé]production|estampe|print|lithograph)\b", blob):
            time.sleep(delay)
            continue

        rec = {
            "source": "chons",
            "source_url": canonical_url,
            "sale_page_url": f"https://{HIST_HOST}/Auction/result",
            "lot_number": parsed["lot_number"],
            "auction_title": f"Chọn Auction — {parsed['artwork_title']}"[:200],
            "sale_date": parsed["sale_date"],
            "sale_location": "Hà Nội",
            "artist_name_raw": parsed["artist_name_raw"],
            "artwork_title": parsed["artwork_title"],
            "medium": parsed["medium"],
            "dimensions": parsed["dimensions"],
            "year": parsed["year"],
            "estimate_low": parsed["estimate_low"],
            "estimate_high": parsed["estimate_high"],
            "hammer_price": parsed["hammer_price"],
            "price_with_premium": None,
            "currency": parsed["currency"],
            "status": "sold",
            "provenance": (f"Wayback snapshot {ts}" if ts else ""),
            "raw_snapshot": json.dumps({
                "artist": parsed["artist_name_raw"],
                "title": parsed["artwork_title"],
                "medium": parsed["medium"],
                "category": parsed["category"],
                "dimensions": parsed["dimensions"],
                "year": parsed["year"],
                "estimate": [parsed["estimate_low"], parsed["estimate_high"]],
                "hammer": parsed["hammer_price"],
                "currency": parsed["currency"],
                "fetched_from": fetch_url,
            }, ensure_ascii=False)[:1500],
        }
        insert_sale_result(conn, rec)
        inserted += 1
        if parsed["sale_date"]:
            sd = parsed["sale_date"]
            if date_min is None or sd < date_min: date_min = sd
            if date_max is None or sd > date_max: date_max = sd
        if verbose:
            print(f"  [{i}/{len(targets)}] OK  {parsed['artist_name_raw']} — "
                  f"{parsed['artwork_title'][:40]}  hammer={parsed['hammer_price']} {parsed['currency']}",
                  flush=True)
        time.sleep(delay)

    conn.commit()
    note = (f"sources=wayback inserted={inserted} no_price={skipped_no_price} "
            f"no_artist={skipped_no_artist} nonvn={skipped_nonvn}")
    log_crawl_run(conn, "chons", target_slug=f"{HIST_HOST}/auction/*",
                  started_at=run_started, lots_scanned=scanned, lots_inserted=inserted,
                  sale_date_min=date_min, sale_date_max=date_max, status="ok", note=note[:200])
    if verbose:
        print(f"\n  inserted={inserted}  scanned={scanned}  "
              f"no_price={skipped_no_price}  no_artist={skipped_no_artist}  nonvn={skipped_nonvn}",
              flush=True)
    return inserted, scanned
