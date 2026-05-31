"""One-shot migration: SQLite (artonis_price_mvp.sqlite) → Supabase Postgres.

Order matters — parents before children (FK dependencies):
  artists → exhibitions → source_files → exhibition_artists
         → price_observations → sale_results
         → upcoming_auctions, crawl_runs, imports (no FK)

For each table:
  1. Read all rows from SQLite
  2. Strip SQLite primary keys + remap FKs to new Postgres IDs
  3. Batch-insert via PostgREST (service_role bypasses RLS)
  4. Build sqlite_id → postgres_id map for children

Run from repo root:
  python3 supabase/migrate_data.py
"""
import sqlite3
import requests
import os
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "artonis_price_mvp.sqlite"
ENV_PATH = Path(__file__).parent.parent / ".env.local"

# Load env
ENV = {}
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            ENV[k] = v
SUPABASE_URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
HEADERS = {
    'apikey': KEY,
    'Authorization': f'Bearer {KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=representation',
}

# SQLite source
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

BATCH = 200


def post_batch(table, rows):
    """Insert rows, return server response (with new IDs). Handles batches."""
    if not rows:
        return []
    results = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i+BATCH]
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        r = requests.post(url, headers=HEADERS, json=chunk, timeout=60)
        if not r.ok:
            print(f"  ERR {table} batch {i}: HTTP {r.status_code}")
            print(f"  body: {r.text[:500]}")
            print(f"  sample row: {chunk[0]}")
            sys.exit(1)
        results.extend(r.json())
        print(f"  {table} {i + len(chunk)}/{len(rows)} ok", flush=True)
    return results


def clean(row, drop_keys=('id', 'created_at', 'updated_at')):
    """Drop SQLite-internal columns (auto-generated in Postgres). Convert bool-likes."""
    out = {}
    for k in row.keys():
        if k in drop_keys:
            continue
        v = row[k]
        # SQLite integer 0/1 → Postgres boolean where applicable
        out[k] = v
    return out


