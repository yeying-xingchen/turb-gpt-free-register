// Run: node --test tests/test_task_center_ui.js
// Execute the real task-center UI functions and event handlers without external services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'webui/templates/index.html'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `Missing function ${name}`);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}

const noControls = {pause: false, resume: false, cancel: false};
const task = (id, job_type = 'plus_activation', extra = {}) => ({
  id, job_type, job_id: null, status: 'running', source_status: 'paying',
  email: 'demo@example.test', progress: 65, stage: '支付中', capabilities: {...noControls}, ...extra,
});

function harness(respond) {
  const nodes = new Map(), calls = [], timers = new Map(), confirmations = [], toasts = [];
  let timerId = 0;
  const node = id => {
    if (!nodes.has(id)) {
      const classes = new Set(['hidden']), events = {};
      nodes.set(id, {
        id, textContent: '', innerHTML: '', disabled: false, events,
        scrollTop: 0, clientHeight: 200, scrollHeight: 200,
        classList: {
          add: name => classes.add(name), remove: name => classes.delete(name),
          contains: name => classes.has(name),
          toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); },
        },
        addEventListener: (name, fn) => { (events[name] ||= []).push(fn); },
      });
    }
    return nodes.get(id);
  };
  const context = {
    TASK_CENTER_JOBS: [], TASK_CENTER_HISTORY: [], TASK_CENTER_HISTORY_TOTAL: 0,
    taskCenterLoading: false, taskCenterActiveRequest: null, taskCenterHistoryRequest: 0,
    taskCenterHistoryPage: 1, taskCenterHistorySize: 20,
    JOBS: [], JOBS_TOTAL: 0, JOB_STATUS_COUNTS: {}, jobsRenderSignature: '', PAGERS: {jobs: {page: 1, size: 20}},
    activeLogJob: null, activeLogTask: null, logViewRevision: 0, logTimer: null,
    document: {hidden: false, getElementById: node}, $: selector => node(selector.slice(1)),
    confirm: message => { confirmations.push(message); return true; }, showToast: message => toasts.push(message),
    updateModalScrollLock() {}, renderJobs() {}, loadSummary() {},
    setInterval: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; },
    clearInterval: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      const data = await (respond ? respond(url, options) : undefined);
      return {ok: true, json: async () => data || {ok: true, items: [], total: 0, page: 1, page_size: 20, status_counts: {}, cancelled: 1}};
    },
  };
  vm.createContext(context);
  const names = [
    'fmt', 'esc', 'pillV2', 'formatDateTime', 'api',
    'taskCenterLabel', 'taskCenterStatus', 'taskCenterStatusPill', 'pendingRegistrationTaskCount',
    'taskCenterProgress', 'updateTaskCenterSummary', 'renderTaskCenter', 'renderTaskCenterHistory',
    'captureTaskOtpInputs', 'restoreTaskOtpInputs',
    'taskCenterPoolByName', 'renderTaskCenterPools', 'taskCenterPoolWorkersValue',
    'applyTaskCenterConcurrency',
    'taskCenterHistoryGo', 'refreshTaskCenterHistory', 'refreshTaskCenterActive', 'refreshTaskCenterBadge',
    'refreshTaskCenter', 'refreshJobs', 'handleTaskCenterAction', 'submitTaskManualOtp', 'cancelAllPendingJobs',
    'closeLogModal', 'openLog', 'pollLog', 'renderTaskLogDetails', 'openTaskLog', 'pollTaskLog', 'isTabVisible',
  ];
  vm.runInContext(names.map(functionSource).join('\n'), context);
  vm.runInContext(source.slice(source.indexOf('const taskCenterBody ='), source.indexOf('let modalScrollY =')), context);
  return {
    context, node, calls, timers, confirmations, toasts,
    click(id, targetMap = {}) {
      for (const fn of node(id).events.click || []) fn({currentTarget: node(id), target: {closest: selector => targetMap[selector] || null}});
    },
  };
}

const labels = {
  plus_activation: '开通 Plus', live_check: '查活', plan_check: '查套餐', extract_link: '提链',
  scan_payment: '扫码支付', totp_setup: '开启 2FA', email_change: '换绑邮箱',
  codex_agent: 'Codex 授权', registration: '账号注册', codex_retry: 'Codex 补跑',
};

