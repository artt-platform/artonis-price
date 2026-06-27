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
