"""Invaluable crawler — aggregates 100s of auction houses. Playwright required for anti-bot.
Provides: artist, title, medium, dimensions, estimate (hammer needs login - not captured)."""
import re
import time
from urllib.parse import urljoin

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run
from crawlers.direct_owned_houses import is_direct_owned, is_excluded
from crawlers.parsers import is_attribution

BASE = "https://www.invaluable.com"

# Vietnamese artist slug-IDs on Invaluable.
#
# UNLIKE other crawlers, this list cannot be derived from
# data/vn_artist_catalog.py.  Invaluable's per-artist URL embeds an
# opaque 10-char hash ID ('pho-le-e2gj8yti0x') with no public name→hash
# mapping.  Hashes are discovered manually via:
#   - sitemap_artist_sold_{1..5}.xml scan (sitemap walk)
#   - invaluable.com/artists search via Playwright (Cloudflare blocks curl)
#
# Coverage gap to close — these catalog artists likely have Invaluable
# pages but their hash slug is not yet discovered:
#   Lê Huy Hòa, Lưu Công Nhân, Nguyễn Trọng Kiệm (the rest of the Nhân
#   Hòa Hậu Kiệm group — Trần Lưu Hậu already seeded as hau-tran-luu).
# Original 9 discovered manually; 13 added via sitemap scan 2026-06-20.
VN_ARTISTS = [
    # ─── Đông Dương / Bộ Tứ Trời Âu (highest auction volume) ───
    ("pho-le-e2gj8yti0x", "Le Pho"),
    ("vu-cao-dam-a66cifzwj8", "Vu Cao Dam"),
    ("thu-mai-trung-5uyddinxpg", "Mai Trung Thu"),
    ("thi-luu-le-f0nhkt6ylm", "Le Thi Luu"),
    ("phan-chanh-nguyen-31jb0v4ys3", "Nguyen Phan Chanh"),
    ("van-to-ngoc-kv3lei58fg", "To Ngoc Van"),
    ("can-tran-van-8cjqm4q98d", "Tran Van Can"),
    ("nguyen-gia-tri-osazdyf3y6", "Nguyen Gia Tri"),
    ("pham-hau-rked7esf7v", "Pham Hau"),
    # ─── Bộ Tứ Hà Nội ───
    ("bui-xuan-phai-pwauujf4v2", "Bui Xuan Phai"),
    ("nguyen-sang-7whaovmann", "Nguyen Sang"),
    ("nguyen-tu-nghiem-a30sjp2rme", "Nguyen Tu Nghiem"),
    # ─── French Indochina school adjacent ───
    ("alix-dailhac-ayme-n72w6nwo56", "Alix Aymé"),
    ("joseph-inguimberty-2ud5mwyar1", "Joseph Inguimberty"),
    # ─── Đổi Mới / Contemporary ───
    ("lebadang-gablwd0251", "Lebadang"),
    ("hau-tran-luu-0wx9ctzcby", "Tran Luu Hau"),
    ("hai-pham-an-z9tlw6za25", "Pham An Hai"),
    ("dinh-quan-fjz76pnnfi", "Dinh Quan"),
    ("le-thanh-son-n5mvwrhodf", "Le Thanh Son"),
    ("nguyen-lam-u8xqwzm847", "Nguyen Lam"),
    ("thanh-binh-nguyen-027bbf808c", "Nguyen Thanh Binh"),
    ("chuong-thanh-2oyzu9cp3o", "Thanh Chuong"),
    # ─── Gang of Five / Đổi Mới Hà Nội ───
    ("dung-hong-viet-dgjgs2zn6h", "Hong Viet Dung"),
    ("phong-dao-hai-jy26j9j340", "Dao Hai Phong"),
    ("hung-bui-huu-1g69oe3i2y", "Bui Huu Hung"),
    ("hoa-dang-xuan-s0o1amemkp", "Dang Xuan Hoa"),
    ("trung-nguyen-pk5fzyq7yb", "Nguyen Trung"),
]


def _extract_cards_via_browser(page):
    """Run JS in-page to extract all cards around auction-lot links.
    Also pulls the card thumbnail src so we get an image_url without
    visiting the per-lot page (which is CF-blocked)."""
    js = """
    () => {
        const results = [];
        const links = document.querySelectorAll('a[href*="/auction-lot/"]');
        const seen = new Set();
        for (const link of links) {
            const href = link.getAttribute('href');
            if (seen.has(href)) continue;
            seen.add(href);
            let el = link;
            for (let i = 0; i < 8; i++) {
                if (!el.parentElement) break;
                el = el.parentElement;
                if (el.innerText.length > 150) break;
            }
            // Find the thumbnail image inside the card.  Prefer the
            // largest <img> (Invaluable cards use a card-level lot
            // thumbnail + sometimes a tiny house logo).
            let imgUrl = '';
            const imgs = el.querySelectorAll('img');
            let bestArea = 0;
            for (const img of imgs) {
                const src = img.src || img.getAttribute('data-src') || '';
                if (!src || !src.startsWith('http')) continue;
                if (src.includes('logo') || src.includes('avatar')) continue;
                const area = (img.naturalWidth || img.width || 0) * (img.naturalHeight || img.height || 0);
                if (area > bestArea) { bestArea = area; imgUrl = src; }
            }
            results.push({href, text: el.innerText.trim().substring(0, 1500), image: imgUrl});
        }
        return results;
    }
    """
    return page.evaluate(js)


