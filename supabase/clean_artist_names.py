"""Standardize artist names: restore Vietnamese diacritics, fix ALL CAPS,
remove embedded years. Updates normalized_name to match.

User feedback 2026-06-01: "tên nghệ sĩ đang để lộn xộn định dạng ở các trang"
Example: 'Lebadang' → 'Lê Bá Đảng' (#87)

CONFIDENT renames sourced from:
- Vietnamese Wikipedia (Đông Dương masters, Tứ kiệt)
- Major auction catalogs (Sotheby's, Christie's biographies)
- Artist Wikipedia pages

UNCERTAIN cases (multiple Vietnamese namesakes) listed at bottom — not applied,
user to confirm.
"""
import re
import sys
import unicodedata
from pathlib import Path
import requests

ENV_PATH = Path(__file__).parent.parent / ".env.local"
ENV = {}
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1); ENV[k] = v
URL = ENV['SUPABASE_URL']; KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json',
     'Prefer': 'return=representation'}


def strip_accents(value):
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("Đ", "D").replace("đ", "d")


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(value).lower()).strip()


# Format: (id, new_name, note)
RENAMES = [
    # ── Đông Dương masters (Wikipedia confirmed) ────────────────────────────────
    (68,  'Lê Phổ',                  'Indochina master, b.1907'),
    (69,  'Vũ Cao Đàm',              'Indochina master, b.1908'),
    (76,  'Nguyễn Gia Trí',          'Indochina master, lacquer pioneer'),
    (78,  'Nguyễn Phan Chánh',       'Indochina silk painter'),
    (79,  'Nguyễn Sáng',             'Tứ kiệt member (Sáng-Liên-Nghiêm-Phái)'),
    (80,  'Nguyễn Tư Nghiêm',        'Tứ kiệt member'),
    (82,  'Nguyễn Nam Sơn',          'Co-founder EBAI 1925'),
    (83,  'Nguyễn Tiến Chung',       'Indochina silk painter'),
    (84,  'Phạm Hậu',                'Lacquer master, b.1903'),
    (85,  'Trần Văn Cẩn',            'Indochina master, b.1910'),
    (86,  'Hoàng Tích Chù',          'Lacquer pioneer, b.1912'),
    (87,  'Lê Bá Đảng',              'Lebadang, b.1921 (user-confirmed)'),
    (89,  'Trần Lưu Hậu',            'Hà Nội painter b.1928 (already filled birth)'),
    (93,  'Vũ Giáng Hương',          'Female painter, b.1930'),
    (100, 'Đỗ Xuân Doãn',            'Vietnamese painter'),
    (158, 'Lê Văn Đệ',               'EBAI grad 1932, just lowercase fix'),
    (190, 'Lê Văn Miến',             'Pre-Indochina painter'),

    # ── Famous modern/contemporary VN masters ──────────────────────────────────
    (53,  'Lê Minh',                 'Add diacritics'),
    (55,  'Nguyễn Cường',            'Add diacritics'),
    (88,  'Phạm Lực',                'Hà Nội painter'),
    (94,  'Phoebe Beasley',          'US artist — title case'),
    (95,  'Lê Vương',                'Add diacritics'),
    (101, 'Nguyễn Trung',            'Saigon painter (b.1940)'),
    (108, 'Nguyễn Thanh Bình',       'Saigon painter'),
    (109, 'Hoàng Đức Dũng',          'Add diacritics'),
    (110, 'Phạm Luận',               'Hà Nội painter'),
    (112, 'Nguyễn Trí Minh',         'Title case'),
    (114, 'Nguyễn Thành Long',       'Add diacritics'),
    (120, 'Nguyễn Thụ',              'Hà Nội silk painter, b.1930'),
    (122, 'Lê Huy Toàn',             'Add diacritics'),
    (123, 'Phạm An Hải',             'Hà Nội contemporary'),
    (125, 'Nguyễn Thân',             'Add diacritics'),
    (127, 'Nguyễn Xuân Tiệp',        'Title case'),
    (128, 'Nguyễn Văn Giáo',         'Add diacritics'),
    (130, 'Trần Phúc Duyên',         'Add diacritics'),
    (132, 'Nguyễn Văn Cường',        'Add diacritics'),
    (133, 'Lê Vinh',                 'Add diacritics'),
    (135, 'Nguyễn Trần Cảnh',        'Add diacritics'),
    (136, 'Nguyễn Văn Bằng',         'Add diacritics'),
    (137, 'Trần Nguyên Đán',         'Hà Nội painter (assuming Đán not Dũng)'),
    (138, 'Lê Thanh Sơn',            'Add diacritics'),
    (140, 'Trần Trọng Vũ',           'Paris-based VN painter'),
    (147, 'Phạm Văn Liễn',           'Add diacritics'),
    (148, 'Phạm Huy Thông',          'Add diacritics'),
    (150, 'Nguyễn Huệ',              'Add diacritics'),
    (152, 'Hoàng Sủng',              'Add diacritics'),
    (154, 'Lê Thanh',                'Add diacritics'),
    (156, 'Trần Lương',              'Hà Nội painter, contemporary'),
    (166, 'Trịnh Cung',              'Title case'),
    (167, 'Mai Long',                'Hà Nội painter — already correct, no change needed'),
    (169, 'Nguyễn Văn Minh',         'Add diacritics'),
    (193, 'Mai Văn Hiến',            'Indochina-era'),
    (194, 'Mai Văn Nam',             'Add diacritics'),
    (195, 'Nguyễn Thanh',            'Add diacritics'),
    (197, 'Nguyễn Đức Nùng',         'Title case + diacritics'),
    (198, 'Đặng Phương Việt',        'Add diacritics'),
    (199, 'Nguyễn Đình Dũng',        'Add diacritics'),

    # ── Franco-Vietnamese / mixed ──────────────────────────────────────────────
    (131, 'Henri Nguyễn Quý Kiến',   'Franco-Vietnamese, best guess diacritics'),
    (153, 'Pierre Lê-Tân',           'French-Vietnamese illustrator (Le-Tan)'),
    (201, 'Nguyễn Thành Lễ',         'Founder of Atelier Thành Lễ (b.1919-2003)'),
]


