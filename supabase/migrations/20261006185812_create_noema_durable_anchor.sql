create schema if not exists noema;

create table if not exists noema.mirror_auth (
  token_sha256 text primary key,
  label text not null,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  rotated_at timestamptz
);

create table if not exists noema.mirror_records (
  id bigint generated always as identity primary key,
  stream text not null,
  record_key text not null,
  version_sha256 text not null,
  occurred_at timestamptz,
  payload jsonb not null,
  source_commit text,
  source_host text,
  mirrored_at timestamptz not null default now(),
  unique(stream, record_key, version_sha256)
);
create index if not exists mirror_records_occurred_idx on noema.mirror_records(occurred_at desc nulls last);
create index if not exists mirror_records_stream_key_idx on noema.mirror_records(stream,record_key,mirrored_at desc);

create table if not exists noema.mirror_checkpoints (
  stream text primary key,
  last_cursor text,
  last_mirrored_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb
);

create table if not exists noema.recovery_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  source_commit text,
  source_host text,
  database_sha256 text not null,
  database_bytes bigint not null check(database_bytes >= 0),
  row_counts jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  storage_locator text,
  verified boolean not null default false,
  verification jsonb not null default '{}'::jsonb
);

create table if not exists noema.jobs (
  job_id uuid primary key default gen_random_uuid(),
  kind text not null,
  owner text not null,
  state text not null check(state in ('queued','leased','running','succeeded','failed','cancelled','expired')),
  priority integer not null default 0,
  payload jsonb not null default '{}'::jsonb,
  resource_budget jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  lease_owner text,
  lease_expires_at timestamptz,
  attempts integer not null default 0 check(attempts >= 0),
  max_attempts integer not null default 3 check(max_attempts >= 1),
  result jsonb,
  last_error_class text
);
create table if not exists noema.job_events (
  id bigint generated always as identity primary key,
  job_id uuid not null references noema.jobs(job_id) on delete cascade,
  event_type text not null,
  actor text not null,
  occurred_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);
create index if not exists jobs_sched_idx on noema.jobs(state,priority desc,created_at);
create index if not exists job_events_job_idx on noema.job_events(job_id,occurred_at);

alter table noema.mirror_auth enable row level security;
alter table noema.mirror_records enable row level security;
alter table noema.mirror_checkpoints enable row level security;
alter table noema.recovery_snapshots enable row level security;
alter table noema.jobs enable row level security;
alter table noema.job_events enable row level security;
