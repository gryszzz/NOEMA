grant usage on schema noema to service_role;
grant select,insert,update on noema.mirror_auth,noema.mirror_records,noema.mirror_checkpoints,
  noema.recovery_snapshots,noema.jobs,noema.job_events to service_role;
grant usage,select on all sequences in schema noema to service_role;