test('all unified task types render labels, string IDs and only supported controls', () => {
  const ui = harness(), c = ui.context;
  c.TASK_CENTER_JOBS = Object.keys(labels).map((type, i) => task(`account-${i + 1}`, type));
  c.TASK_CENTER_JOBS.push(task('registration-19', 'registration', {job_id: 19, capabilities: {pause: true, resume: false, cancel: true}}));
  c.TASK_CENTER_JOBS.push(task('registration-20', 'codex_retry', {job_id: 20, status: 'paused', capabilities: {pause: false, resume: true, cancel: true}}));
  c.renderTaskCenter();
  const html = ui.node('taskCenterBody').innerHTML;
  for (const label of Object.values(labels)) assert.ok(html.includes(label), label);
  for (let i = 1; i <= 10; i++) {
    assert.ok(html.includes(`data-task-view-log="account-${i}"`));
    for (const action of ['pause', 'resume', 'cancel']) assert.ok(!html.includes(`data-task-${action}="account-${i}"`));
  }
  assert.match(html, /data-task-pause="registration-19"/);
  assert.match(html, /data-task-resume="registration-20"/);
  assert.doesNotMatch(html, /data-task-resume="registration-19"|data-task-pause="registration-20"|NaN/);
});

test('active and history escape server fields and clamp progress before inserting HTML', () => {
  const ui = harness(), c = ui.context;
  const hostile = '<img src=x onerror="bad()">';
  const row = task(`account-1" ${hostile}`, 'unknown', {
    label: hostile, email: hostile, progress_message: hostile, error_message: hostile,
    started_at: hostile, completed_at: hostile, progress: '0; background:url(bad)', status: hostile,
  });
  c.TASK_CENTER_JOBS = [row, task('account-2', 'live_check', {progress: 999}), task('account-3', 'plan_check', {progress: -12})];
  c.TASK_CENTER_HISTORY = [row];
  c.renderTaskCenter(); c.renderTaskCenterHistory();
  for (const id of ['taskCenterBody', 'taskCenterHistoryBody']) {
    assert.doesNotMatch(ui.node(id).innerHTML, /<img|NaN|background:url/);
    assert.match(ui.node(id).innerHTML, /&lt;img/);
  }
  assert.match(ui.node('taskCenterBody').innerHTML, /width:100%/);
  assert.match(ui.node('taskCenterBody').innerHTML, /width:0%/);
});

test('needs_attention and deactivated are visible results, never green success', () => {
  const ui = harness(), c = ui.context;
  c.TASK_CENTER_HISTORY = [
    task('account-1', 'plus_activation', {status: 'needs_attention', error_message: '支付结果待查'}),
    task('account-2', 'live_check', {status: 'success', source_status: 'deactivated'}),
  ];
  c.renderTaskCenterHistory();
  const html = ui.node('taskCenterHistoryBody').innerHTML;
  assert.match(html, /待核实/); assert.match(html, /支付结果待查/); assert.match(html, /账号已废/);
  assert.doesNotMatch(html, /100%|jobs-v2-pill--success/);
  assert.match(c.taskCenterStatusPill({status: 'failed', source_status: 'needs_attention'}), /待核实/);
  assert.match(c.taskCenterStatusPill({status: 'deactivated'}), /账号已废/);
});

test('registration partial success remains visible without changing control status', () => {
  const ui = harness(), c = ui.context;
  const row = task('registration-7', 'registration', {
    job_id: 7, status: 'success', display_status: 'partial_success', source_status: 'success', error_message: 'Codex 授权失败',
  });
  c.TASK_CENTER_HISTORY = [row]; c.renderTaskCenterHistory();
  assert.match(ui.node('taskCenterHistoryBody').innerHTML, /部分成功/);
  assert.match(ui.node('taskCenterHistoryBody').innerHTML, /Codex 授权失败/);
  assert.doesNotMatch(ui.node('taskCenterHistoryBody').innerHTML, /100%|jobs-v2-pill--success/);
});

