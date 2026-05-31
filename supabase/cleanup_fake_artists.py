"""One-shot cleanup: remove 3 fake artist rows (#184, #185, #186) + fix linked exhibitions.

Run from repo root:
  python3 supabase/cleanup_fake_artists.py

What it does (in order):
  1. Delete exhibition_artists junction rows for IDs 184/185/186
  2. Delete the 3 fake artist rows
  3. Exh #49 (Kaleidoscope): split 7 Vietnamese artists, create artist rows, recreate junction
  4. Exh #55 (Quang San EBAI 100 years): set organizer, clear artists_text
  5. Exh #57 (Nguyễn Lâm memorial): create artist 'Nguyễn Lâm', set organizer, fix link
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


def strip_accents(value):
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("Đ", "D").replace("đ", "d")


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(value).lower()).strip()


def get_or_create_artist(name):
    """Find artist by normalized_name, or create. Returns id."""
    normalized = normalize_key(name)
    r = requests.get(
        f"{URL}/rest/v1/artists?normalized_name=eq.{normalized}&select=id,name",
        headers=H, timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if rows:
        print(f"  EXISTS #{rows[0]['id']} {rows[0]['name']!r} (normalized={normalized!r})")
        return rows[0]['id']
    r = requests.post(
        f"{URL}/rest/v1/artists",
        headers=H,
        json={'name': name, 'normalized_name': normalized},
        timeout=30,
    )
    if not r.ok:
        print(f"  ERR insert {name!r}: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    new_id = r.json()[0]['id']
    print(f"  CREATED #{new_id} {name!r}")
    return new_id


def link(exhibition_id, artist_id):
    r = requests.post(
        f"{URL}/rest/v1/exhibition_artists",
        headers={**H, 'Prefer': 'resolution=ignore-duplicates,return=minimal'},
        json={'exhibition_id': exhibition_id, 'artist_id': artist_id},
        timeout=30,
    )
    if r.status_code not in (200, 201, 204, 409):
        print(f"  ERR link exh={exhibition_id} artist={artist_id}: {r.status_code} {r.text[:200]}")


def patch_exhibition(exh_id, fields):
    r = requests.patch(
        f"{URL}/rest/v1/exhibitions?id=eq.{exh_id}",
        headers=H, json=fields, timeout=30,
    )
    if not r.ok:
        print(f"  ERR patch exh {exh_id}: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    print(f"  PATCHED exh #{exh_id}: {fields}")


# === STEP 1: Drop junction rows for fake artists ===
print("\n[1/5] Drop junction rows for fake artist IDs 184/185/186")
for aid in (184, 185, 186):
    r = requests.delete(
        f"{URL}/rest/v1/exhibition_artists?artist_id=eq.{aid}",
        headers=H, timeout=30,
    )
    print(f"  deleted junction for artist {aid}: HTTP {r.status_code}")


# === STEP 2: Delete fake artist rows ===
print("\n[2/5] Delete fake artist rows 184/185/186")
for aid in (184, 185, 186):
    r = requests.delete(
        f"{URL}/rest/v1/artists?id=eq.{aid}",
        headers=H, timeout=30,
    )
    print(f"  deleted artist {aid}: HTTP {r.status_code}")


# === STEP 3: Exhibition #49 — split 7 artists ===
print("\n[3/5] Exh #49 (Kaleidoscope): create 7 Vietnamese artists + link")
EXH_49_ARTISTS = [
    "Trần Hạnh",
    "Khánh Vân",
    "Vũ Tuấn Việt",
    "Thảo Phương",
    "Đỗ Hà Hoài",
    "Cao Văn Thục",
    "Nguyễn Phạm Đình Tuấn",
]
for n in EXH_49_ARTISTS:
    aid = get_or_create_artist(n)
    link(49, aid)
patch_exhibition(49, {
    'artists_text': ", ".join(EXH_49_ARTISTS),
})


# === STEP 4: Exhibition #55 — Quang San / EBAI 100 years ===
print("\n[4/5] Exh #55 (Bảo tàng Quang San EBAI 100 năm)")
patch_exhibition(55, {
    'organizer': 'Bảo tàng Quang San',
    'artists_text': '',  # NOTE: triển lãm EBAI 100 năm (1925-2025), cần add danh sách Đông Dương masters thủ công
})
print("  ⚠ Triển lãm này là EBAI 100 năm (École des Beaux-Arts de l'Indochine 1925-2025)")
print("    User chưa có danh sách nghệ sĩ → để trống artists_text, add tay sau.")


# === STEP 5: Exhibition #57 — Nguyễn Lâm memorial ===
print("\n[5/5] Exh #57 (Triển lãm 100 ngày mất Nguyễn Lâm)")
nl_id = get_or_create_artist("Nguyễn Lâm")
link(57, nl_id)
patch_exhibition(57, {
    'organizer': 'ArtBlue Studio Singapore và các nhà sưu tập',
    'artists_text': 'Nguyễn Lâm',
})


print("\nDONE — cleanup complete.")
