"""Sync DB exhibitions.drive_path with current Drive folder names.

Drive folders were renamed (date format YYYYMMDD → YYMMDD, venue cleanup).
This script updates existing DB rows to match current Drive paths so that
future scan-drive runs use UPDATE (not INSERT duplicates).

Skips the 2 Bùi Tiến Tuấn folders that don't match any existing DB row
(Lụa 2022 + Tranh tại nhà — new exhibitions, will be added by scan-drive).
"""
import json
import re
import sys
from pathlib import Path
import requests
import subprocess

ENV_PATH = Path(__file__).parent.parent / ".env.local"
ENV = {}
with open(ENV_PATH) as f:
    for line in f:
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
}

# ─── Fetch current Drive folder list ──────────────────────────────────────────
result = subprocess.run(
    ['rclone', 'lsf', 'gdrive_artonis:', '--max-depth', '1', '--dirs-only'],
    capture_output=True, text=True, timeout=60
)
drive_folders = [
    l.strip().rstrip('/') for l in result.stdout.splitlines()
    if l.strip() and not l.startswith('1. Catalogue')
]
print(f"Drive: {len(drive_folders)} folders\n")

# ─── Fetch DB exhibitions ─────────────────────────────────────────────────────
r = requests.get(f"{URL}/rest/v1/exhibitions?select=id,drive_path,start_date", headers=H, timeout=30)
db_rows = r.json()
print(f"DB: {len(db_rows)} exhibitions\n")

# ─── Matching ─────────────────────────────────────────────────────────────────
def first_token(s, n=2):
    return ' '.join((s or '').strip().lower().split()[:n])

def extract_date(f):
    m = re.search(r' - (\d{6}) - ', f) or re.search(r' - (\d{6})$', f) \
        or re.search(r' - (\d{8}) - ', f) or re.search(r' - (\d{8})$', f)
    if not m: return None
    d = m.group(1)
    return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else f'20{d[:2]}-{d[2:4]}-{d[4:]}'

# Skip ambiguous Bùi Tiến Tuấn cases (multiple Drive folders share same first tokens)
SKIP = {
    'Bùi Tiến Tuấn - Lụa 2022 - 250207 - Ánh Dương Art Space',
    'Bùi Tiến Tuấn - Tranh tại nhà',
}

updates = []
unmatched = []
matched_db_ids = set()
for f in drive_folders:
    if f in SKIP:
        unmatched.append((f, 'SKIPPED (new exh, scan-drive will add)'))
        continue
    f_token = first_token(f)
    f_date = extract_date(f)
    # candidates: DB rows whose first 2 tokens match
    cand = []
    for r in db_rows:
        dp = (r.get('drive_path') or '').rstrip('/')
        if not dp or dp.startswith('metadata://') or r['id'] in matched_db_ids:
            continue
        if first_token(dp) == f_token:
            score = 100 if r.get('start_date') == f_date else 0
            cand.append((score, r))
    if not cand:
        unmatched.append((f, 'no DB match'))
        continue
    cand.sort(key=lambda c: -c[0])
    chosen = cand[0][1]
    new_path = f + '/'
    if new_path == chosen['drive_path']:
        continue  # already matches
    updates.append((chosen['id'], chosen['drive_path'], new_path))
    matched_db_ids.add(chosen['id'])

print(f"Planned updates: {len(updates)}")
print(f"Unmatched/skipped Drive folders: {len(unmatched)}\n")

# ─── Apply updates ────────────────────────────────────────────────────────────
print("Applying updates...")
for db_id, old, new in updates:
    r = requests.patch(
        f"{URL}/rest/v1/exhibitions?id=eq.{db_id}",
        headers=H, json={'drive_path': new}, timeout=30,
    )
    if not r.ok:
        print(f"  ERR #{db_id}: {r.status_code} {r.text[:200]}")
    else:
        print(f"  ✓ #{db_id}: {old.rstrip('/')} → {new.rstrip('/')}")

print(f"\nDONE. {len(updates)} drive_path updated.")
if unmatched:
    print("\nLeft for scan-drive to add as new:")
    for f, reason in unmatched:
        print(f"  {f}  [{reason}]")