test('unified active/history APIs own counts and backend global pagination', async () => {
  const ui = harness(url => {
    if (url === '/api/tasks/active') return {items: [task('account-8')], total: 8, status_counts: {active: 8, running: 5, pending: 2, paused: 1}};
    if (url.startsWith('/api/tasks/history')) return {items: [task('account-101')], total: 101, page: 3, page_size: 20};
    if (url.startsWith('/api/jobs?')) return {items: [], total: 0, status_counts: {active: 1, running: 1}};
    throw new Error(`Unexpected ${url}`);
  });
  const c = ui.context;
  await c.refreshTaskCenter();
  assert.equal(ui.node('taskCenterBadge').textContent, '8');
  assert.equal(ui.node('taskCenterRunningCount').textContent, '5');
  assert.equal(ui.node('taskCenterPendingCount').textContent, '2');
  assert.equal(ui.node('taskCenterPausedCount').textContent, '1');
  assert.equal(ui.node('taskCenterHistoryCount').textContent, '共 101 条');
  assert.match(ui.node('pager-task-center-history').innerHTML, /第 3 \/ 6 页/);
  assert.equal(c.TASK_CENTER_HISTORY.length, 1);
  await c.refreshJobs({refreshSummary: false});
  assert.equal(ui.node('taskCenterBadge').textContent, '8');
  c.taskCenterHistoryGo(1); await settle();
  assert.ok(ui.calls.some(call => call.url === '/api/tasks/history?page=4&page_size=20'));
  assert.ok(ui.calls.every(call => !/\/api\/jobs\/(active|history)/.test(call.url)));
});

test('older history responses cannot replace a newer backend page', async () => {
  const pending = [];
  const ui = harness(() => new Promise(resolve => pending.push(resolve))), c = ui.context;
  const first = c.refreshTaskCenterHistory();
  c.taskCenterHistoryPage = 2;
  const second = c.refreshTaskCenterHistory();
  pending[1]({items: [task('account-2')], total: 30, page: 2, page_size: 20}); await second;
  pending[0]({items: [task('account-1')], total: 30, page: 1, page_size: 20}); await first;
  assert.equal(c.taskCenterHistoryPage, 2);
  assert.equal(c.TASK_CENTER_HISTORY[0].id, 'account-2');
});

test('failed history navigation shows escaped feedback without unhandled rejection', async () => {
  const ui = harness(() => { throw new Error('<img>offline'); }), c = ui.context;
  c.TASK_CENTER_HISTORY_TOTAL = 40;
  c.taskCenterHistoryGo(1); await settle();
  assert.match(ui.node('taskCenterHint').innerHTML, /历史任务加载失败/);
  assert.match(ui.node('taskCenterHint').innerHTML, /&lt;img/);
  assert.doesNotMatch(ui.node('taskCenterHint').innerHTML, /<img/);
});

test('background badge refresh coalesces active requests and skips hidden documents', async () => {
  let resolve;
  const ui = harness(() => new Promise(done => { resolve = done; })), c = ui.context;
  const first = c.refreshTaskCenterBadge(), second = c.refreshTaskCenterBadge();
  assert.equal(ui.calls.length, 1);
  resolve({items: [], status_counts: {active: 102}}); await Promise.all([first, second]);
  assert.equal(ui.node('taskCenterBadge').textContent, '99+');
  c.document.hidden = true; await c.refreshTaskCenterBadge(); assert.equal(ui.calls.length, 1);
  c.updateTaskCenterSummary({active: 0}); assert.equal(ui.node('taskCenterBadge').classList.contains('hidden'), true);
  assert.match(functionSource('activateTab'), /else refreshTaskCenterBadge\(\)/);
  const polling = source.slice(source.indexOf('// ---------- 初始化 / 可见页面轮询'));
  assert.equal((polling.match(/else refreshTaskCenterBadge\(\)/g) || []).length, 2);
});

test('delegated actions preserve string IDs and reject unsupported account controls', async () => {
  const ui = harness(), c = ui.context;
  c.TASK_CENTER_JOBS = [
    task('registration-7', 'registration', {capabilities: {pause: true, resume: true, cancel: true}}),
    task('account-7', 'codex_retry'),
    task('account-8', 'plus_activation', {capabilities: {cancel: 'true'}}),
  ];
  c.refreshTaskCenter = c.refreshJobs = () => {};
  for (const action of ['pause', 'resume', 'cancel']) {
    const dataset = {[`task${action[0].toUpperCase()}${action.slice(1)}`]: 'registration-7'};
    ui.click('taskCenterBody', {[`[data-task-${action}]`]: {dataset}}); await settle();
    await c.handleTaskCenterAction('account-7', action, {});
  }
  await c.handleTaskCenterAction('account-8', 'cancel', {});
  await c.handleTaskCenterAction('registration-7', 'delete', {});
  assert.deepEqual(ui.calls.map(call => call.url), [
    '/api/tasks/registration-7/pause', '/api/tasks/registration-7/resume', '/api/tasks/registration-7/cancel',
  ]);
  assert.ok(ui.calls.every(call => call.options.method === 'POST'));
  const seen = []; c.openTaskLog = id => seen.push(id);
  ui.click('taskCenterBody', {'[data-task-view-log]': {dataset: {taskViewLog: 'account-7'}}});
  ui.click('taskCenterHistoryBody', {'[data-task-history-log]': {dataset: {taskHistoryLog: 'registration-7'}}});
  assert.deepEqual(seen, ['account-7', 'registration-7']);
});

