-- Add biographical fields to artists: hometown, education, additional roles,
-- alternate name (Western order, real name, pseudonyms).

alter table artists add column if not exists hometown text;
alter table artists add column if not exists education text;
alter table artists add column if not exists also_known_as text;
alter table artists add column if not exists additional_roles text;
-- additional_roles is a free-text field: "poet", "calligrapher", "sculptor",
-- or comma-separated combinations. Empty/null = visual artist only.

comment on column artists.hometown is 'Birthplace city/region (Vietnamese form, e.g. "Hà Nội", "Huế")';
comment on column artists.education is 'École des Beaux-Arts d''Indochine cohort or other formal training';
comment on column artists.also_known_as is 'Real name, French/Western variant, or pseudonym (e.g. "Bàng Khởi Phụng")';
comment on column artists.additional_roles is 'Free-text list of non-painting roles, e.g. "poet, calligrapher"';
