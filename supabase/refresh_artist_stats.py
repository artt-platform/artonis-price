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
        f"{URL}/rest/v1/sale_results?select=artist_id,price_usd,price_with_premium_usd,kind,status&order=id&limit=1000&offset={from_idx}",
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
    # Operator rule 2026-07-01: aggregate ONLY sold lots.  Withdrawn /
    # passed / estimate_only rows should already be excluded upstream
    # but some (e.g. Alix Aymé Bonhams withdrawn 'Encre sur papier'
    # rows with price_usd $108–$486 populated) leaked through and were
    # dragging her painting-range floor to $136.
    if s.get('status') != 'sold':
        continue
    # Operator rule 2026-06-29: artist range ONLY counts paintings.
    # Drawings (Crayon sur papier 30×24), prints (lithograph editions),
    # sculptures (bronze busts), and medals are distinct market
    # segments with their own price tiers — mixing them under the
    # single "Giá đấu giá" range gives nonsense ranges like
    # $816–$1.01M for Lê Thị Lựu where the $816 floor is a single
    # 30×24 cm pencil sketch and the $1.01M ceiling is a gouache-on-
    # silk masterwork.  classify_kind assigns kind on every row
    # (default 'painting'); skip anything else from the aggregate.
    if s.get('kind') and s.get('kind') != 'painting':
        continue
    p = s.get('price_with_premium_usd') or s.get('price_usd')
    if p is None or p <= 0: continue
    if aid not in agg:
        agg[aid] = {'min': p, 'max': p, 'sum': 0, 'count': 0, 'prices': []}
    a = agg[aid]
    a['min'] = min(a['min'], p)
    a['max'] = max(a['max'], p)
    a['sum'] += p
    a['count'] += 1
    a['prices'].append(p)

# Update each artist
print(f"\nUpdating {len(agg)} artists with new aggregates...")
ok = 0
for aid, a in agg.items():
    avg = a['sum'] / a['count'] if a['count'] else None
    # Q1 / median / Q3 — typical-range display for /artists list.
    # Operator 2026-07-01: min-max spans extremes (Alix Aymé $612 to
    # $609K, Phạm Hậu $1K to $1.24M — the low end is a real one-off
    # sketch, the high end is a headline masterpiece).  Q1–Q3 shows
    # the price band 50 % of the artist's paintings actually clear at,
    # which is what operators actually want to see.
    prices = sorted(a['prices'])
    n = len(prices)
    def _pct(q):
        if n == 0: return None
        idx = (n - 1) * q
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return prices[lo] + (prices[hi] - prices[lo]) * (idx - lo)
    q1 = _pct(0.25)
    median = _pct(0.50)
    q3 = _pct(0.75)
    body = {
        'auction_count': a['count'],
        'overall_min_usd': round(a['min'], 2),
        'overall_max_usd': round(a['max'], 2),
        'overall_avg_usd': round(avg, 2) if avg else None,
        'overall_q1_usd': round(q1, 2) if q1 is not None else None,
        'overall_median_usd': round(median, 2) if median is not None else None,
        'overall_q3_usd': round(q3, 2) if q3 is not None else None,
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
