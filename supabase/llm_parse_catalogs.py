"""Extract artwork prices from catalog PDFs/JPGs via Claude API.

For each candidate exhibition:
  1. Download relevant PDF(s) + price-image JPG(s) from Drive
  2. Extract PDF text via pdfplumber + OCR JPGs via tesseract (vie+eng)
  3. Send to Claude (claude-sonnet-4-6, effort=low, prompt-cached system prompt)
  4. Get structured JSON: list of artworks with artist/title/dimensions/medium/year/price/currency
  5. Map to artist_id via normalized_name lookup
  6. Insert into Supabase price_observations

Env vars (from .env.local):
  ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Flags:
  DRY_RUN=1   skip Supabase insert, print what would be inserted
  LIMIT=N     process first N candidates only
  ONLY=14,15  process specific exhibition IDs (comma-separated)

Run:
  python3 supabase/llm_parse_catalogs.py
  DRY_RUN=1 LIMIT=2 python3 supabase/llm_parse_catalogs.py
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
import pdfplumber
import pytesseract
from PIL import Image
import requests
import anthropic

# ─── Setup ────────────────────────────────────────────────────────────────────
ENV = {}
ENV_PATH = Path(__file__).parent.parent / ".env.local"
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        ENV[k] = v

os.environ.setdefault('ANTHROPIC_API_KEY', ENV.get('ANTHROPIC_API_KEY', ''))
URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}',
     'Content-Type': 'application/json', 'Prefer': 'return=minimal'}

DRY_RUN = bool(os.environ.get('DRY_RUN'))
LIMIT = int(os.environ.get('LIMIT', 0))
ONLY = set(int(x) for x in os.environ.get('ONLY', '').split(',') if x.strip())

PDF_DIR = Path('/tmp/artonis_pdf')
PDF_DIR.mkdir(exist_ok=True)
client = anthropic.Anthropic()

# ─── Vietnamese name normalization (matches MVP normalize_key) ────────────────
def strip_accents(s):
    t = unicodedata.normalize("NFD", s or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.replace("Đ", "D").replace("đ", "d")

def normalize_key(s):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(s).lower()).strip()

# ─── Schema for structured output ─────────────────────────────────────────────
ARTWORK_SCHEMA = {
    "type": "object",
    "properties": {
        "artworks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist_name": {"type": "string", "description": "Full Vietnamese name with proper diacritics (e.g. 'Lê Phổ', not 'Le Pho'). Empty string if not identifiable."},
                    "artwork_title": {"type": "string"},
                    "dimensions": {"type": "string", "description": "e.g. '60x80 cm' — keep unit"},
                    "medium": {"type": "string", "description": "Chất liệu, e.g. 'Sơn dầu trên toan', 'Lụa', 'Sơn mài'"},
                    "year": {"type": "string", "description": "4-digit year if stated, else empty string"},
                    "price_amount": {"type": "number"},
                    "currency": {"type": "string", "description": "VND, USD, EUR. Guess from context if not stated (VND default for Vietnamese catalogs)"},
                    "status": {"type": "string", "description": "e.g. 'sold', 'available' if mentioned, else empty"},
                },
                "required": ["artist_name", "artwork_title", "price_amount", "currency"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["artworks"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract artwork prices from Vietnamese art exhibition catalogues.

Input is the text content of a catalog PDF, possibly combined with OCR text from price-list JPG images.

For each artwork that has a stated price, return:
- artist_name: full Vietnamese name with proper diacritics (Lê Phổ, not Le Pho; Vũ Cao Đàm, not Vu Cao Dam)
- artwork_title: as written in the catalog
- dimensions: e.g. "60x80 cm" — preserve the unit
- medium: chất liệu (Sơn dầu trên toan, Lụa, Sơn mài, Acrylic, Mực trên giấy dó, etc.)
- year: 4-digit year if stated, else empty string
- price_amount: numeric value only — strip commas, dots-as-separator, "đ" suffix
- currency: VND / USD / EUR — guess from context if not stated (Vietnamese galleries default to VND)
- status: "sold" / "available" if mentioned, else empty

Critical rules:
1. **Multi-artist catalogs:** group exhibitions list each artist with their own artworks. Pay attention to which artist owns which artwork — names usually appear above/before each artwork block, or in a column header.
2. **Skip artworks without prices.** If a piece is shown without a price, omit it entirely.
3. **JPG OCR overrides:** if a price appears in OCR text from a price-list image but not in the PDF main text, USE the OCR price.
4. **Currency parsing:** "350.000.000" or "350,000,000" → 350000000 (assume VND if no unit). "$3,500" → 3500 USD. "3.500 USD" → 3500 USD. "350 triệu" → 350000000 VND. "1 tỷ" → 1000000000 VND.
5. **Multi-language metadata:** catalogs may mix Vietnamese + English. Title can stay in original language.
6. **Don't invent data.** If artist name unclear from context, leave artist_name empty (don't guess).

Return JSON: {"artworks": [...]}"""


