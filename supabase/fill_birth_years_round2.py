"""Round 2: apply researched birth_years for 12 well-sourced artists + 5 user-confirmed.

Sources documented in script comments. CONFIDENT only — uncertain cases
left for user to confirm separately.
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
    'Prefer': 'return=representation',
}

# Format: (id, birth, death, source_note)
FILLS = [
    # User-confirmed 2026-05-31
    (9,   1972, None, 'User-confirmed'),
    (10,  1972, None, 'User-confirmed'),
    (13,  1982, None, 'User-confirmed'),
    (32,  1990, None, 'User-confirmed'),
    (37,  1991, None, 'User-confirmed'),
    # Research CONFIDENT (single year, direct source)
    (43,  1994, None, 'Đỗ Hà Hoài — Hanoi Grapevine 2023 bio: b.1994 Gia Lai, HCMC FA 2018'),
    (58,  1986, None, 'Mai Thị Kim Uyên — Vietnam.vn / Nguyen Art Gallery: b.1986 Quảng Nam'),
    (60,  1987, None, 'Nguyễn Thị Loan Phương — Tuổi Trẻ 2025 "Đường lên mây"'),
    (65,  1952, None, 'Hồng Lĩnh — Phụ Nữ Online 2022: real name Phạm Thị Quý, age 70, wife of Lê Triều Điển'),
    (67,  2003, None, 'Hoàng Long Hải — An Ninh Thủ Đô 2025: Kingston School of Art London grad'),
    (172, 1981, None, 'Phùng Thanh Hà — VietnamNet: HUMG grad, painter w/ Schiller'),
    (174, 1941, 2025, 'Nguyễn Lâm — NLĐ obit 28-6-2025: real name Lâm Huỳnh Long, Cần Thơ, lacquer master'),
    (176, 1949, None, 'Dương Sen — Hanoi Art House: b.1949 Nghệ An, HCMC FA 1983'),
    (180, 1955, None, 'Đặng Kim Long — Vietnam.vn: fellow Royal Art Society SA 2017'),
    (182, 1962, None, 'Nguyễn Hoài Hương — Saigon FA grad 1986 → b. ~1962 (inferred but well-attested)'),
    (201, 1919, 2003, 'Nguyen Thanh Le — MutualArt/Artnet: Thủ Dầu Một workshop, Indochina era'),
    (57,  1989, None, 'Trương Thế Linh — QSAM + Luxuo Next Gen 2020: Hue FA grad 2013, age range 1988-90'),
]

print(f"Applying {len(FILLS)} birth_year fills...\n")
for aid, by, dy, note in FILLS:
    body = {'birth_year': by}
    if dy: body['death_year'] = dy
    r = requests.patch(
        f"{URL}/rest/v1/artists?id=eq.{aid}",
        headers=H, json=body, timeout=30,
    )
    if not r.ok:
        print(f"  ERR #{aid}: {r.status_code} {r.text[:200]}")
        continue
    data = r.json()[0]
    death_str = f' († {data["death_year"]})' if data['death_year'] else ''
    print(f"  ✓ #{aid:>3} | {data['name']:<30s} → {data['birth_year']}{death_str}")
    print(f"        {note}")

print("\nDONE.")
