alter table noema.mirror_records add column if not exists operation text not null default 'upsert'
  check(operation in ('upsert','tombstone'));
alter table noema.mirror_records add column if not exists source_rowid bigint;
alter table noema.mirror_records add column if not exists source_schema_version text;
alter table noema.mirror_records add column if not exists source_event_id text;

-- The previous worker encoded SQLite rowids in record_key but did not populate
-- the later provenance columns. Preserve those original identities in place;
-- the synthetic event identity is explicitly namespaced as legacy metadata.
update noema.mirror_records
set source_rowid = split_part(record_key, ':', 2)::bigint,
    source_schema_version = coalesce(source_schema_version, 'legacy-unversioned')
where source_rowid is null
  and split_part(record_key, ':', 1) = stream
  and split_part(record_key, ':', 2) ~ '^[1-9][0-9]{0,18}$'
  and split_part(record_key, ':', 3) = ''
  and split_part(record_key, ':', 2)::numeric <= 9223372036854775807;

update noema.mirror_records
set source_event_id = 'legacy:' || id::text,
    source_schema_version = coalesce(source_schema_version, 'legacy-unversioned')
where stream = any(array['agent_runtime','agent_cycle_timings','missions','mission_handoffs',
  'ecosystem_specialists','research_trials','autonomous_research_runs','bill_budget',
  'prediction_account_records','prediction_account_sync_state'])
  and source_event_id is null;

alter table noema.mirror_records drop constraint if exists mirror_records_stream_record_key_version_sha256_key;
create unique index if not exists mirror_records_legacy_content_dedupe
  on noema.mirror_records(stream,record_key,version_sha256) where source_event_id is null;
create unique index if not exists mirror_records_source_event_dedupe
  on noema.mirror_records(stream,source_event_id) where source_event_id is not null;

create or replace function noema.noema_mirror_ingest(p_records jsonb, p_checkpoint jsonb default null)
returns jsonb language plpgsql security definer set search_path = noema, pg_temp as $$
declare rec jsonb; accepted integer := 0; duplicates integer := 0; affected integer;
  checkpoint_missing boolean := false;
  old_epoch text; new_epoch text; baseline_start bigint;
begin
  if jsonb_typeof(p_records) <> 'array' or jsonb_array_length(p_records) > 500 then
    raise exception 'invalid or oversized records';
  end if;
  if p_checkpoint is not null then
    select metadata->>'capture_epoch' into old_epoch from noema.mirror_checkpoints
      where stream=p_checkpoint->>'stream';
    checkpoint_missing := not found;
    new_epoch := p_checkpoint->'metadata'->>'capture_epoch';
    if checkpoint_missing and new_epoch is not null
       and p_checkpoint->>'stream'=any(array['agent_runtime','agent_cycle_timings','missions','mission_handoffs',
         'ecosystem_specialists','research_trials','autonomous_research_runs','bill_budget',
         'prediction_account_records','prediction_account_sync_state']) then
      if exists (
        select 1 from noema.mirror_records
        where stream=p_checkpoint->>'stream'
          and left(source_event_id, length(new_epoch)+1)=new_epoch||':'
      ) then
        raise exception 'mutable checkpoint is missing for an existing capture epoch';
      end if;
      select coalesce(max(id),0) into baseline_start from noema.mirror_records
        where stream=p_checkpoint->>'stream';
    end if;
    if new_epoch is not null and new_epoch is distinct from old_epoch then
      if baseline_start is null then
        select coalesce(max(id),0) into baseline_start from noema.mirror_records
          where stream=p_checkpoint->>'stream';
      end if;
    end if;
  end if;
  for rec in select value from jsonb_array_elements(p_records) loop
    if rec->>'operation' not in ('upsert','tombstone') or rec->>'stream' is null
       or rec->>'record_key' is null or rec->>'version_sha256' is null
       or jsonb_typeof(rec->'payload') <> 'object' then raise exception 'malformed mirror record'; end if;
    if rec->>'stream'=any(array['agent_runtime','agent_cycle_timings','missions','mission_handoffs','ecosystem_specialists',
      'research_trials','autonomous_research_runs','bill_budget','prediction_account_records',
      'prediction_account_sync_state']) and
      length(coalesce(rec->>'source_event_id','')) not between 1 and 160 then
      raise exception 'mutable mirror version has no source event identity';
    end if;
    insert into noema.mirror_records(stream,record_key,version_sha256,occurred_at,payload,source_commit,
      source_host,operation,source_rowid,source_schema_version,source_event_id)
    values(rec->>'stream',rec->>'record_key',rec->>'version_sha256',
      nullif(rec->>'occurred_at','')::timestamptz,rec->'payload',rec->>'source_commit',rec->>'source_host',
      coalesce(rec->>'operation','upsert'),nullif(rec->>'source_rowid','')::bigint,rec->>'source_schema_version',
      rec->>'source_event_id')
    on conflict do nothing;
    get diagnostics affected = row_count;
    if affected=1 then accepted := accepted+1; else duplicates := duplicates+1; end if;
  end loop;
  if p_checkpoint is not null then
    insert into noema.mirror_checkpoints(stream,last_cursor,metadata,last_mirrored_at)
    values(p_checkpoint->>'stream',p_checkpoint->>'last_cursor',
      coalesce(p_checkpoint->'metadata','{}'::jsonb) ||
        case when baseline_start is not null then jsonb_build_object('restore_floor_id',baseline_start) else '{}'::jsonb end,
      now())
    on conflict(stream) do update set last_cursor=excluded.last_cursor,
      metadata=excluded.metadata||jsonb_build_object('restore_floor_id',
        coalesce((excluded.metadata->>'restore_floor_id')::bigint,
          (noema.mirror_checkpoints.metadata->>'restore_floor_id')::bigint,0)),
      last_mirrored_at=excluded.last_mirrored_at;
  end if;
  return jsonb_build_object('status','persisted','accepted',accepted,'duplicates',duplicates);
