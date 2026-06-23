"""Backfill Bonhams auction_title from the auction page og:title.

The Bonhams Typesense API the lot crawler uses only carries
auctionId, department.name, and brand — not the actual sale name.
The original crawler stamped 'Bonhams [Brand] — {department}' which
reads as 'Bonhams / Cornette — Southeast Asian Modern & Contemporary
Art' across every auction in that department.  The real sale names
('Vietnamese Art Online', 'Southeast Asian Art Online', etc.) are
only on the auction page's og:title.

This script fetches /auction/{id}/ once per unique auctionId in the
DB, parses the 'Bonhams [Brand] : SALE_NAME' pattern, and writes
SALE_NAME to every lot in that auction.  98 auctions touched, 646
lots updated.
"""
import re, html, time, requests
from collections import defaultdict
from pathlib import Path

ENV={}
for line in (Path(__file__).resolve().parent.parent/'.env.local').read_text().splitlines():
    line=line.strip()
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1); ENV[k]=v
URL=ENV['SUPABASE_URL']; KEY=ENV['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}
HR={'apikey':KEY,'Authorization':f'Bearer {KEY}'}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"}

OG_RE = re.compile(r'<meta property="og:title" content="([^"]+)"', re.IGNORECASE)
TITLE_PAT = re.compile(r"^Bonhams(?:\s+[A-Za-zÀ-ſ][\w\s\.\-']+?)?\s*:\s*(.+?)$")

def backfill(verbose=False):
    lots = requests.get(
        f"{URL}/rest/v1/sale_results?source=eq.bonhams&select=id,source_url",
        headers={**HR,'Range':'0-1999'}
    ).json()
    by_auc = defaultdict(list)
    for lot in lots:
        m = re.search(r'/auction/(\d+)/', lot.get('source_url') or '')
        if m: by_auc[m.group(1)].append(lot['id'])
    fixed_aucs = fixed_lots = 0
    for aid, ids in by_auc.items():
        try:
            r = requests.get(f"https://www.bonhams.com/auction/{aid}/", headers=UA, timeout=15)
        except Exception:
            continue
        m = OG_RE.search(r.text)
        if not m: continue
        sm = TITLE_PAT.match(html.unescape(m.group(1)))
        if not sm: continue
        name = sm.group(1).strip()
        if not (5 < len(name) < 150): continue
        rv = requests.patch(
            f"{URL}/rest/v1/sale_results?id=in.({','.join(map(str, ids))})",
            headers=H, json={'auction_title': name}
        )
        if rv.status_code in (200, 204):
            fixed_aucs += 1; fixed_lots += len(ids)
            if verbose: print(f"  {aid}: {name!r} → {len(ids)} lots")
        time.sleep(0.3)
    if verbose: print(f"\nUpdated: {fixed_aucs} auctions, {fixed_lots} lots")
    return fixed_lots

if __name__ == '__main__':
    backfill(verbose=True)
