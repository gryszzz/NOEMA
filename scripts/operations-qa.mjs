import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtemp, rm, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';
const folder = await mkdtemp(join(tmpdir(), 'noema-ops-qa-')), db = join(folder, 'fixture.db');
const python = process.env.PYTHON ?? 'python';
execFileSync(python, ['-c', `import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
 c.execute('CREATE TABLE research_queue(task_id,market_id,request,priority,status,created_at)')
 c.execute('INSERT INTO research_queue VALUES(?,?,?,?,?,?)', ('test-1','TEST-MARKET','<img src=x onerror=alert(1)>',1,'pending','2026-09-27T00:00:00+00:00'))
 c.execute('CREATE TABLE ecosystem_specialists(name,family,state,resolved,reliability,calibration_error,after_cost_return,drawdown_fraction,updated_at)')
 c.execute('INSERT INTO ecosystem_specialists VALUES(?,?,?,?,?,?,?,?,?)', ('Test specialist','fixture','shadow',0,0,None,None,None,'2026-09-27T00:00:00+00:00'))
 c.execute('CREATE TABLE research_trials(trial_id,family)')
 c.execute('INSERT INTO research_trials VALUES(?,?)', ('prediction-fixture','prediction_markets'))
 c.execute('CREATE TABLE autonomous_research_runs(id,trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,completed_at,elapsed_seconds,compute_cost_usd,result_json,evidence_path,mission_id)')
 c.execute('INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (1,'prediction-fixture','kalshi-history','market_data_quality','b'*64,'fixture-v1','completed','2026-09-27T00:00:01+00:00','2026-09-27T00:00:04+00:00',3,None,'{"observations":18,"critic_review":{"result_accepted":true,"verdict":"PASS"}}',None,'mission-fixture'))
 c.execute('CREATE TABLE missions(mission_id,trial_id,evidence_hash,session_id,run_id,objective,status,specialist,capability_grants_json,resource_grant_json,result_json,lesson_id,created_at,updated_at,completed_at)')
 c.execute('INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('mission-fixture','trial-fixture','a'*64,'session-fixture',1,'Inspect a bounded fixture opportunity','completed','Test specialist','["read_frozen_evidence"]','{"network":"denied","live_execution":false}','{"critic_review":{"verdict":"PASS"}}',1,'2026-09-27T00:00:00+00:00','2026-09-27T00:00:05+00:00','2026-09-27T00:00:05+00:00'))
 c.execute('CREATE TABLE mission_events(id,mission_id,created_at,actor,event_type,status,detail,payload_json)')
 c.execute('INSERT INTO mission_events VALUES(?,?,?,?,?,?,?,?)', (0,'mission-fixture','2026-09-27T00:00:00+00:00','NOEMA','opportunity_discovered','discovered','Bounded opportunity entered observation','{}'))
 c.execute('INSERT INTO mission_events VALUES(?,?,?,?,?,?,?,?)', (1,'mission-fixture','2026-09-27T00:00:05+00:00','evidence-critic','critic_evaluation','completed','Evidence critic passed integrity checks','{}'))
 c.execute('CREATE TABLE mission_handoffs(handoff_id,mission_id,created_at,updated_at,from_specialist,to_specialist,objective,status,capability_grants_json,resource_grant_json,result_json)')
 c.execute('INSERT INTO mission_handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('handoff-fixture','mission-fixture','2026-09-27T00:00:04+00:00','2026-09-27T00:00:05+00:00','Test specialist','evidence-critic','Check result integrity','completed','["read_result"]','{"network":"denied"}','{"verdict":"PASS"}'))
`, db]);
const port = await new Promise(resolve => { const s = createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
const server = spawn(python, ['-m', 'uvicorn', 'noema.dashboard_app:app', '--host', '127.0.0.1', '--port', String(port)], { env: {...process.env, NOEMA_DB_PATH: db}, stdio: 'ignore' });
const url = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({headless:true});
try {
 for(let i=0;i<100;i++){try{if((await fetch(url)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
 for(const width of [390,768,1024,1440]){
  const page = await browser.newPage({viewport:{width,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url); await page.getByRole('heading',{name:'NOEMA OPERATING WORLD'}).waitFor();
  await page.locator('#lane-rows').getByText('Prediction markets',{exact:true}).waitFor();
  await page.locator('#inactive-lanes').getByText('SaaS / micro-SaaS',{exact:true}).waitFor({state:'attached'});
  assert.equal(await page.locator('#metric-revenue').innerText(),'Unknown');
  assert.equal(await page.locator('#metric-net').innerText(),'Unknown');
  assert.ok((await page.locator('#lane-rows').innerText()).includes('revenue unmeasured'));
  await page.locator('#roster-list').getByText('Test specialist',{exact:true}).waitFor();
  assert.equal(await page.locator('#metric-cash').innerText(),'Unknown');
  assert.equal(await page.locator('#metric-paper').innerText(),'Unknown');
  assert.equal(await page.locator('#lead-state').innerText(),'UNKNOWN');
  assert.equal(await page.locator('#focus-title').innerText(),'Inspect a bounded fixture opportunity');
  await page.locator('#mission-list').getByText('result PASS',{exact:false}).waitFor();
  await page.locator('#mission-list').getByText('Test specialist · lane unknown · result PASS · Test specialist → evidence-critic',{exact:false}).waitFor();
  await page.getByRole('button',{name:/MISSION · mission-fixtur/}).click();
  await page.locator('#evidence-chart').getByRole('button',{name:/18 observations/}).waitFor();
  assert.equal(await page.locator('#evidence-total').innerText(),'18');
  assert.equal(await page.locator('#evidence-chart .chart-bar').evaluateAll(nodes=>nodes.every(node=>Number(node.getAttribute('y'))>=0)),true,'evidence bars stay inside the plotted area');
  assert.ok((await page.locator('#event-trace').innerText()).includes('Evidence critic passed integrity checks'));
  assert.equal(await page.locator('#world-entity-list button').count() >= 2,true,'world map projects persisted NOEMA and mission entities');
  assert.ok((await page.locator('#world-map-state').innerText()).includes('exact-ID links'));
  assert.ok(await page.locator('#world-map-canvas').evaluate(canvas => {
    const ctx=canvas.getContext('2d'), pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    let changed=0; for(let i=0;i<pixels.length;i+=4) if(pixels[i]>45||pixels[i+1]>55||pixels[i+2]>65) changed++;
    return changed>100;
  }), 'spatial map renders its real entity and relationship geometry');
  await page.locator('#world-map-canvas').scrollIntoViewIfNeeded();
  const mapBefore=await page.locator('#world-map-canvas').evaluate(canvas=>canvas.toDataURL());
  const mapBox=await page.locator('#world-map-canvas').boundingBox();
  const dragX=mapBox.x+mapBox.width*.1, dragY=mapBox.y+mapBox.height*.4;
  await page.mouse.move(dragX,dragY);
  await page.mouse.down(); await page.mouse.move(dragX+42,dragY+12,{steps:3}); await page.mouse.up();
  const mapMoved=await page.locator('#world-map-canvas').evaluate((canvas,before)=>canvas.toDataURL()!==before,mapBefore);
  assert.ok(mapMoved,`drag orbits the world camera at ${width}px`);
  await page.locator('#world-event-list button').first().click();
  assert.ok((await page.locator('#world-inspector-title').innerText()).includes('opportunity discovered'));
  assert.ok((await page.locator('#world-entity-list').innerText()).includes('MISSION · Inspect a bounded fixture opportunity · discovered'), 'historical replay must not reveal the later completed mission status');
  assert.equal(await page.locator('#world-entity-list').getByText(/EXPERIMENT/).count(),0,'experiment created after the selected event is hidden from the earlier view');
  await page.locator('#world-time-live').click();
  assert.ok((await page.locator('#world-entity-list').innerText()).includes('MISSION · Inspect a bounded fixture opportunity · completed'));
  assert.ok((await page.locator('#support-worker').innerText()).startsWith('OpenClaw ·'));
  await page.locator('#deep-records').locator('summary').click();
  await page.getByRole('button',{name:'Research queue',exact:true}).click();
  await page.getByRole('button',{name:'TEST-MARKET',exact:true}).click();
  assert.ok((await page.locator('#detail').innerText()).includes('<img src=x'));
  assert.equal(await page.locator('#detail img').count(),0);
  await page.getByRole('button',{name:'Specialists',exact:true}).click();
  await page.getByRole('button',{name:'Test specialist',exact:true}).click();
  await page.getByRole('button',{name:'Back to previous record',exact:true}).click();
  assert.equal((await page.locator('#title').innerText()).toLowerCase(),'research queue');
  await page.getByLabel('Filter loaded records').fill('no match');
  assert.equal(await page.locator('#rows tr').count(),0);
  await page.getByLabel('Filter loaded records').fill('');
  for(const button of await page.locator('#views button').all()) await button.click();
  assert.equal(await page.locator('header a[href*="github"]').count(),0);
  const overflow = await page.evaluate(() => ({
   scrollWidth: document.documentElement.scrollWidth,
   elements: [...document.querySelectorAll('body *')].map(node => ({
    tag: node.tagName, id: node.id, className: typeof node.className === 'string' ? node.className : '',
    right: Math.round(node.getBoundingClientRect().right), width: Math.round(node.getBoundingClientRect().width),
    parentWidth: Math.round(node.parentElement?.getBoundingClientRect().width ?? 0),
    parentTag: node.parentElement?.tagName, parentId: node.parentElement?.id,
    parentClass: typeof node.parentElement?.className === 'string' ? node.parentElement.className : '',
    computed: getComputedStyle(node).width, maxWidth: getComputedStyle(node).maxWidth,
   })).filter(item => item.right > innerWidth + 1).slice(0, 12),
  }));
  assert.equal(overflow.scrollWidth>width,false,`overflow at ${width}: ${JSON.stringify(overflow)}`);
  if(process.env.QA_SCREENSHOTS){await mkdir(process.env.QA_SCREENSHOTS,{recursive:true});await page.screenshot({path:join(process.env.QA_SCREENSHOTS,`noema-operations-${width}.png`)});}
  await page.route('**/api/operations',route=>route.fulfill({status:503,body:'unavailable'}));
  await page.getByRole('button',{name:'Refresh live state',exact:true}).click();
  await page.getByRole('alert').waitFor(); assert.ok((await page.getByRole('alert').innerText()).includes('last successful snapshot'));
  assert.equal(await page.locator('#detail img').count(),0);
  assert.deepEqual(errors,[]);
  await page.close();
 }
 console.log('Operational console: four viewports, real SQLite/API, record drilldown/backtracking, inert untrusted text, filters and stale retention passed.');
} finally {await browser.close();server.kill();await rm(folder,{recursive:true,force:true});}
