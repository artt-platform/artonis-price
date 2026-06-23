"""Le Auction: extract 'Nguồn gốc: X' from raw_snapshot → provenance.

Background:
- BidSpirit hosts Le Auction lots at /ui/lotPage/ URLs that are JS-
  rendered SPA shells.  Plain HTTP fetch returns boilerplate ("Discover
  and bid on fine art..."), not the lot details.
- The crawler (le_auction.py) gets lot detail from the BidSpirit API
  during the active Playwright session and stores it truncated in the
  raw_snapshot column.
- Original parser left provenance="" for all Le Auction lots; this
  script extracts the 'Nguồn gốc: X' phrase that's consistently
  present in the VN description payload.

Also replaces the garbage catalog_description rows (BidSpirit shell
HTML) the generic backfill script left behind with the actual VN
content, so future LLM extract passes have something useful to work on.
"""
import re, html, requests
from pathlib import Path

ENV={}
for line in (Path(__file__).resolve().parent.parent/'.env.local').read_text().splitlines():
    line=line.strip()
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1); ENV[k]=v
URL=ENV['SUPABASE_URL']; KEY=ENV['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
HR={'apikey':KEY,'Authorization':f'Bearer {KEY}'}

PROV_RE = re.compile(r"Nguồn\s+gốc\s*:\s*([^<\n]{2,200})", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
BOILER = ("BidSpirit", "Discover and bid")

def backfill(verbose=False):
    lots = requests.get(
        f"{URL}/rest/v1/sale_results?source=eq.le_auction"
        f"&raw_snapshot=not.is.null"
        f"&select=id,raw_snapshot,catalog_description,provenance",
        headers={**HR,'Range':'0-999'}
    ).json()
    prov_fixed = desc_set = 0
    for lot in lots:
        raw = html.unescape(lot['raw_snapshot'] or '')
        plain = re.sub(r'\s+', ' ', TAG_RE.sub(' ', raw).replace('&nbsp;', ' ')).strip()
        payload = {}
        if not lot.get('provenance'):
            m = PROV_RE.search(plain)
            if m:
                p = m.group(1).strip().rstrip('.,;:')
                if 2 <= len(p) <= 200:
                    payload['provenance'] = p
                    prov_fixed += 1
        cur = lot.get('catalog_description') or ''
        if any(b in cur for b in BOILER) and len(plain) > 20:
            payload['catalog_description'] = plain[:2000]
            desc_set += 1
        if payload:
            requests.patch(
                f"{URL}/rest/v1/sale_results?id=eq.{lot['id']}",
                headers=H, json=payload
            )
    if verbose:
        print(f"prov: {prov_fixed}, desc replaced: {desc_set}")
    return prov_fixed

if __name__ == '__main__':
    backfill(verbose=True)
