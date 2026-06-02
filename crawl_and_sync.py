"""Crawl all auction sources → SQLite, then sync delta to Supabase.

Flow:
  1. Open SQLite. Note baseline `max(id)` per source.
  2. Run each crawler with its existing seed URLs.
  3. Find rows where id > baseline (i.e. inserted this run).
  4. Upsert into Supabase via PostgREST.
       - Unique key: source_url (already unique constraint)
       - Use Prefer: resolution=ignore-duplicates so re-runs are idempotent.

Env:
  CRAWLERS=christies,sothebys   # comma-separated; default = all
  SKIP_SUPABASE=1               # crawl-only, no Supabase sync
  MAX_PAGES=50                  # per crawler

Run:
  python3 crawl_and_sync.py
"""
import os
import sys
import sqlite3
import time
import traceback
from datetime import datetime
from pathlib import Path
import requests

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))
DB_PATH = APP_ROOT / "data" / "artonis_price_mvp.sqlite"

# Load env
ENV = {}
env_path = APP_ROOT / ".env.local"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        ENV[k] = v
URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
H = {
    'apikey': KEY,
    'Authorization': f'Bearer {KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=merge-duplicates,return=minimal',
}

# Crawler config: (mod_key, module_path, db_source_name, entry_func_name)
ALL_CRAWLERS = [
    ('christies',      'crawlers.christies',      'christies',      'crawl'),
    ('sothebys',       'crawlers.sothebys',       'sothebys',       'crawl'),
    ('phillips',       'crawlers.phillips',       'phillips',       'crawl'),
    ('bonhams',        'crawlers.bonhams',        'bonhams',        'crawl_all'),
    ('aguttes',        'crawlers.aguttes',        'aguttes',        'crawl'),
    ('tajan',          'crawlers.tajan',          'tajan',          'crawl'),
    ('millon',         'crawlers.millon',         'millon',         'crawl_all'),
    ('millon_vn',      'crawlers.millon_vn',      'millon',         'crawl'),
    ('gros_delettrez', 'crawlers.gros_delettrez', 'gros-delettrez', 'crawl'),
    ('invaluable',     'crawlers.invaluable',     'invaluable',     'crawl_all'),
    ('global_auction', 'crawlers.global_auction', 'global-auction', 'crawl'),
    # Chọn — site offline since 2021, manual via Wayback only (excluded from automated runs):
    # ('chons',          'crawlers.chons',          'chons',          'crawl'),
]

WHICH = os.environ.get('CRAWLERS', '').strip()
if WHICH:
    keep = set(WHICH.split(','))
    crawlers = [c for c in ALL_CRAWLERS if c[0] in keep]
else:
    crawlers = ALL_CRAWLERS
print(f"WHICH={WHICH!r} → crawlers={[c[0] for c in crawlers]}")

MAX_PAGES = int(os.environ.get('MAX_PAGES', '100'))
SKIP_SUPABASE = bool(os.environ.get('SKIP_SUPABASE'))


