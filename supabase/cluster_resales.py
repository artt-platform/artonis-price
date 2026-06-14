"""Cluster sale_results rows that refer to the same physical artwork.

Same-artwork detection (per artist, painting/sculpture kinds only):
  - Dimensions must match within ±1.5 cm on each side (or be exact-match)
    after parsing "W x H cm" — this is the hardest filter, since two
    different works at the same artist rarely share exact dimensions.
  - Title fuzzy match (token-set ratio ≥ 70) OR year+medium agreement
    OR sale_date < other to avoid linking a piece to its later copy.

Output: same artwork_uuid (uuid4 hex) on all rows in a cluster of size ≥ 2.

Idempotent: run again to absorb new rows. Existing artwork_uuid values
are kept; only NULL ones get clustered.

Run:
  python3 supabase/cluster_resales.py
  DRY_RUN=1 python3 supabase/cluster_resales.py
"""
import os, re, sys, uuid
from collections import defaultdict
from pathlib import Path
import requests
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
ENV = {}
for line in (ROOT / ".env.local").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
URL = ENV["SUPABASE_URL"]; KEY = ENV["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}

DRY = os.environ.get("DRY_RUN", "") == "1"

# ─── Helpers ────────────────────────────────────────────────────────────────
DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


def parse_dims(s):
    """Return (w_cm, h_cm) as floats, or None if unparseable."""
    if not s:
        return None
    s = s.replace(",", ".")
    m = DIM_RE.search(s)
    if not m:
        return None
    try:
        w, h = float(m.group(1)), float(m.group(2))
        if w <= 1 or h <= 1 or w > 1000 or h > 1000:
            return None
        return (w, h)
    except (ValueError, TypeError):
        return None


def dims_match(a, b, tol=1.5):
    """Same dimensions ±1.5cm (allow w/h swap)."""
    if a is None or b is None:
        return False
    aw, ah = a; bw, bh = b
    return ((abs(aw - bw) <= tol and abs(ah - bh) <= tol) or
            (abs(aw - bh) <= tol and abs(ah - bw) <= tol))


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_title(t):
    if not t:
        return ""
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("Đ", "D").replace("đ", "d").lower()
    # Strip common date suffixes like "circa 1942" or "vers 1948"
    t = re.sub(r"(?:circa|vers|ca\.?)\s*\d{4}", "", t)
    # Strip year-only tail
    t = re.sub(r",?\s*\d{4}\s*$", "", t).strip()
    return t


def token_set_ratio(a, b):
    """Jaccard over tokens. 0.0 to 1.0."""
    ta = set(_TOKEN_RE.findall(normalize_title(a)))
    tb = set(_TOKEN_RE.findall(normalize_title(b)))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union


def same_artwork(r1, r2):
    """Heuristic: are these two rows the same physical artwork?

    Conservative — better to under-cluster than to merge two distinct works.
    Dimensions must match within ±1.5cm (w/h swap allowed) AND token-set
    Jaccard on normalized titles ≥ 0.7. A loose year/medium fallback was
    tried and produced false positives (e.g. Lê Phổ "Flowers" 90x80 #1 vs
    "Still Life" 90x80 #2 — same artist size but different works), so it
    was removed.
    """
    d1 = parse_dims(r1.get("dimensions"))
    d2 = parse_dims(r2.get("dimensions"))
    if not d1 or not d2:
        return False
    if not dims_match(d1, d2):
        return False
    return token_set_ratio(r1.get("artwork_title", ""), r2.get("artwork_title", "")) >= 0.7


# ─── Union-Find ─────────────────────────────────────────────────────────────
class UF:
    def __init__(self):
        self.p = {}

    def add(self, x):
        if x not in self.p:
            self.p[x] = x

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.p[ry] = rx

    def groups(self):
        g = defaultdict(list)
        for x in self.p:
            g[self.find(x)].append(x)
        return list(g.values())


# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    # Fetch all sale_results painting/sculpture rows with artist_id + dimensions
    print("Fetching sale_results …", flush=True)
    rows = []
    fr = 0
    while True:
        rsp = requests.get(
            f"{URL}/rest/v1/sale_results?artist_id=not.is.null&"
            f"kind=in.(painting,sculpture)&dimensions=not.is.null&"
            f"select=id,artist_id,artwork_title,medium,dimensions,year,"
            f"artwork_uuid,sale_date,price_usd",
            headers={"apikey": KEY, "Range": f"{fr}-{fr+999}"},
        ).json()
        if not rsp:
            break
        rows.extend(rsp)
        if len(rsp) < 1000:
            break
        fr += 1000
    print(f"  total candidates: {len(rows)}", flush=True)

    # Group by artist for O(N²) within-artist matching (typical artist <100 lots)
    by_artist = defaultdict(list)
    for r in rows:
        by_artist[r["artist_id"]].append(r)

    uf = UF()
    pair_count = 0
    for aid, sales in by_artist.items():
        if len(sales) < 2:
            continue
        ids = [s["id"] for s in sales]
        for i in ids:
            uf.add(i)
        for i in range(len(sales)):
            for j in range(i + 1, len(sales)):
                if same_artwork(sales[i], sales[j]):
                    uf.union(sales[i]["id"], sales[j]["id"])
                    pair_count += 1

    print(f"  matched pairs: {pair_count}", flush=True)

    # Build clusters
    clusters = [g for g in uf.groups() if len(g) >= 2]
    print(f"  clusters of size ≥2: {len(clusters)}", flush=True)
    print(f"  rows in clusters:    {sum(len(c) for c in clusters)}", flush=True)

    # Show samples
    sample_by_id = {r["id"]: r for r in rows}
    for c in sorted(clusters, key=lambda c: -len(c))[:5]:
        print(f"\n  cluster ({len(c)}):", flush=True)
        for rid in c[:6]:
            r = sample_by_id[rid]
            print(f"    id={rid} aid={r['artist_id']} "
                  f"{r.get('sale_date') or '???':<11} "
                  f"{(r.get('artwork_title') or '')[:40]:<40} "
                  f"{r.get('dimensions') or '':<14} "
                  f"${r.get('price_usd') or 0:>10,.0f}", flush=True)

    if DRY:
        print("\nDRY_RUN — no UUIDs assigned")
        return

    # Assign UUIDs: reuse existing uuid in cluster if any, else generate.
    print("\nAssigning UUIDs…", flush=True)
    updated = 0
    for c in clusters:
        existing = {sample_by_id[rid].get("artwork_uuid") for rid in c
                    if sample_by_id[rid].get("artwork_uuid")}
        if existing:
            target = next(iter(existing))
        else:
            target = uuid.uuid4().hex
        # Patch all rows in cluster that don't already have this UUID
        to_set = [rid for rid in c
                  if sample_by_id[rid].get("artwork_uuid") != target]
        if not to_set:
            continue
        ids_in = ",".join(str(x) for x in to_set)
        rsp = requests.patch(
            f"{URL}/rest/v1/sale_results?id=in.({ids_in})",
            headers=H, json={"artwork_uuid": target},
        )
        if rsp.status_code in (200, 204):
            updated += len(to_set)
        else:
            print(f"  fail batch: HTTP {rsp.status_code} {rsp.text[:200]}",
                  flush=True)
            break

    print(f"\nDone. {updated} rows tagged with artwork_uuid.")


if __name__ == "__main__":
    main()