test('manual OTP mode puts a code input next to running registration tasks only', async () => {
  const ui = harness(() => ({ok: true, email: 'demo@example.test'})), c = ui.context;
  c.TASK_CENTER_JOBS = [
    task('registration-21', 'registration', {job_id: 21, status: 'running', email: 'demo@example.test', manual_otp_required: true}),
    task('registration-22', 'registration', {job_id: 22, status: 'pending', manual_otp_required: true}),
    task('registration-23', 'registration', {job_id: 23, status: 'running'}),
    task('account-9', 'live_check', {job_id: null, status: 'running', manual_otp_required: true}),
  ];
  c.renderTaskCenter();
  const html = ui.node('taskCenterBody').innerHTML;
  assert.match(html, /id="task-otp-21"/);
  assert.match(html, /data-task-otp="registration-21"/);
  assert.doesNotMatch(html, /id="task-otp-22"|data-task-otp="registration-22"/);
  assert.doesNotMatch(html, /id="task-otp-23"|data-task-otp="registration-23"/);
  assert.doesNotMatch(html, /data-task-otp="account-9"/);

  // 空验证码不提交，只有合法 4–8 位数字才会发请求。
  await c.submitTaskManualOtp('registration-21', {});
  assert.equal(ui.calls.length, 0);
  ui.node('task-otp-21').value = '12ab';
  await c.submitTaskManualOtp('registration-21', {});
  assert.equal(ui.calls.length, 0);

  ui.node('task-otp-21').value = '123456';
  ui.click('taskCenterBody', {'[data-task-otp]': {dataset: {taskOtp: 'registration-21'}, disabled: false}});
  await settle();
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.calls[0].url, '/api/manual-otp');
  assert.equal(ui.calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(ui.calls[0].options.body), {
    email: 'demo@example.test', code: '123456', job_id: 21,
  });
  assert.equal(ui.node('task-otp-21').value, '');
  assert.match(ui.toasts[ui.toasts.length - 1], /已提交验证码/);
});

test('task center redraw keeps the code typed into the OTP input', () => {
  const ui = harness(), c = ui.context;
  // 任务中心每 3 秒整表重绘一次，输入中的验证码和焦点必须被保留。
  const typed = {
    dataset: {otpJob: '21'}, value: '1234', selectionStart: 4, selectionEnd: 4,
    focused: false, focus() { this.focused = true; }, setSelectionRange(start, end) { this.selection = [start, end]; },
  };
  c.document.activeElement = typed;
  const captured = c.captureTaskOtpInputs({querySelectorAll: () => [typed]});
  assert.equal(captured.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(captured[0])), {job: '21', value: '1234', focused: true, start: 4, end: 4});

  c.restoreTaskOtpInputs(captured);
  const restored = ui.node('task-otp-21');
  assert.equal(restored.value, '1234');

  // 未聚焦且为空的输入框不参与恢复，避免覆盖后端刷新后的新输入框。
  assert.equal(c.captureTaskOtpInputs({querySelectorAll: () => [{dataset: {otpJob: '22'}, value: '', selectionStart: 0, selectionEnd: 0}]}).length, 0);
  assert.equal(c.captureTaskOtpInputs({}).length, 0);
});

