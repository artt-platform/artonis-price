"""Gros & Delettrez crawler — Paris auction house (Drouot tradition).

Scope: walks /en/past-sales, then within each catalog page filters lot URLs whose
slug contains a tracked VN-artist marker. Each lot page is parsed for title /
medium / dim / estimate / hammer.

Data sample (lot 174741/32934345 — Cactus, 1965):
  Estimation : 4000 - 6000 EUR
  Result : 3 300EUR    (without fees — that's the hammer)
  Diem PHUNG-THI (1920-2002)
  Cactus, 1965
  Original bronze-painted plaster. Monogrammed and dated DPT 65.
  28 x 17 x 13cm
"""
import re
import time
import html as html_lib

import requests

from crawlers.common import insert_sale_result, clean_text, clean_artist_name, log_crawl_run

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}

BASE = "https://www.gros-delettrez.com"

# Slugs that mark a tracked VN artist.  Auto-derived from
# data/vn_artist_catalog.py so adding a new artist to the catalog
# enables Gros & Delettrez coverage automatically — no per-crawler list
# to keep in sync.  See CONVENTIONS.md 'Discovery: catalog-driven'.
def _build_vn_slug_kws():
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
        for m in list(sys.modules.keys()):
            if "vn_artist_catalog" in m:
                del sys.modules[m]
        from vn_artist_catalog import VN_ARTIST_CATALOG
    except ImportError:
        return ()
    kws = set()
    for normalized in VN_ARTIST_CATALOG:
        if not normalized or len(normalized) < 5:
            continue
        # 'nguyen trong kiem' → 'nguyen-trong-kiem'
        slug_full = normalized.replace(' ', '-')
        if len(slug_full) >= 6:
            kws.add(slug_full)
        # Family-elision: French houses often drop the family name —
        # 'trong-kiem' instead of 'nguyen-trong-kiem'.  Only emit when the
        # eluded form is still ≥ 8 chars; shorter forms collide with
        # non-VN names ('van-de' from 'tran van de' matches Van Der Rohe,
        # 'the-son' matches 'the song', 'ke-an' matches 'snake-and-...').
        # User audit on Everard 2026-06 surfaced these false positives.
        tokens = normalized.split()
        if len(tokens) >= 3:
            elided = '-'.join(tokens[1:])
            if len(elided) >= 8:
                kws.add(elided)
    # Variants outside the catalog normalisation.  'lebadang' is how
    # Lê Bá Đáng is slugified abroad; the catalog has 'le ba dang'.
    kws.update(('lebadang', 'diem-phung', 'cao-dam-vu', 'mai-trung-thu'))
    return tuple(sorted(kws))

VN_SLUG_KWS = _build_vn_slug_kws()


def _strip(s):
    if not s:
        return ""
    # Drop <script>/<style> first — their text content survives a plain tag-stripper
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html_lib.unescape(s)).strip()


def list_past_catalogs():
    """Return list of (catalog_id, slug, url) tuples for past catalogues."""
    r = requests.get(f"{BASE}/en/past-sales", headers=H, timeout=20)
    r.raise_for_status()
    cats = sorted(set(re.findall(r'href="(/en/catalog/(\d+)-([^"]+))"', r.text)))
    return [(cat_id, slug, BASE + path) for path, cat_id, slug in cats]


def list_vn_lots_in_catalog(cat_url):
    """Return list of (lot_url, slug) for VN-named lots in the given catalog."""
    r = requests.get(cat_url, headers=H, timeout=25)
    r.raise_for_status()
    lots = set()
    # Word-boundary match against slug tokens (not substring).  'le-pho'
    # must not match 'after-lartigue-villerville-photo', 'le-lam' must
    # not match 'metal-table-lamp'.  Token bounds: start, end, or hyphen.
    kw_re = re.compile(
        r'(?:^|-)(?:' + '|'.join(re.escape(k) for k in VN_SLUG_KWS) + r')(?:-|$)',
        re.IGNORECASE,
    ) if VN_SLUG_KWS else None
    for m in re.finditer(r'href="(/en/lot/\d+/(\d+)-([^"]+))"', r.text):
        path, _, slug = m.groups()
        if kw_re and kw_re.search(slug):
            lots.add((BASE + path, slug))
    return sorted(lots)