# ─── Helpers ──────────────────────────────────────────────────────────────────
def rclone_download(drive_path, dest):
    # Drive returns mixed NFC/NFD folder names — try NFD first (more common), then NFC
    last_err = ""
    for form in ('NFD', 'NFC'):
        normalized = unicodedata.normalize(form, drive_path)
        r = subprocess.run(
            ['rclone', 'copyto', f'gdrive_artonis:{normalized}', str(dest)],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0:
            return True, ""
        last_err = r.stderr[:200]
    return False, last_err


def extract_pdf_text(path):
    try:
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n\n--- PAGE BREAK ---\n\n".join(pages)
    except Exception as e:
        return f"[PDF parse error: {e}]"


def ocr_image(path):
    try:
        return pytesseract.image_to_string(Image.open(path), lang='vie+eng')
    except Exception as e:
        return f"[OCR error: {e}]"


def parse_with_claude(catalog_text, jpg_ocr_text, exh_title, exh_artists):
    """Call Claude API. Returns list of artwork dicts + usage info."""
    user_content = f"Exhibition: {exh_title}\nDeclared artists: {exh_artists}\n\n"
    user_content += f"=== CATALOG PDF TEXT ===\n{catalog_text[:60000]}\n\n"  # cap input
    if jpg_ocr_text:
        user_content += f"=== PRICE-LIST JPG OCR TEXT ===\n{jpg_ocr_text[:20000]}"

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": ARTWORK_SCHEMA},
        },
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["artworks"], resp.usage


def find_artist_id(name, artists_index):
    """Find artist_id by normalized_name. Returns None if no match."""
    if not name or not name.strip():
        return None
    key = normalize_key(name)
    return artists_index.get(key)


