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

# Aggregate by artist_id — only lots with a realised price count.
# Operator rule 2026-06-26: passed / withdrawn / estimate_only lots
# don't belong in Artonis at all (we don't track 'lot existed but
# didn't sell' — only completed sales with hammer prices), so any
# unpriced row should already have been filtered before sync.  If
# one leaks through, ignore it here too.
agg = {}
for s in all_sales:
    aid = s.get('artist_id')
    if not aid: continue
    p = s.get('price_with_premium_usd') or s.get('price_usd')
    if p is None or p <= 0: continue
    if aid not in agg:
        agg[aid] = {'min': p, 'max': p, 'sum': 0, 'count': 0}
    a = agg[aid]
    a['min'] = min(a['min'], p)
    a['max'] = max(a['max'], p)
    a['sum'] += p
    a['count'] += 1

# Update each artist
print(f"\nUpdating {len(agg)} artists with new aggregates...")
ok = 0
for aid, a in agg.items():
    avg = a['sum'] / a['count'] if a['count'] else None
    body = {
        'auction_count': a['count'],
        'overall_min_usd': round(a['min'], 2),
        'overall_max_usd': round(a['max'], 2),
        'overall_avg_usd': round(avg, 2) if avg else None,
    }
    r = requests.patch(f"{URL}/rest/v1/artists?id=eq.{aid}", headers={**H, 'Prefer': 'return=minimal'},
                       json=body, timeout=30)
    if r.ok: ok += 1
    else: print(f"  ERR #{aid}: {r.status_code}")
print(f"\nDone. {ok}/{len(agg)} artists updated.")

# Reset orphans — artists whose stored auction_count > 0 but who no
# longer have any priced lots.  Without this, an artist whose lots
# were deleted (e.g. estimate_only cleanup 2026-06-26) keeps a stale
# auction_count and stays visible on /artists with no real data.
print("\nLooking for orphaned counts to clear…")
all_artists = requests.get(
    f"{URL}/rest/v1/artists?select=id,auction_count&limit=500",
    headers={'apikey': KEY}, timeout=30,
).json()
zeroed = 0
for a in all_artists:
    if (a.get('auction_count') or 0) > 0 and a['id'] not in agg:
        r = requests.patch(
            f"{URL}/rest/v1/artists?id=eq.{a['id']}",
            headers={**H, 'Prefer': 'return=minimal'},
            json={'auction_count': 0,
                  'overall_min_usd': None, 'overall_max_usd': None,
                  'overall_avg_usd': None},
            timeout=30,
        )
        if r.ok: zeroed += 1
print(f"Cleared {zeroed} orphans.")
