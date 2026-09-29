// Lightweight navigable spatial view. All vertices and edges are projected from
// the same bounded /api/operations snapshot rendered by the workstation.
const esc = (value) => String(value ?? 'Unknown');
const palette = { core: '#e8b977', mission: '#8bcbb2', agent: '#8ab7d7', experiment: '#d1a7df', lesson: '#b18ec7', session: '#8998a0', evidence: '#dd9f6e', market: '#67b7bb', forecast: '#b6a6dc' };

function stamp(value) {
  const n = Date.parse(value ?? '');
  return Number.isFinite(n) ? n : 0;
}

// forecast_ledger.created_at uses SQLite CURRENT_TIMESTAMP, which is UTC but lacks a suffix.
function forecastStamp(value) {
  if (typeof value !== 'string') return 0;
  return stamp(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value.replace(' ', 'T')}Z`);
}

function buildModel(snapshot, cutoff = Infinity) {
  const sections = snapshot?.sections ?? {};
  const rows = (key) => sections[key]?.rows ?? [];
  const replaying = Number.isFinite(cutoff);
  const missions = rows('missions').filter((m) => stamp(m.created_at) <= cutoff && (replaying || m.status !== 'superseded'));
  const events = rows('mission_events').filter((e) => stamp(e.created_at) <= cutoff);
  const handoffs = rows('handoffs').filter((h) => stamp(h.created_at) <= cutoff);
  const runs = rows('research_runs').filter((r) => stamp(r.created_at) <= cutoff);
  const sessions = rows('sessions').filter((s) => stamp(s.created_at) <= cutoff);
  const decisions = rows('decisions').slice(0, 12).filter((d) => {
    const at = forecastStamp(d.created_at);
    return at > 0 && at <= cutoff;
  });
  const nodes = [{ id: 'agent:NOEMA', type: 'core', label: 'NOEMA', status: replaying ? 'system identity' : snapshot?.runtime?.state ?? 'unknown', created: 0 }];
  const edges = [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const put = (node) => {
    if (!node.id || byId.has(node.id)) return;
    byId.set(node.id, node); nodes.push(node);
  };
  const specialistNames = new Set();
  if (replaying) {
    for (const event of events) if (event.actor && event.actor !== 'NOEMA') specialistNames.add(event.actor);
  } else {
    for (const mission of missions) if (mission.specialist) specialistNames.add(mission.specialist);
  }
  for (const handoff of handoffs) {
    if (handoff.from_specialist) specialistNames.add(handoff.from_specialist);
    if (handoff.to_specialist) specialistNames.add(handoff.to_specialist);
  }
  for (const decision of decisions) {
    const marketId = `${decision.venue}:${decision.market_id}`;
    const marketKey = `market:${marketId}`;
    const forecastKey = `forecast:${decision.id}`;
    const at = forecastStamp(decision.created_at);
    put({ id: marketKey, type: 'market', label: `${decision.venue} · ${decision.market_id.slice(-12)}`,
      status: 'forecast ledger reference', created: at, record: { venue: decision.venue, market_id: decision.market_id, created_at: decision.created_at } });
    put({ id: forecastKey, type: 'forecast', label: `${decision.decision?.toUpperCase() ?? 'RECORDED'} · ${decision.market_id.slice(-12)}`,
      status: decision.decision ?? 'recorded', created: at, record: decision });
    edges.push({ from: marketKey, to: forecastKey, type: 'venue + market id', at });
    edges.push({ from: 'agent:NOEMA', to: forecastKey, type: 'immutable forecast record', at });
  }
  for (const name of specialistNames) {
    const assignment = replaying ? null : missions.find((mission) => mission.specialist === name && ['claimed', 'running', 'waiting'].includes(mission.status));
    const status = replaying ? 'historical participant' : assignment?.status ?? 'idle · no open mission';
    put({ id: `agent:${name}`, type: 'agent', label: name, status, created: Infinity });
  }
  for (const mission of missions) {
    const id = `mission:${mission.mission_id}`;
    const lineage = events.filter((event) => event.mission_id === mission.mission_id)
      .sort((a, b) => stamp(a.created_at) - stamp(b.created_at) || Number(a.id) - Number(b.id));
    const knownTransition = lineage.at(-1);
    const missionStatus = replaying ? knownTransition?.status ?? 'discovered' : mission.status;
    const missionAgent = replaying ? lineage.find((event) => event.event_type === 'claimed')?.actor ?? null : mission.specialist;
    const safeMission = replaying ? { ...mission, status: missionStatus, specialist: missionAgent,
      updated_at: mission.created_at, result: null, measurement: null, completed_at: null,
      lesson_id: rows('lessons').some((lesson) => lesson.id === mission.lesson_id && stamp(lesson.created_at) <= cutoff) ? mission.lesson_id : null } : mission;
    put({ id, type: 'mission', label: mission.objective ?? mission.mission_id, status: missionStatus,
      created: stamp(mission.created_at), missionId: mission.mission_id, record: safeMission });
    if (missionAgent) edges.push({ from: `agent:${missionAgent}`, to: id, type: 'assigned specialist', at: stamp(mission.created_at), missionId: mission.mission_id });
    if (lineage.some((event) => event.actor === 'NOEMA' && event.event_type === 'opportunity_discovered')) {
      edges.push({ from: 'agent:NOEMA', to: id, type: 'opportunity discovered', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.evidence_hash) {
      const evidenceId = `evidence:${mission.evidence_hash}`;
      put({ id: evidenceId, type: 'evidence', label: `Frozen evidence · ${mission.evidence_hash.slice(0, 12)}`,
        status: 'hash linked', created: stamp(mission.created_at), missionId: mission.mission_id,
        record: { mission_id: mission.mission_id, evidence_hash: mission.evidence_hash, created_at: mission.created_at } });
      edges.push({ from: id, to: evidenceId, type: 'frozen evidence hash', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.session_id && sessions.some((s) => s.session_id === mission.session_id)) {
      const session = sessions.find((s) => s.session_id === mission.session_id);
      const sid = `session:${session.session_id}`;
      const sessionStatus = replaying && session.completed_at && stamp(session.completed_at) > cutoff ? 'running' : session.status;
      const safeSession = replaying ? { ...session, status: sessionStatus, completed_at: sessionStatus === 'running' ? null : session.completed_at, result: null } : session;
      put({ id: sid, type: 'session', label: `Session ${session.session_id.slice(0, 8)}`, status: sessionStatus,
        created: stamp(session.created_at), missionId: mission.mission_id, record: safeSession });
      edges.push({ from: id, to: sid, type: 'exact session id', at: stamp(mission.created_at), missionId: mission.mission_id });
    }
    if (mission.trial_id && runs.some((r) => r.trial_id === mission.trial_id)) {
      const run = runs.find((r) => r.trial_id === mission.trial_id);
      const rid = `experiment:${mission.trial_id}`;
      const runStatus = replaying && run.completed_at && stamp(run.completed_at) > cutoff ? 'running' : run.status;
      const safeRun = replaying ? { ...run, status: runStatus, completed_at: runStatus === 'running' ? null : run.completed_at, result: null } : run;
      put({ id: rid, type: 'experiment', label: `${run.kind} · ${mission.trial_id.slice(0, 8)}`, status: runStatus,
        created: stamp(run.created_at), missionId: mission.mission_id, record: safeRun });
      edges.push({ from: id, to: rid, type: 'exact trial id', at: stamp(run.created_at), missionId: mission.mission_id });
    }
    if (mission.lesson_id) {
      const lesson = rows('lessons').find((item) => item.id === mission.lesson_id);
      if (lesson && stamp(lesson.created_at) <= cutoff) {
        const lid = `lesson:${lesson.id}`;
        put({ id: lid, type: 'lesson', label: `Lesson ${lesson.id}`, status: 'persisted', created: stamp(lesson.created_at), missionId: mission.mission_id, record: lesson });
        edges.push({ from: id, to: lid, type: 'lesson recorded', at: stamp(lesson.created_at), missionId: mission.mission_id });
      }
    }
  }
  for (const handoff of handoffs) {
    const mission = missions.find((m) => m.mission_id === handoff.mission_id);
    if (!mission) continue;
    const handoffStatus = replaying && stamp(handoff.updated_at) > cutoff ? 'requested' : handoff.status;
    edges.push({ from: `agent:${handoff.from_specialist}`, to: `agent:${handoff.to_specialist}`,
      type: `handoff · ${handoffStatus}`, at: stamp(handoff.created_at), missionId: handoff.mission_id });
  }
  const timeline = [
    ...events.map((event) => ({ id: `mission-event:${event.id}`, at: stamp(event.created_at), time: event.created_at,
      title: event.event_type.replaceAll('_', ' '), detail: event.detail, status: event.status,
      actor: event.actor, missionId: event.mission_id, kind: 'MISSION EVENT', source: event })),
    ...rows('activity').map((event) => ({ id: `runtime-event:${event.id}`, at: stamp(event.created_at), time: event.created_at,
      title: `${event.stage} · ${event.status}`, detail: event.detail, status: event.status,
      actor: event.tool ?? 'NOEMA', missionId: event.mission_id ?? null, kind: 'RUNTIME EVENT', source: event })),
    ...decisions.map((decision) => ({ id: `forecast-event:${decision.id}`, at: forecastStamp(decision.created_at), time: decision.created_at,
      title: `forecast · ${decision.venue} · ${decision.market_id}`, detail: `${decision.decision ?? 'recorded'} · ${decision.model_version ?? 'model unknown'} · p=${decision.probability_yes ?? 'unknown'}`,
      status: decision.decision ?? 'recorded', actor: decision.model_version ?? 'NOEMA', missionId: null, kind: 'FORECAST LEDGER', source: decision })),
  ].filter((e) => e.at && e.at <= cutoff).sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
  return { nodes, edges: edges.filter((e) => byId.has(e.from) && byId.has(e.to) && (!e.at || e.at <= cutoff)), timeline };
}

function project(node, width, height, camera) {
  let x = node.x, y = node.y, z = node.z;
  const cy = Math.cos(camera.yaw), sy = Math.sin(camera.yaw), cp = Math.cos(camera.pitch), sp = Math.sin(camera.pitch);
  let rx = x * cy - z * sy, rz = x * sy + z * cy;
  let ry = y * cp - rz * sp; rz = y * sp + rz * cp;
  rx = (rx + camera.panX) * camera.zoom; ry = (ry + camera.panY) * camera.zoom; rz *= camera.zoom;
  const scale = 560 / Math.max(140, 620 - rz);
  return { x: width / 2 + rx * scale, y: height / 2 - ry * scale, scale };
}

export function createNoemaWorld() {
  const canvas = document.getElementById('world-map-canvas');
  if (!canvas) return { update() {} };
  const ctx = canvas.getContext('2d', { alpha: false });
  if (!ctx) return { update() {} };
  const controls = {
    range: document.getElementById('world-time-range'),
    list: document.getElementById('world-entity-list'),
    camera: { yaw: -.22, pitch: .16, zoom: 1, panX: 0, panY: 0 },
  };
  let source = null, nodes = [], edges = [], timeline = [], selectedId = 'agent:NOEMA', focusNodeId = 'agent:NOEMA', replayIndex = null, dpr = 1;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const setInspector = (node) => {
    if (!node) return;
    selectedId = node.id;
    focusNodeId = node.missionId ? `mission:${node.missionId}` : node.id;
    document.getElementById('world-inspector-kind').textContent = node.type.toUpperCase();
    document.getElementById('world-inspector-title').textContent = node.label;
    const mission = node.record?.mission_id ?? node.missionId;
    const time = node.record?.updated_at ?? node.record?.created_at;
    const timeAt = ['market', 'forecast'].includes(node.type) ? forecastStamp(time) : stamp(time);
    document.getElementById('world-inspector-detail').textContent = [
      `State · ${node.status ?? 'unknown'}`, mission ? `Mission · ${mission}` : null,
      timeAt ? `Recorded · ${new Date(timeAt).toLocaleString()}` : time ? 'Recorded · time unavailable' : 'Recorded entity',
      node.record?.evidence_hash ? `Evidence · ${node.record.evidence_hash}` : null,
    ].filter(Boolean).join(' · ');
    controls.list.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.nodeId === node.id)));
    draw();
  };
  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  function draw() {
    const rect = canvas.getBoundingClientRect(); if (!rect.width || !rect.height) return;
    const width = rect.width, height = rect.height;
    const core = nodes.find((node) => node.type === 'core');
    const rest = nodes.filter((node) => node !== core);
    for (let i = 0; i < rest.length; i++) {
      const node = rest[i], ring = ({ agent: 140, mission: 230, market: 320, experiment: 365,
        session: 400, forecast: 445, evidence: 485, lesson: 520 }[node.type] ?? 350);
      const same = rest.slice(0, i).filter((item) => item.type === node.type).length;
      const typeCount = Math.max(1, rest.filter((item) => item.type === node.type).length);
      const angle = same / typeCount * Math.PI * 2 - Math.PI / 2;
      node.x = Math.cos(angle) * ring; node.y = Math.sin(angle) * ring * .48;
      node.z = ((same % 3) - 1) * 105 + (node.type === 'mission' ? 20 : 0);
    }
    if (core) { core.x = 0; core.y = 0; core.z = 0; }
    const positions = new Map(nodes.map((node) => [node.id, project(node, width, height, controls.camera)]));
    const sorted = [...nodes].sort((a, b) => (positions.get(a.id)?.scale ?? 0) - (positions.get(b.id)?.scale ?? 0));
    for (const node of sorted) node.screen = positions.get(node.id);
    const activeEdges = edges.filter((edge) => edge.from === focusNodeId || edge.to === focusNodeId);
    const neighborhood = new Set([focusNodeId, ...activeEdges.map((edge) => edge.from), ...activeEdges.map((edge) => edge.to)]);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#0b1217'; ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = '#1a2930'; ctx.lineWidth = 1;
    for (let x = (width / 2) % 50; x < width; x += 50) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); }
    for (let y = (height / 2) % 50; y < height; y += 50) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
    for (const edge of edges) {
      const from = positions.get(edge.from), to = positions.get(edge.to); if (!from || !to) continue;
      ctx.beginPath(); ctx.moveTo(from.x, from.y); ctx.lineTo(to.x, to.y);
      const active = activeEdges.includes(edge);
      ctx.strokeStyle = active ? (edge.type.startsWith('handoff') ? '#bc8ad4dd' : '#8bcbb2cc') : '#54706b22';
      ctx.lineWidth = active ? 1.4 : 1; ctx.stroke();
    }
    for (const node of sorted) {
      const p = positions.get(node.id); if (!p || p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) continue;
      const color = palette[node.type] ?? '#91a3aa', radius = (node.type === 'core' ? 13 : node.type === 'mission' ? 8 : 6) * Math.min(1.8, p.scale * 1.5);
      ctx.beginPath(); ctx.arc(p.x, p.y, radius + (focusNodeId === node.id ? 4 : 0), 0, Math.PI * 2);
      ctx.strokeStyle = focusNodeId === node.id ? '#f0cc91' : `${color}88`; ctx.lineWidth = 1; ctx.stroke();
      ctx.globalAlpha = neighborhood.has(node.id) ? 1 : .18;
      ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
      ctx.globalAlpha = 1;
      if (!neighborhood.has(node.id)) continue;
      ctx.fillStyle = '#d8e0df'; ctx.font = `${node.type === 'core' ? '11px' : '9px'} ui-monospace, monospace`;
      const label = node.label.length > 34 ? `${node.label.slice(0, 32)}…` : node.label; ctx.fillText(label, p.x + radius + 5, p.y + 3);
    }
    canvas._hitNodes = sorted;
    const state = source?.runtime?.state ?? 'unknown';
    document.getElementById('world-map-state').textContent = `${nodes.length} persisted entities · ${edges.length} exact-ID links · ${replayIndex === null ? `NOEMA ${state}` : 'historical state'}`;
  }
  function setTime(index) {
    replayIndex = index;
    const cutoff = index === null || !timeline.length ? Infinity : timeline[index]?.at ?? Infinity;
    const model = buildModel(source, cutoff); nodes = model.nodes; edges = model.edges;
    document.getElementById('world-time-value').textContent = index === null ? 'Latest known state' : new Date(cutoff).toLocaleString();
    document.getElementById('world-time-count').textContent = `${timeline.length} loaded persisted events · ${index === null ? 'following live state' : `as of event ${index + 1}/${timeline.length}`}`;
    const eventList = document.getElementById('world-event-list'); eventList.replaceChildren();
    for (const [eventIndex, event] of timeline.slice(-12).entries()) {
      const absoluteIndex = timeline.length - Math.min(12, timeline.length) + eventIndex;
      const button = document.createElement('button'); button.type = 'button'; button.dataset.eventIndex = String(absoluteIndex);
      button.setAttribute('aria-pressed', String(index === absoluteIndex));
      const time = new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      button.textContent = `${time} · ${event.actor} · ${event.title}`;
      button.onclick = () => {
        controls.range.value = String(absoluteIndex); setTime(absoluteIndex);
        const missionNode = nodes.find((node) => node.missionId === event.missionId);
        if (missionNode) selectedId = missionNode.id;
        setInspector({ id: event.id, type: event.kind, label: event.title, status: event.status,
          missionId: event.missionId, record: { created_at: event.time, detail: event.detail,
            mission_id: event.missionId, actor: event.actor, status: event.status } });
      };
      eventList.append(button);
    }
    controls.list.replaceChildren();
    for (const node of nodes) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.nodeId = node.id;
      button.setAttribute('aria-pressed', String(node.id === selectedId));
      button.textContent = `${node.type.toUpperCase()} · ${node.label} · ${node.status ?? 'unknown'}`;
      button.onclick = () => setInspector(node); controls.list.append(button);
    }
    const found = nodes.find((node) => node.id === selectedId);
    setInspector(found ?? nodes[0]); draw();
  }
  function update(snapshot) {
    source = snapshot;
    const all = buildModel(source, Infinity);
    timeline = all.timeline;
    controls.range.max = String(Math.max(0, timeline.length - 1));
    if (replayIndex === null) {
      controls.range.value = String(Math.max(0, timeline.length - 1)); setTime(null);
    } else {
      replayIndex = Math.min(replayIndex, Math.max(0, timeline.length - 1));
      controls.range.value = String(replayIndex); setTime(replayIndex);
    }
  }
  let drag = null;
  canvas.addEventListener('pointerdown', (event) => { canvas.setPointerCapture(event.pointerId); drag = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, button: event.button, pan: event.shiftKey || event.button === 2 }; });
  canvas.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y; drag.x = event.clientX; drag.y = event.clientY;
    if (drag.pan) { controls.camera.panX += dx; controls.camera.panY += dy; }
    else { controls.camera.yaw += dx * .006; controls.camera.pitch = Math.max(-1.1, Math.min(1.1, controls.camera.pitch + dy * .004)); }
    draw();
  });
  canvas.addEventListener('pointerup', (event) => {
    if (!drag) return;
    const moved = Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY);
    if (moved < 4 && !drag.pan) {
      const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
      const hit = (canvas._hitNodes ?? []).map((node) => ({ node, p: node.screen }))
        .filter(({ p }) => p && Math.hypot(p.x - x, p.y - y) < 22).sort((a, b) => Math.hypot(a.p.x - x, a.p.y - y) - Math.hypot(b.p.x - x, b.p.y - y))[0];
      if (hit) setInspector(hit.node);
    }
    drag = null;
  });
  canvas.addEventListener('contextmenu', (event) => event.preventDefault());
  canvas.addEventListener('wheel', (event) => { event.preventDefault(); controls.camera.zoom = Math.max(.55, Math.min(1.9, controls.camera.zoom * (event.deltaY > 0 ? .92 : 1.08))); draw(); }, { passive: false });
  controls.range.addEventListener('input', () => setTime(Number(controls.range.value)));
  document.getElementById('world-time-live').onclick = () => { replayIndex = null; update(source); };
  document.getElementById('world-reset').onclick = () => { Object.assign(controls.camera, { yaw: -.22, pitch: .16, zoom: 1, panX: 0, panY: 0 }); draw(); };
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener('resize', resize);
  if (reducedMotion.matches) canvas.dataset.motion = 'reduced';
  return { update };
}
