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
 c.execute('CREATE TABLE research_trials(trial_id,family,status,created_at)')
 c.execute('INSERT INTO research_trials VALUES(?,?,?,?)', ('prediction-fixture','prediction_markets','shadow','2026-09-27T00:00:00+00:00'))
 c.execute('CREATE TABLE autonomous_research_runs(id,trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,completed_at,elapsed_seconds,compute_cost_usd,result_json,evidence_path,mission_id)')
 c.execute('INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (1,'prediction-fixture','kalshi-history','market_data_quality','b'*64,'fixture-v1','completed','2026-09-27T00:00:01+00:00','2026-09-27T00:00:04+00:00',3,None,'{"observations":18,"critic_review":{"result_accepted":true,"verdict":"PASS"}}',None,'mission-fixture'))
 c.execute('CREATE TABLE missions(mission_id,trial_id,evidence_hash,session_id,run_id,objective,status,specialist,capability_grants_json,resource_grant_json,result_json,lesson_id,created_at,updated_at,completed_at)')
 c.execute('INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('mission-fixture','trial-fixture','a'*64,'session-fixture',1,'Inspect a bounded fixture opportunity','completed','Test specialist','["read_frozen_evidence"]','{"network":"denied","live_execution":false}','{"critic_review":{"verdict":"PASS"}}',1,'2026-09-27T00:00:00+00:00','2026-09-27T00:00:05+00:00','2026-09-27T00:00:05+00:00'))
 c.execute('CREATE TABLE mission_events(id,mission_id,created_at,actor,event_type,status,detail,payload_json)')
 c.execute('INSERT INTO mission_events VALUES(?,?,?,?,?,?,?,?)', (0,'mission-fixture','2026-09-27T00:00:00+00:00','NOEMA','opportunity_discovered','discovered','Bounded opportunity entered observation','{}'))
 c.execute('INSERT INTO mission_events VALUES(?,?,?,?,?,?,?,?)', (1,'mission-fixture','2026-09-27T00:00:05+00:00','evidence-critic','critic_evaluation','completed','Evidence critic passed integrity checks','{}'))
 c.execute('CREATE TABLE mission_handoffs(handoff_id,mission_id,created_at,updated_at,from_specialist,to_specialist,objective,status,capability_grants_json,resource_grant_json,result_json)')
 c.execute('INSERT INTO mission_handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('handoff-fixture','mission-fixture','2026-09-27T00:00:04+00:00','2026-09-27T00:00:05+00:00','Test specialist','evidence-critic','Check result integrity','completed','["read_result"]','{"network":"denied"}','{"verdict":"PASS"}'))
from datetime import UTC, datetime
from decimal import Decimal
from noema.economic_ledger import EconomicEvent, EconomicLedger
ledger = EconomicLedger(sys.argv[1])
ledger.record_event(EconomicEvent(
 provider='fixture-wallet', event_type='owner_deposit', external_reference_id='fixture-deposit',
 occurred_at=datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0), currency='USD', amount=Decimal(25),
 amount_usd=Decimal(25), reconciliation_state='RECONCILED', value_state='realized',
 capital_class='owner_capital', confidence_state='provider_confirmed',
 completeness_state='complete', evidence={'fixture': True},
))
ledger.conn.close()
with sqlite3.connect(sys.argv[1]) as c:
 c.execute('CREATE TABLE forecast_ledger(id INTEGER PRIMARY KEY,created_at TEXT,venue TEXT,market_id TEXT,snapshot_json TEXT,forecast_json TEXT,opportunity_json TEXT,action_json TEXT)')
 c.execute('INSERT INTO forecast_ledger VALUES(?,?,?,?,?,?,?,?)', (1,'2026-09-27T00:00:02+00:00','fixture-venue','BTC-15M','{"title":"BTC fixture 15m","captured_at":"2026-09-27T00:00:02+00:00","yes_bid":0.48,"yes_ask":0.50,"liquidity_usd":5000}','{"probability_yes":0.58,"lower_bound":0.52,"upper_bound":0.64,"model_version":"fixture-model-v1","evidence_ids":["evidence-btc-1"]}','{"market_probability":0.49,"raw_edge":0.09,"estimated_cost":0.02,"uncertainty_penalty":0.01,"robust_edge":0.06}','{"decision":"PASS","reason":"Fixture evidence requires review"}'))
`, db]);
const port = await new Promise(resolve => { const s = createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
const server = spawn(python, ['-m', 'uvicorn', 'noema.dashboard_app:app', '--host', '127.0.0.1', '--port', String(port)], { env: {...process.env, NOEMA_DB_PATH: db}, stdio: 'ignore' });
const url = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({headless:true});
try {
 for(let i=0;i<100;i++){try{if((await fetch(url)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
 for(const width of [390,768,1024,1440]){
  const page = await browser.newPage({viewport:{width,height:1000}, reducedMotion:'reduce'}), errors=[], failedRequests=[], externalRequests=[], consoleErrors=[], failedResponses=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console', message=>{if(message.type()==='error')consoleErrors.push(message.text());});
  page.on('requestfailed', request=>failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`));
  page.on('request', request=>{if(new URL(request.url()).origin!==url)externalRequests.push(request.url());});
  page.on('response', response=>{if(response.status()>=400)failedResponses.push(`${response.status()} ${response.url()}`);});
  await page.goto(url); await page.getByRole('heading',{name:'NOEMA OPERATING WORLD'}).waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(() => document.querySelector('#autonomous-rule-rack')?.innerText.includes('Prediction execution'));
  assert.ok((await page.locator('#autonomous-rule-rack').innerText()).includes('Prediction execution'));
  assert.ok((await page.locator('#autonomous-kill-board').innerText()).includes('BTC-15M'));
  assert.ok((await page.locator('#autonomous-shift-report').innerText()).includes('Unknown'));
  await page.locator('#tab-economics').click();
  assert.equal(await page.locator('#tab-economics').getAttribute('aria-selected'),'true');
  await page.locator('#lane-rows').getByText('Prediction markets',{exact:true}).waitFor({state:'attached'});
  await page.waitForFunction(() => !document.getElementById('refresh').disabled);
  const feedKeys = await page.locator('#live-feed .feed-event button').evaluateAll(
    buttons => buttons.map(button => button.dataset.eventKey),
  );
  for (const key of ['mission:0','mission:1','decision:1']) {
   assert.ok(feedKeys.includes(key),`feed includes persisted record ${key}`);
  }
  assert.ok(await page.locator('.feed-event-detail').first().evaluate(node=>parseFloat(getComputedStyle(node).fontSize)>=14),'activity prose remains readable');
  assert.equal(await page.locator('#text-size-toggle').count(),0,'the unnecessary text-size control is removed');
  await page.locator('[data-feed-filter="decision"]').click();
  assert.equal(await page.locator('#live-feed .feed-event').count(),1);
  assert.ok((await page.locator('#live-feed').innerText()).includes('BTC-15M'));
  await page.locator('#feed-search').fill('no matching record');
  assert.equal(await page.locator('#live-feed .feed-event').count(),0);
  await page.locator('#feed-search').fill('');
  await page.locator('[data-feed-filter="all"]').click();
  await page.locator('[data-event-key="mission:1"]').click();
  assert.equal(await page.locator('#selection-reader-text').innerText(),'Evidence critic passed integrity checks');
  await page.locator('#clear-shared-selection').click();
  assert.equal(await page.locator('#world-map-canvas').getAttribute('data-motion'),'reduced');
  await page.locator('#inactive-lanes').getByText('SaaS / micro-SaaS',{exact:true}).waitFor({state:'attached'});
  assert.equal(await page.locator('#capital-pnl').innerText(),'UNRECONCILED');
  assert.ok((await page.locator('#mission-economics-progress').innerText()).includes('SELF-FUNDING UNPROVEN'));
  assert.ok((await page.locator('#lane-rows').innerText()).includes('revenue unmeasured'));
  assert.equal(await page.locator('.attention-share, .lane-attention').count(),0,'research attention is absent when no persisted allocation exists');
  await page.locator('#roster-list').getByText('Test specialist',{exact:true}).waitFor();
  assert.equal(await page.locator('#capital-pnl').innerText(),'UNRECONCILED');
  const canonicalText = await page.locator('#canonical-economy-grid').innerText();
  assert.ok(canonicalText.toLowerCase().includes('reconciled owner capital subtotal'), canonicalText);
  assert.ok(canonicalText.includes('Unknown'), canonicalText);
  assert.ok(canonicalText.includes('$25.00'), 'reconciled owner capital is shown as capital, not revenue');
  assert.ok((await page.locator('#canonical-economy-grid').innerText()).toLowerCase().includes('self-funding ratio'));
  assert.ok((await page.locator('#canonical-economy-grid').innerText()).includes('Unknown'));
  assert.equal(await page.locator('#capital-pnl').textContent(),'UNRECONCILED');
  assert.equal(await page.locator('#capital-change24').textContent(),'Unknown');
  assert.equal(await page.locator('#capital-balance').count(),1,'one canonical balance headline');
  assert.ok(['UNRECONCILED','STALE','UNAVAILABLE'].includes(await page.locator('#capital-balance').innerText()),'Agent Balance never promotes a partial source subtotal to live NAV');
  assert.ok((await page.locator('#capital-balance-note').innerText()).includes('excludes open positions'),'subtotal exclusions remain visible');
  for (const tab of await page.locator('[data-category]').all()) {
    await tab.click();
    const name=await tab.getAttribute('data-category');
    assert.equal(await page.locator(`#category-${name}`).isVisible(),true);
    assert.equal(await page.locator('.category-panel:visible').count(),1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`category ${name} fits at ${width}px`);
  }
  await page.locator('[data-category=prediction]').click();
  assert.equal(await page.locator('#lead-state').innerText(),'UNKNOWN');
  assert.equal(await page.locator('#focus-title').innerText(),'Inspect a bounded fixture opportunity');
  await page.locator('#mission-list').getByText('result PASS',{exact:false}).waitFor();
  await page.locator('#mission-list').getByText('Test specialist · lane unknown · result PASS · Test specialist → evidence-critic',{exact:false}).waitFor();
  await page.getByRole('button',{name:/MISSION · mission-fixtur/}).click();
  assert.equal(await page.locator('#shared-selection-title').innerText(),'Inspect a bounded fixture opportunity');
  assert.ok((await page.locator('#timeline-context').innerText()).toLowerCase().includes('mission-fixture'));
  assert.ok((await page.locator('#workstation-entities').innerText()).includes('Inspect a bounded fixture opportunity'));
  await page.locator('#tab-research').click();
  await page.locator('#evidence-chart').getByRole('button',{name:/18 observations/}).waitFor({state:'attached'});
  assert.equal(await page.locator('#evidence-total').innerText(),'18');
  assert.equal(await page.locator('#evidence-chart .chart-bar').evaluateAll(nodes=>nodes.every(node=>Number(node.getAttribute('y'))>=0)),true,'evidence bars stay inside the plotted area');
  assert.ok((await page.locator('#event-trace').innerText()).includes('Evidence critic passed integrity checks'));
  assert.equal(await page.locator('#world-entity-list button').count() >= 2,true,'world map projects persisted NOEMA and mission entities');
  assert.match(await page.locator('#world-map-state').innerText(),/entities/, 'topology status reports the currently rendered entity count');
  assert.ok(await page.locator('#world-map-canvas').evaluate(canvas => {
    const ctx=canvas.getContext('2d'), pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    let changed=0; for(let i=0;i<pixels.length;i+=4) if(pixels[i]>45||pixels[i+1]>55||pixels[i+2]>65) changed++;
    return changed>100;
  }), 'spatial map renders its real entity and relationship geometry');
  await page.locator('#world-map-canvas').scrollIntoViewIfNeeded();
  if(process.env.QA_SCREENSHOTS){await mkdir(process.env.QA_SCREENSHOTS,{recursive:true});await page.screenshot({path:join(process.env.QA_SCREENSHOTS,`noema-world-${width}.png`)});}
  const mapBefore=await page.locator('#world-map-canvas').evaluate(canvas=>canvas.toDataURL());
  const mapBox=await page.locator('#world-map-canvas').boundingBox();
  const dragX=mapBox.x+mapBox.width*.1, dragY=mapBox.y+mapBox.height*.4;
  await page.mouse.move(dragX,dragY);
  await page.mouse.down(); await page.mouse.move(dragX+42,dragY+12,{steps:3}); await page.mouse.up();
  const mapMoved=await page.locator('#world-map-canvas').evaluate((canvas,before)=>canvas.toDataURL()!==before,mapBefore);
  assert.ok(mapMoved,`drag orbits the world camera at ${width}px`);
  const beforeZoom=await page.locator('#world-map-canvas').evaluate(canvas=>canvas.toDataURL());
  await page.mouse.move(dragX,dragY); await page.mouse.wheel(0,-120);
  assert.notEqual(await page.locator('#world-map-canvas').evaluate(canvas=>canvas.toDataURL()),beforeZoom,`wheel zoom updates the camera at ${width}px`);
  await page.locator('#world-event-list button').first().click();
  assert.ok((await page.locator('#world-inspector-title').innerText()).includes('opportunity discovered'));
  assert.ok((await page.locator('#world-entity-list').innerText()).includes('MISSION · Inspect a bounded fixture opportunity · discovered'), 'historical replay must not reveal the later completed mission status');
  assert.equal(await page.locator('#world-entity-list').getByText(/EXPERIMENT/).count(),0,'experiment created after the selected event is hidden from the earlier view');
  await page.locator('#world-time-live').click();
  assert.ok((await page.locator('#world-entity-list').innerText()).includes('MISSION · Inspect a bounded fixture opportunity · completed'));
  await page.locator('[data-category=markets]').click();
  await page.locator('#workstation-radar-list').getByRole('button',{name:/BTC fixture 15m/}).click();
  assert.equal(await page.locator('#shared-selection-title').innerText(),'BTC fixture 15m');
  assert.equal(await page.locator('#decision-pane-state').innerText(),'recorded · 1 persisted');
  assert.ok((await page.locator('#workstation-decision-list').innerText()).includes('Fixture evidence requires review'));
  assert.ok((await page.locator('#workstation-experiment-list').innerText()).includes('No experiment link persisted by exact market identifier.'));
  assert.equal(await page.locator('#evidence-run-count').innerText(),'NO EXACT LINKED RUNS');
  assert.equal(await page.locator('#validation-history').evaluate(node=>node.closest('.category-panel').id),'category-research');
  assert.equal(await page.locator('.paper-history').count(),0,'paper P&L is absent from the operations console');
  assert.ok((await page.locator('#trace-limit').innerText()).includes('market BTC-15M'));
  assert.equal(await page.locator('#capital-pnl').innerText(),'UNRECONCILED');
  await page.locator('[data-inspector-tab="evidence"]').click();
  assert.ok((await page.locator('#shared-selection-evidence').innerText()).includes('evidence-btc-1'));
  await page.locator('[data-inspector-tab="economics"]').click();
  assert.ok((await page.locator('#shared-selection-economics').innerText()).toLowerCase().includes('market-attributed live p&l'));
  await page.locator('[data-inspector-tab="overview"]').click();
  await page.locator('#clear-shared-selection').click();
  assert.ok((await page.locator('#support-worker').innerText()).startsWith('OpenClaw ·'));
  await page.locator('[data-category=system]').click();
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
  assert.deepEqual(failedResponses,[],`HTTP failures at ${width}px`);
  assert.deepEqual(consoleErrors,[],`console errors at ${width}px`);
  const overflow = await page.evaluate(() => ({
   scrollWidth: document.documentElement.scrollWidth,
   bodyScrollWidth: document.body.scrollWidth,
   containers: ['.world', '.world-map', '.world-timebar', '#world-event-list', '#world-entity-list'].map(selector => {
    const node = document.querySelector(selector);
    if (!node) return { selector, missing: true };
    const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
    return { selector, left: Math.round(rect.left), right: Math.round(rect.right), width: Math.round(rect.width),
     scrollWidth: node.scrollWidth, clientWidth: node.clientWidth, scrollLeft: Math.round(node.scrollLeft),
     overflowX: style.overflowX, contain: style.contain, transform: style.transform };
   }),
   elements: [...document.querySelectorAll('body *')].map(node => ({
    tag: node.tagName, id: node.id, className: typeof node.className === 'string' ? node.className : '',
    left: Math.round(node.getBoundingClientRect().left), right: Math.round(node.getBoundingClientRect().right), width: Math.round(node.getBoundingClientRect().width),
    parentWidth: Math.round(node.parentElement?.getBoundingClientRect().width ?? 0),
    parentTag: node.parentElement?.tagName, parentId: node.parentElement?.id,
    parentClass: typeof node.parentElement?.className === 'string' ? node.parentElement.className : '',
    parentOverflowX: node.parentElement ? getComputedStyle(node.parentElement).overflowX : '',
    parentOverflowY: node.parentElement ? getComputedStyle(node.parentElement).overflowY : '',
    computed: getComputedStyle(node).width,
    maxWidth: getComputedStyle(node).maxWidth,
   })).filter(item => item.right > innerWidth + 1).slice(0, 8),
  }));
  assert.equal(overflow.scrollWidth>width,false,`overflow at ${width}: ${JSON.stringify(overflow)}`);
  if(process.env.QA_SCREENSHOTS){await mkdir(process.env.QA_SCREENSHOTS,{recursive:true});await page.screenshot({path:join(process.env.QA_SCREENSHOTS,`noema-operations-${width}.png`)});}
  await page.route('**/api/operations',route=>route.fulfill({status:503,body:'unavailable'}));
  await page.getByRole('button',{name:'Refresh live state',exact:true}).click();
  await page.getByRole('alert').waitFor(); assert.ok((await page.getByRole('alert').innerText()).includes('last successful snapshot'));
  assert.equal(await page.locator('#detail img').count(),0);
  assert.deepEqual(errors,[]); assert.deepEqual(failedRequests,[]); assert.deepEqual(externalRequests,[]);
 await page.close();
 }
 // Exercise commit delivery independently of slow optional checks, including an in-flight pause.
 const streamPage = await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 await streamPage.addInitScript(() => {
  const NativeEventSource = window.EventSource;
  window.EventSource = class extends NativeEventSource {
   constructor(url, options) { super(url, options); if(String(url).includes('runtime-stream')) window.qaRuntimeStream = this; }
  };
 });
 let releaseProvider;
 const providerWait = new Promise(resolve=>{releaseProvider=resolve;});
 await streamPage.route('**/api/provider-health',async route=>{await providerWait;await route.fulfill({json:{}});});
 await streamPage.goto(url);
 await streamPage.waitForFunction(()=>['mission:0','mission:1','decision:1'].every(key =>
  document.querySelector(`#live-feed [data-event-key="${key}"]`)));
 assert.equal(await streamPage.locator('#refresh').isDisabled(),true,'optional provider still pending while activity is usable');
 const fixtureSnapshot = await (await fetch(`${url}/api/operations`)).json();
 const fixtureMissionId = Date.now();
 fixtureSnapshot.sections.mission_events.rows.push({id:fixtureMissionId,mission_id:'mission-fixture',actor:'qa-fixture',event_type:'fixture_update',status:'completed',detail:'<img src=x onerror=alert(1)> Full recorded update',created_at:new Date().toISOString()});
 let releaseOperating, operatingStarted;
 const operatingWait = new Promise(resolve=>{releaseOperating=resolve;});
 const requestStarted = new Promise(resolve=>{operatingStarted=resolve;});
 let delayOperating = true;
 await streamPage.route('**/api/operations',async route=>{
  if(delayOperating){operatingStarted();await operatingWait;}
  await route.fulfill({json:fixtureSnapshot});
 });
 await streamPage.evaluate(()=>window.qaRuntimeStream.dispatchEvent(new MessageEvent('change')));
 await requestStarted;
 await streamPage.locator('#pause-updates').click();
 releaseOperating();
 await streamPage.waitForFunction(()=>document.getElementById('display-status').textContent.includes('updates waiting'));
 assert.equal(await streamPage.locator(`[data-event-key="mission:${fixtureMissionId}"]`).count(),0,'an in-flight response cannot change a paused view');
 delayOperating=false;
 await streamPage.locator('#pause-updates').click();
 await streamPage.locator(`[data-event-key="mission:${fixtureMissionId}"]`).waitFor();
 await streamPage.waitForFunction(()=>document.getElementById('brief-delta').textContent==='+1 new',{timeout:5000});
 assert.equal(await streamPage.locator('#brief-delta').innerText(),'+1 new');
 await streamPage.locator(`[data-event-key="mission:${fixtureMissionId}"]`).click();
 assert.equal(await streamPage.locator('#selection-reader-text').innerText(),'<img src=x onerror=alert(1)> Full recorded update');
 assert.equal(await streamPage.locator('#selection-reader img').count(),0,'event text cannot create HTML');
 await streamPage.evaluate(()=>window.qaRuntimeStream.dispatchEvent(new Event('error')));
 assert.equal(await streamPage.locator('#sync-state').innerText(),'STREAM RECONNECTING');
 releaseProvider();
 await streamPage.waitForFunction(()=>!document.getElementById('refresh').disabled);
 await streamPage.close();
 // Deterministic live capital fixture: source subtotals, chart windows and stale retention.
 const capitalPage=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const capitalErrors=[]; capitalPage.on('pageerror',error=>capitalErrors.push(error.message));
 const observed=new Date().toISOString(), earlier=new Date(Date.now()-60000).toISOString();
 const contributions=[
  {id:'venue:Kalshi',label:'Kalshi',amount_usd:'0',observed_at:observed,status:'CACHED',included:true},
  {id:'venue:Polymarket US',label:'Polymarket US',amount_usd:'0.03',observed_at:observed,status:'CACHED',included:true},
  {id:'wallet:solana:fixture-sol',label:'solana',amount_usd:'10.16',observed_at:observed,status:'CACHED',included:true},
  {id:'wallet:base:fixture-evm',label:'base',amount_usd:'9.97',observed_at:observed,status:'CACHED',included:true},
  {id:'revenue:stripe',label:'Stripe available',amount_usd:'0',observed_at:earlier,status:'STALE',included:false},
 ];
 const liveCapitalFixture={current:{amount_usd:'20.16',observed_at:observed,status:'CACHED',valued_sources:4,expected_sources:8,observed_sources:5,unpriced_sources:0,stale_sources:1,scope:'fixture-owned-accounts',contributions},history_status:'recorded',sample_count:2,change_24h_usd:null,points:[{at:earlier,amount_usd:'19.50',scope:'fixture-owned-accounts'},{at:observed,amount_usd:'20.16',scope:'fixture-owned-accounts'}]};
 const requestedWindows=[];
 await capitalPage.route('**/api/capital-history?*',route=>{const window=new URL(route.request().url()).searchParams.get('window');requestedWindows.push(window);return route.fulfill({json:{...liveCapitalFixture,window}});});
 await capitalPage.route('**/api/wallet-status',route=>route.fulfill({json:{observed_at:observed,networks:[{chain:'solana',address:'fixture-sol',sol:'0.085369584',readable:true,native_value_usd:'10.16'},{chain:'base',address:'fixture-evm',native_balance:'0.003723771147532087',readable:true,native_value_usd:'9.97'}]}}));
 await capitalPage.route('**/api/prediction-venues',route=>route.fulfill({json:{as_of:observed,venues:[{venue:'Kalshi',account:{status:'authenticated_read_only',observed_at:observed,cash_balance_usd:'0',positions:0,fills:0}},{venue:'Polymarket US',account:{status:'authenticated_read_only',observed_at:observed,cash_balance_usd:'0.03'}}]}}));
 await capitalPage.goto(url);
 await capitalPage.waitForFunction(()=>document.getElementById('capital-balance').textContent==='STALE');
 assert.equal(await capitalPage.locator('#capital-balance').count(),1);
 assert.equal(await capitalPage.locator('#capital-chart circle').count(),2,'chart uses actual history observations');
 assert.ok((await capitalPage.locator('#capital-chart-caption').innerText()).includes('2 persisted live observations'),
  'canonical balance chart exposes persisted history coverage');
 for(const window of ['1H','7D','30D','ALL','24H']){
  await capitalPage.locator(`[data-capital-window="${window}"]`).click();
  await capitalPage.waitForFunction(value=>document.getElementById('capital-chart-caption').textContent.includes(`· ${value} ·`),window);
 }
 assert.ok(['1H','24H','7D','30D','ALL'].every(window=>requestedWindows.includes(window)));
 await capitalPage.locator('[data-category=web3]').click();
 const solCard=capitalPage.locator('#capital-wallet-list article[data-account-id="wallet:solana:fixture-sol"]');
 assert.ok((await solCard.innerText()).includes('$10.16 included in known subtotal'));
 await solCard.locator('.capital-account-main').click();
 assert.equal(await capitalPage.locator('#shared-selection-title').innerText(),'Solana');
 await capitalPage.waitForFunction(()=>document.getElementById('feed-window').textContent.includes('· Solana ·'),
  null,{timeout:5000});
 assert.ok((await capitalPage.locator('#feed-window').innerText()).includes('Solana'));
 assert.equal(await capitalPage.locator('#web3-live-details .category-card').count(),1,'shared wallet selection scopes category detail');
 await capitalPage.locator('#category-clear').click();
 assert.equal(await capitalPage.locator('#web3-live-details .category-card').count(),2,
  'Web3 details show only the two wallet networks present in the live fixture');
 await capitalPage.locator('[data-category=revenue]').click();
 assert.ok((await capitalPage.locator('#revenue-live-details').innerText()).includes('Excluded'));
 await capitalPage.waitForFunction(()=>!document.getElementById('refresh').disabled);
 await capitalPage.route('**/api/capital-history?*',route=>route.fulfill({status:503,body:'unavailable'}));
 const failedHistoryResponse=capitalPage.waitForResponse(response=>response.url().includes('/api/capital-history?')&&response.status()===503);
 await capitalPage.locator('#refresh').click();
 await failedHistoryResponse;
 await capitalPage.waitForFunction(()=>document.getElementById('capital-balance')?.textContent==='STALE');
 assert.equal(await capitalPage.locator('#capital-balance').innerText(),'STALE','unavailable history marks the top-level balance stale');
 assert.ok((await capitalPage.locator('#capital-balance-note').innerText()).includes('$20.16'),'last known subtotal remains visible with stale state');
 assert.deepEqual(capitalErrors,[]);
 await capitalPage.close();
 const touchPage = await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
 const touchErrors=[];
 touchPage.on('pageerror',e=>touchErrors.push(e.message));
 await touchPage.goto(url);
 await touchPage.getByRole('heading',{name:'NOEMA OPERATING WORLD'}).waitFor();
 await touchPage.locator('#world-map-canvas').scrollIntoViewIfNeeded();
 const touchCanvas=touchPage.locator('#world-map-canvas');
 const touchBounds=await touchCanvas.boundingBox();
 const cdp=await touchPage.context().newCDPSession(touchPage);
 const beforePinch=await touchCanvas.evaluate(canvas=>canvas.toDataURL());
 await cdp.send('Input.synthesizePinchGesture',{x:touchBounds.x+touchBounds.width/2,y:touchBounds.y+touchBounds.height/2,scaleFactor:1.35,relativeSpeed:800,gestureSourceType:'touch'});
 await touchPage.waitForTimeout(100);
 assert.notEqual(await touchCanvas.evaluate(canvas=>canvas.toDataURL()),beforePinch,'two-finger touch pinch changes the world zoom');
 assert.deepEqual(touchErrors,[]);
 await touchPage.close();
 console.log('Operational console: all categories at four viewports, canonical balance, persisted chart windows, source subtotals, readable typography, filters, full-text inspection, independent live delivery, in-flight pause/resume, reconnect state, real SQLite/API, replay, touch and stale retention passed.');
} finally {await browser.close();server.kill();await rm(folder,{recursive:true,force:true});}
