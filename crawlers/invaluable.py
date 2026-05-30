"""Invaluable crawler — aggregates 100s of auction houses. Playwright required for anti-bot.
Provides: artist, title, medium, dimensions, estimate (hammer needs login - not captured)."""
import re
import time
from urllib.parse import urljoin

from crawlers.common import parse_amount, parse_date, insert_sale_result, clean_text, log_crawl_run

BASE = "https://www.invaluable.com"

# Known Vietnamese artist slug-IDs on Invaluable (discovered manually)
VN_ARTISTS = [
    ("pho-le-e2gj8yti0x", "Le Pho"),
    ("vu-cao-dam-a66cifzwj8", "Vu Cao Dam"),
    ("thu-mai-trung-5uyddinxpg", "Mai Trung Thu"),
    ("thi-luu-le-f0nhkt6ylm", "Le Thi Luu"),
    ("phan-chanh-nguyen-31jb0v4ys3", "Nguyen Phan Chanh"),
    ("alix-dailhac-ayme-n72w6nwo56", "Alix Aymé"),
    ("joseph-inguimberty-2ud5mwyar1", "Joseph Inguimberty"),
    ("nguyen-lam-u8xqwzm847", "Nguyen Lam"),
    ("thanh-binh-nguyen-027bbf808c", "Nguyen Thanh Binh"),
]


def _extract_cards_via_browser(page):
    """Run JS in-page to extract all cards around auction-lot links."""
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
            results.push({href, text: el.innerText.trim().substring(0, 1500)});
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
