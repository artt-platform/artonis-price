"""Recompute artists.overall_min/max/avg_usd + auction_count from sale_results.

Run after crawl_and_sync.py to update cached aggregates."""
import requests
from pathlib import Path

ENV = {}
for l in Path(__file__).parent.parent.joinpath('.env.local').read_text().splitlines():
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); ENV[k] = v
URL = ENV['SUPABASE_URL']; KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}

# Use Supabase RPC via raw SQL through a stored procedure would be cleaner,
# but for now do client-side recompute. Get all artists + all sales, compute, batch-update.

print("Fetching all artists + sale_results...")
artists = requests.get(f"{URL}/rest/v1/artists?select=id&limit=300", headers={'apikey': KEY}, timeout=30).json()

# Fetch all sale_results with paging
all_sales = []
from_idx = 0
while True:
    r = requests.get(
        f"{URL}/rest/v1/sale_results?select=artist_id,price_usd,price_with_premium_usd&order=id&limit=1000&offset={from_idx}",
        headers={'apikey': KEY}, timeout=30
    )
    chunk = r.json()
    if not chunk: break
    all_sales.extend(chunk)
    if len(chunk) < 1000: break
    from_idx += 1000
print(f"  artists: {len(artists)}, sales: {len(all_sales)}")

# Aggregate by artist_id.  auction_count = every linked lot the
# artist appears in (including estimate_only / passed / withdrawn) —
# so newly catalogued artists whose only known lot is upcoming or
# unpriced still show on /artists.  Min/max/avg are computed from
# priced lots only.  Operator caught Vũ Đăng Bốn 2026-06-26 — he
# was hidden because the only G&D lot was 'no live bidding' /
# estimate_only and price_usd stayed null.
agg = {}
for s in all_sales:
    aid = s.get('artist_id')
    if not aid: continue
    if aid not in agg:
        agg[aid] = {'min': None, 'max': None, 'sum': 0, 'priced_count': 0, 'total_count': 0}
    a = agg[aid]
    a['total_count'] += 1
    p = s.get('price_with_premium_usd') or s.get('price_usd')
    if p is None or p <= 0: continue
    a['min'] = p if a['min'] is None else min(a['min'], p)
    a['max'] = p if a['max'] is None else max(a['max'], p)
    a['sum'] += p
    a['priced_count'] += 1

# Update each artist
print(f"\nUpdating {len(agg)} artists with new aggregates...")
ok = 0
for aid, a in agg.items():
    avg = a['sum'] / a['priced_count'] if a['priced_count'] else None
    body = {
        'auction_count': a['total_count'],
        'overall_min_usd': round(a['min'], 2) if a['min'] is not None else None,
        'overall_max_usd': round(a['max'], 2) if a['max'] is not None else None,
        'overall_avg_usd': round(avg, 2) if avg else None,
    }
    r = requests.patch(f"{URL}/rest/v1/artists?id=eq.{aid}", headers={**H, 'Prefer': 'return=minimal'},
                       json=body, timeout=30)
    if r.ok: ok += 1
    else: print(f"  ERR #{aid}: {r.status_code}")
print(f"\nDone. {ok}/{len(agg)} artists updated.")
