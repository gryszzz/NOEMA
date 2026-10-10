create or replace function noema.noema_mirror_authorize(p_hash text)
returns boolean language sql security definer set search_path = noema, pg_temp as $$
  select exists(select 1 from noema.mirror_auth where token_sha256=p_hash and active)
$$;

create or replace function noema.noema_mirror_ingest(p_records jsonb, p_checkpoint jsonb default null)
returns jsonb language plpgsql security definer set search_path = noema, pg_temp as $$
declare rec jsonb; accepted integer := 0; duplicates integer := 0; affected integer;
begin
  if jsonb_typeof(p_records) <> 'array' or jsonb_array_length(p_records) > 500 then
    raise exception 'invalid or oversized records';
  end if;
  for rec in select value from jsonb_array_elements(p_records) loop
    insert into noema.mirror_records(stream,record_key,version_sha256,occurred_at,payload,source_commit,source_host)
    values(rec->>'stream',rec->>'record_key',rec->>'version_sha256',
      nullif(rec->>'occurred_at','')::timestamptz,rec->'payload',rec->>'source_commit',rec->>'source_host')
    on conflict(stream,record_key,version_sha256) do nothing;
    get diagnostics affected = row_count;
    if affected=1 then accepted := accepted+1; else duplicates := duplicates+1; end if;
  end loop;
  if p_checkpoint is not null then
    insert into noema.mirror_checkpoints(stream,last_cursor,metadata,last_mirrored_at)
    values(p_checkpoint->>'stream',p_checkpoint->>'last_cursor',coalesce(p_checkpoint->'metadata','{}'::jsonb),now())
    on conflict(stream) do update set last_cursor=excluded.last_cursor,
      metadata=excluded.metadata,last_mirrored_at=excluded.last_mirrored_at;
  end if;
  return jsonb_build_object('status','persisted','accepted',accepted,'duplicates',duplicates);
end $$;

create or replace function noema.noema_mirror_checkpoints_get(p_streams text[])
returns jsonb language sql security definer set search_path = noema, pg_temp as $$
  select coalesce(jsonb_object_agg(stream,jsonb_build_object(
    'last_cursor',last_cursor,'metadata',metadata,'last_mirrored_at',last_mirrored_at
  )),'{}'::jsonb) from noema.mirror_checkpoints where stream=any(p_streams)
$$;

create or replace function noema.noema_mirror_pull(p_streams text[],p_since_id bigint default 0,p_limit integer default 500)
returns jsonb language sql security definer set search_path = noema, pg_temp as $$
  with page as (
    select id,stream,record_key,version_sha256,occurred_at,payload,source_commit,source_host,mirrored_at
    from noema.mirror_records where stream=any(p_streams) and id>p_since_id
    order by id limit least(greatest(p_limit,1),1000)
  )
  select jsonb_build_object(
    'records',coalesce(jsonb_agg(to_jsonb(page) order by id),'[]'::jsonb),
    'next_id',coalesce(max(id),p_since_id),
    'has_more',(select count(*)=least(greatest(p_limit,1),1000) from page)
  ) from page
$$;

create or replace function noema.noema_snapshot_manifest(p_snapshot jsonb)
returns jsonb language plpgsql security definer set search_path = noema, pg_temp as $$
declare sid uuid;
begin
  insert into noema.recovery_snapshots(source_commit,source_host,database_sha256,database_bytes,row_counts,storage_locator,verified,verification)
  values(p_snapshot->>'source_commit',p_snapshot->>'source_host',p_snapshot->>'database_sha256',
    (p_snapshot->>'database_bytes')::bigint,coalesce(p_snapshot->'row_counts','{}'::jsonb),
    p_snapshot->>'storage_locator',coalesce((p_snapshot->>'verified')::boolean,false),
    coalesce(p_snapshot->'verification','{}'::jsonb)) returning snapshot_id into sid;
  return jsonb_build_object('snapshot_id',sid);
end $$;

revoke all on all functions in schema noema from public, anon, authenticated;
grant execute on all functions in schema noema to service_role;
