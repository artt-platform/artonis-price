"""Lê Auction House — leauction.bidspirit.com (Hà Nội).

Bidspirit Angular SPA. Lot data lives behind `Api.auctions.catalog.loadAuctionDayCatalog`,
which requires a logged-in bidder session. Credentials live in `.env.local`
as BIDSPIRIT_EMAIL / BIDSPIRIT_PASSWORD.

Crawl flow:
  1. Playwright Chromium → load https://leauction.bidspirit.com/
  2. Call `LoginUtils.doLogin(email, password, false, true, cb)` (override
     existing sessions). The user object returned confirms login.
  3. Fetch past sales index via /api/auctions/list/getNonHiddenAuctionDays.
  4. For each (auctionId, dayId), call `loadAuctionDayCatalog` and read the
     full lot list — name (title + dims + medium), author, soldPrice,
     estimatedPrice, currency, dimensions, etc.
  5. Parse Lê Auction's "name" string (mixed Vietnamese metadata) into
     artist / artwork / dimensions / medium, then insert.
"""
import os
import re
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from crawlers.common import (
    insert_sale_result, clean_text, clean_artist_name, log_crawl_run,
    parse_amount,
)


HOUSE_URL = "https://leauction.bidspirit.com/"
SOURCE = "le_auction"


def _load_env():
    env = {}
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


_VN_AUTHOR_KEEP_RE = re.compile(
    r"(?i)^(?:NGHỆ THUẬT VIỆT NAM|VIỆT NAM\b|VIETNAM|VIETNAMESE|HỌA SĨ\b)"
)


def _normalize_author(raw):
    """Lê Auction sometimes labels lots with provenance text in author ("VIỆT NAM THẾ KỈ XX")
    rather than a real artist. Strip those; return ('', '') so caller skips."""
    if not raw:
        return "", None
    cleaned = clean_text(raw)
    if _VN_AUTHOR_KEEP_RE.match(cleaned):
        return "", None  # placeholder/provenance, no artist
    return clean_artist_name(cleaned)


_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|in|inch)?",
    re.IGNORECASE,
)


def _parse_lot_name(name):
    """Lê Auction crams artist hint + title + dims + medium into one `name` string.

    Common pattern:
      'VIỆT NAM THẾ KỈ XX, "Thuyền tam bản trên sông", 45 x 64 cm, Sơn dầu trên vải'

    Returns dict(artwork_title, dimensions, medium).
    """
    if not name:
        return {"artwork_title": "", "dimensions": "", "medium": ""}
    text = clean_text(name)
    # Title is the first quoted segment
    m_t = re.search(r"[\"“”«»](.+?)[\"“”«»]", text)
    artwork_title = clean_text(m_t.group(1)) if m_t else ""

    # Dimensions
    dims = ""
    m_d = _DIM_RE.search(text)
    if m_d:
        dims = f"{m_d.group(1).replace(',', '.')} x {m_d.group(2).replace(',', '.')} cm"

    # Medium — last comma-separated segment that's not a dimension or title
    medium = ""
    parts = [p.strip(" .,") for p in text.split(",")]
    for p in reversed(parts):
        if not p or _DIM_RE.search(p):
            continue
        if p.startswith(("\"", "“", "«")):
            continue
        if 3 < len(p) < 80:
            medium = p
            break

    return {
        "artwork_title": artwork_title or text[:120],
        "dimensions": dims,
        "medium": medium,
    }


def _estimate_low_high(est_text):
    """Parse '$1,000 - $2,000' → (1000.0, 2000.0, 'USD')."""
    if not est_text:
        return None, None, "USD"
    parts = re.split(r"\s*[-–]\s*", est_text, maxsplit=1)
    if len(parts) != 2:
        a, cur = parse_amount(est_text)
        return a, None, cur or "USD"
    lo, cur_lo = parse_amount(parts[0])
    hi, cur_hi = parse_amount(parts[1])
    cur = cur_lo or cur_hi or "USD"
    return lo, hi, cur


_NAME_PROFILE = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _load_vn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS
    return VN_ARTIST_CATALOG, NON_VN_EXCLUSIONS


def _is_vietnamese(artist_clean, vn_catalog, exclusions):
    from artonis_price_mvp import normalize_key
    norm = normalize_key(artist_clean)
    if not norm or norm in exclusions:
        return False
    if norm in vn_catalog:
        return True
    for k in vn_catalog:
        if norm == k or norm.startswith(k + " ") or k.startswith(norm + " "):
            return True
    return False


