"""Invaluable auction_title + sale_date sweep — periodic Playwright batch.

Wired to .github/workflows/invaluable_sweep.yml — runs every 6 hours,
processes BATCH_SIZE lots per invocation.  Cloudflare aggressively
blocks rapid bursts in the same session, so the script:

  - Restarts the browser every 25 lots (memory + fingerprint reset).
  - Creates a fresh context per request (Cloudflare cookie reset).
  - Dismisses the CybotCookiebot consent dialog that intercepts the
    'Auction Details' click target.
  - Force-clicks via JS as a fallback when the consent dialog blocks
    the normal click path.

Extracts from the Auction Details accordion:
  - sale_name → auction_title (replaces 'X via Invaluable' fallback)
  - sale_date → ISO YYYY-MM-DD (replaces card-derived date which
    sometimes captured a near-future estimate date instead of the
    actual past hammer date).

CLI:
  python3 supabase/sweep_invaluable_titles.py [--batch N] [--max M]
"""
import argparse
import gc
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

ENV = {}
for line in Path('.env.local').read_text().splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        ENV[k] = v

URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
HR = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}

from playwright.sync_api import sync_playwright


SKIP = {'bid on-the-go!', 'explore this auction', 'request more information',
        'auction details', 'terms', 'similar items', 'view auction'}
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/'
      '605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15')
MONTHS = {m: i for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], start=1)}


def _parse_iso_date(text):
    """Parse 'June 25, 2016, 02:30 PM CET' → '2016-06-25'."""
    m = re.search(r'\b([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\b', text)
    if not m:
        return None
    mon_full, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    mon = MONTHS.get(mon_full[:3])
    if not mon:
        return None
    if not (1900 < year < 2100) or not (1 <= day <= 31):
        return None
    return f'{year:04d}-{mon:02d}-{day:02d}'


def process_one(page, url):
    """Fetch one lot, return (sale_name, sale_date) — either may be None."""
    page.goto(url, timeout=25000, wait_until='domcontentloaded')
    page.wait_for_timeout(1200)
    page.evaluate("""() => {
        const d = document.getElementById('CybotCookiebotDialog');
        if (d) d.remove();
        document.querySelectorAll('.carousel-popup-view').forEach(e => e.remove());
    }""")
    try:
        loc = page.locator('text=/^Auction Details$/').first
        loc.scroll_into_view_if_needed(timeout=2000)
        try:
            loc.click(timeout=2000)
        except Exception:
            loc.click(force=True, timeout=2000)
        page.wait_for_timeout(700)
    except Exception:
        pass
    body = page.inner_text('body')
    if 'Just a moment' in body[:500] or len(body) < 500:
        return None, None
    i = body.find('Auction Details')
    if i < 0:
        return None, None
    block = body[i:i + 500]
    m = re.match(r'Auction Details\n([^\n]{5,200})\n', block)
    sale_name = None
    if m:
        s = m.group(1).strip().rstrip(',. ')
        if s.lower() not in SKIP and 5 < len(s) < 200:
            sale_name = s
    sale_date = _parse_iso_date(block)
    return sale_name, sale_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch', type=int, default=25,
                    help='Restart browser every N lots')
    ap.add_argument('--max', type=int, default=50,
                    help='Max lots to process this invocation')
    args = ap.parse_args()

    # Target lots: prefer those with the bad 'via Invaluable' suffix
    # OR a suspicious sale_date in the future (>= today + 60 days).
    # The 'via Invaluable' pool drains first; once empty the cron just
    # exits cheaply.
    lots = requests.get(
        f"{URL}/rest/v1/sale_results?source=eq.invaluable"
        f"&auction_title=ilike.*via%20Invaluable*"
        f"&select=id,source_url&order=id",
        headers={**HR, 'Range': f'0-{args.max - 1}'}
    ).json()
    print(f"Lots: {len(lots)}", flush=True)
    if not lots:
        print("Nothing to sweep.", flush=True)
        return

    fixed = failed = 0
    i = 0
    while i < len(lots):
        end = min(i + args.batch, len(lots))
        with sync_playwright() as p:
            b = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled',
                      '--no-sandbox']
            )
            for j in range(i, end):
                lot = lots[j]
                ctx = b.new_context(user_agent=UA,
                                    viewport={'width': 1920, 'height': 1080})
                page = ctx.new_page()
                try:
                    sale, date = process_one(page, lot['source_url'])
                except Exception:
                    sale, date = None, None
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                payload = {}
                if sale:
                    payload['auction_title'] = sale[:200]
                if date:
                    payload['sale_date'] = date
                if payload:
                    rv = requests.patch(
                        f"{URL}/rest/v1/sale_results?id=eq.{lot['id']}",
                        headers=H, json=payload
                    )
                    if rv.status_code in (200, 204):
                        fixed += 1
                    else:
                        failed += 1
                else:
                    failed += 1
                time.sleep(0.3)
            try:
                b.close()
            except Exception:
                pass
        print(f"  [{end}/{len(lots)}] fixed={fixed} failed={failed}", flush=True)
        i = end
        gc.collect()
    print(f"\nFINAL: fixed={fixed}/{len(lots)} failed={failed}", flush=True)


if __name__ == '__main__':
    main()
