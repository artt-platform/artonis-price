import re, unicodedata, requests
from pathlib import Path
env={}
for l in Path('/Users/trietnguyen/Documents/Company/Artonis/App/ArtonisArtistPrice/.env.local').read_text().splitlines():
    if '=' in l and not l.startswith('#'):
        k,v=l.split('=',1); env[k]=v.strip()
URL=env['SUPABASE_URL']; KEY=env['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json'}

def strip_accents(s):
    t = unicodedata.normalize("NFD", s or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.replace("Đ","D").replace("đ","d")
def normalize_key(s):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(s).lower()).strip()

# (id, new_name, note)
RENAMES = [
    (75, 'Bùi Xuân Phái', 'Tứ kiệt master'),
    (98, 'Bùi Hữu Hùng', 'lacquer painter'),
    (81, 'Dương Bích Liên', 'Tứ kiệt master'),
    (91, 'Huỳnh Phương Đông', 'war artist'),
    (99, 'Đỗ Quang Em', 'Saigon master'),
    (159, 'Lương Xuân Nhị', 'Indochina silk painter'),
    (119, 'Lưu Công Nhân', 'Hà Nội master'),
    (115, 'Đinh Quân', 'contemporary'),
    (118, 'Tạ Thúc Bình', 'Indochina-era'),
    (121, 'Huỳnh Văn Thuận', 'painter'),
    (155, 'Hồng Việt Dũng', 'contemporary'),
    (139, 'Trương Tân', 'contemporary'),
    (124, 'Văn Dương Thành', 'female painter'),
    (143, 'Tô Ngọc Thành', 'painter'),
    (96, 'Đinh Ý Nhi', 'female painter'),
    (107, 'Quốc Thái', 'painter'),
    (106, 'Quỳnh Hương', 'painter'),
    (191, 'Trịnh Công Sơn', 'musician-painter'),
    (192, 'Thành Chương', 'well-known painter'),
    (178, 'Đỗ Duy Tuấn', 'painter'),
    (202, 'Điềm Phùng Thị', 'famous sculptor'),
]
print(f'Applying {len(RENAMES)} diacritic restorations...\n')
errs = []
for aid, name, note in RENAMES:
    body = {'name': name, 'normalized_name': normalize_key(name)}
    r = requests.patch(f"{URL}/rest/v1/artists?id=eq.{aid}", headers={**H, 'Prefer': 'return=representation'}, json=body, timeout=30)
    if not r.ok:
        errs.append((aid, r.status_code, r.text[:200]))
        print(f"  X #{aid}: {r.status_code} {r.text[:200]}")
        continue
    d = r.json()[0]
    print(f"  ✓ #{aid:>3} → {d['name']:<28s} [{note}]")
if errs:
    print(f'\n{len(errs)} errors:')
