"""Deep re-scan ALL Millon dept-1113 ventes.

Operator request 2026-06-26: many lots missing from /sales for
recent Millon Vietnam sales (vente3748 has 2/55, vente4258 had
24/123, etc.).  Re-crawl every dept-1113 catalog with current
filter logic, then sync delta to Supabase.

Run: python3 supabase/deep_rescan_millon.py
"""
from __future__ import annotations
import os, sys, sqlite3, re
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    p = ROOT / ".env.local"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()


def all_dept_1113_ventes() -> list[str]:
    """Walk every page of the dept=1113 listing and return the catalog
    slugs.  As of 2026-06-26 the count is 23."""
    out = set()
    for page in range(20):
        qs = f"op=submit&f%5B0%5D=department%3A1113&page={page}"
        r = requests.get(f"https://www.millon.com/catalogue/ventes-passees?{qs}",
                         headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        # Extract full slugs like "vente4258-arts-du-vietnam-duplex-paris-hanoi-11e-edition"
        slugs = set(re.findall(r"/catalogue/(vente\d+-[a-z0-9\-]+)", r.text))
        if not slugs - out:
            break
        out |= slugs
    return sorted(out, key=lambda s: -int(re.match(r"vente(\d+)", s).group(1)))


def main() -> None:
    from crawlers.millon import crawl_past_catalogs

    slugs = all_dept_1113_ventes()
    print(f"Found {len(slugs)} dept-1113 catalog slugs (most-recent first):")
    for s in slugs[:10]:
        print(f"  {s}")
    if len(slugs) > 10:
        print(f"  … ({len(slugs)-10} more)")

    # SQLite already has image_url column from earlier session
    conn = sqlite3.connect(str(ROOT / "data" / "artonis_price_mvp.sqlite"))
    print(f"\nStarting deep re-scan…")
    crawl_past_catalogs(
        conn,
        catalog_slugs=slugs,
        delay=1.5,
        detail_delay=1.0,
        verbose=True,
        filter_vn=True,
    )
    conn.commit()
    conn.close()
    print("\nDone.  Sync to Supabase + image backfill next.")


if __name__ == "__main__":
    main()
