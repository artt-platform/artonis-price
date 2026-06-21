"""Invaluable detail crawler v5 — adds currency-correct pricing + artist validation.

v4 → v5 changes:
1. Parser now extracts (estimate_low, estimate_high, estimate_currency) — many
   Invaluable lots were imported with HKD/GBP/EUR estimates stored as if they
   were USD. v5 recomputes price_usd = midpoint × FX so Christie's HK lots
   stop looking like multi-million-dollar pieces.
2. Artist validation: 'Artist or Maker' from the page is normalized and matched
   against the stored artist's display_name. Mismatches (e.g. URL slug
   'nguyen-trung-phan' mapped onto Nguyễn Trung) are unmapped — artist_id
   becomes NULL until a real mapping is established.
"""
import sys
import time
import random
import argparse
import re
import unicodedata
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from parser_v3 import parse_lot_page

ENV = {}
for line in Path('.env.local').read_text().splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        ENV[k] = v
URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}
HR = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'

# FX → USD (approximate; refresh periodically — these are for pricing convert,
# not currency markets, so small drift OK).
FX = {
    'USD': 1.0, 'HKD': 0.128, 'GBP': 1.27, 'EUR': 1.08, 'MYR': 0.22,
    'SGD': 0.74, 'TWD': 0.032, 'CHF': 1.10, 'AUD': 0.66, 'CAD': 0.73,
    'JPY': 0.0064, 'CNY': 0.139, 'KRW': 0.00072,
}


def normalize_name(s):
    """Diacritic-strip, lowercase, alpha-only word tokens for fuzzy match."""
    if not s:
        return set()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = s.replace('Đ', 'D').replace('đ', 'd').lower()
    s = re.sub(r'[^a-z\s]', ' ', s)
    return {w for w in s.split() if len(w) > 1}


def load_artist_lookup():
    """Map artist_id → set of normalized name tokens (for validation)."""
    artists = []
    fr = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/artists?select=id,name,display_name',
            headers={**HR, 'Range': f'{fr}-{fr+999}'},
            timeout=30,
        ).json()
        if not isinstance(r, list) or not r:
            break
        artists.extend(r)
        fr += 1000
        if len(r) < 1000:
            break
    return {a['id']: normalize_name(a.get('display_name') or a['name']) for a in artists}


_SLUG_STOP = {
    'vietnamese','vietnam','b','born','ne','en','xxe','siecle','xx','xxie',
    'signed','attributed','attr','attribue','to','of','a','an','the','painting',
    'school','ecole','village','self','abstract','untitled','still','life',
    'portrait','nu','nude','flowers','landscape','mother','child','lady',
    'ladies','lacquer','oil','ink','watercolour','watercolor','pastel','mixed',
    'media','panel','canvas','silk','paper','wood','gouache','acrylic',
    'attribuee','attribues','d','dapres','apres','french','american',
    'after','circle','manner','follower','workshop',
}


def slug_artist_tokens(url):
    """Extract artist-name tokens from /auction-lot/{slug} prefix.
    Stops at first STOP token, digit, or single-char token."""
    if not url:
        return None
    m = re.search(r'/auction-lot/([a-z0-9\-]+)', url)
    if not m:
        return None
    slug = m.group(1)
    slug = re.sub(r'-c-[a-z0-9]{8,12}$', '', slug)  # trailing hash
    slug = re.sub(r'-\d{1,4}$', '', slug)         # trailing lot number
    tokens = []
    for t in slug.split('-'):
        if not t or t in _SLUG_STOP or t.isdigit() or len(t) < 2:
            break
        tokens.append(t)
    return set(tokens) if len(tokens) >= 2 else None


def validate_artist(parsed_h1_name, source_url, mapped_tokens):
    """Decide whether to keep stored artist_id or unmap.

    Conservative rule: only flag MISMATCH when slug_tokens (highly reliable)
    contains at least one extra/different token vs mapped, AND that token
    isn't a common modifier. URL slug is canonical — Invaluable always
    prefixes the slug with the full artist name.

    H1 parsing is fallible (descriptions like 'Abstract by Nguyen Gia Tri'
    can confuse the regex), so we never unmap based on H1 alone.
    """
    if not mapped_tokens:
        return True, None
    slug_tokens = slug_artist_tokens(source_url)
    if not slug_tokens:
        return True, None  # can't validate → trust stored mapping
    # Exact match — definitely the same artist
    if slug_tokens == mapped_tokens:
        return True, None
    # Stored is a subset of slug → slug has extra real name tokens →
    # different artist (e.g. slug 'nguyen trung phan' ⊃ stored 'nguyen trung')
    if mapped_tokens < slug_tokens:
        return False, slug_tokens
    # Slug is a subset of stored — slug is incomplete (some artists have
    # short slugs); trust stored.
    if slug_tokens < mapped_tokens:
        return True, None
    # Token sets disagree on at least one name token → mismatch
    return False, slug_tokens


