"""Re-parse group exhibitions with strict per-artist attribution via Claude.

Strategy:
  1. Delete existing (likely mis-attributed) observations for known-bad exhibitions
  2. For text-heavy PDFs: extract text-layer + send to Claude with explicit
     "these are the K artists in this show" directive
  3. For image-only PDFs: convert pages to JPEG → send via vision
  4. Reject any artwork whose returned artist_name doesn't fuzzy-match one of
     the show's declared artists.
"""
import os, sys, json, re, unicodedata, tempfile, base64
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
ANTH=ENV['ANTHROPIC_API_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json','Prefer':'return=minimal'}

import anthropic
client = anthropic.Anthropic(api_key=ANTH)
DRY = os.environ.get('DRY','0')=='1'
DEL_BAD = os.environ.get('DEL_BAD','0')=='1'

arts = requests.get(f'{URL}/rest/v1/artists?select=id,name,normalized_name', headers={'apikey':KEY}).json()
artist_by_norm = {a['normalized_name']: a['id'] for a in arts}

# Targets — (exh_id, declared_artists, pdf_path, image_only)
CASES = [
    {'exh_id':37,'artists':['Phạm Thanh Toàn','Nguyễn Ngọc Dân','Đặng Quang Tiến'],
     'pdf':'/tmp/artonis_drive/Phạm Thanh Toàn + Nguyễn Ngọc Dân + Đặng Quang Tiến – Toàn Dân Tiến – 251206 – Riverside Complex/Catalogue & Statement_Toàn Dân Tiến.pdf',
     'image_only':True, 'delete_old':True},
    {'exh_id':39,'artists':['Tào Linh','Doãn Hoàng Lâm'],
     'pdf':'/tmp/artonis_drive/Tào Linh + Doãn Hoàng Lâm - Trầm Tích - 250214 - Quang San/Tào Linh + Doãn Hoàng Lâm - Trầm Tích - 20250214 - Quang San.pdf',
     'image_only':False, 'delete_old':False},
    {'exh_id':53,'artists':['Đào Minh Tuấn','Đào Minh Tú','Vũ Tuấn Việt'],
     'pdf':'/tmp/artonis_drive/Đào Minh Tuấn + Đào Minh Tú + Vũ Tuấn Việt - Thực Tại Xô Lệch - 250806 - Annam/Đào Minh Tuấn + Đào Minh Tú + Vũ Tuấn Việt - Thực Tại Xô Lệch - 20250806 - Annam.pdf',
     'image_only':True, 'delete_old':True},
    {'exh_id':18,'artists':['Lê Triều Điển','Hồng Lĩnh','Lê Triết'],
     'pdf':'/tmp/artonis_drive/Lê Triều Điển + Hồng Lĩnh + Lê Triết - Đồng Hành - 260128 - Bảo tàng Mỹ thuật TpHCM/Thông tin Triển lãm Đồng Hành.pdf',
     'image_only':False, 'delete_old':False},
]

SCHEMA = {
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
    },"required":["artworks"],"additionalProperties":False,
}

def make_prompt(declared_artists):
    return f"""Extract artwork prices from a Vietnamese group exhibition catalogue.

EXACT artist roster for this show (only these {len(declared_artists)} are valid):
{chr(10).join('- ' + a for a in declared_artists)}

For each priced artwork:
- artist_name: MUST be one of the {len(declared_artists)} declared artists above (use the exact Vietnamese form with proper diacritics)
- artwork_title (as written)
- dimensions (e.g. "60x80 cm")
- medium (chất liệu)
- year
- price_amount (numeric only)
- currency (VND / USD / EUR)
- status (sold / available / empty)

Rules:
1. If you can't identify which of the {len(declared_artists)} declared artists owns a piece, OMIT it.
2. Skip artworks without explicit prices.
3. Currency: "350.000.000" → 350000000 VND; "$3,500" → 3500 USD; "350 triệu" → 350000000 VND.
4. Don't invent. Don't return artist names not in the roster."""

