"""LLM-based price extraction over locally-downloaded /tmp/artonis_drive files.
Uses Claude Sonnet 4-6 with structured output for the exhibitions still missing
price_observations after the free text-layer + OCR pass."""
import os, sys, json, re, unicodedata, time
from pathlib import Path
import requests
import pdfplumber

sys.path.insert(0,'/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice')
os.chdir('/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice')
from artonis_price_mvp import normalize_key

ENV={}
for line in open('.env.local'):
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); ENV[k]=v
URL=ENV['SUPABASE_URL']; KEY=ENV['SUPABASE_SERVICE_ROLE_KEY']
ANTH_KEY=ENV['ANTHROPIC_API_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json','Prefer':'return=minimal'}

import anthropic
client = anthropic.Anthropic(api_key=ANTH_KEY)
DRY = os.environ.get('DRY','0')=='1'
LOCAL_DIR = Path('/tmp/artonis_drive')

# Fetch state
exhs = requests.get(f'{URL}/rest/v1/exhibitions?select=id,title,artists_text,drive_path', headers={'apikey':KEY}).json()
po = []
fr=0
while True:
    rsp = requests.get(f'{URL}/rest/v1/price_observations?select=exhibition_id', headers={'apikey':KEY,'Range':f'{fr}-{fr+999}'}).json()
    if not rsp: break
    po.extend(rsp); 
    if len(rsp)<1000: break
    fr+=1000
has_po = set(p['exhibition_id'] for p in po if p.get('exhibition_id'))
arts = requests.get(f'{URL}/rest/v1/artists?select=id,name,normalized_name', headers={'apikey':KEY}).json()
artist_by_norm = {a['normalized_name']: a['id'] for a in arts}

def folder_norm(s):
    t = unicodedata.normalize('NFKD', s)
    t = ''.join(ch for ch in t if unicodedata.category(ch) != 'Mn')
    t = t.replace('Đ','D').replace('đ','d').lower().strip().rstrip('/')
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'- 20(\d{2})(\d{2})(\d{2})', r'- \1\2\3', t)
    return t

exh_by_folder = {}
for e in exhs:
    dp = (e.get('drive_path') or '').rstrip('/')
    if dp: exh_by_folder[folder_norm(dp)] = e

ARTWORK_SCHEMA = {
    "type":"object","properties":{
        "artworks":{"type":"array","items":{
            "type":"object","properties":{
                "artist_name":{"type":"string"},
                "artwork_title":{"type":"string"},
                "dimensions":{"type":"string"},
                "medium":{"type":"string"},
                "year":{"type":"string"},
                "price_amount":{"type":"number"},
                "currency":{"type":"string"},
                "status":{"type":"string"},
            },
            "required":["artist_name","artwork_title","price_amount","currency"],
            "additionalProperties":False,
        }}
    },
    "required":["artworks"],"additionalProperties":False,
}

SYSTEM_PROMPT = """You extract artwork prices from Vietnamese art exhibition catalogues.

Input is the text content of a catalog PDF (text layer + possibly OCR fragments).

For each artwork that has a stated price, return:
- artist_name: full Vietnamese name with proper diacritics
- artwork_title: as written
- dimensions: e.g. "60x80 cm"
- medium: chất liệu (Sơn dầu, Lụa, Sơn mài, etc.)
- year: 4-digit year if stated, else empty
- price_amount: numeric only (strip commas, dots, đ)
- currency: VND / USD / EUR
- status: "sold" / "available" / empty

Rules:
1. Multi-artist catalogs: attribute each artwork to its named artist.
2. Skip artworks without prices.
3. Currency parsing: "350.000.000" → 350000000 VND; "$3,500" → 3500 USD; "350 triệu" → 350000000 VND; "1 tỷ" → 1000000000 VND.
4. Don't invent data: leave artist_name empty if unclear.
"""

def extract_pdf_text(path):
    try:
        with pdfplumber.open(path) as pdf:
            return "\n\n--- PAGE ---\n\n".join(p.extract_text() or '' for p in pdf.pages)
    except Exception as e:
        return f'[PDF err: {e}]'

