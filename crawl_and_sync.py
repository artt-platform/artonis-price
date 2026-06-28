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

# Centralised list of Supabase-authoritative columns.  See module
# docstring for the rule — every column populated by a script in
# supabase/*.py outside the crawl path must appear there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from supabase.sync_protect import strip_authoritative, push_safe_status  # noqa: E402
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
    ('artcurial',      'crawlers.artcurial',      'artcurial',      'crawl'),
    ('drouot',         'crawlers.drouot',         'drouot',         'crawl'),
    # Drouot 4-hourly refetch — option C of SPEC §14.4.  Lightweight:
    # only refetches watchlist URLs whose sale_date is within 24h.
    # Most calls find 0 due URLs and exit in seconds.
    ('drouot_refetch', 'crawlers.drouot',         'drouot',         'crawl_refetch_only'),
    ('larasati',       'crawlers.larasati',       'larasati',       'crawl'),
    ('ravenel',        'crawlers.ravenel',        'ravenel',        'crawl'),
    ('osenat',         'crawlers.osenat',         'osenat',         'crawl'),
    ('le_auction',     'crawlers.le_auction',     'le_auction',     'crawl'),
    # Heritage Auctions — DataDome anti-bot blocked, requires manual cookies (excluded):
    # ('heritage',       'crawlers.heritage',       'heritage',       'crawl'),
    ('millon',         'crawlers.millon',         'millon',         'crawl_all'),
    ('millon_past',    'crawlers.millon',         'millon',         'crawl_past_broad'),
    # millon-vietnam.com is a MIRROR of millon.com — every lot
    # appears under both domains.  Crawling it caused 268 duplicate
    # rows (deleted 2026-06-28).  Per SPEC §3.7, only millon.com is
    # crawled.  Keep crawlers/millon_vn.py for reference but DO NOT
    # re-enable without removing the dedup logic on Supabase side.
    # ('millon_vn',      'crawlers.millon_vn',      'millon',         'crawl'),
    ('gros_delettrez', 'crawlers.gros_delettrez', 'gros-delettrez', 'crawl'),
    ('dawsons',        'crawlers.dawsons',        'dawsons',        'crawl'),
    # /auction-catalog/ WordPress platform — 5 houses sharing the same
    # React-rendered lot layout.  Each entry point opens a fresh Playwright
    # browser, so they're listed separately to keep run-time predictable.
    ('joshua_kodner',  'crawlers.auction_catalog_platform', 'joshua_kodner',   'crawl_joshua'),
    ('akiba_galleries','crawlers.auction_catalog_platform', 'akiba_galleries', 'crawl_akiba'),
    ('lawsons',        'crawlers.auction_catalog_platform', 'lawsons',         'crawl_lawsons'),
    ('john_moran',     'crawlers.auction_catalog_platform', 'john_moran',      'crawl_moran'),
    ('shapiro',        'crawlers.auction_catalog_platform', 'shapiro',         'crawl_shapiro'),
    ('auction_33',     'crawlers.auction_catalog_platform', 'auction_33',      'crawl_33auction'),
    ('invaluable',     'crawlers.invaluable',     'invaluable',     'crawl_all'),
    ('global_auction', 'crawlers.global_auction', 'global-auction', 'crawl'),
    # BidWizard / online-auctions platform — shared by several US regional
    # houses.  Add a new (key, host, default_location) row in
    # crawlers/bidwizard.py::HOUSES to enable another house here.
    ('everard',        'crawlers.bidwizard',      'everard',        'crawl_everard'),
    ('austin_auction', 'crawlers.bidwizard',      'austin_auction', 'crawl_austin'),
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


def push_crawl_run(source, started_at, finished_at, lots_scanned, lots_inserted, status, note):
    """Push a crawl_runs row to Supabase so the /admin/cron monitor page sees recent activity."""
    payload = {
        'source': source,
        'started_at': started_at,
        'finished_at': finished_at,
        'lots_scanned': int(lots_scanned or 0),
        'lots_inserted': int(lots_inserted or 0),
        'status': status,
        'note': (note or '')[:500] if note else None,
    }
    try:
        r = requests.post(
            f"{URL}/rest/v1/crawl_runs",
            headers=H, json=payload, timeout=15,
        )
        if r.status_code not in (200, 201, 204):
            print(f"  ⚠ crawl_runs log failed: HTTP {r.status_code} {r.text[:120]}")
    except requests.RequestException as e:
        print(f"  ⚠ crawl_runs log error: {e}")


