-- 2026-06-25 — Postgres-side trigger to fill derived fields
-- (area_m2 + price_per_m2_usd) so manual REST inserts and SQL backfills
-- can't silently leave $/m² null when width/height/price are populated.
--
-- Today's incident: 6 lots imported via direct REST POST landed with
-- width_cm, height_cm, price_usd populated but area_m2 and
-- price_per_m2_usd left null because the Python helper
-- compute_area_and_price_per_m2() wasn't called.  UI shows "—" for
-- those rows; artist aggregates miss them.  This trigger is the
-- belt-and-suspenders fix — even if a caller forgets the helper, the
-- DB fills the gap on the way in.
--
-- Scope: ONLY paintings.  Sculpture / print / drawing / medal lots
-- have intentionally-null $/m² (different markets, different units of
-- comparison) — see crawlers/common.py:insert_sale_result.  The
-- trigger respects that distinction.
--
-- Non-destructive: trigger only FILLS nulls, never overwrites
-- existing values.  Callers can still set their own area_m2 = NULL
-- explicitly (e.g. to null out the 3D-lacquer box that the Python
-- side knows is a sculpture) by writing kind='sculpture' first.

CREATE OR REPLACE FUNCTION fill_derived_dim_fields()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  -- Only fill for paintings.  Other kinds keep their intentional nulls.
  IF NEW.kind IS DISTINCT FROM 'painting' THEN
    RETURN NEW;
  END IF;

  -- area_m2 = width × height / 10000 (cm² → m²), 4-decimal precision
  IF NEW.area_m2 IS NULL
     AND NEW.width_cm IS NOT NULL AND NEW.width_cm > 0
     AND NEW.height_cm IS NOT NULL AND NEW.height_cm > 0 THEN
    NEW.area_m2 := round((NEW.width_cm * NEW.height_cm / 10000.0)::numeric, 4);
  END IF;

  -- price_per_m2_usd = price_usd / area_m2, 2-decimal precision
  IF NEW.price_per_m2_usd IS NULL
     AND NEW.price_usd IS NOT NULL AND NEW.price_usd > 0
     AND NEW.area_m2 IS NOT NULL AND NEW.area_m2 > 0 THEN
    NEW.price_per_m2_usd := round((NEW.price_usd / NEW.area_m2)::numeric, 2);
  END IF;

  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION fill_derived_dim_fields() IS
  'Fills area_m2 and price_per_m2_usd on sale_results when null + '
  'kind=painting + numeric inputs present.  Belt-and-suspenders against '
  'manual REST inserts that bypass crawlers/common.py:insert_sale_result.';

DROP TRIGGER IF EXISTS sale_results_fill_derived ON sale_results;

CREATE TRIGGER sale_results_fill_derived
  BEFORE INSERT OR UPDATE OF
    width_cm, height_cm, area_m2, price_usd, price_per_m2_usd, kind
  ON sale_results
  FOR EACH ROW
  EXECUTE FUNCTION fill_derived_dim_fields();

-- One-shot backfill: 44 painting lots historically have width/height/
-- price_usd populated but never got area_m2 or price_per_m2_usd
-- computed (probably from earlier ad-hoc inserts or old crawler runs
-- before the Python helper was added).  Fire the same logic in bulk.
UPDATE sale_results
SET area_m2 = round((width_cm * height_cm / 10000.0)::numeric, 4)
WHERE kind = 'painting'
  AND area_m2 IS NULL
  AND width_cm IS NOT NULL AND width_cm > 0
  AND height_cm IS NOT NULL AND height_cm > 0;

UPDATE sale_results
SET price_per_m2_usd = round((price_usd / area_m2)::numeric, 2)
WHERE kind = 'painting'
  AND price_per_m2_usd IS NULL
  AND price_usd IS NOT NULL AND price_usd > 0
  AND area_m2 IS NOT NULL AND area_m2 > 0;
