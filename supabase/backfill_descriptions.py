"""One-shot cleanup: re-fetch Osenat + Artcurial lot pages and replace
their catalog_description with a properly-scoped extract.

Osenat: scope to <div class="fiche_lot_description" id="lotDesc-{lot_id}">
Artcurial: scope to <div class="fiche_lot_description" id="lotDesc-{lot_id}">
          OR fall back to the API's `comment` field via the page's
          embedded JSON.

Run: python3 supabase/backfill_descriptions.py
"""
from __future__ import annotations
import os, sys, re, time, html as _html
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent


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
URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def _osenat_lot_id(url: str) -> str | None:
    # https://www.osenat.com/lot/169117/30471529-... → 30471529
    m = re.search(r"/lot/\d+/(\d+)-", url)
    return m.group(1) if m else None


def _osenat_clean_description(html: str, lot_id: str) -> str:
    m = re.search(
        r'<div class="fiche_lot_description" id="lotDesc-' + re.escape(lot_id) + r'">(.+?)</div>',
        html, re.DOTALL,
    )
    return _strip_html(m.group(1)) if m else ""


def _artcurial_clean_description(html: str) -> str:
    # Artcurial's lot page exposes the API JSON in a <script> tag.
    # The 'comment' field has the full narrative.
    m = re.search(r'"comment"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if not m:
        return ""
    raw = m.group(1)
    # Decode JSON-style escapes
    raw = (raw.replace('\\"', '"').replace('\\n', '\n').replace('\\r', '')
              .replace('\\/', '/').replace('\\\\', '\\'))
    return _strip_html(raw)[:2000]


def backfill_osenat() -> None:
    r = requests.get(f"{URL}/rest/v1/sale_results",
                     params={"source":"eq.osenat","source_url":"not.is.null",
                             "select":"id,source_url","limit":"500"},
                     headers=H, timeout=20).json()
    print(f"\nOsenat lots: {len(r)}")
    ok = empty = fail = 0
    for i, row in enumerate(r, 1):
        url = row["source_url"]
        lot_id = _osenat_lot_id(url)
        if not lot_id:
            fail += 1
            continue
        try:
            rr = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        except Exception:
            fail += 1
            continue
        if rr.status_code != 200:
            fail += 1
            continue
        desc = _osenat_clean_description(rr.text, lot_id)
        if not desc:
            empty += 1
            # Wipe the garbage rather than leave it
            pr = requests.patch(f"{URL}/rest/v1/sale_results",
                                params={"id":f"eq.{row['id']}"}, headers=H,
                                json={"catalog_description": None}, timeout=10)
            continue
        pr = requests.patch(f"{URL}/rest/v1/sale_results",
                            params={"id":f"eq.{row['id']}"}, headers=H,
                            json={"catalog_description": desc[:2000]}, timeout=10)
        if pr.status_code < 300:
            ok += 1
            if i <= 3 or i % 10 == 0:
                print(f"  [{i}/{len(r)}] {desc[:80]!r}…")
        time.sleep(0.4)
    print(f"  Osenat done: {ok} cleaned, {empty} empty-wiped, {fail} fetch-failed")


def backfill_artcurial() -> None:
    r = requests.get(f"{URL}/rest/v1/sale_results",
                     params={"source":"eq.artcurial","source_url":"not.is.null",
                             "select":"id,source_url","limit":"500"},
                     headers=H, timeout=20).json()
    print(f"\nArtcurial lots: {len(r)}")
    ok = empty = fail = 0
    for i, row in enumerate(r, 1):
        url = row["source_url"]
        try:
            rr = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=20)
        except Exception:
            fail += 1
            continue
        if rr.status_code != 200:
            fail += 1
            continue
        desc = _artcurial_clean_description(rr.text)
        if not desc:
            empty += 1
            continue
        pr = requests.patch(f"{URL}/rest/v1/sale_results",
                            params={"id":f"eq.{row['id']}"}, headers=H,
                            json={"catalog_description": desc[:2000]}, timeout=10)
        if pr.status_code < 300:
            ok += 1
            if i <= 3 or i % 10 == 0:
                print(f"  [{i}/{len(r)}] {desc[:80]!r}…")
        time.sleep(0.4)
    print(f"  Artcurial done: {ok} cleaned, {empty} empty, {fail} fetch-failed")


if __name__ == "__main__":
    backfill_osenat()
    backfill_artcurial()