def pdf_to_images(pdf_path, max_pages=12):
    """Convert PDF pages to base64-encoded JPEGs via pdftoppm."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        # pdftoppm -jpeg -r 150 input.pdf out_prefix → produces out_prefix-1.jpg, ...
        import subprocess
        prefix = Path(td) / 'p'
        r = subprocess.run(['pdftoppm','-jpeg','-r','120','-f','1','-l',str(max_pages),
                           str(pdf_path), str(prefix)], capture_output=True, timeout=120)
        if r.returncode != 0:
            print(f'  pdftoppm err: {r.stderr.decode()[:200]}'); return out
        for img in sorted(Path(td).glob('p-*.jpg')):
            if img.stat().st_size > 4_500_000:  # too big — skip
                continue
            out.append(base64.standard_b64encode(img.read_bytes()).decode())
    return out

def extract_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return '\n\n--- PAGE ---\n\n'.join((p.extract_text() or '') for p in pdf.pages)

def call_claude(prompt, content):
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=8000,
        output_config={"effort":"low","format":{"type":"json_schema","schema":SCHEMA}},
        system=[{"type":"text","text":prompt,"cache_control":{"type":"ephemeral"}}],
        messages=[{"role":"user","content":content}],
    )
    out = next(b.text for b in resp.content if b.type=='text')
    return json.loads(out)['artworks'], resp.usage

# Step 1: Delete bad obs
if DEL_BAD and not DRY:
    for case in CASES:
        if case.get('delete_old'):
            rsp = requests.delete(f"{URL}/rest/v1/price_observations?exhibition_id=eq.{case['exh_id']}", headers=H)
            print(f"  deleted obs for exh#{case['exh_id']}: HTTP {rsp.status_code}")

# Step 2-4: Re-process
all_rows = []
total_in = total_out = 0
for case in CASES:
    eid = case['exh_id']
    declared = case['artists']
    prompt = make_prompt(declared)
    print(f"\n→ exh#{eid} ({len(declared)} artists: {', '.join(declared)})")
    if not os.path.exists(case['pdf']):
        print(f"   missing pdf"); continue

    if case['image_only']:
        # Send pages as images
        imgs_b64 = pdf_to_images(case['pdf'])
        if not imgs_b64:
            print(f"   no images extracted"); continue
        print(f"   sending {len(imgs_b64)} pages as images")
        content = [{"type":"text","text":f"Catalog pages for the group exhibition. Roster: {', '.join(declared)}"}]
        for b in imgs_b64[:10]:
            content.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b}})
    else:
        text = extract_text(case['pdf'])
        print(f"   text {len(text)} chars")
        content = f"Group exhibition. Roster: {', '.join(declared)}\n\n=== TEXT ===\n{text[:60000]}"

    if DRY: continue

    try:
        artworks, usage = call_claude(prompt, content)
    except Exception as e:
        print(f"   err: {e}"); continue
    total_in += usage.input_tokens; total_out += usage.output_tokens
    print(f"   {len(artworks)} priced (tokens in={usage.input_tokens}, out={usage.output_tokens})")

    # Build artist matching map for THIS show only — strict
    show_artists = {normalize_key(n): n for n in declared}
    declared_ids = {n: artist_by_norm.get(normalize_key(n)) for n in declared}

    for a in artworks:
        # Match returned artist_name to one of the declared artists
        ret_norm = normalize_key(a.get('artist_name',''))
        match = None
        for k in show_artists:
            if k == ret_norm or k in ret_norm or ret_norm in k:
                match = show_artists[k]; break
        if not match:
            print(f"     ⚠ skip — unrecognized artist: {a.get('artist_name')!r} | title {a.get('artwork_title','')[:40]!r}")
            continue
        aid = declared_ids[match]
        if not aid:
            print(f"     ⚠ skip — artist {match!r} not in artists table")
            continue
        all_rows.append({
            'artist_id': aid, 'exhibition_id': eid,
            'artwork_title': (a.get('artwork_title') or '')[:300],
            'medium': a.get('medium') or None,
            'dimensions': a.get('dimensions') or '',
            'year': a.get('year') or None,
            'price_amount': a.get('price_amount'),
            'currency': a.get('currency') or 'VND',
            'status': a.get('status') or '',
            'confidence': 0.75,
        })

print(f"\nrows={len(all_rows)} tokens in={total_in} out={total_out} cost~${total_in/1e6*3+total_out/1e6*15:.3f}")
if all_rows and not DRY:
    rsp = requests.post(f'{URL}/rest/v1/price_observations', headers=H, json=all_rows, timeout=120)
    print(f'insert HTTP {rsp.status_code}')
    if rsp.status_code not in (200,201,204): print(rsp.text[:300])
