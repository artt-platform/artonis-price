"""Parse downloaded /tmp/artonis_drive files via free text-layer pdfplumber.
For each file, match its parent folder to an exhibition by drive_path.
Insert solo-artist's prices into Supabase price_observations.
"""
import os, sys, re, unicodedata, json
from pathlib import Path
import requests
sys.path.insert(0, '/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice')
os.chdir('/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice')
from artonis_price_mvp import parse_pdf_price_catalogue, normalize_key

ENV={}
for line in open('.env.local'):
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); ENV[k]=v
URL=ENV['SUPABASE_URL']; KEY=ENV['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json','Prefer':'return=minimal'}

DRY = os.environ.get('DRY','0')=='1'
LOCAL_DIR = Path('/tmp/artonis_drive')

# Fetch all exhibitions + existing observations + artists
exhs = requests.get(f'{URL}/rest/v1/exhibitions?select=id,title,artists_text,drive_path', headers={'apikey':KEY}).json()
po_exh = set(p['exhibition_id'] for p in requests.get(f'{URL}/rest/v1/price_observations?select=exhibition_id', headers={'apikey':KEY}).json() if p.get('exhibition_id'))
artists = requests.get(f'{URL}/rest/v1/artists?select=id,name,normalized_name', headers={'apikey':KEY}).json()
artist_by_norm = {a['normalized_name']: a['id'] for a in artists}

def folder_norm(s):
    """Normalize a folder name for matching: NFC, lowercase, strip diacritics, collapse spaces."""
    t = unicodedata.normalize('NFKD', s)
    t = ''.join(ch for ch in t if unicodedata.category(ch) != 'Mn')
    t = t.replace('Đ','D').replace('đ','d')
    t = re.sub(r'\s+', ' ', t.lower().strip())
    # Strip trailing slash + date format unification
    t = re.sub(r'- 20(\d{2})(\d{2})(\d{2})', r'- \1\2\3', t)
    return t

# Match folders to exhibitions
exh_by_folder = {}
for e in exhs:
    dp = (e.get('drive_path') or '').rstrip('/')
    if dp:
        exh_by_folder[folder_norm(dp)] = e

# Walk local files
results = {'matched': 0, 'no_exh': 0, 'no_artist': 0, 'parsed': 0, 'priced': 0, 'inserted': 0}
to_insert = []
for path in sorted(LOCAL_DIR.rglob('*.pdf')):
    rel = path.relative_to(LOCAL_DIR)
    folder = rel.parent.name if rel.parent.name else ''
    if not folder:
        continue
    fnorm = folder_norm(folder)
    exh = exh_by_folder.get(fnorm)
    if not exh:
        # try fuzzy: match by prefix (first 30 chars)
        for k, v in exh_by_folder.items():
            if fnorm[:30] == k[:30] and len(fnorm) > 20:
                exh = v
                break
    if not exh:
        results['no_exh'] += 1
        print(f'  ?? no exh match: {folder[:50]}', flush=True)
        continue
    if exh['id'] in po_exh:
        continue
    results['matched'] += 1

    # Parse PDF
    try:
        arts = parse_pdf_price_catalogue(path, use_ocr_fallback=True)
    except Exception as ex:
        print(f'  ✗ parse err {path.name[:50]}: {ex}', flush=True)
        continue
    if not arts:
        print(f'  - exh#{exh["id"]:>2} {exh["title"][:40] if exh["title"] else "??"} | {path.name[:40]}: NO ITEMS', flush=True)
        continue
    priced = [a for a in arts if a.get('price_amount')]
    results['parsed'] += len(arts)
    results['priced'] += len(priced)
    if not priced:
        continue

    # Resolve artist (solo only for now)
    art_text = (exh.get('artists_text') or '').strip()
    names = [n.strip() for n in re.split(r'[,\n+]', art_text) if n.strip() and len(n.strip()) > 1]
    aid = None
    if len(names) == 1:
        aid = artist_by_norm.get(normalize_key(names[0]))
    if not aid:
        results['no_artist'] += 1
        print(f'  ⚠ exh#{exh["id"]} {exh["title"][:30] if exh["title"] else "?"}: no solo artist ({names})', flush=True)
        continue

    print(f'  ✓ exh#{exh["id"]:>2} {exh["title"][:40] if exh["title"] else "?"} | {path.name[:40]}: {len(priced)} priced', flush=True)
    for a in priced:
        to_insert.append({
            'artist_id': aid,
            'exhibition_id': exh['id'],
            'artwork_title': (a.get('artwork_title') or '')[:300],
            'medium': a.get('medium'),
            'dimensions': a.get('dimensions'),
            'year': a.get('year'),
            'price_amount': a.get('price_amount'),
            'currency': a.get('currency') or 'VND',
            'status': a.get('status') or '',
            'confidence': 0.6,
        })

print(f'\nTotal: matched={results["matched"]}, no_exh={results["no_exh"]}, no_artist={results["no_artist"]}')
print(f'Parsed: {results["parsed"]} items, priced: {results["priced"]}, to insert: {len(to_insert)}', flush=True)

if not DRY and to_insert:
    rsp = requests.post(f'{URL}/rest/v1/price_observations', headers=H, json=to_insert, timeout=60)
    if rsp.status_code in (200,201,204):
        print(f'Inserted {len(to_insert)} observations ✓')
    else:
        print(f'Insert failed: {rsp.status_code} {rsp.text[:300]}')
elif DRY:
    print('(DRY-RUN — not inserting)')
