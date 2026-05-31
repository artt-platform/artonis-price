-- ============================================================================
-- Artonis Price — initial Postgres schema (migrated from SQLite MVP)
-- Apply via: Supabase Dashboard → SQL Editor → paste this file → Run
-- Or via CLI: supabase db push (after supabase link --project-ref qxskbtkfybpdnckexrwd)
-- ============================================================================

-- ============ ENUM TYPES ====================================================
create type sale_kind as enum ('painting', 'sculpture', 'print', 'drawing', 'medal');
create type sale_status as enum (
  'sold', 'passed', 'withdrawn', 'upcoming', 'unknown',
  'estimate', 'estimate_only'
);
create type support_type as enum ('canvas', 'silk', 'paper', 'lacquer', 'panel', 'metal');

-- ============ ARTISTS =======================================================
-- Vietnamese artists (Indochine masters + contemporary). Aggregates are cached
-- (avg/median/max) — recompute via materialized view OR background job.
create table artists (
  id              bigint generated always as identity primary key,
  name            text not null unique,
  normalized_name text not null,
  display_name    text,
  birth_year      integer,
  death_year      integer,
  -- Cached aggregates over sale_results + price_observations
  exhibition_count           integer default 0,
  auction_count              integer default 0,
  price_count                integer default 0,
  overall_min_usd            numeric(14,2),
  overall_max_usd            numeric(14,2),
  overall_avg_usd            numeric(14,2),
  overall_median_per_m2_usd  numeric(14,2),
  avg_price_per_m2           numeric(14,2),
  median_price_per_m2        numeric(14,2),
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index artists_normalized_name_idx on artists(normalized_name);
create index artists_display_name_idx on artists(display_name);

-- ============ EXHIBITIONS ===================================================
create table exhibitions (
  id                  bigint generated always as identity primary key,
  drive_path          text unique,
  source_bucket       text,
  code                text,
  event_type          text,
  date_token          text,
  start_date          date,
  city                text,
  title               text,
  artists_text        text,
  organizer           text,
  venue               text,
  online_status       text,
  artwork_count       integer,
  metadata_json       jsonb,
  venue_segments_json jsonb,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
create index exhibitions_start_date_idx on exhibitions(start_date);
create index exhibitions_venue_idx on exhibitions(venue);

-- ============ EXHIBITION_ARTISTS (M:N junction) =============================
create table exhibition_artists (
  exhibition_id bigint references exhibitions(id) on delete cascade,
  artist_id     bigint references artists(id) on delete cascade,
  primary key (exhibition_id, artist_id)
);

-- ============ SOURCE FILES (raw uploaded catalogs/price files) ==============
create table source_files (
  id                  bigint generated always as identity primary key,
  exhibition_id       bigint references exhibitions(id) on delete set null,
  drive_path          text unique,
  filename            text,
  extension           text,
  source_kind         text,
  has_price_hint      boolean default false,
  has_catalogue_hint  boolean default false,
  imported_at         timestamptz default now()
);

-- ============ PRICE OBSERVATIONS (from gallery price files) =================
create table price_observations (
  id              bigint generated always as identity primary key,
  artist_id       bigint references artists(id) on delete set null,
  exhibition_id   bigint references exhibitions(id) on delete cascade,
  source_file_id  bigint references source_files(id) on delete set null,
  artwork_title   text,
  medium          text,
  dimensions      text,
  width_cm        numeric(8,2),
  height_cm       numeric(8,2),
  area_m2         numeric(10,4),
  year            text,
  price_amount    numeric(14,2),
  currency        text,
  price_per_m2    numeric(14,2),
  status          text,
  raw_row_json    jsonb,
  confidence      numeric(3,2) default 0.4,
  observed_at     timestamptz default now()
);
create index price_observations_artist_idx on price_observations(artist_id);
create index price_observations_exhibition_idx on price_observations(exhibition_id);

-- ============ SALE RESULTS (auction lot results — core data) ================
create table sale_results (
  id                      bigint generated always as identity primary key,
  source                  text not null,
  source_url              text unique not null,
  sale_page_url           text,
  lot_number              text,
  auction_title           text,
  sale_date               date,
  sale_location           text,
  artist_id               bigint references artists(id) on delete set null,
  artist_name_raw         text,
  artwork_title           text,
  medium                  text,
  dimensions              text,
  width_cm                numeric(8,2),
  height_cm               numeric(8,2),
  area_m2                 numeric(10,4),
  year                    text,
  estimate_low            numeric(14,2),
  estimate_high           numeric(14,2),
  hammer_price            numeric(14,2),
  price_with_premium      numeric(14,2),
  currency                text,
  price_usd               numeric(14,2),
  price_with_premium_usd  numeric(14,2),
  price_per_m2_usd        numeric(14,2),
  status                  sale_status default 'sold',
  kind                    sale_kind default 'painting',
  support_type            support_type,
  provenance              text,
  raw_snapshot            jsonb,
  scraped_at              timestamptz default now(),
  created_at              timestamptz default now(),
  updated_at              timestamptz default now()
);
create index sale_results_artist_idx on sale_results(artist_id);
create index sale_results_source_idx on sale_results(source);
create index sale_results_sale_date_idx on sale_results(sale_date);
create index sale_results_status_idx on sale_results(status);
create index sale_results_kind_idx on sale_results(kind);
create index sale_results_support_idx on sale_results(support_type);
create index sale_results_artist_support_idx
  on sale_results(artist_id, support_type)
  where price_per_m2_usd is not null;

-- ============ UPCOMING AUCTIONS (scheduled sale-level events) ===============
create table upcoming_auctions (
  id              bigint generated always as identity primary key,
  source          text not null,
  sale_page_url   text unique,
  auction_title   text,
  sale_date       date,
  sale_location   text,
  expected_lots   integer,
  scraped_at      timestamptz default now()
);

-- ============ CRAWL RUNS (scraper execution audit log) ======================
create table crawl_runs (
  id              bigint generated always as identity primary key,
  source          text not null,
  target_slug     text,
  started_at      timestamptz,
  finished_at     timestamptz,
  lots_scanned    integer default 0,
  lots_inserted   integer default 0,
  sale_date_min   date,
  sale_date_max   date,
  status          text,
  note            text
);
create index crawl_runs_source_started_idx on crawl_runs(source, started_at desc);

-- ============ IMPORTS (raw file import log) =================================
create table imports (
  id          bigint generated always as identity primary key,
  source      text,
  detail      text,
  status      text,
  count       integer,
  created_at  timestamptz default now()
);

-- ============ updated_at TRIGGER ============================================
create or replace function update_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger artists_updated_at
  before update on artists
  for each row execute function update_updated_at();

create trigger exhibitions_updated_at
  before update on exhibitions
  for each row execute function update_updated_at();

create trigger sale_results_updated_at
  before update on sale_results
  for each row execute function update_updated_at();

-- ============ ROW LEVEL SECURITY (RLS) ======================================
-- MVP: public read on everything (anyone with anon key can SELECT).
-- service_role key bypasses RLS automatically (used by Next.js server + crawlers).
-- Future: add per-user policies for paid tier / private collections.

alter table artists enable row level security;
alter table exhibitions enable row level security;
alter table exhibition_artists enable row level security;
alter table source_files enable row level security;
alter table price_observations enable row level security;
alter table sale_results enable row level security;
alter table upcoming_auctions enable row level security;
alter table crawl_runs enable row level security;
alter table imports enable row level security;

-- Public read policy for browsing data
create policy "Public read artists" on artists
  for select using (true);
create policy "Public read exhibitions" on exhibitions
  for select using (true);
create policy "Public read exhibition_artists" on exhibition_artists
  for select using (true);
create policy "Public read source_files" on source_files
  for select using (true);
create policy "Public read price_observations" on price_observations
  for select using (true);
create policy "Public read sale_results" on sale_results
  for select using (true);
create policy "Public read upcoming_auctions" on upcoming_auctions
  for select using (true);
-- crawl_runs + imports: NO public read (audit logs, server-only)

-- ============ HELPER VIEWS (for fast UI queries) ============================

-- Median $/m² per artist (used by suspicious-lot detector, artist detail)
create or replace view artist_median_per_support as
with ranked as (
  select artist_id, support_type, price_per_m2_usd,
         row_number() over (partition by artist_id, support_type order by price_per_m2_usd) as rn,
         count(*) over (partition by artist_id, support_type) as cnt
  from sale_results
  where kind = 'painting'
    and price_per_m2_usd > 0
    and price_per_m2_usd < 50000000
    and support_type is not null
    and artist_id is not null
)
select artist_id, support_type, cnt as n,
       avg(case when rn = (cnt+1)/2 or rn = cnt/2+1 then price_per_m2_usd end) as median_ppm
from ranked
where cnt >= 3
group by artist_id, support_type, cnt;
