"""Production Invaluable detail crawler v4.

Uses parser_v3 with:
- Fresh BrowserContext per lot (anti-bot resilience).
- Retry-on-empty (page rendered as 'www.invaluable.com' h1 → reload).
- Overwrite-with-parsed: parser v3 yields clean fields; ALWAYS overwrite any
  prior garbage in DB when a new value is extracted (the prior values from
  card-text scraping had artist+dims+medium mashed into artwork_title etc.).
- Skips lots where parser returned empty (browser couldn't render).
"""
import sys
import time
import random
import argparse
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


def build_payload(data):
    """Build PATCH payload from parser output. Overwrite-with-parsed strategy:
    if parser yields a value, we trust it more than whatever was in DB."""
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
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--offset', type=int, default=0)
    ap.add_argument('--delay', type=float, default=5.0)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--source-url')
    ap.add_argument('--retry-on-empty', action='store_true', default=True)
    args = ap.parse_args()

    if args.source_url:
        lots = [{'source_url': args.source_url, 'id': None}]
    else:
        lots = []
        fr = 0
        while True:
            r = requests.get(
                f'{URL}/rest/v1/sale_results?select=id,source_url&source=eq.invaluable&order=id.asc',
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
        errs = 0
        for i, lot in enumerate(lots, 1):
            data = None
            for attempt in (1, 2):
                ctx = browser.new_context(user_agent=UA, viewport={'width': 1920, 'height': 1080})
                page = ctx.new_page()
                try:
                    data = parse_lot_page(page, lot['source_url'])
                    # Did the page actually render? parser_v3 returns near-empty dict
                    # (just 'url') when the body was too short to be a real lot page.
                    if data.get('artwork_title') or data.get('auction_house') or data.get('medium'):
                        ctx.close()
                        break
                    # Empty — retry once with longer wait
                    ctx.close()
                    if attempt == 1 and args.retry_on_empty:
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

            payload = build_payload(data) if data else {}

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

            if i % 10 == 0:
                print(f'  [{i}/{len(lots)}] done={done} updated={updated} empty={empty} errs={errs}', flush=True)

            time.sleep(args.delay + random.random() * 2)

        browser.close()
        print(f'\nFinal: done={done} updated={updated} empty={empty} errs={errs}', flush=True)


if __name__ == '__main__':
    main()