def _parse_card(card_text, canonical_artist=""):
    """Parse one Invaluable card text. Returns dict or None."""
    text = card_text
    # Extract sale date (e.g., "Mar. 28, 2026")
    m_date = re.search(r"([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})", text)
    sale_date = parse_date(m_date.group(1)) if m_date else ""

    # Extract estimate: "Est: $750,000 - $950,000" or "Est: S$120,000 - S$150,000" (SGD)
    m_est = re.search(r"Est[:\s]+(\w*\$?|€|£)?\s*([\d,]+(?:\s*\w{0,3})?)\s*[-–]\s*(\w*\$?|€|£)?\s*([\d,]+)", text)
    est_low = est_high = None
    currency = "USD"
    if m_est:
        low_cur = (m_est.group(1) or "").upper()
        high_cur = (m_est.group(3) or "").upper()
        cur_marker = low_cur or high_cur
        if "S$" in cur_marker or "SGD" in cur_marker:
            currency = "SGD"
        elif "HK" in cur_marker:
            currency = "HKD"
        elif "£" in cur_marker:
            currency = "GBP"
        elif "€" in cur_marker:
            currency = "EUR"
        else:
            # Plain `$` is ambiguous. Default to HKD for Sotheby's/Christie's HK sales
            # (we can infer from text context — Chinese characters or Hong Kong cues).
            if re.search(r"[一-鿿]", text) or "Hong Kong" in text or "香港" in text:
                currency = "HKD"
            elif "Singapore" in text:
                currency = "SGD"
            # Otherwise USD remains the default for US-based houses
        try:
            est_low = float(m_est.group(2).replace(",", "").split()[0])
            est_high = float(m_est.group(4).replace(",", "").split()[0])
        except ValueError:
            pass

    # Extract title — line after date, usually including artist name
    # Pattern: "LE PHO (1907-2001). Title. medium..." or "Le Pho 黎譜 | Title"
    title = ""
    medium = ""
    dimensions = ""
    # Try to grab the core "Title" portion
    # Post-date line is the heading
    after_date = text
    if m_date:
        after_date = text[m_date.end():]
    lines = [l.strip() for l in after_date.split("\n") if l.strip()]
    heading = lines[0] if lines else ""

    # "LE PHO (1907-2001). La partie de cartes (The card game). oil on canvas..."
    m_heading = re.match(
        r"([A-ZÀ-Ÿ][A-ZÀ-Ÿ\s\-]+)\s*\(\d{4}[-\s\d]*\)\.?\s*(.+)",
        heading,
    )
    if m_heading:
        title_plus = m_heading.group(2)
        # Title is before ". oil" or ". pencil" or "."
        parts = re.split(r"\.\s+(?=[a-z])", title_plus, maxsplit=1)
        title = clean_text(parts[0])
        if len(parts) > 1:
            # Second part likely has medium + dims
            rest = parts[1]
            m_dim = re.search(r"(\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?\s*cm)", rest, re.IGNORECASE)
            if m_dim:
                dimensions = m_dim.group(1)
                medium = clean_text(rest[:m_dim.start()])

    # Fallback: Title after "| "
    if not title:
        m_pipe = re.search(r"\|\s*(.+?)(?:\n|$)", heading)
        if m_pipe:
            title = clean_text(m_pipe.group(1))

    # Dimensions fallback — also check "X by Y cm" format and "XxYcm"
    if not dimensions:
        for pat in [
            r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm",
            r"(\d+(?:[.,]\d+)?)\s*by\s*(\d+(?:[.,]\d+)?)\s*cm",
        ]:
            m_dim = re.search(pat, text, re.IGNORECASE)
            if m_dim:
                dimensions = f"{m_dim.group(1).replace(',','.')} x {m_dim.group(2).replace(',','.')} cm"
                break

    # Auction house — usually on its own line near the end
    auction_house = ""
    for line in reversed(lines[-5:]):
        if re.search(r"(Christie|Sotheby|Bonhams|Phillips|Koller|Kodner|Artcurial|Aguttes|Heritage|Swann)", line, re.I):
            auction_house = line
            break

    return {
        "title": title,
        "medium": medium,
        "dimensions": dimensions,
        "sale_date": sale_date,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "currency": currency,
        "auction_house": auction_house,
        "artist_name_raw": canonical_artist,
    }


