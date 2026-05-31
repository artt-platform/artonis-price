"""Cleanup round 2: split concatenated artist rows #173 and #183.

#173 "Lê Triều Điển Hồng Lĩnh"  → split into existing #64 Lê Triều Điển + #65 Hồng Lĩnh
   (linked to Exh #18 "Đồng hành"; also need to link existing #66 Lê Triết)
#183 "Tào Linh Doãn Hoàng Lâm" → split into existing #23 Tào Linh + #40 Doãn Hoàng Lâm
   (linked to Exh #39 "Trầm tích")

Run: python3 supabase/cleanup_fake_artists_v2.py
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


def link(exh_id, artist_id):
    r = requests.post(
        f"{URL}/rest/v1/exhibition_artists",
        headers={**H, 'Prefer': 'resolution=ignore-duplicates,return=minimal'},
        json={'exhibition_id': exh_id, 'artist_id': artist_id}, timeout=30,
    )
    if r.status_code not in (200, 201, 204, 409):
        print(f"  ERR link exh={exh_id} artist={artist_id}: {r.status_code} {r.text[:200]}")
    else:
        print(f"  linked exh #{exh_id} ↔ artist #{artist_id}")


def patch_exh(exh_id, fields):
    r = requests.patch(
        f"{URL}/rest/v1/exhibitions?id=eq.{exh_id}",
        headers=H, json=fields, timeout=30,
    )
    if not r.ok:
        print(f"  ERR patch exh {exh_id}: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    print(f"  patched exh #{exh_id}: {fields}")


def delete_artist(aid):
    # First drop junction rows
    r = requests.delete(
        f"{URL}/rest/v1/exhibition_artists?artist_id=eq.{aid}",
        headers=H, timeout=30,
    )
    print(f"  dropped junction for artist {aid}: HTTP {r.status_code}")
    r = requests.delete(
        f"{URL}/rest/v1/artists?id=eq.{aid}",
        headers=H, timeout=30,
    )
    print(f"  deleted artist {aid}: HTTP {r.status_code}")


# === #173: Lê Triều Điển Hồng Lĩnh → split + link to exh #18 ===
print("\n[1/2] Exh #18 'Đồng hành' — split #173 + link 3 real artists")
delete_artist(173)
for aid in (64, 65, 66):  # Lê Triều Điển, Hồng Lĩnh, Lê Triết
    link(18, aid)
patch_exh(18, {'artists_text': 'Lê Triều Điển, Hồng Lĩnh, Lê Triết'})


# === #183: Tào Linh Doãn Hoàng Lâm → split + link to exh #39 ===
print("\n[2/2] Exh #39 'Trầm tích' — split #183 + link 2 real artists")
delete_artist(183)
for aid in (23, 40):  # Tào Linh, Doãn Hoàng Lâm
    link(39, aid)
patch_exh(39, {'artists_text': 'Tào Linh, Doãn Hoàng Lâm'})


print("\nDONE — round 2 cleanup complete.")