def insert_observations(artworks, exh_id, artists_index):
    """Map each artwork → artist_id, insert as price_observations row."""
    rows = []
    skipped = []
    for a in artworks:
        aid = find_artist_id(a.get('artist_name', ''), artists_index)
        if not aid:
            skipped.append(a.get('artist_name', '?'))
            continue
        rows.append({
            'artist_id': aid,
            'exhibition_id': exh_id,
            'artwork_title': a.get('artwork_title'),
            'medium': a.get('medium') or None,
            'dimensions': a.get('dimensions') or None,
            'year': a.get('year') or None,
            'price_amount': a.get('price_amount'),
            'currency': a.get('currency') or 'VND',
            'status': a.get('status') or '',
            'confidence': 0.75,
        })
    if DRY_RUN or not rows:
        return len(rows), skipped
    r = requests.post(f"{URL}/rest/v1/price_observations", headers=H,
                      json=rows, timeout=60)
    if not r.ok:
        print(f"   ✗ INSERT ERR: {r.status_code} {r.text[:200]}")
        return 0, skipped
    return len(rows), skipped


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"{'DRY-RUN ' if DRY_RUN else ''}LLM catalog parser starting...\n")

    # Fetch candidates: exh with PDF/JPG sources, no existing observations
    exh = requests.get(f"{URL}/rest/v1/exhibitions?select=id,title,artists_text,drive_path",
                       headers={'apikey': KEY}, timeout=30).json()
    sf = requests.get(f"{URL}/rest/v1/source_files?select=exhibition_id,filename,extension,source_kind",
                      headers={'apikey': KEY}, timeout=30).json()
    po = requests.get(f"{URL}/rest/v1/price_observations?select=exhibition_id",
                      headers={'apikey': KEY}, timeout=30).json()
    artists = requests.get(f"{URL}/rest/v1/artists?select=id,name,normalized_name",
                           headers={'apikey': KEY}, timeout=30).json()
    artists_index = {a['normalized_name']: a['id'] for a in artists}

    has_po = set(p['exhibition_id'] for p in po if p['exhibition_id'])
    sf_by_exh = {}
    for f in sf:
        sf_by_exh.setdefault(f['exhibition_id'], []).append(f)

    candidates = []
    for e in exh:
        if e['id'] in has_po: continue
        if not e.get('drive_path') or e['drive_path'].startswith('metadata://'): continue
        if ONLY and e['id'] not in ONLY: continue
        files = sf_by_exh.get(e['id'], [])
        parseable = [f for f in files
                     if f['extension'] in ('.pdf',) and f.get('source_kind') in ('catalogue', 'price_catalogue', 'document')]
        price_imgs = [f for f in files
                      if f['extension'] in ('.jpg', '.jpeg', '.png') and f.get('source_kind') == 'price_image']
        if parseable or price_imgs:
            candidates.append((e, parseable, price_imgs))

    if LIMIT:
        candidates = candidates[:LIMIT]
    print(f"Candidates: {len(candidates)}\n")

    total_usage = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
    total_inserted = 0
    for exh, pdfs, jpgs in candidates:
        exh_id = exh['id']
        exh_dir = exh['drive_path'].rstrip('/')
        print(f"┌─ exh#{exh_id:>2} {(exh['title'] or '')[:55]}")
        print(f"│   artists_text: {(exh['artists_text'] or '?')[:60]}")
        print(f"│   files: {len(pdfs)} PDF + {len(jpgs)} price-JPG")

        # Download + extract PDF text
        catalog_text = ""
        for pdf_meta in pdfs:
            local = PDF_DIR / f"exh{exh_id}_{pdf_meta['filename']}"
            if not local.exists():
                ok, err = rclone_download(f"{exh_dir}/{pdf_meta['filename']}", local)
                if not ok:
                    print(f"│   ✗ DL fail {pdf_meta['filename']!r}: {err}")
                    continue
            catalog_text += f"\n\n=== FILE: {pdf_meta['filename']} ===\n"
            catalog_text += extract_pdf_text(local)

        # OCR price-list JPGs
        jpg_ocr_text = ""
        for jpg_meta in jpgs:
            local = PDF_DIR / f"exh{exh_id}_{jpg_meta['filename']}"
            if not local.exists():
                ok, err = rclone_download(f"{exh_dir}/{jpg_meta['filename']}", local)
                if not ok:
                    print(f"│   ✗ DL fail {jpg_meta['filename']!r}: {err}")
                    continue
            jpg_ocr_text += f"\n\n=== JPG OCR: {jpg_meta['filename']} ===\n"
            jpg_ocr_text += ocr_image(local)

        if not catalog_text.strip() and not jpg_ocr_text.strip():
            print(f"│   ⚠ no content extracted, skip")
            print(f"└─\n")
            continue

        # Call Claude
        try:
            artworks, usage = parse_with_claude(
                catalog_text, jpg_ocr_text, exh['title'] or '', exh['artists_text'] or '',
            )
        except anthropic.APIError as e:
            print(f"│   ✗ API err: {e.message[:200] if hasattr(e, 'message') else e}")
            print(f"└─\n")
            continue

        total_usage['input'] += usage.input_tokens
        total_usage['output'] += usage.output_tokens
        total_usage['cache_read'] += usage.cache_read_input_tokens or 0
        total_usage['cache_write'] += usage.cache_creation_input_tokens or 0
        print(f"│   tokens: in={usage.input_tokens} out={usage.output_tokens} cache_r={usage.cache_read_input_tokens or 0}")
        print(f"│   → extracted {len(artworks)} artworks")

        # Show preview
        for a in artworks[:3]:
            print(f"│      • {a.get('artist_name','?')[:25]:<25} | {a.get('artwork_title','')[:35]:<35} | {a.get('price_amount')} {a.get('currency')}")
        if len(artworks) > 3:
            print(f"│      ... ({len(artworks)-3} more)")

        # Insert
        inserted, skipped = insert_observations(artworks, exh_id, artists_index)
        total_inserted += inserted
        if skipped:
            print(f"│   ⚠ skipped (no artist match): {skipped[:5]}")
        marker = '✓' if DRY_RUN else '✓ inserted'
        print(f"│   {marker} {inserted} rows")
        print(f"└─\n")

    # Cost summary
    in_cost = total_usage['input'] * 3 / 1_000_000
    out_cost = total_usage['output'] * 15 / 1_000_000
    cache_r_cost = total_usage['cache_read'] * 0.30 / 1_000_000
    cache_w_cost = total_usage['cache_write'] * 3.75 / 1_000_000
    print(f"\n╔═══ SUMMARY ═══")
    print(f"║ Exhibitions processed: {len(candidates)}")
    print(f"║ Total observations: {total_inserted}{' (dry-run, not inserted)' if DRY_RUN else ''}")
    print(f"║ ")
    print(f"║ Tokens: input={total_usage['input']}, output={total_usage['output']}")
    print(f"║         cache_read={total_usage['cache_read']}, cache_write={total_usage['cache_write']}")
    print(f"║ Cost:   input=${in_cost:.4f} output=${out_cost:.4f}")
    print(f"║         cache=${cache_r_cost + cache_w_cost:.4f}")
    print(f"║ TOTAL:  ${in_cost + out_cost + cache_r_cost + cache_w_cost:.4f}")
    print(f"╚════════════════")


if __name__ == '__main__':
    main()
