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
  display_name text,
  phone text,
  language_code text,
  onboarding_completed boolean not null default false,
  onboarded_at timestamptz,
  terms_accepted_at timestamptz,
  terms_version varchar(16),
  is_blocked boolean not null default false,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

-- Existing KeepGram databases are upgraded idempotently on every app startup.
alter table users add column if not exists display_name text;
alter table users add column if not exists onboarding_completed boolean not null default false;
alter table users add column if not exists onboarded_at timestamptz;
alter table users add column if not exists terms_accepted_at timestamptz;
alter table users add column if not exists terms_version varchar(16);

create table if not exists storage_channels (
  id uuid primary key default gen_random_uuid(),
  user_id uuid unique not null references users(id) on delete cascade,
  telegram_channel_id bigint unique not null,
  channel_title text,
  channel_username text,
  is_active boolean not null default true,
  manifest_message_id bigint,
  manifest_dirty_at timestamptz,
  manifest_updated_at timestamptz,
  linked_at timestamptz not null default now()
);

alter table storage_channels add column if not exists manifest_message_id bigint;
alter table storage_channels add column if not exists manifest_dirty_at timestamptz;
alter table storage_channels add column if not exists manifest_updated_at timestamptz;

create table if not exists user_settings (
  user_id uuid primary key references users(id) on delete cascade,
  default_catalog text not null default 'Umumiy',
  index_message_enabled boolean not null default false,
  default_favorite boolean not null default false,
  auto_manifest_enabled boolean not null default true,
  language text not null default 'uz',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table user_settings add column if not exists auto_manifest_enabled boolean not null default true;

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
  item_count integer not null default 1 check (item_count between 1 and 100),
  file_kinds text[] not null default '{}',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, code),
  unique(channel_id, channel_message_id)
);

alter table files add column if not exists item_count integer not null default 1;
alter table files add column if not exists file_kinds text[] not null default '{}';

update files
set file_kinds = array[
  case
    when file_type = 'photo' then 'image'
    when file_type in ('video', 'animation', 'video_note') then 'video'
    when file_type in ('audio', 'voice') then 'audio'
    when file_type = 'document' and lower(title) ~ '\.(jpg|jpeg|png|webp|heic|heif|bmp|tif|tiff)$' then 'image'
    when file_type = 'document' and lower(title) ~ '\.pdf$' then 'pdf'
    when file_type = 'document' and lower(title) ~ '\.(doc|docx|odt|rtf)$' then 'word'
    when file_type = 'document' and lower(title) ~ '\.(xls|xlsx|xlsm|csv|ods)$' then 'excel'
    when file_type = 'document' then 'other'
    else file_type
  end
]
where cardinality(file_kinds) = 0;

create table if not exists file_parts (
  id uuid primary key default gen_random_uuid(),
  file_id uuid not null references files(id) on delete cascade,
  channel_message_id bigint not null,
  position smallint not null check (position between 0 and 99),
  content_type varchar(32) not null,
  file_kind varchar(32) not null,
  file_name text,
  file_extension varchar(16),
  mime_type text,
  telegram_file_unique_id text,
  file_size bigint,
  created_at timestamptz not null default now(),
  unique(file_id, position),
  unique(file_id, channel_message_id)
);

insert into file_parts(
  file_id,channel_message_id,position,content_type,file_kind,
  file_name,file_extension,mime_type,telegram_file_unique_id,file_size
)
select f.id,f.channel_message_id,0,f.file_type,f.file_kinds[1],
       case when f.file_type='document' then f.title else null end,
       case when f.file_type='document' and f.title like '%.%'
            then left(lower(regexp_replace(f.title, '^.*\.', '')),16) else null end,
       null,f.telegram_file_unique_id,f.file_size
from files f
where not exists (select 1 from file_parts fp where fp.file_id=f.id);

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

create table if not exists app_settings (
  singleton boolean primary key default true check (singleton),
  super_backup_enabled boolean not null default false,
  super_backup_channel_id bigint,
  updated_at timestamptz not null default now()
);

insert into app_settings(singleton) values(true) on conflict(singleton) do nothing;

create table if not exists backup_assets (
  id uuid primary key default gen_random_uuid(),
  file_id uuid references files(id) on delete set null,
  owner_telegram_id bigint not null,
  owner_name text,
  owner_username text,
  version integer not null default 1,
  status varchar(16) not null default 'pending'
    check (status in ('pending','processing','active','deleted','replaced','missing','failed')),
  title text not null,
  code varchar(6) not null,
  file_kinds text[] not null default '{}',
  item_count integer not null default 1,
  source_channel_id bigint not null,
  source_channel_title text,
  source_message_ids bigint[] not null,
  backup_channel_id bigint,
  backup_message_ids bigint[],
  index_message_id bigint,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(file_id, version)
);

alter table backup_assets add column if not exists owner_name text;
alter table backup_assets add column if not exists owner_username text;
alter table backup_assets add column if not exists source_channel_title text;
alter table backup_assets drop constraint if exists backup_assets_status_check;
alter table backup_assets add constraint backup_assets_status_check
  check (status in ('pending','processing','active','deleted','replaced','missing','failed'));

create index if not exists files_user_created_idx on files(user_id, created_at desc);
create index if not exists files_user_code_idx on files(user_id, code);
create index if not exists files_user_catalog_idx on files(user_id, catalog);
create index if not exists files_tags_gin_idx on files using gin(tags);
create index if not exists files_kinds_gin_idx on files using gin(file_kinds);
create index if not exists files_title_trgm_idx on files using gin(lower(title) gin_trgm_ops);
create index if not exists file_parts_file_position_idx on file_parts(file_id, position);
create index if not exists file_parts_unique_id_idx
  on file_parts(telegram_file_unique_id) where telegram_file_unique_id is not null;
create index if not exists users_last_seen_idx on users(last_seen_at desc);
create index if not exists audit_logs_created_idx on audit_logs(created_at desc);
create index if not exists backup_assets_status_created_idx on backup_assets(status, created_at);
create index if not exists backup_assets_owner_idx on backup_assets(owner_telegram_id, created_at desc);
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
alter table file_parts enable row level security;
alter table channel_link_tokens enable row level security;
alter table audit_logs enable row level security;
alter table app_settings enable row level security;
alter table backup_assets enable row level security;

revoke all on table users, storage_channels, user_settings, catalogs, files, file_parts,
  channel_link_tokens, audit_logs from anon, authenticated;
revoke all on table app_settings, backup_assets from anon, authenticated;
