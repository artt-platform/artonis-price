"""Invaluable detail parser v3 — proper field separation.

Key fix vs v2: every Playwright session gets a fresh BrowserContext (not just
fresh Page). v2 reused context across requests and Invaluable's anti-bot
silently downgraded later responses to a generic page (H1 reads 'www.invaluable.com',
body returns no lot data). Fresh context per lot eliminates that.

Field separation:
  artwork_title:   from H1 'Lot N: [artist info] TITLE, YYYY' — strip artist + year
  year:            trailing ', YYYY' in H1 OR 'Date' label
  medium:          'Medium' label
  dimensions:      'Dimensions' label (inch+fraction → cm conversion)
  provenance:      'Provenance' label (multi-line preserved)
  auction_house:   <a href="/auction-house/{slug}"> inner text
"""
import re
import sys
from playwright.sync_api import sync_playwright


def _h1_to_title_year(h1, artist_name):
    """Extract clean title + year from H1 like 'Lot 58: DANG XUAN HOA : Objects in My House, 1995'."""
    if not h1:
        return None, None
    text = h1.replace('\xa0', ' ').strip()
    # 1. Drop 'Lot N: ' prefix
    text = re.sub(r'^Lot\s+\d+\s*[:,\-–]\s*', '', text)
    # 2. Drop artist name (try Title-Case, UPPER, plus reversed-order versions —
    #    'Artist or Maker' label may have lastname-firstname while H1 has
    #    firstname-lastname). Iterate until no more strip needed.
    if artist_name:
        parts = artist_name.split()
        variants = []
        for p in (artist_name, ' '.join(reversed(parts))):
            variants.extend([p, p.upper()])
        # Dedupe while preserving order
        seen = set(); ordered = []
        for v in variants:
            if v.lower() not in seen:
                seen.add(v.lower()); ordered.append(v)
        for variant in ordered:
            pat = re.escape(variant)
            new_text = re.sub(
                rf'^{pat}\s*(?:\([^)]*\))?\s*(?:b\.\s*\d{{4}}[\-,\s]*)?[,;:\-–.]?\s*',
                '',
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            if new_text != text:
                text = new_text
                break
    # 3. Strip leading "(Country, year-)" pattern
    text = re.sub(r'^\([^)]*\)\s*[,;:\-–.]?\s*', '', text)
    # 4. Pull trailing year ", YYYY" into separate field
    year = None
    m = re.search(r',\s*(\d{4})\b\s*[.,;]?\s*$', text)
    if m:
        year = m.group(1)
        text = text[: m.start()].strip()
    else:
        m2 = re.search(r',\s*(\d{4})\s*[.,]\s+(?:Gouache|Oil|Acrylic|Mixed|Lacquer|Ink|Watercolor)', text, re.IGNORECASE)
        if m2:
            year = m2.group(1)
            text = text[: m2.start()].strip()
    # 5. Drop trailing medium descriptor ('Gouache on paper 21" x 29" sight.')
    text = re.sub(
        r'[,.;:\s]+(?:Gouache|Oil|Acrylic|Mixed media|Lacquer|Ink|Watercolor|Pastel|Pencil|Charcoal)\b.+$',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # 6. Drop trailing dimensions pattern ('Untitled 16 x 12 in. (40.6 x 30.5 cm)...')
    text = re.sub(
        r'\s+\d+(?:\s+\d+/\d+)?(?:[.,]\d+)?\s*[xX×]\s*\d+(?:\s+\d+/\d+)?(?:[.,]\d+)?\s*(?:in|cm|inches?|\")\.?.*$',
        '',
        text,
    )
    # 7. Drop CJK translation block (Chinese/Japanese/Korean script) — for lots
    #    that bilingually list 'English Title 中文標題', keep only the English.
    #    Also catch the 'i. ii. iii.' list marker that precedes the CJK list.
    text = re.sub(
        r'\s+(?:i+v?\.\s+)?[\u3000-\u303f\u3400-\u9fff\uff00-\uffef].*$',
        '',
        text,
    )
    # 8. Drop leading CJK char + separator ('鄧春和 | TITLE')
    text = re.sub(r'^[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+\s*[|:\-–]\s*', '', text)
    # 9. Cleanup trailing punctuation
    text = text.rstrip('.,;:– ').strip()
    if len(text) < 2 or len(text) > 300:
        return None, year
    return text, year


_FRAC_NUM = r'\d+(?:\s+\d+/\d+)?(?:[.,]\d+)?'
_DIM_RE = re.compile(rf'({_FRAC_NUM})\s*[xX×]\s*({_FRAC_NUM})\s*(cm|inches?|in|")\b')


def _parse_num(s):
    s = s.replace(',', '.')
    if ' ' in s and '/' in s:
        whole, frac = s.rsplit(' ', 1)
        try:
            num, den = frac.split('/')
            return float(whole) + float(num) / float(den)
        except ValueError:
            return float(whole)
    return float(s)


def _parse_dims_text(text):
    """Find first w x h cm/in pattern, return (w_cm, h_cm, raw_match) or None."""
    m = _DIM_RE.search(text)
    if not m:
        return None
    try:
        w = _parse_num(m.group(1))
        h = _parse_num(m.group(2))
    except ValueError:
        return None
    unit = m.group(3).lower()
    if unit.startswith('in') or unit == '"':
        w *= 2.54
        h *= 2.54
    return round(w, 2), round(h, 2), m.group(0)


def _section_value(body, label, stop_labels):
    """Pull text between '\\n{label}\\n' and the next stop label (or EOF)."""
    needle = f'\n{label}\n'
    idx = body.find(needle)
    if idx < 0:
        return None
    start = idx + len(needle)
    end = len(body)
    for sl in stop_labels:
        i = body.find(f'\n{sl}\n', start)
        if i >= 0:
            end = min(end, i)
    value = body[start:end].strip()
    return value if value else None


_SECTION_STOPS = [
    'Artist or Maker', 'Medium', 'Date', 'Provenance', 'Notes', 'Style', 'Period',
    'Condition Report', 'Description', 'Dimensions', 'Item Overview',
    'Request more information', 'Payment & Shipping', 'Auction Details', 'Terms',
    'Bid On-the-Go!',
]


def parse_lot_page(page, url):
    """Parse one lot detail page; return dict of extracted fields."""
    page.goto(url, timeout=45000, wait_until='domcontentloaded')
    try:
        page.wait_for_selector('text=/^Artist or Maker$/', timeout=15000)
    except Exception:
        pass

    out = {'url': url}
    body = page.inner_text('body')
    if len(body) < 500 or 'Lot details' not in body and 'Item Overview' not in body:
        return out  # Page didn't render; let caller retry

    # Artist via 'Artist or Maker' label
    artist = _section_value(body, 'Artist or Maker', _SECTION_STOPS)
    if artist and len(artist) < 100:
        out['artist'] = artist

    # H1 → title + year
    try:
        h1s = page.locator('h1').all_text_contents()
        h1 = h1s[0] if h1s else ''
        if h1 and h1.strip().lower() != 'www.invaluable.com':
            out['h1_raw'] = h1
            title, year = _h1_to_title_year(h1, artist or '')
            if title:
                out['artwork_title'] = title
            if year:
                out['year'] = year
    except Exception:
        pass

    # Date label overrides h1 year only if h1 didn't find one
    date_val = _section_value(body, 'Date', _SECTION_STOPS)
    if date_val and 'year' not in out:
        m = re.search(r'\b(19\d{2}|20\d{2})\b', date_val)
        if m:
            out['year'] = m.group(1)

    # Medium
    medium = _section_value(body, 'Medium', _SECTION_STOPS)
    if medium and len(medium) < 200:
        out['medium'] = medium

    # Dimensions section first; fallback to whole body
    dims_text = _section_value(body, 'Dimensions', _SECTION_STOPS)
    dim_result = _parse_dims_text(dims_text) if dims_text else None
    if not dim_result:
        dim_result = _parse_dims_text(body)
    if dim_result:
        w, h, raw = dim_result
        out['width_cm'] = w
        out['height_cm'] = h
        out['area_m2'] = round(w * h / 10000, 4)
        out['dimensions'] = raw

    # Provenance
    prov = _section_value(body, 'Provenance', _SECTION_STOPS)
    if prov and len(prov) < 1000:
        out['provenance'] = prov

    # Auction house from <a href="/auction-house/">
    try:
        hl = page.locator('a[href*="/auction-house/"]').first
        if hl.count() > 0:
            text = hl.inner_text().strip()
            if text and 1 < len(text) < 100:
                out['auction_house'] = text
                out['auction_house_url'] = 'https://www.invaluable.com' + hl.get_attribute('href')
    except Exception:
        pass

    return out


# ---- CLI: test on a single URL ----
if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.invaluable.com/auction-lot/dang-xuan-hoa-b-vietnam-1959-human-objects-1999-179-c-9c144f0a37"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        ctx = b.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
            viewport={'width': 1920, 'height': 1080},
        )
        page = ctx.new_page()
        result = parse_lot_page(page, url)
        for k, v in result.items():
            print(f'  {k}: {v!r}')
        b.close()