def crawl_artist(page, slug, canonical_name, timeout=40000):
    """Fetch Invaluable artist page and return list of lot records."""
    url = f"{BASE}/artist/{slug}/sold-at-auction-prices/"
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    except Exception as e:
        return None, f"goto failed: {e}"
    page.wait_for_timeout(3500)
    # Scroll to load lazy content
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 1800)")
        page.wait_for_timeout(1300)

    cards = _extract_cards_via_browser(page)
    if not cards:
        return [], None

    records = []
    for card in cards:
        parsed = _parse_card(card["text"], canonical_name)
        if not parsed or not parsed.get("title"):
            continue
        # Invaluable cards don't expose city, but the currency reliably maps to a region.
        cur = (parsed.get("currency") or "").upper()
        location_guess = {
            "USD": "New York", "HKD": "Hong Kong", "GBP": "London",
            "EUR": "Paris", "SGD": "Singapore", "CHF": "Zürich",
        }.get(cur, "")
        # Skip when the upstream house has its own direct crawler — we
        # don't want Invaluable duplicating Bonhams/Christie's/Aguttes/
        # Austin/etc. lots that the direct crawler already covers more
        # accurately (real hammer vs Invaluable's mid-estimate proxy).
        # See crawlers/direct_owned_houses.py for the full list.
        if is_direct_owned(parsed.get("auction_house", "")):
            continue
        # Operator-flagged low-trust houses (Cadmore: fake paintings,
        # lots re-listed without buyers).  See SPEC §13.
        if is_excluded(parsed.get("auction_house", "")):
            continue
        # Skip 'attributed to' / 'after X' / 'circle of' lots — not the
        # confirmed artist's work, would skew per-artist medians.  See
        # crawlers/parsers/fake_markers.py.  Rule documented in SPEC §13.
        if is_attribution(card.get("href", ""), parsed.get("title", "") or ""):
            continue
        rec = {
            "source": "invaluable",
            "source_url": urljoin(BASE, card["href"]),
            "lot_number": "",
            "auction_title": parsed.get("auction_house", ""),
            "sale_date": parsed.get("sale_date", ""),
            "sale_location": location_guess,
            "artist_name_raw": canonical_name,
            "artwork_title": parsed.get("title", ""),
            "medium": parsed.get("medium", ""),
            "dimensions": parsed.get("dimensions", ""),
            "estimate_low": parsed.get("estimate_low"),
            "estimate_high": parsed.get("estimate_high"),
            # Use midpoint of estimate as "hammer" proxy since hammer requires login
            "hammer_price": ((parsed.get("estimate_low") or 0) + (parsed.get("estimate_high") or 0)) / 2 or None,
            "currency": parsed.get("currency", "USD"),
            "status": "estimate_only",  # mark that this is estimate, not realized
            "raw_snapshot": card["text"][:500],
            # Card thumbnail — fetched from the Invaluable artist-listing
            # page where CF is more permissive than the lot-detail page.
            # Stores the CDN URL directly (image.invaluable.com / etc.);
            # backfill_og_images.py won't try to refetch these.
            "image_url": (card.get("image") or None),
        }
        if rec["hammer_price"]:
            records.append(rec)
    return records, None


def crawl_all(conn, artists=None, delay=3, verbose=True):
    """Launch Playwright once, crawl all artists."""
    from playwright.sync_api import sync_playwright
    artists = artists or VN_ARTISTS
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()
        from datetime import datetime
        for slug, name in artists:
            run_started = datetime.utcnow().isoformat() + "Z"
            recs, err = crawl_artist(page, slug, name)
            if err:
                if verbose:
                    print(f"  [{slug}] err: {err}")
                log_crawl_run(conn, "invaluable", target_slug=slug, started_at=run_started,
                              status="error", note=str(err)[:200])
                time.sleep(delay)
                continue
            date_min = date_max = None
            for r in recs:
                insert_sale_result(conn, r)
                sd = r.get("sale_date") or ""
                if sd:
                    if date_min is None or sd < date_min: date_min = sd
                    if date_max is None or sd > date_max: date_max = sd
            conn.commit()
            log_crawl_run(conn, "invaluable", target_slug=slug, started_at=run_started,
                          lots_scanned=len(recs), lots_inserted=len(recs),
                          sale_date_min=date_min, sale_date_max=date_max,
                          status="ok", note=name)
            if verbose:
                print(f"  [{name}] {len(recs)} estimates inserted")
            total += len(recs)
            time.sleep(delay)
        browser.close()
    return total