test('bulk cancel counts only pending registration IDs with cancellation capability', async () => {
  const ui = harness(), c = ui.context;
  const rows = [
    task('registration-1', 'registration', {status: 'pending', capabilities: {cancel: true}}),
    task('registration-2', 'codex_retry', {status: 'pending', capabilities: {cancel: true}}),
    task('registration-3', 'registration', {status: 'running', capabilities: {cancel: true}}),
    task('account-4', 'plus_activation', {status: 'pending'}),
    task('account-5', 'codex_retry', {status: 'pending'}),
  ];
  c.TASK_CENTER_JOBS = rows; c.JOB_STATUS_COUNTS = {pending: 99};
  c.refreshTaskCenter = c.refreshJobs = () => {};
  assert.equal(c.pendingRegistrationTaskCount(), 2);
  ui.click('btnCancelPendingTaskCenter'); await settle();
  assert.match(ui.confirmations[0], /取消 2 个排队中的注册任务/);
  assert.match(ui.confirmations[0], /不影响.*支付/);
  assert.equal(c.JOB_STATUS_COUNTS.pending, 99);
  assert.equal(ui.calls[0].url, '/api/jobs/cancel-pending');
  assert.match(ui.toasts[0], /排队注册任务/);
  const activeUi = harness(() => ({items: rows, status_counts: {active: 5, pending: 4}}));
  await activeUi.context.refreshTaskCenterActive();
  assert.equal(activeUi.node('btnCancelPendingTaskCenter').textContent, '取消排队注册任务（2）');
  assert.equal(activeUi.node('btnCancelPendingTaskCenter').disabled, false);
  const accountUi = harness(() => ({items: rows.slice(3), status_counts: {active: 2}}));
  await accountUi.context.refreshTaskCenterActive();
  assert.equal(accountUi.node('btnCancelPendingTaskCenter').disabled, true);
});

test('task logs use unified endpoint and safe details without registration OTP controls', async () => {
  const hostile = '<img src=x onerror=bad()>', row = task('account-14', 'plus_activation', {
    status: 'needs_attention', progress_message: hostile, email: hostile, manual_otp_required: true,
  });
  const ui = harness(() => ({job: row, log: hostile})), c = ui.context;
  c.TASK_CENTER_JOBS = [row];
  c.openTaskLog('account-14'); await settle();
  assert.equal(c.activeLogJob, null); assert.equal(c.activeLogTask, 'account-14');
  assert.equal(ui.calls[0].url, '/api/tasks/account-14/log');
  assert.equal(ui.node('logContent').textContent, hostile);
  assert.match(ui.node('logTaskDetails').innerHTML, /待核实|&lt;img/);
  assert.doesNotMatch(ui.node('logTaskDetails').innerHTML, /<img|manual-otp|<input/);
  assert.equal(ui.timers.size, 0);
  await c.pollLog(); assert.equal(ui.calls.length, 1);
  c.closeLogModal(); assert.equal(c.activeLogTask, null);
  assert.equal(ui.node('logPanel').classList.contains('hidden'), true);
});

test('task logs keep polling active states and stop for every terminal result', async () => {
  for (const status of ['pending', 'running', 'paused', 'stopping', 'success', 'failed', 'partial_success', 'stopped', 'cancelled', 'needs_attention', 'deactivated']) {
    const ui = harness(() => ({job: task('account-1', 'live_check', {status}), log: 'log'}));
    ui.context.openTaskLog('account-1'); await settle();
    assert.equal(ui.timers.size, ['pending', 'running', 'paused', 'stopping'].includes(status) ? 1 : 0, status);
  }
});

test('switching task and registration logs rejects stale responses and keeps job endpoint', async () => {
  const responses = [];
  const ui = harness(url => new Promise(resolve => responses.push({url, resolve}))), c = ui.context;
  c.openLog(17); c.openTaskLog('account-18');
  assert.equal(ui.timers.size, 1);
  responses[0].resolve({job: {status: 'success'}, log: 'stale registration'}); await settle();
  assert.equal(ui.node('logContent').textContent, '加载中…');
  assert.equal(ui.timers.size, 1);
  responses[1].resolve({job: task('account-18'), log: 'current task'}); await settle();
  assert.equal(ui.node('logContent').textContent, 'current task');
  const staleTask = c.pollTaskLog(); c.openLog(19);
  responses[2].resolve({job: task('account-18', 'live_check', {status: 'success'}), log: 'stale task'}); await staleTask;
  assert.equal(ui.node('logContent').textContent, '加载中…');
  assert.equal(ui.node('logTaskDetails').classList.contains('hidden'), true);
  assert.equal(c.activeLogJob, 19); assert.equal(c.activeLogTask, null);
  assert.equal(responses[3].url, '/api/jobs/19/log');
  responses[3].resolve({job: {status: 'running'}, log: 'current registration'}); await settle();
  assert.equal(ui.node('logContent').textContent, 'current registration');
  const closing = c.pollLog(); c.closeLogModal();
  responses[4].resolve({job: {status: 'running'}, log: 'closed log'}); await closing;
  assert.equal(ui.node('logContent').textContent, 'current registration');
  assert.equal(ui.timers.size, 0);
});