def build_payload(data, artist_lookup, current_artist_id):
    """Build PATCH payload from parser output."""
    p = {}
    if data.get('artwork_title') and len(data['artwork_title']) > 1:
        p['artwork_title'] = data['artwork_title'][:300]
    if data.get('year'):
        p['year'] = data['year']
    if data.get('medium'):
        p['medium'] = data['medium'][:200]
    if data.get('provenance'):
        p['provenance'] = data['provenance'][:1000]
    if data.get('width_cm') and data.get('height_cm'):
        p['width_cm'] = data['width_cm']
        p['height_cm'] = data['height_cm']
        p['area_m2'] = data['area_m2']
        p['dimensions'] = data['dimensions'][:100]
    if data.get('auction_house'):
        house = data['auction_house'][:50]
        p['sale_location'] = house
        p['auction_title'] = f'{house} via Invaluable'[:200]

    # === Currency + price (the v5 fix) ===
    if data.get('estimate_low') and data.get('estimate_high'):
        cur = data.get('estimate_currency', 'USD')
        p['estimate_low'] = data['estimate_low']
        p['estimate_high'] = data['estimate_high']
        p['currency'] = cur
        fx = FX.get(cur, 1.0)
        if data.get('hammer_price'):
            ham_cur = data.get('hammer_currency', cur)
            ham_fx = FX.get(ham_cur, fx)
            p['hammer_price'] = data['hammer_price']
            p['price_usd'] = round(data['hammer_price'] * ham_fx, 2)
        else:
            mid = (data['estimate_low'] + data['estimate_high']) / 2
            p['price_usd'] = round(mid * fx, 2)
            p['hammer_price'] = None  # we don't actually know

    # === Artist validation (the v5 fix) ===
    if current_artist_id:
        mapped = artist_lookup.get(current_artist_id, set())
        ok, suggested = validate_artist(
            data.get("artist_from_h1") or data.get("artist") or "",
            data.get("url", ""),
            mapped,
        )
        if not ok:
            new_id = None
            for aid, tokens in artist_lookup.items():
                if tokens and tokens == suggested:
                    new_id = aid
                    break
            p["artist_id"] = new_id
            p["_artist_mismatch"] = (" ".join(sorted(suggested)), new_id)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--offset', type=int, default=0)
    ap.add_argument('--delay', type=float, default=5.0)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--source-url')
    args = ap.parse_args()

    artist_lookup = load_artist_lookup()
    print(f'Loaded {len(artist_lookup)} artists for validation', flush=True)

    if args.source_url:
        r = requests.get(
            f'{URL}/rest/v1/sale_results?source_url=eq.{requests.utils.quote(args.source_url, safe="")}&select=id,artist_id,source_url',
            headers=HR, timeout=15,
        ).json()
        lots = r if r else [{'source_url': args.source_url, 'id': None, 'artist_id': None}]
    else:
        lots = []
        fr = 0
        while True:
            r = requests.get(
                f'{URL}/rest/v1/sale_results?select=id,source_url,artist_id&source=eq.invaluable&order=id.asc',
                headers={**HR, 'Range': f'{fr}-{fr+999}'},
                timeout=30,
            ).json()
            if not isinstance(r, list) or not r:
                break
            lots.extend(r)
            fr += 1000
            if len(r) < 1000:
                break
        if args.offset:
            lots = lots[args.offset:]
        if args.limit:
            lots = lots[: args.limit]
    print(f'Lots to process: {len(lots)}', flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )

        done = 0
        updated = 0
        empty = 0
        unmapped = 0
        errs = 0
        for i, lot in enumerate(lots, 1):
            data = None
            for attempt in (1, 2):
                ctx = browser.new_context(user_agent=UA, viewport={'width': 1920, 'height': 1080})
                page = ctx.new_page()
                try:
                    data = parse_lot_page(page, lot['source_url'])
                    if data.get('artwork_title') or data.get('auction_house') or data.get('medium'):
                        ctx.close()
                        break
                    ctx.close()
                    if attempt == 1:
                        time.sleep(10)
                except Exception as e:
                    errs += 1
                    ctx.close()
                    if errs <= 5:
                        print(f'  [{i}] ERR {type(e).__name__}: {e}', flush=True)
                    if errs > 20:
                        print(f'  Too many errors, stopping at {i}', flush=True)
                        browser.close()
                        return
                    time.sleep(8)
                    data = None
                    break
            done += 1

            payload = build_payload(data, artist_lookup, lot.get('artist_id')) if data else {}
            mismatch_info = payload.pop('_artist_mismatch', None)
            if mismatch_info:
                parsed_name, new_id = mismatch_info
                unmapped += 1
                if unmapped <= 20:
                    print(f'  [{i}] ARTIST MISMATCH lot {lot["id"]}: parsed={parsed_name!r} '
                          f'old_id={lot.get("artist_id")} new_id={new_id}', flush=True)

            if not payload:
                empty += 1
            elif args.dry_run:
                print(f'  [{i}] {lot["source_url"][-50:]}')
                for k, v in payload.items():
                    print(f'      {k}: {v!r}')
            elif lot.get('id'):
                r = requests.patch(
                    f'{URL}/rest/v1/sale_results?id=eq.{lot["id"]}',
                    headers=H, json=payload, timeout=15,
                )
                if r.status_code in (200, 204):
                    updated += 1

            if i % 25 == 0:
                print(f'  [{i}/{len(lots)}] done={done} updated={updated} unmapped={unmapped} empty={empty} errs={errs}', flush=True)

            time.sleep(args.delay + random.random() * 2)

        browser.close()
        print(f'\nFinal: done={done} updated={updated} unmapped={unmapped} empty={empty} errs={errs}', flush=True)


if __name__ == '__main__':
    main()
