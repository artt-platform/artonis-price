"""Send price-list JPG images directly to Claude Sonnet vision for the
remaining ~14 exhibitions that have no PDF text data available."""
import os, sys, json, re, unicodedata, base64
from pathlib import Path
import requests
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

# Fetch state — paginate to get all observation exhibition_ids
exhs = requests.get(f'{URL}/rest/v1/exhibitions?select=id,title,artists_text,drive_path', headers={'apikey':KEY}).json()
has_po = set()
fr=0
while True:
    rsp = requests.get(f'{URL}/rest/v1/price_observations?select=exhibition_id', headers={'apikey':KEY,'Range':f'{fr}-{fr+999}'}).json()
    if not rsp: break
    for p in rsp:
        if p.get('exhibition_id'): has_po.add(p['exhibition_id'])
    if len(rsp)<1000: break
    fr+=1000
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

# Filter images to only PRICE-LIST candidates (skip posters)
# Heuristic: filename mentions "giá", "price", "bảng", "list", "tranh", "báo giá"
# Default: include any image NOT a "poster" or "tiểu sử" or "thumb"
def is_pricelist_candidate(name):
    low = name.lower()
    skip_keywords = ['poster', 'thumb', 'avatar', 'tiểu sử', 'tieu su', 'cover', 'preview', 'banner', 'logo']
    if any(k in low for k in skip_keywords): return False
    take_keywords = ['gia', 'giá', 'price', 'list', 'bảng', 'báo', 'danh sách', 'danh sach', 'tranh', 'catalog']
    if any(k in low for k in take_keywords): return True
    # Fallback: include all .jpg that aren't poster-shaped (we'll let LLM decide)
    return True

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

SYSTEM_PROMPT = """You extract artwork prices from images of Vietnamese art exhibition price lists.

The images may be:
- A price list table (rows of artworks with prices)
- A catalogue page with prices
- A poster (usually no prices — return empty list)

For each artwork with a clearly stated price, return:
- artist_name: full Vietnamese name with proper diacritics
- artwork_title: as written
- dimensions: e.g. "60x80 cm"
- medium: chất liệu
- year: 4-digit year if stated, else empty
- price_amount: numeric only
- currency: VND / USD / EUR
- status: "sold" / "available" / empty

Rules:
1. Skip artworks without explicit prices.
2. If image is just a poster (no price info), return {"artworks": []}.
3. Currency: "350.000.000" → 350000000 VND; "$3,500" → 3500 USD.
4. Don't invent. Leave artist_name empty if unclear.
"""

def to_b64(path):
    return base64.standard_b64encode(path.read_bytes()).decode()

def call_vision(images, exh_title, exh_artists):
    """images = list of (path, mime). Send all in one request."""
    content = [{"type":"text","text":f"Exhibition: {exh_title}\nDeclared artists: {exh_artists}"}]
    for path, mime in images[:10]:  # cap at 10 images per call
        content.append({
            "type":"image",
            "source":{"type":"base64","media_type":mime,"data":to_b64(path)},
        })
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=8000,
        output_config={"effort":"low","format":{"type":"json_schema","schema":ARTWORK_SCHEMA}},
        system=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}],
        messages=[{"role":"user","content":content}],
    )
    out = next(b.text for b in resp.content if b.type=='text')
    return json.loads(out)['artworks'], resp.usage

# Find missing exhibitions WITH images
missing_imgs = []
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
    imgs = []
    for ext in ('*.jpg','*.jpeg','*.png','*.JPG','*.JPEG','*.PNG'):
        imgs.extend(folder.glob(ext))
    imgs = sorted(set(imgs))
    if imgs:
        missing_imgs.append((exh, imgs))

print(f'Missing exhibitions with JPG candidates: {len(missing_imgs)}', flush=True)
for exh, imgs in missing_imgs:
    print(f"  #{exh['id']:>2} {exh['title'][:35] if exh['title'] else '?'} | {len(imgs)} img(s)", flush=True)

if DRY:
    print('\nDRY — no API calls'); sys.exit(0)

all_rows = []
total_in = 0; total_out = 0
for exh, imgs in missing_imgs:
    art_text = (exh.get('artists_text') or '').strip()
    print(f"\n→ #{exh['id']} {exh['title'][:40] if exh['title'] else '?'} | {len(imgs)} imgs", flush=True)
    # Build image list
    img_payloads = []
    for p in imgs[:10]:
        ext = p.suffix.lower()
        mime = {'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png'}.get(ext,'image/jpeg')
        if p.stat().st_size > 5_000_000:
            print(f'   skip large img {p.name} ({p.stat().st_size//1024}KB)', flush=True)
            continue
        img_payloads.append((p, mime))
    if not img_payloads:
        continue
    try:
        artworks, usage = call_vision(img_payloads, exh['title'] or '?', art_text)
    except Exception as e:
        print(f'   err: {e}', flush=True); continue
    total_in += usage.input_tokens; total_out += usage.output_tokens
    print(f"   {len(artworks)} priced (in={usage.input_tokens}, out={usage.output_tokens})", flush=True)

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
            'confidence': 0.7,
        })

print(f"\nrows={len(all_rows)} tokens in={total_in} out={total_out} cost~${total_in/1e6*3+total_out/1e6*15:.2f}")
if all_rows:
    rsp = requests.post(f'{URL}/rest/v1/price_observations', headers=H, json=all_rows, timeout=120)
    print(f'insert HTTP {rsp.status_code}')
    if rsp.status_code not in (200,201,204): print(rsp.text[:300])