def trim_to_date(v):
    """SQLite stores dates as TEXT (YYYY-MM-DD or empty). Postgres expects date or null."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Take first 10 chars if YYYY-MM-DD
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return None  # malformed date → null


def trim_to_timestamptz(v):
    if not v:
        return None
    s = str(v).strip()
    return s if s else None


# ============ 1. ARTISTS ============
print("\n[1/9] artists")
sqlite_artists = list(conn.execute("SELECT * FROM artists ORDER BY id"))
artist_rows = []
for r in sqlite_artists:
    d = clean(r)
    d['updated_at'] = trim_to_timestamptz(d.get('updated_at'))
    artist_rows.append(d)
inserted = post_batch('artists', artist_rows)
artist_map = {old.id_: new['id'] for old, new in zip(
    [type('R', (), {'id_': r['id']})() for r in sqlite_artists], inserted)}
artist_map = dict(zip([r['id'] for r in sqlite_artists], [i['id'] for i in inserted]))
print(f"  → {len(inserted)} artists migrated; sample map {list(artist_map.items())[:3]}")


# ============ 2. EXHIBITIONS ============
print("\n[2/9] exhibitions")
sqlite_exhibitions = list(conn.execute("SELECT * FROM exhibitions ORDER BY id"))
exh_rows = []
for r in sqlite_exhibitions:
    d = clean(r)
    d['start_date'] = trim_to_date(d.get('start_date'))
    d['updated_at'] = trim_to_timestamptz(d.get('updated_at'))
    # JSON columns may be stored as text in SQLite — keep as-is, Postgres jsonb accepts
    exh_rows.append(d)
inserted = post_batch('exhibitions', exh_rows)
exh_map = dict(zip([r['id'] for r in sqlite_exhibitions], [i['id'] for i in inserted]))
print(f"  → {len(inserted)} exhibitions")


# ============ 3. SOURCE FILES ============
print("\n[3/9] source_files")
sqlite_sf = list(conn.execute("SELECT * FROM source_files ORDER BY id"))
sf_rows = []
for r in sqlite_sf:
    d = clean(r)
    d['exhibition_id'] = exh_map.get(d.get('exhibition_id'))
    d['has_price_hint'] = bool(d.get('has_price_hint'))
    d['has_catalogue_hint'] = bool(d.get('has_catalogue_hint'))
    d['imported_at'] = trim_to_timestamptz(d.get('imported_at'))
    sf_rows.append(d)
inserted = post_batch('source_files', sf_rows)
sf_map = dict(zip([r['id'] for r in sqlite_sf], [i['id'] for i in inserted]))
print(f"  → {len(inserted)} source_files")


# ============ 4. EXHIBITION_ARTISTS junction ============
print("\n[4/9] exhibition_artists")
sqlite_ea = list(conn.execute("SELECT * FROM exhibition_artists"))
ea_rows = []
for r in sqlite_ea:
    eid = exh_map.get(r['exhibition_id'])
    aid = artist_map.get(r['artist_id'])
    if eid and aid:
        ea_rows.append({'exhibition_id': eid, 'artist_id': aid})
inserted = post_batch('exhibition_artists', ea_rows)
print(f"  → {len(inserted)} junction rows")


# ============ 5. PRICE_OBSERVATIONS ============
print("\n[5/9] price_observations")
sqlite_po = list(conn.execute("SELECT * FROM price_observations ORDER BY id"))
po_rows = []
for r in sqlite_po:
    d = clean(r)
    d['artist_id'] = artist_map.get(d.get('artist_id'))
    d['exhibition_id'] = exh_map.get(d.get('exhibition_id'))
    d['source_file_id'] = sf_map.get(d.get('source_file_id'))
    d['observed_at'] = trim_to_timestamptz(d.get('observed_at'))
    po_rows.append(d)
inserted = post_batch('price_observations', po_rows)
print(f"  → {len(inserted)} observations")


# ============ 6. SALE_RESULTS (core) ============
print("\n[6/9] sale_results")
sqlite_sr = list(conn.execute("SELECT * FROM sale_results ORDER BY id"))
sr_rows = []
for r in sqlite_sr:
    d = clean(r, drop_keys=('id', 'created_at', 'updated_at'))
    d['artist_id'] = artist_map.get(d.get('artist_id'))
    d['sale_date'] = trim_to_date(d.get('sale_date'))
    d['scraped_at'] = trim_to_timestamptz(d.get('scraped_at'))
    # Ensure status, kind, support_type fit enum values (Postgres rejects unknown)
    status = d.get('status') or 'sold'
    if status not in ('sold', 'passed', 'withdrawn', 'upcoming', 'unknown', 'estimate', 'estimate_only'):
        status = 'unknown'
    d['status'] = status
    kind = d.get('kind') or 'painting'
    if kind not in ('painting', 'sculpture', 'print', 'drawing', 'medal'):
        kind = 'painting'
    d['kind'] = kind
    sup = d.get('support_type')
    if sup and sup not in ('canvas', 'silk', 'paper', 'lacquer', 'panel', 'metal'):
        sup = None
    d['support_type'] = sup
    sr_rows.append(d)
inserted = post_batch('sale_results', sr_rows)
print(f"  → {len(inserted)} sale_results")


# ============ 7. UPCOMING_AUCTIONS ============
print("\n[7/9] upcoming_auctions")
sqlite_ua = list(conn.execute("SELECT * FROM upcoming_auctions ORDER BY id"))
ua_rows = []
for r in sqlite_ua:
    d = clean(r)
    d['sale_date'] = trim_to_date(d.get('sale_date'))
    d['scraped_at'] = trim_to_timestamptz(d.get('scraped_at'))
    ua_rows.append(d)
inserted = post_batch('upcoming_auctions', ua_rows)
print(f"  → {len(inserted)} upcoming")


# ============ 8. CRAWL_RUNS ============
print("\n[8/9] crawl_runs")
sqlite_cr = list(conn.execute("SELECT * FROM crawl_runs ORDER BY id"))
cr_rows = []
for r in sqlite_cr:
    d = clean(r)
    d['started_at'] = trim_to_timestamptz(d.get('started_at'))
    d['finished_at'] = trim_to_timestamptz(d.get('finished_at'))
    d['sale_date_min'] = trim_to_date(d.get('sale_date_min'))
    d['sale_date_max'] = trim_to_date(d.get('sale_date_max'))
    cr_rows.append(d)
inserted = post_batch('crawl_runs', cr_rows)
print(f"  → {len(inserted)} crawl runs")


# ============ 9. IMPORTS ============
print("\n[9/9] imports")
sqlite_imp = list(conn.execute("SELECT * FROM imports ORDER BY id"))
imp_rows = []
for r in sqlite_imp:
    d = clean(r, drop_keys=('id',))
    d['created_at'] = trim_to_timestamptz(d.get('created_at'))
    imp_rows.append(d)
inserted = post_batch('imports', imp_rows)
print(f"  → {len(inserted)} imports")

print("\nDONE — all tables migrated.")
conn.close()
