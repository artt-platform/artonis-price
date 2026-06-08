"""Bulk download + parse all catalogue PDFs for exhibitions without price_observations.

Steps per exhibition:
  1. Build NFD path from exh.drive_path + source_file.filename
  2. rclone copyto → /tmp/artonis_pdf/<exh_id>.pdf
  3. Run MVP parse_pdf_price_catalogue (text + OCR fallback)
  4. Map artworks to artist (solo exh: assume single artist)
  5. Insert into Supabase price_observations + bump artist.price_count

Run from repo root:
  python3 supabase/bulk_parse_pdfs.py

DRY_RUN env var: skip Supabase inserts, just report yields.
"""
import os
import sys
import re
import subprocess
import unicodedata
from pathlib import Path
import requests

# MVP imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from artonis_price_mvp import parse_pdf_price_catalogue, normalize_key

DRY_RUN = bool(os.environ.get('DRY_RUN'))
PDF_DIR = Path('/tmp/artonis_pdf')
PDF_DIR.mkdir(exist_ok=True)

ENV = {}
ENV_PATH = Path(__file__).parent.parent / ".env.local"
for l in ENV_PATH.read_text().splitlines():
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); ENV[k] = v
URL = ENV['SUPABASE_URL']; KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
ANON = ENV['SUPABASE_ANON_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}

# Fetch exh + source_files + existing observations
exh_all = requests.get(f"{URL}/rest/v1/exhibitions?select=id,title,artists_text,drive_path", headers=H, timeout=30).json()
sf_all = requests.get(f"{URL}/rest/v1/source_files?select=exhibition_id,filename,extension,source_kind", headers=H, timeout=30).json()
po_existing = requests.get(f"{URL}/rest/v1/price_observations?select=exhibition_id", headers=H, timeout=30).json()
has_po = set(p['exhibition_id'] for p in po_existing if p['exhibition_id'])
artists_all = requests.get(f"{URL}/rest/v1/artists?select=id,name,normalized_name", headers=H, timeout=30).json()

# Build artist lookup by normalized_name
artist_id_by_norm = {a['normalized_name']: a['id'] for a in artists_all}

# Build sf map
sf_by_exh = {}
for f in sf_all:
    sf_by_exh.setdefault(f['exhibition_id'], []).append(f)


def to_nfd(s): return unicodedata.normalize('NFD', s)
def to_nfc(s): return unicodedata.normalize('NFC', s)


def rclone_download(drive_relative_path, dest):
    """Download from gdrive_artonis: using NFD form.
    Retries with a 6-digit date format (`- YYMMDD -`) if the original
    8-digit form (`- 20YYMMDD -`) doesn't resolve — Drive folders/files
    use the short form while the DB sometimes stores the long form."""
    candidates = [drive_relative_path]
    short_dates = re.sub(r" - 20(\d{2})(\d{2})(\d{2}) ", r" - \1\2\3 ", drive_relative_path)
    if short_dates != drive_relative_path:
        candidates.append(short_dates)
    last_err = ""
    for cand in candidates:
        src = f"gdrive_artonis:{to_nfd(cand)}"
        result = subprocess.run(
            ['rclone', 'copyto', src, str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        last_err = result.stderr
    return False, last_err


def find_artist_id(name):
    norm = normalize_key(name)
    return artist_id_by_norm.get(norm)


# Process each candidate exhibition
candidates = []
for e in exh_all:
    if e['id'] in has_po:
        continue
    if not e.get('drive_path') or e['drive_path'].startswith('metadata://'):
        continue
    files = sf_by_exh.get(e['id'], [])
    pdfs = [f for f in files if f['extension'] == '.pdf' and f.get('source_kind') in ('catalogue', 'price_catalogue', 'document')]
    if pdfs:
        candidates.append((e, pdfs))

print(f'Processing {len(candidates)} candidate exhibitions (PDFs)...\n')

results = []
for exh, pdfs in candidates:
    exh_dir = exh['drive_path'].rstrip('/')
    artists_text = (exh.get('artists_text') or '').strip()
    # Solo or group?
    artist_names = [a.strip() for a in re.split(r'[,\n+]', artists_text) if a.strip() and len(a.strip()) > 1]
    solo_artist_id = find_artist_id(artist_names[0]) if len(artist_names) == 1 and artist_names else None

    print(f'\n┌─ exh#{exh["id"]:>2} {exh["title"][:55] if exh["title"] else "(no title)"}')
    print(f'│   artists: {", ".join(artist_names) if artist_names else "?"}')
    print(f'│   solo_artist_id: {solo_artist_id}')

    all_artworks = []
    for pdf_meta in pdfs:
        filename = pdf_meta['filename']
        local = PDF_DIR / f"exh{exh['id']}_{filename}"
        if not local.exists():
            drive_path = f"{exh_dir}/{filename}"
            ok, err = rclone_download(drive_path, local)
            if not ok:
                print(f'│   ✗ download failed: {filename!r}  {err[:100]}')
                continue
        # Parse
        try:
            arts = parse_pdf_price_catalogue(local, ocr_verbose=False)
        except Exception as e:
            print(f'│   ✗ parse error: {e}')
            continue
        priced = [a for a in arts if a.get('price_amount')]
        print(f'│   PDF: {filename[:45]} → {len(arts)} items, {len(priced)} with price')
        all_artworks.extend(arts)

    if not all_artworks:
        results.append((exh['id'], 0, 0, 'no_data'))
        continue

    priced = [a for a in all_artworks if a.get('price_amount')]
    if not priced:
        results.append((exh['id'], len(all_artworks), 0, 'no_prices'))
        continue

    if DRY_RUN:
        results.append((exh['id'], len(all_artworks), len(priced), 'dry'))
        for a in priced[:3]:
            print(f'│   → {a["artwork_title"][:40]!r}  {a["price_amount"]} {a.get("currency","")}')
        continue

    # Insert observations
    rows_to_insert = []
    for a in priced:
        # If solo exhibition, attribute to that artist
        aid = solo_artist_id
        # TODO: for group exhibitions, try to match by surrounding name in PDF
        if not aid:
            continue
        rows_to_insert.append({
            'artist_id': aid,
            'exhibition_id': exh['id'],
            'artwork_title': a.get('artwork_title'),
            'medium': a.get('medium'),
            'dimensions': a.get('dimensions'),
            'year': a.get('year'),
            'price_amount': a.get('price_amount'),
            'currency': a.get('currency') or 'VND',
            'status': a.get('status') or '',
            'confidence': 0.6,
        })
    if rows_to_insert:
        r = requests.post(f"{URL}/rest/v1/price_observations", headers={**H, 'Prefer': 'return=minimal'},
                          json=rows_to_insert, timeout=60)
        if not r.ok:
            print(f'│   ✗ insert err: HTTP {r.status_code} {r.text[:200]}')
            results.append((exh['id'], len(all_artworks), len(priced), 'insert_err'))
            continue
        print(f'│   ✓ inserted {len(rows_to_insert)} observations')
        results.append((exh['id'], len(all_artworks), len(priced), 'inserted'))
    else:
        print('│   ⚠ no artist mapping (group exh — skipped)')
        results.append((exh['id'], len(all_artworks), len(priced), 'no_artist'))

# Summary
print('\n\n╔═══ SUMMARY ═══')
total_obs = sum(r[2] for r in results if r[3] == 'inserted')
print(f'║ Exhibitions processed: {len(results)}')
print(f'║   inserted:    {sum(1 for r in results if r[3]=="inserted")}')
print(f'║   no_data:     {sum(1 for r in results if r[3]=="no_data")}')
print(f'║   no_prices:   {sum(1 for r in results if r[3]=="no_prices")}')
print(f'║   no_artist:   {sum(1 for r in results if r[3]=="no_artist")}')
print(f'║   insert_err:  {sum(1 for r in results if r[3]=="insert_err")}')
print(f'║ Total observations inserted: {total_obs}')
print('╚════════════════')
