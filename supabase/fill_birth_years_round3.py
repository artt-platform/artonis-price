"""Round 3: user-confirmed birth_years + 1 display_name update."""
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
    'Prefer': 'return=representation',
}

# User-confirmed 2026-06-01
UPDATES = [
    (41,  {'birth_year': 1997}, 'Khánh Vân — vankhanhart.com'),
    (42,  {'birth_year': 1992, 'display_name': 'Đào Thảo Phương'}, 'Thảo Phương (Đào Thảo Phương) — Hải Phòng'),
    (44,  {'birth_year': 1995}, 'Cao Văn Thục — Hà Nam'),
    (45,  {'birth_year': 1987}, 'Nguyễn Phạm Đình Tuấn — Quảng Nam'),
    (47,  {'birth_year': 1937}, 'Hồ Hoàng Đài — Huế'),
    (56,  {'birth_year': 2012}, 'Phạm Hải Nguyên — Lạng Sơn (child prodigy)'),
    (62,  {'birth_year': 1959}, 'Trần Kim Hòa — Sài Gòn (NOT Công Kim Hoa)'),
    (63,  {'birth_year': 1980}, 'Nguyễn Thị Kim Chi — Đồng Tháp'),
    (66,  {'birth_year': 1975}, 'Lê Triết — son of Lê Triều Điển'),
    (171, {'birth_year': 1980}, 'Benjamin Schiller — approximate, German painter "Beni"'),
]

print(f"Applying {len(UPDATES)} updates...\n")
for aid, body, note in UPDATES:
    r = requests.patch(
        f"{URL}/rest/v1/artists?id=eq.{aid}",
        headers=H, json=body, timeout=30,
    )
    if not r.ok:
        print(f"  ERR #{aid}: {r.status_code} {r.text[:200]}")
        continue
    data = r.json()[0]
    print(f"  ✓ #{aid:>3} | {data['name']:<28s} → b={data['birth_year']} disp={data.get('display_name') or '-'}")
    print(f"        {note}")

print("\nDONE.")
