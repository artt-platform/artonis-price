"""Estimate end_date for exhibitions where it's null but start_date exists.

Heuristics:
- Museum (Bảo tàng): solo 30 days, group 60 days
- Hội Mỹ Thuật: typically 7-14 days (shorter)
- Gallery (rest): solo 21 days, group 35 days

These are ESTIMATES. User can override known dates manually.
"""
import requests
import re
from datetime import date, timedelta
from pathlib import Path

env = {}
for l in Path(__file__).parent.parent.joinpath('.env.local').read_text().splitlines():
    if '=' in l and not l.startswith('#'):
        k, v = l.split('=', 1); env[k] = v.strip()
URL = env['SUPABASE_URL']; KEY = env['SUPABASE_SERVICE_ROLE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}


def estimate_days(venue: str, is_group: bool) -> int:
    v = (venue or '').lower()
    if 'bảo tàng' in v or 'museum' in v:
        return 60 if is_group else 30
    if 'hội mỹ thuật' in v or 'hội hoạ' in v:
        return 14
    # Default gallery
    return 35 if is_group else 21


# Fetch nulls with start_date
rows = requests.get(
    f"{URL}/rest/v1/exhibitions?select=id,title,venue,start_date,end_date,artists_text&end_date=is.null&start_date=not.is.null",
    headers={'apikey': KEY}, timeout=30,
).json()

print(f'Processing {len(rows)} exhibitions...\n')
applied = 0
for r in rows:
    venue = r['venue'] or ''
    at = (r.get('artists_text') or '').strip()
    parts = [p for p in re.split(r'[,\n]', at) if p.strip()]
    is_group = len(parts) > 1
    days = estimate_days(venue, is_group)
    start = date.fromisoformat(r['start_date'])
    end = start + timedelta(days=days)
    body = {'end_date': end.isoformat()}
    resp = requests.patch(f"{URL}/rest/v1/exhibitions?id=eq.{r['id']}", headers=H, json=body, timeout=30)
    if resp.ok:
        applied += 1
        marker = '👥' if is_group else '👤'
        print(f"  ✓ #{r['id']:>3} {marker} {start} +{days}d → {end}  | {(r['title'] or '')[:35]} @ {venue[:25]}")
    else:
        print(f"  ✗ #{r['id']}: {resp.status_code}")

print(f'\nDONE. Applied {applied}/{len(rows)} estimates.')
