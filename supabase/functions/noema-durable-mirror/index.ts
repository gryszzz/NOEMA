import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const encoder = new TextEncoder();
const maxBodyBytes = 4 * 1024 * 1024;
const allowedStreams = new Set([
  "agent_runtime", "agent_heartbeats", "agent_cycle_timings", "runtime_events", "missions",
  "mission_events", "mission_handoffs", "ecosystem_specialists", "ecosystem_reviews",
  "research_trials", "autonomous_research_runs", "forecast_ledger", "economic_events",
  "economic_provider_coverage", "economic_reserve_attestations", "economic_counterfactuals",
  "bill_budget", "bill_entries", "prediction_account_records", "prediction_account_sync_state",
]);
const mutableStreams = new Set([
  "agent_runtime", "agent_cycle_timings", "missions", "mission_handoffs", "ecosystem_specialists", "research_trials",
  "autonomous_research_runs", "bill_budget", "prediction_account_records",
  "prediction_account_sync_state",
]);
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json", "cache-control": "no-store" },
});

Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const length = Number(request.headers.get("content-length") || 0);
  if (length > maxBodyBytes) return json({ error: "body_too_large" }, 413);
  const raw = await request.text();
  if (encoder.encode(raw).byteLength > maxBodyBytes) return json({ error: "body_too_large" }, 413);

  const supplied = request.headers.get("x-noema-mirror-token") || "";
  if (supplied.length < 32 || supplied.length > 512) return json({ error: "unauthorized" }, 401);
  const supabaseUrl = Deno.env.get("SUPABASE_URL") || "";
  let secretBag: Record<string, string> = {};
  try {
    secretBag = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
  } catch {
    return json({ error: "service_unavailable" }, 503);
  }
  const serviceRoleKey = secretBag.default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
  if (!supabaseUrl || !serviceRoleKey) return json({ error: "service_unavailable" }, 503);
  const client = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(supplied));
  const tokenHash = [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, "0")).join("");
  const { data: authorized, error: authError } = await client.schema("noema")
    .rpc("noema_mirror_authorize", { p_hash: tokenHash });
  if (authError || authorized !== true) return json({ error: "unauthorized" }, 401);

  let body: Record<string, unknown>;
  try {
    body = JSON.parse(raw);
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("invalid_json");
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const action = body.action;
  if (action === "health") return json({ status: "ready", version: 3 });

  if (action === "checkpoints") {
    const streams = body.streams;
    if (!Array.isArray(streams) || streams.some((s) => typeof s !== "string" || !allowedStreams.has(s))) {
      return json({ error: "invalid_streams" }, 400);
    }
    const { data, error } = await client.schema("noema").rpc("noema_mirror_checkpoints_get", { p_streams: streams });
    return error ? json({ error: "checkpoint_read_failed" }, 503) : json({ status: "ready", checkpoints: data || {} });
  }

  if (action === "ingest") {
    const records = body.records;
    const checkpoint = body.checkpoint;
    if (!Array.isArray(records) || records.length > 500) return json({ error: "invalid_records" }, 400);
    for (const record of records) {
      if (!record || typeof record !== "object" || Array.isArray(record)) return json({ error: "invalid_record" }, 400);
      const item = record as Record<string, unknown>;
      if (typeof item.stream !== "string" || !allowedStreams.has(item.stream)
        || typeof item.record_key !== "string" || item.record_key.length > 1024
        || typeof item.version_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.version_sha256)
        || !item.payload || typeof item.payload !== "object" || Array.isArray(item.payload)
        || !["upsert", "tombstone"].includes(String(item.operation || "upsert"))
        || (mutableStreams.has(String(item.stream))
          && (typeof item.source_event_id !== "string"
            || item.source_event_id.length < 1 || item.source_event_id.length > 160))) {
        return json({ error: "invalid_record" }, 400);
      }
    }
    if (checkpoint && (typeof checkpoint !== "object" || !allowedStreams.has(String((checkpoint as Record<string, unknown>).stream)))) {
      return json({ error: "invalid_checkpoint" }, 400);
    }
    const { data, error } = await client.schema("noema").rpc("noema_mirror_ingest", {
      p_records: records, p_checkpoint: checkpoint || null,
    });
    return error ? json({ error: "ingest_failed" }, 503) : json(data);
  }

  if (action === "pull") {
    const streams = body.streams;
    if (!Array.isArray(streams) || streams.length < 1 || streams.length > allowedStreams.size
      || streams.some((s) => typeof s !== "string" || !allowedStreams.has(s))) return json({ error: "invalid_streams" }, 400);
    const since = Number(body.since_id ?? 0);
    const limit = Number(body.limit ?? 500);
    const through = body.through_id == null ? null : Number(body.through_id);
    if (!Number.isSafeInteger(since) || since < 0 || !Number.isInteger(limit) || limit < 1 || limit > 1000
      || (through !== null && (!Number.isSafeInteger(through) || through < since))) return json({ error: "invalid_cursor" }, 400);
    const { data, error } = await client.schema("noema").rpc("noema_mirror_pull", {
      p_streams: streams, p_since_id: since, p_limit: limit, p_through_id: through,
    });
    return error ? json({ error: "pull_failed" }, 503) : json(data || { records: [], has_more: false, next_id: since });
  }

  if (action === "export_manifest") {
    const streams = body.streams;
    if (!Array.isArray(streams) || streams.length !== allowedStreams.size
      || streams.some((s) => typeof s !== "string" || !allowedStreams.has(s))
      || new Set(streams).size !== allowedStreams.size) return json({ error: "invalid_streams" }, 400);
    const { data, error } = await client.schema("noema").rpc("noema_mirror_export_manifest", { p_streams: streams });
    return error ? json({ error: "manifest_failed" }, 503) : json(data || { complete: false });
  }

  if (action === "snapshot_manifest") {
    const snapshot = body.snapshot;
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return json({ error: "invalid_snapshot" }, 400);
    const { data, error } = await client.schema("noema").rpc("noema_snapshot_manifest", { p_snapshot: snapshot });
    return error ? json({ error: "snapshot_failed" }, 503) : json(data);
  }
  return json({ error: "unsupported_action" }, 400);
});
