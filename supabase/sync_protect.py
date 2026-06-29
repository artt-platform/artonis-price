"""Single source of truth for columns that Supabase-side scripts own.

ANY column populated by a script in `supabase/*.py` (hammer pullers,
image backfills, LLM extractors, orientation fixers, sweeps,
operator manual PATCHes) MUST appear in `SUPABASE_AUTHORITATIVE` below.

`crawl_and_sync.py` (and every other writer that bulk-upserts onto
existing rows) MUST call `strip_authoritative(row)` before pushing
the payload to PostgREST.  This drops the listed keys when their
value is null / empty so the upsert leaves the Supabase value
intact instead of merging stale SQLite over it.

Three rounds of silent data loss in June 2026 traced back to a
denylist that grew by hand every time a new puller was added.
Centralising the list here means a future contributor can audit
"is my new column protected?" in one place instead of grepping the
600-line crawl_and_sync.py.

How to use:

    from supabase.sync_protect import strip_authoritative, push_safe_status
    for row in rows:
        strip_authoritative(row)
        push_safe_status(row)
        payload.append(row)
"""
from __future__ import annotations
from typing import Iterable

# Every column that a Supabase-side script writes.  Adding a new
# script that PATCHes column X?  Add X here too.
SUPABASE_AUTHORITATIVE: tuple[str, ...] = (
    # Images — backfill_og_images.py + per-crawler og:image fills
    "image_url",
    "image_phash",
    # Hammer + derived prices — Pull_Sothebys.command,
    # pull_drouot_hammers.py, pull_invaluable_hammers.py,
    # import_invaluable_hammer.py, import_invaluable_manual.py,
    # manual operator PATCHes from forwarded screenshots.
    "hammer_price",
    "price_with_premium",
    "price_usd",
    "price_with_premium_usd",
    "price_per_m2_usd",
    "currency",
    # Width/height/area — fix_dim_orientation.py swaps these against
    # image aspect, so a re-crawl with the wrong-side-first dim
    # would re-swap them back.
    "width_cm",
    "height_cm",
    "area_m2",
    "dimensions",
    # LLM-extracted text — llm_extract_fields.py runs after each
    # crawl + sweep, so any field it fills must outlive the next
    # sync round.
    "medium",
    "year",
    "provenance",
    "artwork_title",
    "catalog_description",
    # Estimate backfill — backfill_millon_estimates.py
    "estimate_low",
    "estimate_high",
    # Sweep-fixed metadata — sweep_invaluable_titles.py rewrites
    # 'X via Invaluable' stubs into the real sale name + date.
    "auction_title",
    "sale_date",
    "sale_location",
    # Operator-set artwork groupings — cluster_resales.py + manual
    # admin merges.
    "artwork_uuid",
)


# Status values that mean "this row has not received a real
# resolution yet" — any of these in a sync payload should be
# dropped so a hammer puller's status='sold' / 'passed' /
# 'withdrawn' is preserved on the Supabase side.
_PROVISIONAL_STATUSES: frozenset[str] = frozenset({
    "",
    "estimate",       # legacy Larasati label
    "estimate_only",
    "unknown",
})


def strip_authoritative(row: dict, extra: Iterable[str] = ()) -> dict:
    """Drop every authoritative key whose SQLite value is null/empty.

    Mutates and returns `row`.  Pass `extra` to add ad-hoc keys
    (e.g. when a one-off backfill script writes a column that's not
    yet in the canonical list).
    """
    for k in SUPABASE_AUTHORITATIVE:
        v = row.get(k)
        if v is None or v == "":
            row.pop(k, None)
    for k in extra:
        v = row.get(k)
        if v is None or v == "":
            row.pop(k, None)
    return row


def push_safe_status(row: dict) -> dict:
    """Drop `status` when SQLite has a provisional value.

    Hammer pullers set status='sold' on the Supabase side; that must
    survive a sync round that re-pushes SQLite's
    'estimate_only' / 'estimate' / 'unknown' for the same source_url.
    """
    if row.get("status") in _PROVISIONAL_STATUSES or row.get("status") is None:
        row.pop("status", None)
    return row


# Seconds.  When a Supabase row's `updated_at` exceeds its
# `scraped_at` by more than this margin, treat it as a manual
# operator edit and skip the whole sync write onto it.  60 s is
# enough slack to absorb the drift between when crawl_and_sync
# stamps scraped_at on the row dict and PostgREST stamps updated_at.
MANUAL_EDIT_GAP_SECONDS: int = 60