test('task log URLs encode IDs and failures display as text', async () => {
  const ui = harness(() => { throw new Error('<script>offline</script>'); });
  ui.context.openTaskLog('account-1/extra'); await settle();
  assert.equal(ui.calls[0].url, '/api/tasks/account-1%2Fextra/log');
  assert.equal(ui.node('logContent').textContent, '日志加载失败：<script>offline</script>');
});

test('task-center copy documents all types, per-task controls and concurrency', () => {
  const taskCenter = template.slice(template.indexOf('id="tab-task-center"'), template.indexOf('id="tab-accounts"'));
  for (const label of Object.values(labels)) assert.ok(taskCenter.includes(label), label);
  assert.match(taskCenter, /每类任务按实际能力显示暂停、恢复和取消/);
  assert.match(taskCenter, /id="btnCancelPendingTaskCenter"[^>]*disabled>取消排队注册任务（0）/);
  assert.match(taskCenter, /id="taskCenterPoolSelect"/);
  assert.match(taskCenter, /id="taskCenterPoolWorkers"/);
  assert.match(taskCenter, /id="btnApplyTaskCenterConcurrency"/);
  assert.match(template, /id="logTaskDetails"/);
  const legacy = fs.readFileSync(path.join(root, 'webui/templates/index_legacy.html'), 'utf8');
  assert.match(legacy, /id="btnCancelPending"[^>]*>取消排队注册任务/);
  assert.doesNotMatch(taskCenter, /取消全部排队|取消所有排队/);
});

test('concurrency panel lists pools and applies a new worker count', async () => {
  const pools = [
    {name: 'registration', label: '账号注册', workers: 4, max_workers: 16, running: 1, pending: 2, threads: 4},
    {name: 'live_check', label: '账号查活', workers: 3, max_workers: 16, running: 0, pending: 0, threads: 3},
  ];
  const ui = harness(() => ({pools, items: [], status_counts: {active: 3}}));
  const c = ui.context;
  await c.refreshTaskCenterActive();
  assert.match(ui.node('taskCenterPoolSelect').innerHTML, /账号注册/);
  assert.match(ui.node('taskCenterPoolSelect').innerHTML, /账号查活/);
  assert.equal(ui.node('taskCenterPoolWorkers').value, '4');
  assert.equal(ui.node('taskCenterPoolWorkers').max, '16');
  assert.match(ui.node('taskCenterPoolHint').textContent, /1 运行 · 2 排队/);

  ui.node('taskCenterPoolSelect').value = 'live_check';
  c.renderTaskCenterPools();
  assert.equal(ui.node('taskCenterPoolWorkers').value, '3');

  ui.node('taskCenterPoolWorkers').value = '6';
  await c.applyTaskCenterConcurrency(ui.node('btnApplyTaskCenterConcurrency'));
  const call = ui.calls.find(item => item.url === '/api/tasks/concurrency');
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(JSON.parse(call.options.body), {job_type: 'live_check', workers: 6});
  assert.match(ui.toasts[0], /并发/);
});

test('concurrency panel rejects out-of-range worker counts before calling the API', async () => {
  const pools = [{name: 'live_check', label: '账号查活', workers: 3, max_workers: 16, running: 0, pending: 0, threads: 3}];
  const ui = harness(() => ({pools, items: [], status_counts: {}}));
  const c = ui.context;
  await c.refreshTaskCenterActive();
  ui.node('taskCenterPoolWorkers').value = '99';
  await c.applyTaskCenterConcurrency(ui.node('btnApplyTaskCenterConcurrency'));
  assert.equal(ui.calls.some(item => item.url === '/api/tasks/concurrency'), false);
  assert.match(ui.toasts[0], /1-16/);
});

test('account task rows keep only server-advertised controls', () => {
  const ui = harness(), c = ui.context;
  c.TASK_CENTER_JOBS = [
    task('account-1', 'live_check', {status: 'running', capabilities: {pause: true, resume: false, cancel: true}}),
    task('account-2', 'plan_check', {status: 'paused', capabilities: {pause: false, resume: true, cancel: true}}),
    task('account-3', 'extract_link', {status: 'stopping', capabilities: {pause: false, resume: false, cancel: false}}),
  ];
  c.renderTaskCenter();
  const html = ui.node('taskCenterBody').innerHTML;
  assert.match(html, /data-task-pause="account-1"/);
  assert.match(html, /data-task-cancel="account-1"/);
  assert.match(html, /data-task-resume="account-2"/);
  assert.match(html, /取消中…/);
  for (const action of ['pause', 'resume', 'cancel'])
    assert.ok(!html.includes(`data-task-${action}="account-3"`), action);
});
