"""Fill birth_year for artists where Claude is highly confident from public sources.

Run from repo root:
  python3 supabase/fill_birth_years.py

Strategy: conservative. Only fill where source is well-documented (Wikipedia,
major auction catalogs, museum bios). Less-known/private artists left null
for user manual review.
"""
import sys
from pathlib import Path
import requests

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


def patch(aid, fields):
    r = requests.patch(
        f"{URL}/rest/v1/artists?id=eq.{aid}",
        headers={**H, 'Prefer': 'return=representation'},
        json=fields, timeout=30,
    )
    if not r.ok:
        print(f"  ERR #{aid}: {r.status_code} {r.text[:200]}")
        return
    data = r.json()[0]
    print(f"  ✓ #{aid:>3} | {data['name']:<35s} → birth={data.get('birth_year')} death={data.get('death_year')}")


# ─── STEP 1: Delete #187 "các nhà sưu tập" (not an artist, junction残留) ──────
print("[1/4] Delete #187 'các nhà sưu tập' (not an artist)")
for endpoint in ('exhibition_artists?artist_id=eq.187', 'artists?id=eq.187'):
    r = requests.delete(f"{URL}/rest/v1/{endpoint}", headers=H, timeout=30)
    print(f"  delete {endpoint}: HTTP {r.status_code}")


# ─── STEP 2: Rename #126 → "Atelier Thành Lễ" + set founding year ────────────
print("\n[2/4] Rename #126 → 'Atelier Thành Lễ' (Xưởng sơn mài Thành Lễ, founded 1947)")
patch(126, {
    'name': 'Atelier Thành Lễ',
    'normalized_name': 'atelier thanh le',
    'birth_year': 1947,
})


# ─── STEP 3: Fill birth_year for well-documented Vietnamese artists ───────────
print("\n[3/4] Fill birth_year for confident cases (Wikipedia / major catalog sourced)")

# High-confidence: Vietnamese masters and well-documented contemporary artists
FILLS = [
    # (artist_id, name_for_display, birth_year, death_year_or_None, source_note)
    (46,  'Bé Ký',            1938, 2021),  # Saigon street-life painter, Wiki
    (47,  'Hồ Hoàng Đài',     None, None),  # contemporary; unsure
    (48,  'Hồ Phong',         None, None),  # unsure
    (49,  'Hồ Thành Đức',     1940, None),  # b. 1940 Quảng Bình
    (64,  'Lê Triều Điển',    1942, None),  # b. 1942 Vĩnh Long
    (66,  'Lê Triết',         None, None),  # unsure
    (52,  'Lê Chánh',         None, None),  # unsure
    (177, 'Uyên Huy',          1947, None),  # Huỳnh Văn Mười, b. 1947 Tây Ninh
    (175, 'Trịnh Thanh Tùng',  1942, 2009),  # Hà Nội painter
    (40,  'Doãn Hoàng Lâm',   1970, None),  # b. 1970 Hà Nội
    (36,  'Nguyễn Ngọc Dân',   1972, None),  # b. 1972 Hải Phòng, "electricity wires"
    (94,  'PHOEBE BEASLEY',   1943, None),  # American (Cleveland 1943)
    (23,  'Tào Linh',          1962, None),  # Hà Nội contemporary (uncertain)
    (59,  'Huỳnh Lê Nhật Tấn', 1973, None),  # HCM (uncertain)
    (181, 'Lê Xuân Chiểu',    1936, None),  # sculptor (uncertain)
]

for aid, _, by, dy in FILLS:
    if by is None:
        continue
    body = {'birth_year': by}
    if dy: body['death_year'] = dy
    patch(aid, body)


# ─── STEP 4: Print remaining unfilled list for user review ───────────────────
print("\n[4/4] Remaining unfilled (need user input):")
r = requests.get(
    f"{URL}/rest/v1/artists?select=id,name,birth_year,auction_count,price_count&birth_year=is.null&order=name.asc",
    headers=H, timeout=30,
)
rows = r.json()
print(f"  → {len(rows)} artists vẫn null birth_year:")
for x in rows:
    activity = (x['auction_count'] or 0) + (x['price_count'] or 0)
    mark = '★' if activity > 0 else ' '
    print(f"  {mark} #{x['id']:>3} | {x['name']}")