end $$;

drop function if exists noema.noema_mirror_pull(text[],bigint,integer);
create or replace function noema.noema_mirror_pull(p_streams text[],p_since_id bigint default 0,
  p_limit integer default 500,p_through_id bigint default null)
returns jsonb language sql security definer set search_path = noema, pg_temp as $$
  with page as (
    select id,stream,record_key,version_sha256,occurred_at,payload,payload::text as payload_json,source_commit,source_host,
      mirrored_at,operation,source_rowid,source_schema_version,source_event_id
    from noema.mirror_records where stream=any(p_streams) and id>p_since_id
      and (p_through_id is null or id<=p_through_id)
    order by id limit least(greatest(p_limit,1),1000)
  )
  select jsonb_build_object('records',coalesce(jsonb_agg(to_jsonb(page) order by id),'[]'::jsonb),
    'next_id',coalesce(max(id),p_since_id),
    'has_more',(select count(*)=least(greatest(p_limit,1),1000) from page)) from page
$$;

create or replace function noema.noema_mirror_export_manifest(p_streams text[])
returns jsonb language plpgsql stable security definer set search_path = noema, pg_temp as $$
declare watermark bigint; requested_count integer; stream_name text; cp noema.mirror_checkpoints%rowtype;
  result jsonb := '[]'::jsonb; complete_all boolean := true; floor_id bigint; n bigint; meta jsonb;
begin
  requested_count := coalesce(array_length(p_streams,1),0);
  if requested_count=0 or requested_count>100 then raise exception 'invalid requested stream set'; end if;
  select coalesce(max(id),0) into watermark from noema.mirror_records;
  foreach stream_name in array p_streams loop
    select * into cp from noema.mirror_checkpoints where stream=stream_name;
    if not found then complete_all := false; cp.stream := stream_name; cp.last_cursor := null; cp.metadata := '{}'::jsonb; end if;
    meta := coalesce(cp.metadata,'{}'::jsonb);
    if meta->>'cursor_kind' not in ('rowid','change_event_id')
      or coalesce((meta->>'baseline_complete')::boolean,false)=false
      or nullif(meta->>'local_high_water','') is null
      or nullif(cp.last_cursor,'') is null
      or (meta->>'cursor_kind'='change_event_id' and nullif(meta->>'restore_floor_id','') is null)
      or (cp.last_cursor)::bigint <> (meta->>'local_high_water')::bigint then complete_all := false; end if;
    floor_id := coalesce((meta->>'restore_floor_id')::bigint,0);
    select count(*) into n from noema.mirror_records where stream=stream_name and id<=watermark;
    result := result || jsonb_build_array(jsonb_build_object('name',stream_name,
      'record_count',n,'restore_floor_id',floor_id,
      'checkpoint',jsonb_build_object('last_cursor',cp.last_cursor,'metadata',meta,'last_mirrored_at',cp.last_mirrored_at)));
  end loop;
  return jsonb_build_object('complete',complete_all,'through_cursor',watermark,
    'checkpoint_metadata',jsonb_build_object('generated_at',now(),'stream_count',requested_count),
    'streams',result);
end $$;

revoke all on function noema.noema_mirror_export_manifest(text[]) from public,anon,authenticated;
grant execute on function noema.noema_mirror_export_manifest(text[]) to service_role;
revoke all on function noema.noema_mirror_pull(text[],bigint,integer,bigint) from public,anon,authenticated;
grant execute on function noema.noema_mirror_pull(text[],bigint,integer,bigint) to service_role;