def call_claude(text, exh_title, exh_artists):
    user_content = f"Exhibition: {exh_title}\nDeclared artists: {exh_artists}\n\n=== PDF TEXT ===\n{text[:60000]}"
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=8000,
        output_config={"effort":"low","format":{"type":"json_schema","schema":ARTWORK_SCHEMA}},
        system=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}],
        messages=[{"role":"user","content":user_content}],
    )
    out = next(b.text for b in resp.content if b.type=='text')
    return json.loads(out)['artworks'], resp.usage

# Find PDFs in missing exhibitions' folders
missing_files = []
for folder in sorted(LOCAL_DIR.iterdir()):
    if not folder.is_dir(): continue
    fnorm = folder_norm(folder.name)
    exh = exh_by_folder.get(fnorm)
    if not exh:
        for k,v in exh_by_folder.items():
            if fnorm[:30] == k[:30] and len(fnorm) > 20:
                exh = v; break
    if not exh: continue
    if exh['id'] in has_po: continue
    pdfs = list(folder.glob('*.pdf'))
    if pdfs:
        missing_files.append((exh, pdfs))

print(f'Missing exhibitions with PDFs: {len(missing_files)}', flush=True)
for exh, pdfs in missing_files:
    print(f"  #{exh['id']:>2} {exh['title'][:35] if exh['title'] else '?'} | {len(pdfs)} pdf(s)", flush=True)

if DRY:
    print('\nDRY — no API calls')
    sys.exit(0)

all_rows = []
total_input = 0; total_output = 0
for exh, pdfs in missing_files:
    art_text = (exh.get('artists_text') or '').strip()
    print(f"\n→ #{exh['id']} {exh['title'][:40] if exh['title'] else '?'}", flush=True)
    text_parts = []
    for p in pdfs:
        t = extract_pdf_text(p)
        text_parts.append(f'[{p.name}]\n{t}')
    combined = '\n\n========\n\n'.join(text_parts)
    if len(combined) < 200:
        print(f'   skip: PDF text too short ({len(combined)} chars). Likely image-only — needs vision.', flush=True)
        continue
    try:
        artworks, usage = call_claude(combined, exh['title'] or '?', art_text)
    except Exception as e:
        print(f'   err: {e}', flush=True)
        continue
    total_input += usage.input_tokens
    total_output += usage.output_tokens
    print(f"   Claude returned {len(artworks)} priced (tokens in={usage.input_tokens}, out={usage.output_tokens})", flush=True)

    # Resolve artists
    names = [n.strip() for n in re.split(r'[,\n+]', art_text) if n.strip()]
    solo_aid = artist_by_norm.get(normalize_key(names[0])) if len(names)==1 else None
    for a in artworks:
        aid = artist_by_norm.get(normalize_key(a.get('artist_name',''))) or solo_aid
        if not aid: continue
        all_rows.append({
            'artist_id': aid, 'exhibition_id': exh['id'],
            'artwork_title': (a.get('artwork_title') or '')[:300],
            'medium': a.get('medium') or None,
            'dimensions': a.get('dimensions') or '',
            'year': a.get('year') or None,
            'price_amount': a.get('price_amount'),
            'currency': a.get('currency') or 'VND',
            'status': a.get('status') or '',
            'confidence': 0.8,
        })

print(f"\nTotal rows to insert: {len(all_rows)}")
print(f"Token usage: in={total_input}, out={total_output}")
# claude-sonnet-4-6 pricing approx: $3/MTok in, $15/MTok out
est_cost = total_input/1e6*3 + total_output/1e6*15
print(f"Est cost: ${est_cost:.3f}")

if all_rows:
    rsp = requests.post(f'{URL}/rest/v1/price_observations', headers=H, json=all_rows, timeout=60)
    print(f"Insert HTTP {rsp.status_code}")
    if rsp.status_code not in (200,201,204):
        print(rsp.text[:300])