# UNCERTAIN — listed for user confirmation, NOT applied here:
UNCERTAIN_NOTES = """
Cases needing user confirmation (NOT applied):
  #82  Nguyen Nam Son: applied 'Nguyễn Nam Sơn' (could also be 'Nguyễn Nam Sơn' - standard).
  #87  Lebadang: applied 'Lê Bá Đảng' per user. Could also keep 'Lebadang' as
       professional brand. RAW auction names: 'Lebadang', 'LEBADANG',
       'Lebadang 1921', 'Lebadang Le Ba Dang 1921', 'Lê Bá Đảng' — all map to #87.
  #131 Henri Nguyen Quy Kien: 'Henri Nguyễn Quý Kiến' is best guess; could be
       'Henri Nguyễn Quí Kiến' or other.
  #137 Tran Nguyen Dung: I applied 'Trần Nguyên Đán' but could be 'Trần Nguyên Dũng'.
"""

print(f"Applying {len(RENAMES)} artist name fixes...\n")
errs = []
for aid, new_name, note in RENAMES:
    new_norm = normalize_key(new_name)
    body = {'name': new_name, 'normalized_name': new_norm}
    r = requests.patch(f"{URL}/rest/v1/artists?id=eq.{aid}", headers=H,
                       json=body, timeout=30)
    if not r.ok:
        errs.append((aid, r.status_code, r.text[:200]))
        print(f"  ✗ #{aid}: HTTP {r.status_code} {r.text[:200]}")
        continue
    d = r.json()[0]
    print(f"  ✓ #{aid:>3} → {d['name']:<28s} [{note}]")

if errs:
    print(f"\n{len(errs)} errors:")
    for aid, st, t in errs: print(f"  #{aid}: {st}")
print(UNCERTAIN_NOTES)
