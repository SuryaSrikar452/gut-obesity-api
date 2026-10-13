-- Run once in the Supabase SQL editor (after schema.sql).
-- Profile entered on the device + one-time QR upload link state.
alter table users
  add column if not exists age int,
  add column if not exists height_cm int,
  add column if not exists weight_kg int,
  add column if not exists upload_code_hash text,          -- sha256 of the QR code (never the code itself)
  add column if not exists upload_code_expires_at timestamptz,
  add column if not exists upload_reading_id uuid,         -- glucose reading chosen on the device
  add column if not exists upload_source text;             -- 'new_test' | 'previous'
create index if not exists users_upload_code_hash on users (upload_code_hash);
