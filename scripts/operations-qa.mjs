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
  await page.goto(url); await page.getByRole('button',{name:'TEST-MARKET',exact:true}).click();
  assert.ok((await page.locator('#detail').innerText()).includes('<img src=x'));
  assert.equal(await page.locator('#detail img').count(),0);
  await page.getByRole('button',{name:'Specialists',exact:true}).click();
  await page.getByRole('button',{name:'Test specialist',exact:true}).click();
  await page.getByRole('button',{name:'Back to previous record',exact:true}).click();
  assert.equal(await page.locator('h1').innerText(),'Research queue');
  await page.getByLabel('Filter loaded records').fill('no match');
  assert.equal(await page.locator('#rows tr').count(),0);
  await page.getByLabel('Filter loaded records').fill('');
  for(const button of await page.locator('#views button').all()) await button.click();
  assert.equal(await page.locator('header a[href*="github"]').count(),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`overflow at ${width}`);
  await page.route('**/api/operations',route=>route.fulfill({status:503,body:'unavailable'}));
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.getByRole('alert').waitFor(); assert.ok((await page.getByRole('alert').innerText()).includes('stale'));
  assert.equal(await page.locator('#detail img').count(),0);
  assert.deepEqual(errors,[]);
  if(process.env.QA_SCREENSHOTS){await mkdir(process.env.QA_SCREENSHOTS,{recursive:true});await page.screenshot({path:join(process.env.QA_SCREENSHOTS,`noema-operations-${width}.png`)});}
  await page.close();
 }
 console.log('Operational console: four viewports, real SQLite/API, record drilldown/backtracking, inert untrusted text, filters and stale retention passed.');
} finally {await browser.close();server.kill();await rm(folder,{recursive:true,force:true});}
