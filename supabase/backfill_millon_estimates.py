"""Regex backfill for Millon estimates the legacy parser missed.

Catalog format is consistent:
  Estimation: 300 €
              -
              500 €
Whitespace + newlines between low, '-', high.  The original detail
parser missed many because its regex required them on one line.
"""
import re, html, requests, sys
from pathlib import Path

ENV={}
for line in (Path(__file__).resolve().parent.parent/'.env.local').read_text().splitlines():
    line=line.strip()
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1); ENV[k]=v
URL=ENV['SUPABASE_URL']; KEY=ENV['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
HR={'apikey':KEY,'Authorization':f'Bearer {KEY}'}

NUM = r"(\d+(?:[\s\xa0 ]\d{3})*)"
EST_RE = re.compile(
    rf"Estimation\s*:\s*{NUM}\s*[€][\s\S]*?-[\s\S]*?{NUM}\s*[€]",
    re.IGNORECASE,
)

def _n(s):
    return int(re.sub(r"[\s\xa0 ]", "", s))

def backfill(limit=2000, verbose=False):
    lots = requests.get(
        f"{URL}/rest/v1/sale_results?source=eq.millon"
        f"&catalog_description=not.is.null&estimate_low=is.null"
        f"&select=id,catalog_description,currency&limit={limit}",
        headers=HR
    ).json()
    fixed = 0
    for lot in lots:
        m = EST_RE.search(html.unescape(lot['catalog_description']))
        if not m: continue
        try:
            low, high = _n(m.group(1)), _n(m.group(2))
        except (ValueError, AttributeError):
            continue
        if not (10 <= low <= 1e7 and low <= high <= 1e7): continue
        payload = {'estimate_low': low, 'estimate_high': high}
        if not lot.get('currency'): payload['currency'] = 'EUR'
        r = requests.patch(
            f"{URL}/rest/v1/sale_results?id=eq.{lot['id']}",
            headers=H, json=payload
        )
        if r.status_code in (200,204):
            fixed += 1
            if verbose: print(f"  ✓ {lot['id']}: {low}-{high} €")
    if verbose:
        print(f"\nFixed: {fixed}/{len(lots)}")
    return fixed

if __name__ == '__main__':
    backfill(verbose=True)