def sync_to_supabase(conn, source, since_scraped_at):
    """Push rows from SQLite scraped/updated since since_scraped_at to Supabase."""
    rows = conn.execute("""
        SELECT s.*, a.name as _artist_name
          FROM sale_results s
          LEFT JOIN artists a ON a.id = s.artist_id
         WHERE s.source = ? AND s.scraped_at >= ?
         ORDER BY s.id
    """, (source, since_scraped_at)).fetchall()
    if not rows:
        return 0, 0

    # Fetch Supabase artist_id map by normalized_name (so FK resolves)
    name_to_id = {}
    artists_resp = requests.get(
        f"{URL}/rest/v1/artists?select=id,normalized_name",
        headers={'apikey': KEY}, timeout=30
    ).json()
    name_to_id = {a['normalized_name']: a['id'] for a in artists_resp}

    import re, unicodedata
    def normalize_key(value):
        t = unicodedata.normalize("NFD", value or "")
        t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
        t = t.replace("Đ", "D").replace("đ", "d")
        return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()

    payload = []
    for r in rows:
        d = dict(r)
        # Drop SQLite-only fields
        for k in ('id', 'created_at', 'updated_at', '_artist_name'):
            d.pop(k, None)
        # Re-resolve artist_id via normalized_name (SQLite IDs ≠ Supabase IDs)
        a_name = r['_artist_name']
        if a_name:
            sup_aid = name_to_id.get(normalize_key(a_name))
            d['artist_id'] = sup_aid
        else:
            d['artist_id'] = None
        # Validate enums
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
        payload.append(d)

    # Batch insert
    BATCH = 100
    total_pushed = 0
    for i in range(0, len(payload), BATCH):
        chunk = payload[i:i + BATCH]
        # Use on_conflict=source_url for upsert
        r = requests.post(
            f"{URL}/rest/v1/sale_results?on_conflict=source_url",
            headers=H, json=chunk, timeout=60,
        )
        if r.status_code in (201, 204, 200):
            total_pushed += len(chunk)
        else:
            print(f"  ✗ batch {i}: HTTP {r.status_code} {r.text[:300]}")
    return len(rows), total_pushed


def main():
    if not DB_PATH.exists():
        print(f"SQLite not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row

    print(f"Crawlers to run: {[c[0] for c in crawlers]}")
    print(f"max_pages={MAX_PAGES}  skip_supabase={SKIP_SUPABASE}\n")

    summary = []
    for mod_key, modpath, db_source, entry_fn in crawlers:
        print(f"\n{'='*60}\n▶ {mod_key} (DB source={db_source}, fn={entry_fn})\n{'='*60}")
        # Sentinel: current time before crawl. Rows with scraped_at >= this are new/updated
        sentinel = (datetime.utcnow().replace(microsecond=0)).isoformat()
        t0 = time.time()
        name = mod_key  # alias for summary
        try:
            mod = __import__(modpath, fromlist=[entry_fn])
            fn = getattr(mod, entry_fn)
            kwargs = {}
            try:
                import inspect
                sig = inspect.signature(fn)
                if 'max_pages' in sig.parameters:
                    kwargs['max_pages'] = MAX_PAGES
                if 'verbose' in sig.parameters:
                    kwargs['verbose'] = True
            except Exception:
                pass
            result = fn(conn, **kwargs)
            conn.commit()
            dur = time.time() - t0
            new_in_sqlite = conn.execute(
                "SELECT COUNT(*) FROM sale_results WHERE source = ? AND scraped_at >= ?",
                (db_source, sentinel)
            ).fetchone()[0]
            print(f"\n  → {mod_key} done in {dur:.0f}s. {new_in_sqlite} rows touched in SQLite (source={db_source}).")

            # Push to Supabase
            pushed_count = 0
            if not SKIP_SUPABASE and new_in_sqlite > 0:
                _, pushed_count = sync_to_supabase(conn, db_source, sentinel)
                print(f"  → Pushed {pushed_count}/{new_in_sqlite} to Supabase.")
            summary.append((name, new_in_sqlite, pushed_count, dur, None))
        except KeyboardInterrupt:
            print(f"\n  ✗ Interrupted")
            break
        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n  ✗ ERROR: {e}")
            print(tb[:1000])
            summary.append((name, 0, 0, time.time() - t0, str(e)[:200]))

    conn.close()

    # Summary
    print(f"\n\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"{'source':<18} {'new':>6} {'pushed':>8} {'time':>6}  status")
    for src, nlocal, pushed, dur, err in summary:
        print(f"{src:<18} {nlocal:>6} {pushed:>8} {dur:>5.0f}s  {err[:40] if err else 'ok'}")
    print(f"\nTotal new rows: {sum(s[1] for s in summary)}")
    print(f"Total synced to Supabase: {sum(s[2] for s in summary)}")


if __name__ == '__main__':
    main()