def sync_to_supabase(conn, source, since_scraped_at):
    """Push rows from SQLite scraped/updated since since_scraped_at to Supabase."""
    rows = conn.execute("""
        SELECT s.*, a.name as _artist_name
          FROM sale_results s
          LEFT JOIN artists a ON a.id = s.artist_id
         WHERE (s.source = ? OR s.via_platform = ?) AND s.scraped_at >= ?
         ORDER BY s.id
    """, (source, source, since_scraped_at)).fetchall()
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

    # Also load the VN-catalog aliases — these map raw-name variants
    # ("lebadang", "mai thu", "cao dam vu" ...) to the canonical Vietnamese
    # name in artists.name. Without this, "Lebadang" sale rows fail to
    # link because normalize_key("Lebadang") = "lebadang" doesn't match
    # artists.normalized_name = "le ba dang".
    catalog_alias_to_canonical = {}
    try:
        from data.vn_artist_catalog import VN_ARTIST_CATALOG
        for alias_norm, (canonical_name, *_rest) in VN_ARTIST_CATALOG.items():
            canonical_norm = normalize_key(canonical_name)
            if canonical_norm in name_to_id:
                catalog_alias_to_canonical[alias_norm] = name_to_id[canonical_norm]
    except ImportError:
        pass

    def resolve_artist_id(raw_name):
        if not raw_name:
            return None
        norm = normalize_key(raw_name)
        return name_to_id.get(norm) or catalog_alias_to_canonical.get(norm)

    payload = []
    for r in rows:
        d = dict(r)
        # Drop SQLite-only fields
        for k in ('id', 'created_at', 'updated_at', '_artist_name'):
            d.pop(k, None)
        # Re-resolve artist_id via normalized_name + catalog aliases
        # (SQLite IDs ≠ Supabase IDs).
        d['artist_id'] = resolve_artist_id(r['_artist_name'])
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
        # Drop every column owned by a Supabase-side script when
        # SQLite has null/empty.  Canonical list lives in
        # supabase/sync_protect.py — add new columns there, not here.
        strip_authoritative(d)
        push_safe_status(d)
        payload.append(d)

    # Group rows by their key signature so each PostgREST batch has
    # uniform columns.  Operator 2026-06-28: strip_authoritative drops
    # different keys per row depending on which Supabase-authoritative
    # fields the SQLite row has NULL for.  Sending a mixed-key batch
    # produces PGRST102 'All object keys must match' and the whole
    # crawl-and-sync silently pushed 0/557 rows for the Millon
    # re-crawl.  Group → batch each group separately.
    from collections import defaultdict
    groups = defaultdict(list)
    for row in payload:
        sig = tuple(sorted(row.keys()))
        groups[sig].append(row)

    BATCH = 100
    total_pushed = 0
    for sig, group_rows in groups.items():
        for i in range(0, len(group_rows), BATCH):
            chunk = group_rows[i:i + BATCH]
            r = requests.post(
                f"{URL}/rest/v1/sale_results?on_conflict=source_url",
                headers=H, json=chunk, timeout=60,
            )
            if r.status_code in (201, 204, 200):
                total_pushed += len(chunk)
            else:
                print(f"  ✗ batch ({len(sig)} keys, {len(chunk)} rows): "
                      f"HTTP {r.status_code} {r.text[:200]}")
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
        started_at_iso = sentinel + 'Z'  # tag as UTC
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
                "SELECT COUNT(*) FROM sale_results WHERE (source = ? OR via_platform = ?) AND scraped_at >= ?",
                (db_source, db_source, sentinel)
            ).fetchone()[0]
            print(f"\n  → {mod_key} done in {dur:.0f}s. {new_in_sqlite} rows touched in SQLite (source={db_source}).")

            # Push to Supabase
            pushed_count = 0
            if not SKIP_SUPABASE and new_in_sqlite > 0:
                _, pushed_count = sync_to_supabase(conn, db_source, sentinel)
                print(f"  → Pushed {pushed_count}/{new_in_sqlite} to Supabase.")
            summary.append((name, new_in_sqlite, pushed_count, dur, None))
            # Log to crawl_runs (Supabase) for monitor page
            finished_at_iso = (datetime.utcnow().replace(microsecond=0)).isoformat() + 'Z'
            if not SKIP_SUPABASE:
                push_crawl_run(db_source, started_at_iso, finished_at_iso,
                               new_in_sqlite, pushed_count, 'ok', f'{mod_key} {dur:.0f}s')
        except KeyboardInterrupt:
            print(f"\n  ✗ Interrupted")
            if not SKIP_SUPABASE:
                push_crawl_run(db_source, started_at_iso,
                               (datetime.utcnow().replace(microsecond=0)).isoformat() + 'Z',
                               0, 0, 'cancelled', f'{mod_key} interrupted')
            break
        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n  ✗ ERROR: {e}")
            print(tb[:1000])
            summary.append((name, 0, 0, time.time() - t0, str(e)[:200]))
            if not SKIP_SUPABASE:
                push_crawl_run(db_source, started_at_iso,
                               (datetime.utcnow().replace(microsecond=0)).isoformat() + 'Z',
                               0, 0, 'error', f'{mod_key}: {str(e)[:200]}')

    conn.close()

    # === LLM enrichment pass ===
    # After all crawlers ran + synced to Supabase, fetch full catalog
    # descriptions for the newly-inserted rows and run Haiku 4.5 over
    # them to fill medium / year / signature_info / provenance.  Skip
    # entirely when SKIP_SUPABASE (local-only run) or ANTHROPIC_API_KEY
    # is unset.
    if not SKIP_SUPABASE and os.environ.get('ANTHROPIC_API_KEY'):
        try:
            print(f"\n\n{'='*60}\nLLM enrichment pass\n{'='*60}")
            # Backfill descriptions for any rows missing them
            from supabase.backfill_catalog_descriptions import backfill
            backfill(limit=200, delay=0.5, verbose=True)
            # Run LLM extract — cap cost at $2 per cron run
            from supabase.llm_extract_fields import run as llm_run
            llm_run(limit=500, delay=0.2, max_cost=2.0, verbose=True)
        except Exception as e:
            print(f"  LLM enrichment skipped: {e}")

    # Summary
    print(f"\n\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"{'source':<18} {'new':>6} {'pushed':>8} {'time':>6}  status")
    for src, nlocal, pushed, dur, err in summary:
        print(f"{src:<18} {nlocal:>6} {pushed:>8} {dur:>5.0f}s  {err[:40] if err else 'ok'}")
    print(f"\nTotal new rows: {sum(s[1] for s in summary)}")
    print(f"Total synced to Supabase: {sum(s[2] for s in summary)}")


if __name__ == '__main__':
    main()
