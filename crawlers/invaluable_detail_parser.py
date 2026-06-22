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
                rf'^{pat}\s*(?:\([^)]*\))?\s*(?:b\.\s*\d{{4}}[\-,\s]*)?[,;:\-–.|]?\s*',
                '',
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            if new_text != text:
                text = new_text
                break
    # 3. Strip leading "(Country, year-)" pattern
    text = re.sub(r'^\([^)]*\)\s*[,;:\-–.|]?\s*', '', text)
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
# Allow the unit between the two numbers too — '100cm x 100cm' is a common
# Vietnamese-catalogue formatting (NTR 'Message' lot regressed because the
# regex only allowed an optional quote between number and 'x', not 'cm').
_DIM_RE = re.compile(
    rf'({_FRAC_NUM})\s*(?:cm|inches?|in|["″])?\s*'
    rf'[xX×]\s*'
    rf'({_FRAC_NUM})\s*(cm|inches?|in|"|″)(?:\s|$|[,.;])'
)


def _title_from_invaluable_slug(url, artist_tokens=None):
    """Recover artwork_title from an Invaluable lot URL when H1 / Description
    parsing failed.  Patterns we handle:

      A) <title>-by-<artist>-<birth>-<death>-<…>      (Christie's-style)
         e.g. spring-garden-by-nguyen-gia-tri-1908-1993-77-x-57-64-c-…
              → 'Spring Garden'

      B) <artist>-<birth>-<death>-<title>-<lot>       (Aguttes / Bonhams style)
         e.g. nguyen-sang-1923-1988-portrait-de-femme-1954-20-c-…
              → 'Portrait de Femme 1954'

      C) <artist>-b-<birth>-<title>-<lot>             (Bonhams 'b. 1962' style)
         e.g. hong-viet-dung-b-1962-lady-with-a-fan-55-c-…
              → 'Lady with a Fan'

      D) <artist>-<title>-o-c-<lot>                   (Litchfield style)
         e.g. dao-hai-phong-boat-by-house-o-c-315-c-…
              → 'Boat by House'                       (o-c = 'oil/canvas' suffix)

      E) <artist>-<lot>                               (no title in slug)
         e.g. dang-xuan-hoa-103-c-cf44656a5c          → None

    `artist_tokens` (deaccented, lowercased) is optional — if supplied the
    parser walks past the leading artist words for patterns B / C / D.
    Without it we only handle pattern A.
    """
    if not url:
        return None
    m = re.match(r'.*/auction-lot/([a-z0-9\-]+)-c-[a-f0-9]+', url)
    if not m:
        return None
    slug = m.group(1)
    title_slug = None

    # A) explicit 'by ARTIST years' separator
    m_by = re.match(r'(.+?)-by-[a-z\-]+\d{4}-\d{4}', slug)
    if m_by:
        title_slug = m_by.group(1)
    elif artist_tokens:
        # B / C / D: artist comes first; walk past artist tokens and the
        # year (or 'b. year') metadata that often follows, then take what's
        # left minus the trailing lot number + suffix codes.
        parts = slug.split('-')
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in artist_tokens:
                i += 1
                continue
            # 'b'/'born' followed by a 4-digit year
            if p in ('b', 'born') and i + 1 < len(parts) and re.fullmatch(r'\d{4}', parts[i + 1]):
                i += 2
                continue
            # 4-digit year(s) — birth, death, or sale year
            if re.fullmatch(r'\d{4}', p):
                i += 1
                continue
            break
        # Strip trailing lot-number / suffix codes — but DON'T strip 4-digit
        # years (1900-2099) because the year is often the last meaningful
        # token of the title ('Portrait de Femme 1954').
        j = len(parts)
        while j > i:
            tail = parts[j - 1]
            if re.fullmatch(r'19\d{2}|20\d{2}', tail):
                break
            if re.fullmatch(r'\d+', tail) or tail in ('o', 'c', 'p'):
                j -= 1
                continue
            break
        if i < j:
            tokens = parts[i:j]
            # Drop any embedded dimension chunks like '27', 'x', '33-5'
            if all(not re.match(r'\d', t) for t in tokens) or len(tokens) >= 2:
                title_slug = '-'.join(tokens)

    if not title_slug:
        return None
    parts = title_slug.split('-')
    if not parts or len(parts) > 10:
        return None
    small = {'a', 'an', 'and', 'of', 'the', 'in', 'on', 'for', 'to', 'at', 'with', 'by', 'de', 'la', 'le', 'du', 'des', 'les', 'et', 'sur'}
    title = ' '.join(w.capitalize() if (i == 0 or w not in small) else w for i, w in enumerate(parts))
    return title if 2 <= len(title) <= 100 else None


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
            # Artist name from H1 = canonical (Invaluable's 'Artist or Maker'
            # label sometimes aggregates 'Nguyen Trung Phan' under 'Nguyen Trung'
            # because they share the prefix; H1 has the full attribution).
            h1_clean = h1.replace('\xa0', ' ').strip()
            h1_after_lot = re.sub(r'^Lot\s+\d+\s*[:,\-–]\s*', '', h1_clean)
            m_artist = re.match(
                r'^([A-Z][A-Za-z\s.\-]+?)(?=\s*[\(\.,]|\s+\d{4}|\s*$)',
                h1_after_lot,
            )
            if m_artist:
                name = m_artist.group(1).strip().rstrip('.,;:')
                # Title-case ALL-CAPS variants
                if name == name.upper() and ' ' in name:
                    name = ' '.join(w.capitalize() for w in name.lower().split())
                if 2 <= len(name) <= 80:
                    out['artist_from_h1'] = name
            # Prefer H1-derived (multi-word) artist for title stripping
            title, year = _h1_to_title_year(h1, out.get('artist_from_h1') or artist or '')
            if title:
                out['artwork_title'] = title
            if year:
                out['year'] = year
    except Exception:
        pass

    # If H1 had no title (just artist name) — fall back to Description section
    # which typically lists title on its 3rd-ish line for many auction houses.
    if not out.get('artwork_title'):
        desc = _section_value(body, 'Description', _SECTION_STOPS)
        if desc:
            lines = [l.strip() for l in desc.split('\n') if l.strip()]
            content_lines = []
            for L in lines:
                # all-caps short name "NGUYEN GIA TRI"
                if L == L.upper() and len(L.split()) <= 5 and L.replace(' ','').isalpha():
                    continue
                # 'ARTIST NAME (VIETNAMESE, YYYY-YYYY)' or 'ARTIST (YYYY-YYYY)' or 'NGUYEN GIA TRI (1908-1993)'
                if re.match(r'^[A-ZÀ-Ý][A-ZÀ-Ý\s\-\.]{2,}\s*\([^)]*\d{4}[^)]*\)\s*$', L):
                    continue
                # 'Vietnamese, b. YYYY' / 'Vietnamese, YYYY-YYYY'
                if re.match(r'^\(?Vietnamese?,?\s+b?\.?\s*\d{4}', L, re.IGNORECASE):
                    continue
                # '(Vietnamese, 1908-1993)' garbage line
                if re.match(r'^\(\s*Vietnamese?,?\s*\d{4}\s*[-–]?\s*\d{0,4}\s*\)\s*$', L, re.IGNORECASE):
                    continue
                # 'Vietnam, YYYY-' line
                if re.match(r'^Vietnam,?\s*\d{4}\-?\s*$', L, re.IGNORECASE):
                    continue
                # '(b. YYYY)' line
                if re.match(r'^\(?b\.?\s*\d{4}\)?\s*$', L):
                    continue
                content_lines.append(L)
            if content_lines:
                cand = content_lines[0].rstrip('.,;: ').strip()
                m_yr = re.search(r',\s*(\d{4})\s*$', cand)
                if m_yr:
                    out['year'] = m_yr.group(1)
                    cand = cand[:m_yr.start()].strip()
                if 2 <= len(cand) <= 200:
                    out['artwork_title'] = cand

    # Final safety: if artwork_title ended up looking like artist+year info
    # ('(Vietnamese, 1908-1993)', 'NGUYEN GIA TRI', etc.), clear it.
    cur_t = (out.get('artwork_title') or '').strip()
    if cur_t:
        if re.match(r'^\(\s*[A-Za-zà-ÿÀ-Ý]+,?\s*(?:b\.?\s*)?\d{4}\s*[-–]?\s*\d{0,4}\s*\)\s*$', cur_t):
            out.pop('artwork_title', None)
        elif re.match(r'^[A-ZÀ-Ý][A-ZÀ-Ý\s\-\.]{2,}\s*\([^)]*\d{4}[^)]*\)?\s*$', cur_t):
            out.pop('artwork_title', None)
        elif re.fullmatch(r'\d+(\.\d+)?\s*[xX×]\s*\d+(\.\d+)?\s*(?:cm|in|inches?)?', cur_t):
            out.pop('artwork_title', None)

    # Safety: if extracted title is just a dimensions string (parser cleanup
    # removed everything else), recover the real title from the URL slug.
    # Pattern: '/auction-lot/<title-slug>-by-<artist-slug>-<years>-<...>-c-<hash>'
    # The 'by-<artist>' marker reliably separates title from artist; if missing
    # we fall back to the leading slug tokens before the trailing numeric chunk.
    cur_title = (out.get('artwork_title') or '').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?\s*[xX×]\s*\d+(?:\.\d+)?\s*(?:cm|in|inches?)?', cur_title):
        recovered = _title_from_invaluable_slug(url)
        if recovered:
            out['artwork_title'] = recovered

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
        # Normalize display: always cm, 1-decimal precision (drop .0 if integer)
        def _fmt(n):
            return f'{int(n)}' if n == int(n) else f'{n:.1f}'
        out['dimensions'] = f'{_fmt(w)} x {_fmt(h)} cm'

    # If Medium label was missing, take it from the prefix of Dimensions
    # section (e.g. 'Gouache on paper 21" x 29" sight' → 'Gouache on paper').
    if 'medium' not in out and dims_text:
        m_med = re.match(r'^(.+?)\s+\d+(?:\s+\d+/\d+)?(?:[.,]\d+)?\s*(?:["″])?\s*[xX×]', dims_text)
        if m_med:
            cand = m_med.group(1).strip().rstrip('.,;:')
            if 3 <= len(cand) <= 100:
                out['medium'] = cand

    # Provenance
    prov = _section_value(body, 'Provenance', _SECTION_STOPS)
    if prov and len(prov) < 1000:
        out['provenance'] = prov

    # Estimate (Est: $X CUR - $Y CUR)
    try:
        body_text = body  # already computed above
        m_est = re.search(
            r'Est:?\s*\$?\s*([\d,]+(?:[.,]\d+)?)\s*([A-Z]{3})?\s*[-–]\s*\$?\s*([\d,]+(?:[.,]\d+)?)\s*([A-Z]{3})?',
            body_text,
        )
        if m_est:
            cur = (m_est.group(4) or m_est.group(2) or 'USD').upper()
            try:
                low = float(m_est.group(1).replace(',', ''))
                high = float(m_est.group(3).replace(',', ''))
                out['estimate_low'] = int(low)
                out['estimate_high'] = int(high)
                out['estimate_currency'] = cur
            except ValueError:
                pass
    except Exception:
        pass

    # Sold/realized hammer (when shown publicly)
    try:
        m_sold = re.search(
            r'(?:Sold|Realized|Realised|Price Realised):?\s*\$?\s*([\d,]+(?:[.,]\d+)?)\s*([A-Z]{3})?',
            body,
        )
        if m_sold and 'Log in to view' not in body[max(0, m_sold.start()-30):m_sold.start()+50]:
            try:
                amt = float(m_sold.group(1).replace(',', ''))
                if amt > 0:
                    out['hammer_price'] = amt
                    if m_sold.group(2):
                        out['hammer_currency'] = m_sold.group(2).upper()
            except ValueError:
                pass
    except Exception:
        pass

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
