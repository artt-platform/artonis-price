-- Distinguish individual artists from workshops / studios / collective
-- traditions (Xưởng Sơn Mài Thành Lễ, Song Hổ, ATELIER THÀNH LÊ, etc.).
--
-- Why: market valuation, attribution confidence, and stats roll-ups all
-- differ between a person and a workshop, even when both produce works
-- under the same name.  Operator request 2026-06-26.

CREATE TYPE artist_kind AS ENUM ('individual', 'workshop', 'unknown');

ALTER TABLE artists
  ADD COLUMN kind artist_kind NOT NULL DEFAULT 'individual';

-- Seed the workshops we know about
UPDATE artists SET kind = 'workshop' WHERE id IN (
  126,  -- Xưởng Sơn Mài Thành Lễ (Thành Lễ workshop, Bình Dương 1947-1975)
  248   -- Song Hổ (bút hiệu / dòng tranh sơn mài thủ công pre-1975)
);

-- Also clear the bogus birth_year on the workshop record (workshops
-- don't have one).  Founding date lives in additional_roles narrative.
UPDATE artists SET birth_year = NULL WHERE id = 126;