def crawl(conn, sale_specs=None, delay=0.5, verbose=True, filter_vn=False, max_pages=50):
    """Crawl Lê Auction (Hà Nội) past sales for sold lots.

    Lê Auction is the dedicated Vietnamese 20th-century art house, so we
    default filter_vn=False (every artist here is presumed VN-relevant) —
    the parser only skips the placeholder "VIỆT NAM THẾ KỈ XX" label.

    Returns (inserted, scanned).
    """
    env = _load_env()
    email = env.get("BIDSPIRIT_EMAIL", "")
    password = env.get("BIDSPIRIT_PASSWORD", "")
    if not email or not password:
        if verbose:
            print("[le_auction] missing BIDSPIRIT_EMAIL/PASSWORD — skipped", flush=True)
        return 0, 0

    vn_catalog, exclusions = _load_vn() if filter_vn else ({}, set())

    # Build dedup index from existing OTHER-source rows in local SQLite.
    # Lê Auction frequently resells lots from Aguttes/Bonhams/Millon — we don't
    # want the same artwork appearing twice with different hammer prices.
    # Match key: (normalized artist, dims as-written sans spaces).
    from artonis_price_mvp import normalize_key
    dedup_keys = set()
    for row in conn.execute(
        "SELECT artist_name_raw, dimensions FROM sale_results WHERE source != ?",
        (SOURCE,),
    ):
        a, d = row[0] or "", row[1] or ""
        an = normalize_key(a)
        dn = d.replace(" ", "").lower()
        if an and dn:
            dedup_keys.add((an, dn))
    if verbose:
        print(f"  [le_auction] dedup index: {len(dedup_keys)} (artist, dims) pairs from other sources", flush=True)

    total_inserted = 0
    total_scanned = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=_NAME_PROFILE)
        page = ctx.new_page()

        if verbose:
            print("  [le_auction] opening homepage…", flush=True)
        page.goto(HOUSE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # ── Login via JS API (forces override of any existing session) ──
        if verbose:
            print("  [le_auction] logging in…", flush=True)
        login_resp = page.evaluate(
            "new Promise((res) => LoginUtils.doLogin("
            f"{json.dumps(email)}, {json.dumps(password)}, false, true, (r) => res(r)))"
        )
        user = (login_resp or {}).get("user", {})
        if not user or not user.get("emailConfirmed"):
            if verbose:
                print(f"  [le_auction] login failed: {login_resp}", flush=True)
            browser.close()
            return 0, 0
        if verbose:
            print(f"  [le_auction] logged in as {user.get('email')} (id={user.get('id')})", flush=True)
        page.wait_for_timeout(1500)

        # ── List past sales ──
        sales = page.evaluate(
            "fetch('/api/auctions/list/getNonHiddenAuctionDays.api?time=PAST&limit=100&offset=0', "
            "{credentials:'include'}).then(r => r.json())"
        )
        all_sales = (sales or {}).get("response", []) or []
        # Skip test sale
        real = [
            s for s in all_sales
            if "test" not in (s.get("auctionMainLangName") or "").lower()
        ]
        if sale_specs is None:
            sale_specs = [(s["auctionId"], s["dayId"]) for s in real]
        if verbose:
            print(f"  [le_auction] {len(sale_specs)} past sales to crawl", flush=True)

        sale_meta_by_day = {s["dayId"]: s for s in all_sales}

        for i, (aid, did) in enumerate(sale_specs[:max_pages], 1):
            run_started = datetime.utcnow().isoformat() + "Z"
            meta = sale_meta_by_day.get(did, {})
            sale_title = meta.get("auctionMainLangName") or f"Auction {aid}/{did}"
            sale_date = meta.get("startDate") or ""

            try:
                cat = page.evaluate(
                    "new Promise((res) => Api.auctions.catalog.loadAuctionDayCatalog("
                    f"{aid}, {did}, 'en', false, (r) => res(r)))"
                )
            except Exception as e:
                if verbose:
                    print(f"  [{i}/{len(sale_specs)}] {sale_title[:40]}: API ERR {e}", flush=True)
                log_crawl_run(conn, SOURCE, target_slug=f"{aid}/{did}", started_at=run_started,
                              status="error", note=str(e)[:200])
                continue

            # `portalKey` is the canonical Bidspirit ID used in deep-link URLs
            # (`/ui/lotPage/leauction/source/catalog/auction/{portalKey}/lot/{id}`).
            # It lives inside auction.auctionDays[<this-day>] alongside the
            # dayId. Without it the lot URL becomes a non-functional hash link.
            portal_key = None
            au = (cat or {}).get("auction") or {}
            for ad in (au.get("auctionDays") or []):
                if ad.get("id") == did:
                    portal_key = ad.get("portalKey")
                    break

            items = (cat or {}).get("items", []) or []
            inserted_this = 0
            for item in items:
                total_scanned += 1
                sold = item.get("soldPrice")
                if not sold:
                    continue  # only record sold lots

                artist_clean, _ = _normalize_author(item.get("author") or "")
                if not artist_clean:
                    continue

                if filter_vn and not _is_vietnamese(artist_clean, vn_catalog, exclusions):
                    continue

                parsed = _parse_lot_name(item.get("name") or "")
                est_lo, est_hi, est_cur = _estimate_low_high(item.get("estimatedPrice") or "")

                currency = item.get("currency") or est_cur or "USD"
                if currency == "$":
                    currency = "USD"
                if currency == "€":
                    currency = "EUR"
                if currency == "₫":
                    currency = "VND"

                try:
                    hammer = float(sold)
                except (TypeError, ValueError):
                    continue

                # Build dims if not in name — use width/height fields
                dims = parsed["dimensions"]
                if not dims and item.get("width") and item.get("height"):
                    dims = f"{item['width']} x {item['height']} cm"

                # Skip if this artwork (artist + dims) already exists from another source
                dedup_k = (normalize_key(artist_clean), dims.replace(" ", "").lower())
                if dedup_k[0] and dedup_k[1] and dedup_k in dedup_keys:
                    if verbose:
                        print(
                            f"      ~ skip dup of other source: {artist_clean[:22]} {dims}",
                            flush=True,
                        )
                    continue

                item_id = item.get("id") or item.get("itemIndex")
                if portal_key and item_id:
                    lot_url = (
                        f"https://leauction.bidspirit.com/ui/lotPage/leauction/source/"
                        f"catalog/auction/{portal_key}/lot/{item_id}/"
                    )
                else:
                    # Fallback to hash-format URL if portalKey missing
                    lot_url = f"{HOUSE_URL}#catalog~{aid}~{did}~item~{item_id}"

                rec = {
                    "source": SOURCE,
                    "source_url": lot_url,
                    "sale_page_url": f"{HOUSE_URL}#catalog~{aid}~{did}",
                    "lot_number": str(item.get("itemIndex") or ""),
                    "auction_title": f"Lê Auction — {sale_title}",
                    "sale_date": sale_date,
                    "sale_location": "Hà Nội",
                    "artist_name_raw": artist_clean,
                    "artwork_title": parsed["artwork_title"][:300],
                    "medium": parsed["medium"] or (item.get("categoryName") or "")[:200],
                    "dimensions": dims,
                    "year": "",
                    "estimate_low": est_lo,
                    "estimate_high": est_hi,
                    "hammer_price": hammer,
                    "price_with_premium": None,
                    "currency": currency,
                    "status": "sold",
                    "provenance": "",
                    "raw_snapshot": (clean_text(item.get("description") or ""))[:500],
                }
                insert_sale_result(conn, rec)
                inserted_this += 1
                if verbose:
                    print(
                        f"      +{artist_clean[:22]:22s} | "
                        f"{parsed['artwork_title'][:30]:30s} | "
                        f"{dims:14s} | {hammer:>10.0f} {currency}",
                        flush=True,
                    )

            conn.commit()
            log_crawl_run(
                conn, SOURCE, target_slug=f"{aid}/{did}", started_at=run_started,
                lots_scanned=len(items), lots_inserted=inserted_this,
                sale_date_min=sale_date or None, sale_date_max=sale_date or None,
                status="ok", note=sale_title[:120],
            )
            if verbose:
                print(
                    f"  [{i}/{len(sale_specs)}] {sale_date or '??????????':10s} "
                    f"{sale_title[:50]:50s}: {len(items):3d} lots / {inserted_this} inserted",
                    flush=True,
                )
            total_inserted += inserted_this
            time.sleep(delay)

        browser.close()

    return total_inserted, total_scanned


if __name__ == "__main__":
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    DB = str(Path(__file__).resolve().parent.parent / "data" / "artonis_price_mvp.sqlite")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    inserted, scanned = crawl(conn)
    print(f"\nDONE — inserted {inserted}, scanned {scanned}")
    conn.close()
