-- KeepGram / Supabase PostgreSQL schema
-- Supabase SQL Editor'da bir marta to'liq ishga tushiring.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  telegram_id bigint unique not null,
  username text,
  first_name text,
  last_name text,
  phone text,
  language_code text,
  is_blocked boolean not null default false,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists storage_channels (
  id uuid primary key default gen_random_uuid(),
  user_id uuid unique not null references users(id) on delete cascade,
  telegram_channel_id bigint unique not null,
  channel_title text,
  channel_username text,
  is_active boolean not null default true,
  linked_at timestamptz not null default now()
);

create table if not exists user_settings (
  user_id uuid primary key references users(id) on delete cascade,
  default_catalog text not null default 'Umumiy',
  index_message_enabled boolean not null default false,
  default_favorite boolean not null default false,
  language text not null default 'uz',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists catalogs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 16 and lower(name) <> 'umumiy'),
  created_at timestamptz not null default now()
);

create unique index if not exists catalogs_user_lower_name_uidx
  on catalogs(user_id, lower(name));

create table if not exists files (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  channel_id uuid not null references storage_channels(id) on delete cascade,
  channel_message_id bigint not null,
  code varchar(6) not null check (code ~ '^[A-HJ-NP-Z2-9]{6}$'),
  title text not null check (char_length(title) between 1 and 180),
  file_type varchar(32) not null,
  catalog text not null default 'Umumiy',
  tags text[] not null default '{}',
  is_favorite boolean not null default false,
  is_missing boolean not null default false,
  telegram_file_unique_id text,
  file_size bigint,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, code),
  unique(channel_id, channel_message_id)
);

create table if not exists channel_link_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid unique not null references users(id) on delete cascade,
  token varchar(32) unique not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);

create table if not exists audit_logs (
  id bigserial primary key,
  actor_type text not null,
  actor_id text,
  action text not null,
  target_type text,
  target_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists files_user_created_idx on files(user_id, created_at desc);
create index if not exists files_user_code_idx on files(user_id, code);
create index if not exists files_user_catalog_idx on files(user_id, catalog);
create index if not exists files_tags_gin_idx on files using gin(tags);
create index if not exists files_title_trgm_idx on files using gin(lower(title) gin_trgm_ops);
create index if not exists users_last_seen_idx on users(last_seen_at desc);
create index if not exists audit_logs_created_idx on audit_logs(created_at desc);
create index if not exists link_tokens_expiry_idx on channel_link_tokens(expires_at);

create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists files_set_updated_at on files;
create trigger files_set_updated_at before update on files
for each row execute function set_updated_at();

drop trigger if exists settings_set_updated_at on user_settings;
create trigger settings_set_updated_at before update on user_settings
for each row execute function set_updated_at();

-- KeepGram browserdan Supabase'ga bevosita ulanmaydi. Faqat server DB ulanishidan foydalanadi.
alter table users enable row level security;
alter table storage_channels enable row level security;
alter table user_settings enable row level security;
alter table catalogs enable row level security;
alter table files enable row level security;
alter table channel_link_tokens enable row level security;
alter table audit_logs enable row level security;

revoke all on table users, storage_channels, user_settings, catalogs, files,
  channel_link_tokens, audit_logs from anon, authenticated;