def is_manually_edited(sup_row: dict, threshold: int = MANUAL_EDIT_GAP_SECONDS) -> bool:
    """Detect 'this row was operator-PATCHed after its last crawl'.

    Normal sync UPSERTs write both scraped_at and updated_at to the
    same timestamp.  A direct PATCH (manual fix, fix_dim_
    orientation.py, llm_extract_fields.py, hammer puller, …) bumps
    updated_at via the trigger but leaves scraped_at alone, so the
    gap grows past `threshold` seconds.

    Operator-flagged 2026-06-28: every Supabase-side fix em ships
    is at risk of the next cron re-scrape over-writing it because
    strip_authoritative only blocks NULL/empty SQLite values, not
    non-null stale ones.  Title cleanups (43 lots), Sothebys status
    (141 lots), dim orientation (620 lots), the lot 19238/19221
    status+hammer fixes — all needed this gap-based protection.

    `sup_row` must include both timestamps.  Missing fields → False
    (default to syncing, never block).
    """
    su = sup_row.get("updated_at")
    sc = sup_row.get("scraped_at")
    if not su or not sc:
        return False
    from datetime import datetime
    try:
        d_up = datetime.fromisoformat(su.rstrip("Z"))
        d_sc = datetime.fromisoformat(sc.rstrip("Z"))
    except (ValueError, AttributeError):
        return False
    return (d_up - d_sc).total_seconds() > threshold


def fetch_supabase_state(supabase_url: str, service_key: str,
                         source_urls: list) -> dict:
    """Batch-pull `source_url, updated_at, scraped_at` for the given
    URLs in groups of 100.  Returns a dict keyed by source_url.

    Used by sync writers to identify which target rows have been
    manually edited so they can skip the UPSERT entirely instead
    of merging stale SQLite over the operator's fix.
    """
    import requests
    headers = {"apikey": service_key,
               "Authorization": f"Bearer {service_key}"}
    out: dict[str, dict] = {}
    BATCH = 100
    for i in range(0, len(source_urls), BATCH):
        chunk = source_urls[i:i + BATCH]
        in_list = ",".join(f'"{u}"' for u in chunk)
        r = requests.get(
            f"{supabase_url}/rest/v1/sale_results",
            params={
                "source_url": f"in.({in_list})",
                "select": "source_url,updated_at,scraped_at",
            },
            headers=headers, timeout=30,
        )
        if r.status_code == 200:
            for row in r.json():
                out[row["source_url"]] = row
    return out


def safe_patch_dim(
    supabase_url: str, service_key: str,
    lot_id: int, width_cm: float, height_cm: float,
    image_url: str | None = None,
) -> tuple[bool, dict]:
    """The ONLY way to write width_cm / height_cm to a sale_results
    row from an ad-hoc or inline script.

    Cross-checks the (w, h) pair against the lot's catalog image and
    SWAPS automatically when the orientation conflicts.  Then PATCHes
    width_cm + height_cm + dimensions string + area_m2 in one shot.

    Operator rule 2026-06-29: every inline PATCH that ever wrote dim
    columns has been wrong on orientation at some point because each
    one re-implemented the W vs H guess from scratch.  Funneling
    every write through this helper means em can't "forget" the
    image-aspect verification — there is no other path.

    Args:
        supabase_url, service_key: from .env.local
        lot_id: sale_results.id
        width_cm, height_cm: parser's best guess.  May be swapped
            internally if image disagrees.
        image_url: optional — when None, the function fetches the row's
            image_url from Supabase before the aspect check.

    Returns:
        (success, patched_dict).  patched_dict contains the FINAL
        (post-swap) values so the caller can log what shipped.
    """
    import requests
    headers = {"apikey": service_key,
               "Authorization": f"Bearer {service_key}",
               "Content-Type": "application/json",
               "Prefer": "return=minimal"}

    # Pull image_url when caller didn't supply one — single round-trip.
    if image_url is None:
        gr = requests.get(
            f"{supabase_url}/rest/v1/sale_results",
            params={"id": f"eq.{lot_id}", "select": "image_url"},
            headers={"apikey": service_key,
                     "Authorization": f"Bearer {service_key}"},
            timeout=10,
        )
        if gr.status_code == 200:
            rows = gr.json()
            if rows:
                image_url = rows[0].get("image_url") or ""

    # Verify orientation against image.  verify_dim_via_image lives in
    # crawlers/common.py — import lazily so this module doesn't drag
    # the whole crawlers package in for callers that don't need dim.
    from crawlers.common import verify_dim_via_image
    new_w, new_h = verify_dim_via_image(width_cm, height_cm, image_url)

    area = None
    if new_w is not None and new_h is not None:
        area = round(new_w * new_h / 10000, 4)
    patch = {
        "width_cm": new_w,
        "height_cm": new_h,
        "dimensions": f"{new_w:g} x {new_h:g} cm" if (new_w and new_h) else None,
        "area_m2": area,
    }
    r = requests.patch(
        f"{supabase_url}/rest/v1/sale_results",
        params={"id": f"eq.{lot_id}"},
        headers=headers, json=patch, timeout=10,
    )
    return (r.status_code < 300), patch