def parse_catalog_page(cat_url):
    """Extract sale_date (ISO yyyy-mm-dd) and auction title."""
    r = requests.get(cat_url, headers=H, timeout=20)
    text = _strip(r.text)
    title = ""
    m = re.search(r"<title>([^<]+)</title>", r.text)
    if m:
        title = m.group(1).split(" - Gros")[0].strip()
    # "Friday 03 April 2026 14:00"
    sale_date = ""
    m = re.search(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        months = {"January": "01", "February": "02", "March": "03", "April": "04",
                  "May": "05", "June": "06", "July": "07", "August": "08",
                  "September": "09", "October": "10", "November": "11", "December": "12"}
        if mo in months:
            sale_date = f"{y}-{months[mo]}-{int(d):02d}"
    return title, sale_date


def parse_lot_page(lot_url):
    r = requests.get(lot_url, headers=H, timeout=25)
    if r.status_code != 200:
        return None
    raw = r.text
    text = _strip(raw)

    # Lot description block (clean, structured). Falls back to full text if missing.
    desc_text = ""
    m_desc = re.search(r'<div class="fiche_lot_description"[^>]*>(.+?)</div>', raw, re.DOTALL)
    if m_desc:
        desc_text = _strip(m_desc.group(1))

    # Lot number
    lot_no = ""
    m = re.search(r"Lot\s+(\d+)", text)
    if m:
        lot_no = m.group(1)

    # Artist (heading is "Diem PHUNG-THI (1920-2002) - Lot 33") — extract before " - Lot"
    artist_raw = ""
    m = re.search(r"<h1[^>]*>(.+?)</h1>", raw, re.DOTALL)
    if m:
        h1 = _strip(m.group(1))
        m2 = re.match(r"^(.+?)\s*-\s*Lot\s*\d+", h1)
        artist_raw = (m2.group(1) if m2 else h1).strip()

    # Estimation: "Estimation : 4000 - 6000 EUR"
    est_low = est_high = None
    m = re.search(r"Estimation\s*[:\s]+(\d[\d\s]*)\s*(?:-|–|à)\s*(\d[\d\s]*)\s*(EUR|USD|HKD|GBP|CHF)", text)
    if m:
        est_low = float(m.group(1).replace(" ", ""))
        est_high = float(m.group(2).replace(" ", ""))
        currency = m.group(3)
    else:
        currency = "EUR"

    # Result (without fees) — that's the hammer
    hammer = None
    m = re.search(r"Result\s*[:\s]+(\d[\d\s]*)\s*(EUR|USD|HKD|GBP|CHF)", text, re.IGNORECASE)
    if m:
        hammer = float(m.group(1).replace(" ", ""))
    # Result with fees → premium price
    premium = None
    m = re.search(r"Result\s+with\s+fees?\s*[:\s]+(\d[\d\s]*)\s*(EUR|USD|HKD|GBP|CHF)", text, re.IGNORECASE)
    if m:
        premium = float(m.group(1).replace(" ", ""))

    # Description block: starts after "{ARTIST} ({YYYY}-{YYYY})" and ends before "General bibliography"/"Bibliographie"/"Provenance"
    # Title line (artwork) usually right after the artist line.
    artwork_title = ""
    medium = ""
    dimensions = ""
    year = ""
    if artist_raw and desc_text:
        # The fiche_lot_description block is structured: ARTIST (years) | TITLE | MEDIUM | DIM | bibliography.
        # Cut at the bibliography/provenance markers so they don't pollute medium parsing.
        block = desc_text
        for stop in ("General bibliography", "Bibliographie", "Provenance", "Related work",
                     "Sculpture Diem", "Diem Phung Thi (", "Notes", "Estimation"):
            idx = block.find(stop)
            if idx > 0:
                block = block[:idx].strip()
                break
        # Split at "(YYYY-YYYY)" — desc only echoes artist once, so first split works
        parts = re.split(r"\(\s*\d{4}\s*[-–]\s*\d{4}\s*\)", block, maxsplit=1)
        if len(parts) >= 2:
            rest = parts[-1].strip(" .,-")
            # Title cut: earliest of ", YYYY" / " YYYY " / "circa YYYY" / first period.
            # Then year is extracted separately, so cutting before it is fine.
            cut = len(rest)
            for pat in (r",\s*(?:circa\s+)?\d{4}\b",
                        r"\s+circa\s+\d{4}\b",
                        r"\.",
                        r"\s+(?:Original|Painted|Bronze|Carved|Wood|Stained|Patinated|Cracked)\b"):
                m = re.search(pat, rest, re.IGNORECASE)
                if m and m.start() < cut:
                    cut = m.start()
            artwork_title = rest[:cut].rstrip(" ,.").strip()[:200]
            # Year — first 4-digit year in the rest text (after title cut)
            m = re.search(r"\bcirca\s+(\d{4})\b|,\s*(\d{4})\b|\b(\d{4})\b", rest)
            if m:
                year = m.group(1) or m.group(2) or m.group(3)
            # Dim: "28 x 17 x 13cm" or "92 x 64.9 cm" or "H. 41 cm"
            m_dim3 = re.search(
                r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm",
                rest, re.IGNORECASE)
            m_dim2 = re.search(
                r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm",
                rest, re.IGNORECASE)
            m_dimH = re.search(r"H\.?\s*(\d+(?:[.,]\d+)?)\s*cm", rest, re.IGNORECASE)
            if m_dim3:
                a, b, c = (g.replace(",", ".") for g in m_dim3.groups())
                dimensions = f"{a} x {b} x {c} cm"
            elif m_dim2:
                a, b = (g.replace(",", ".") for g in m_dim2.groups())
                dimensions = f"{a} x {b} cm"
            elif m_dimH:
                dimensions = f"H. {m_dimH.group(1).replace(',', '.')} cm"
            # Medium = between title-end and dim
            if dimensions:
                if artwork_title and artwork_title in rest:
                    after_title = rest[rest.find(artwork_title) + len(artwork_title):]
                else:
                    after_title = rest
                m_first_dim_pos = re.search(r"\d+(?:[.,]\d+)?\s*[x×]|H\.?\s*\d+", after_title)
                if m_first_dim_pos:
                    medium = after_title[:m_first_dim_pos.start()].strip(" .,;")
                    medium = medium[:200]

    # Cleanup artist
    artist_clean, _ = clean_artist_name(artist_raw)
    return {
        "source": "gros-delettrez",
        "source_url": lot_url,
        "lot_number": lot_no,
        "artist_name_raw": artist_clean or artist_raw,
        "artwork_title": artwork_title,
        "medium": medium,
        "dimensions": dimensions,
        "year": year,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "hammer_price": hammer,
        "price_with_premium": premium,
        "currency": currency,
        "status": "sold" if hammer else "estimate",
    }


def crawl(conn, max_lots=200):
    cats = list_past_catalogs()
    print(f"[gros-delettrez] {len(cats)} past catalogues found", flush=True)
    inserted = 0
    scanned = 0
    for cat_id, slug, cat_url in cats:
        vn_lots = list_vn_lots_in_catalog(cat_url)
        if not vn_lots:
            continue
        title, sale_date = parse_catalog_page(cat_url)
        print(f"  [{cat_id}] {slug} — {len(vn_lots)} VN lots, sale_date={sale_date!r}", flush=True)
        for lot_url, lot_slug in vn_lots:
            scanned += 1
            try:
                rec = parse_lot_page(lot_url)
            except Exception as e:
                print(f"    ERR {lot_url}: {e}")
                continue
            if not rec or not rec.get("artist_name_raw"):
                continue
            rec["auction_title"] = title
            rec["sale_date"] = sale_date
            rec["sale_location"] = "Paris"
            rec["sale_page_url"] = cat_url
            insert_sale_result(conn, rec)
            inserted += 1
            print(f"    +{rec['artist_name_raw']:20s} | {(rec['artwork_title'] or '')[:35]:35s} | {rec['dimensions']:15s} | {rec['hammer_price']!r} {rec['currency']}", flush=True)
            time.sleep(0.4)
            if inserted >= max_lots:
                conn.commit()
                return inserted, scanned
    conn.commit()
    return inserted, scanned


if __name__ == "__main__":
    import sqlite3, sys
    sys.path.insert(0, "/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice")
    DB = "/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice/data/artonis_price_mvp.sqlite"
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    inserted, scanned = crawl(conn)
    print(f"\nDONE — inserted {inserted}, scanned {scanned}")
    conn.close()
