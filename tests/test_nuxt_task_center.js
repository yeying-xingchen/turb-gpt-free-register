// Run: node --test tests/test_nuxt_task_center.js
// Nuxt 任务中心：后台并发纯函数 + 页面接线（按钮、动作、并发 API）。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..');
const modulePromise = import(path.join(root, 'frontend/app/utils/tasks.ts'));
const page = fs.readFileSync(
  path.join(root, 'frontend/app/pages/tasks.vue'),
  'utf8',
);

const pools = [
  { name: 'registration', label: '账号注册', workers: 4, max_workers: 16, running: 1, pending: 2, threads: 4 },
  { name: 'live_check', label: '账号查活', workers: 3, max_workers: 8, running: 0, pending: 0, threads: 3 },
];

test('pool helpers resolve the selected pool and its bounds', async () => {
  const { taskPoolByName, taskPoolWorkers, taskPoolMax } = await modulePromise;
  assert.equal(taskPoolByName(pools, 'live_check').label, '账号查活');
  assert.equal(taskPoolByName(pools, 'missing'), null);
  assert.equal(taskPoolByName(undefined, 'live_check'), null);
  assert.equal(taskPoolWorkers(taskPoolByName(pools, 'live_check')), 3);
  assert.equal(taskPoolWorkers(null), 1);
  assert.equal(taskPoolWorkers({ workers: 'bad' }, 5), 5);
  assert.equal(taskPoolMax(taskPoolByName(pools, 'live_check')), 8);
  assert.equal(taskPoolMax(null), 16);
});

test('worker validation follows each pool max and rejects non-integers', async () => {
  const { taskPoolWorkersValid, taskPoolDirty, taskPoolMax } = await modulePromise;
  const live = taskPoolByNameFor(pools, 'live_check');
  assert.equal(taskPoolWorkersValid(1, live), true);
  assert.equal(taskPoolWorkersValid(8, live), true);
  assert.equal(taskPoolWorkersValid(9, live), false);
  assert.equal(taskPoolWorkersValid(0, live), false);
  assert.equal(taskPoolWorkersValid(3.5, live), false);
  assert.equal(taskPoolWorkersValid('bad', live), false);
  assert.equal(taskPoolWorkersValid(16, null), true);
  assert.equal(taskPoolMax(live), 8);

  assert.equal(taskPoolDirty(3, live), false);
  assert.equal(taskPoolDirty(5, live), true);
  assert.equal(taskPoolDirty(5, null), false);
});

test('pool hint summarises running, pending and live threads', async () => {
  const { taskPoolHint } = await modulePromise;
  assert.equal(taskPoolHint(pools[1]), '0 运行 · 0 排队 · 3 线程');
  assert.equal(taskPoolHint(null), '');
});

function taskPoolByNameFor(list, name) {
  return list.find((pool) => pool.name === name) || null;
}

test('task page wires per-task pause, resume and cancel to capabilities', () => {
  assert.match(page, /v-if="task\.capabilities\?\.pause"/);
  assert.match(page, /v-if="task\.capabilities\?\.resume"/);
  assert.match(page, /v-if="task\.capabilities\?\.cancel"/);
  assert.match(page, /taskAction\(task, 'pause'\)/);
  assert.match(page, /taskAction\(task, 'resume'\)/);
  assert.match(page, /taskAction\(task, 'cancel'\)/);
  assert.match(page, /\/api\/tasks\/\$\{encodeURIComponent\(task\.id\)\}\/\$\{action\}/);
  assert.match(page, /task\.status === 'stopping'/);
});

test('task page exposes the concurrency control and applies it through the API', () => {
  assert.match(page, /taskPoolByName|currentPool/);
  assert.match(page, /\/api\/tasks\/concurrency/);
  assert.match(page, /job_type: poolName\.value/);
  assert.match(page, /aria-label="并发线程数"/);
  assert.match(page, /aria-label="并发减一"/);
  assert.match(page, /aria-label="并发加一"/);
  assert.match(page, /pools\.value = result\.pools/);
});

test('task page keeps a pending concurrency edit across polling refreshes', () => {
  assert.match(page, /if \(!concurrencyDirty\.value\)[\s\S]{0,120}poolWorkers\.value = taskPoolWorkers/);
});
