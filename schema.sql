-- Run in the Supabase SQL editor. Existing tables (users, upload_sessions) are kept as-is.
-- The old `predictions` table (gut-risk output) is no longer written or read; drop it when ready:
--   drop table if exists predictions;

create extension if not exists pgcrypto;

create table if not exists glucose_readings (
  id           uuid primary key default gen_random_uuid(),
  user_id      bigint not null,              -- same type as users.id (adjust if uuid)
  device_id    text   not null,
  value        double precision not null check (value between 40 and 600),
  unit         text   not null default 'mg/dL',
  fasting      boolean,                      -- null = unknown
  measured_at  timestamptz not null default now(),
  client_ref   text,                         -- idempotency key from device retries
  created_at   timestamptz not null default now(),
  unique (user_id, client_ref)
);
create index if not exists glucose_readings_user_time on glucose_readings (user_id, measured_at desc);

create table if not exists analysis_reports (
  id               uuid primary key default gen_random_uuid(),
  user_id          bigint not null,
  device_id        text   not null,
  status           text   not null check (status in ('pending','processing','completed','failed')),
  glucose_reading_id uuid references glucose_readings(id),
  glucose_value    double precision,         -- snapshot copies: reports never change later
  glucose_unit     text,
  glucose_source   text check (glucose_source in ('previous','new_test')),
  glucose_measured_at timestamptz,
  fasting          boolean,
  age              int, height_cm int, weight_kg int,   -- profile used for the run
  sample_id        text,
  csv_path         text,                     -- private storage path, never a public URL
  csv_sha256       text,
  features         jsonb,                    -- [{name,value}] matched, in model order
  preprocessing    text,                     -- e.g. "CLR pseudocount=1e-3"
  model_version    text,
  results          jsonb,                    -- {phyto:[...], food:[...]}
  error            text,
  idempotency_key  text,
  created_at       timestamptz not null default now(),
  completed_at     timestamptz,
  unique (user_id, idempotency_key)
);
create index if not exists analysis_reports_user_time on analysis_reports (user_id, created_at desc);

-- All access goes through the FastAPI service key; block direct client access.
alter table glucose_readings enable row level security;
alter table analysis_reports enable row level security;

-- Private bucket for uploaded CSVs (create in Storage UI or:)
insert into storage.buckets (id, name, public) values ('microbiome-csv','microbiome-csv', false)
  on conflict (id) do nothing;
