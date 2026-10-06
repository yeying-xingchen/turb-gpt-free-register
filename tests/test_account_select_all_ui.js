// Run: node --test tests/test_account_select_all_ui.js
// 旧版控制台「选择全部筛选结果 / 全选所有账号」与分批提交工具。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webui/templates/index.html'), 'utf8');

// 从 console.js 里抽取真实的函数/常量声明，保证测试跟着实现走。
function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `Missing function ${name}`);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}
function declarationSource(name) {
  const match = new RegExp(`^(?:const|let|var) ${name}\\b[\\s\\S]*?;\\s*$`, 'm').exec(source);
  assert.ok(match && match[0].length < 400, `Missing declaration ${name}`);
  return match[0];
}
// vm 里创建的对象/数组跨 realm，比较前先转成本 realm 的普通结构。
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function helperContext(extra = {}) {
  const context = {
    ACCOUNTS: [], ACCOUNT_SELECTED: new Set(), ACCOUNT_SELECTED_ROWS: new Map(),
    ...extra,
  };
  vm.createContext(context);
  vm.runInContext([
    declarationSource('ACCOUNT_BATCH_LIMITS'),
    ...[
      'accountBatchLimit', 'chunkAccountIds', 'mergeBatchResults', 'postAccountBatches',
      'accountBatchProgress', 'accountRowsCoverSelection',
    ].map(functionSource),
  ].join('\n'), context);
  return context;
}

function makeNode() {
  const node = {title: '', disabled: false, value: '', labels: [], classList: {remove() {}, toggle() {}}};
  let text = '';
  Object.defineProperty(node, 'textContent', {
    get: () => text,
    set: value => { text = value; node.labels.push(value); },
  });
  return node;
}

function selectAllHarness({total = 12000, pageSize = 5000} = {}) {
  const nodes = new Map(), urls = [], toasts = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  let pending = null;
  const context = {
    ACCOUNT_SELECTED: new Set(), ACCOUNT_SELECTED_ROWS: new Map(), accountSelectionRevision: 0,
    ACCOUNTS: [{id: 1, email: 'one@example.com'}], ACCOUNTS_TOTAL: total,
    SHOW_ARCHIVED_ACCOUNTS: false, PAGERS: {accounts: {page: 1, size: 20}},
    renders: 0, uiUpdates: 0,
    document: {getElementById: node, querySelectorAll: () => []},
    getAccountLookupFilters: () => ({
      archived: '0', plan: '', codex_status: '', totp_status: '', group: '',
      redemption: 'unredeemed', date_from: '', date_to: '',
    }),
    getAccountsQuery: () => 'needle',
    api: async url => {
      urls.push(url);
      if (pending) return pending(url);
      const page = Number(new URL(url, 'http://local').searchParams.get('page') || 1);
      const start = (page - 1) * pageSize;
      const ids = [];
      for (let id = start + 1; id <= Math.min(start + pageSize, total); id++) ids.push(id);
      return {ok: true, ids, total, page, page_size: pageSize};
    },
    showToast: message => toasts.push(message),
    updateAccountSelectionUi: () => { context.uiUpdates++; },
    renderAccounts: () => { context.renders++; context.updateAccountSelectionUi(); },
  };
  vm.createContext(context);
  const prologue = ['ACCOUNT_SELECT_ALL_PAGE_SIZE', 'ACCOUNT_SELECT_ALL_MAX_PAGES', 'accountSelectAllBusy']
    .map(declarationSource).join('\n');
  vm.runInContext([prologue, ...[
    'clearAccountSelection', 'setAccountSelectAllBusy', 'getAccountSelectAllFilters',
    'collectAccountIdsByScope', 'selectAllAccounts',
  ].map(functionSource)].join('\n'), context);
  return {
    context, node, urls, toasts,
    setPending(fn) { pending = fn; },
    selected() { return [...context.ACCOUNT_SELECTED]; },
  };
}

test('chunkAccountIds 处理空集、整除、余数与非法批次大小', () => {
  const {chunkAccountIds} = helperContext();
  assert.deepEqual(plain(chunkAccountIds([])), []);
  assert.deepEqual(plain(chunkAccountIds([1, 2, 3, 4, 5, 6], 3)), [[1, 2, 3], [4, 5, 6]]);
  assert.deepEqual(plain(chunkAccountIds([1, 2, 3, 4, 5, 6, 7], 3)), [[1, 2, 3], [4, 5, 6], [7]]);
  // 非法批次大小（0 / 负数 / 非数字）退回默认 500，绝不返回超大批次。
  for (const size of [0, -5, 'abc', null, undefined, NaN]) {
    assert.deepEqual(plain(chunkAccountIds([1, 2, 3], size)), [[1, 2, 3]], String(size));
  }
  const many = Array.from({length: 501}, (_, index) => index + 1);
  assert.deepEqual(plain(chunkAccountIds(many)).map(chunk => chunk.length), [500, 1]);
  // Set 也可以直接传入。
  assert.deepEqual(plain(chunkAccountIds(new Set([1, 2, 3]), 2)), [[1, 2], [3]]);
});

test('mergeBatchResults 拼接数组、按合并数组校准计数并保留标量语义', () => {
  const {mergeBatchResults} = helperContext();
  assert.deepEqual(plain(mergeBatchResults([])), {});
  const single = {ok: true, started: [{id: 1}], started_count: 1};
  assert.deepEqual(plain(mergeBatchResults([single])), single);

  const merged = plain(mergeBatchResults([
    {ok: true, started: [{id: 1}], started_count: 1, skipped: [{id: 9}], skipped_count: 4, archived: true},
    {ok: true, started: [{id: 2}], started_count: 1, skipped: [{id: 8}], skipped_count: 6, archived: true},
  ]));
  assert.deepEqual(merged.started, [{id: 1}, {id: 2}]);
  assert.deepEqual(merged.skipped, [{id: 9}, {id: 8}]);
  // 有对应数组时计数按合并后的长度校准，避免出现「计数与明细不一致」。
  assert.equal(merged.started_count, 2);
  assert.equal(merged.skipped_count, 2);
  assert.equal(merged.archived, true);

  // 只有计数、没有明细时按批次求和。
  assert.deepEqual(plain(mergeBatchResults([{failed_count: 2}, {failed_count: 3}])), {failed_count: 5});
  // ok / error / message 等标量保留最后一批（失败的批次由 postAccountBatches 直接抛出）。
  const last = plain(mergeBatchResults([{ok: true, message: 'a'}, {ok: false, error: '第二批失败', message: 'b'}]));
  assert.equal(last.ok, false);
  assert.equal(last.error, '第二批失败');
  assert.equal(last.message, 'b');
});

test('postAccountBatches 按上限分片、每批最多回调一次进度、合并结果', async () => {
  const context = helperContext();
  const calls = [];
  context.api = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push({url, body});
    return {
      ok: true, started: body.account_ids.map(id => ({id})),
      started_count: body.account_ids.length, skipped: [], skipped_count: 0,
    };
  };
  const ids = Array.from({length: 1200}, (_, index) => index + 1);
  const progress = [];
  const merged = plain(await context.postAccountBatches(
    '/api/accounts/check-live-bulk', {workers: 3}, ids, context.accountBatchLimit('live'),
    item => progress.push(plain(item)),
  ));
  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map(call => call.body.account_ids.length), [500, 500, 200]);
  assert.ok(calls.every(call => call.body.account_ids.length <= 500));
  assert.ok(calls.every(call => call.body.workers === 3));
  assert.deepEqual(calls.map(call => [...call.body.account_ids]).flat(), ids);
  assert.equal(calls[0].url, '/api/accounts/check-live-bulk');
  assert.equal(merged.started.length, 1200);
  assert.equal(merged.started_count, 1200);
  // 进度每个批次最多一次，不刷屏。
  assert.deepEqual(progress.map(item => [item.done, item.total, item.sent, item.totalIds]), [
    [1, 3, 500, 1200], [2, 3, 1000, 1200], [3, 3, 1200, 1200],
  ]);

  // 单批时不改变原有行为：只有一次请求，结果原样返回。
  calls.length = 0;
  const one = plain(await context.postAccountBatches('/api/accounts/check-live-bulk', {workers: 3}, [7, 8], 500));
  assert.equal(calls.length, 1);
  assert.deepEqual(plain(calls[0].body.account_ids), [7, 8]);
  assert.equal(one.started_count, 2);
});

test('postAccountBatches 硬失败立即停止，并在错误里保留已成功批次', async () => {
  const context = helperContext();
  let count = 0;
  context.api = async (url, options) => {
    count++;
    if (count === 2) throw new Error('HTTP 500');
    const body = JSON.parse(options.body);
    return {ok: true, started: body.account_ids.map(id => ({id})), started_count: body.account_ids.length};
  };
  const ids = Array.from({length: 1200}, (_, index) => index + 1);
  await assert.rejects(
    () => context.postAccountBatches('/api/accounts/note-bulk', {note: 'x'}, ids, 500),
    error => {
      assert.match(error.message, /第 2\/3 批失败/);
      assert.match(error.message, /已提交 500\/1200/);
      assert.match(error.message, /HTTP 500/);
      assert.equal(error.results.length, 1);
      return true;
    },
  );
  assert.equal(count, 2);
});

test('旧版账号列表提供两个全选按钮、说明文案与 scope 绑定', () => {
  assert.match(html, /<button type="button" class="jobs-tb-btn" id="btnSelectFilteredAccountsV2"[^>]*>选择全部筛选结果<\/button>/);
  assert.match(html, /<button type="button" class="jobs-tb-btn" id="btnSelectAllAccountsV2"[^>]*>全选所有账号<\/button>/);
  assert.match(html, /id="accountsSelectAllHintV2"[^>]*>[^<]*筛选结果 = 当前筛选[^<]*所有账号 = 含已兑换与已归档/);
  // 每页全选复选框保持原样。
  assert.match(html, /<input type="checkbox" id="accountsSelectAllV2"[^>]*>/);
  assert.match(source, /bind\('btnSelectFilteredAccountsV2', \(\) => selectAllAccounts\('filtered'\)\)/);
  assert.match(source, /bind\('btnSelectAllAccountsV2', \(\) => selectAllAccounts\('all'\)\)/);
  assert.match(source, /\/api\/accounts\/ids\?/);
});

test('全选按页收集到 total：filtered 带当前筛选，all 忽略筛选', async () => {
  const ui = selectAllHarness({total: 12000});
  await ui.context.selectAllAccounts('filtered');
  assert.equal(ui.urls.length, 3);
  assert.ok(ui.urls.every(url => url.startsWith('/api/accounts/ids?') && url.includes('scope=filtered')));
  assert.match(ui.urls[0], /page=1/);
  assert.match(ui.urls[1], /page=2/);
  assert.match(ui.urls[2], /page=3/);
  assert.ok(ui.urls.every(url => url.includes('page_size=5000')));
  // 使用与账号列表一致的筛选快照（含关键词）。
  assert.match(ui.urls[0], /redemption=unredeemed/);
  assert.match(ui.urls[0], /q=needle/);
  assert.equal(ui.context.ACCOUNT_SELECTED.size, 12000);
  assert.deepEqual(ui.selected().slice(0, 3), [1, 2, 3]);
  assert.equal(ui.context.renders, 1);
  assert.equal(ui.context.uiUpdates, 2); // renderAccounts 内部一次 + 兜底一次
  assert.match(ui.toasts.at(-1), /已选中筛选结果中的 12000 个账号/);
  assert.ok(ui.node('btnSelectFilteredAccountsV2').labels.includes('选择中… 5000/12000'));
  assert.equal(ui.node('btnSelectFilteredAccountsV2').textContent, '选择全部筛选结果');
  assert.equal(ui.node('btnSelectFilteredAccountsV2').disabled, false);
  // 行缓存只保留当前页已知的行。
  assert.equal(ui.context.ACCOUNT_SELECTED_ROWS.size, 1);

  ui.urls.length = 0;
  await ui.context.selectAllAccounts('all');
  assert.equal(ui.urls.length, 3);
  assert.ok(ui.urls.every(url => url.includes('scope=all')));
  assert.ok(ui.urls.every(url => !url.includes('redemption=') && !url.includes('q=')));
  assert.match(ui.toasts.at(-1), /已选中账号库全部 12000 个账号（含已兑换与已归档）/);
});

test('全选运行中并发点击只发出一轮请求，且按钮在运行中禁用', async () => {
  let release;
  const ui = selectAllHarness({total: 6000});
  ui.setPending(() => new Promise(resolve => { release = () => resolve({ok: true, ids: [1, 2], total: 6000}); }));
  const first = ui.context.selectAllAccounts('filtered');
  assert.equal(ui.node('btnSelectFilteredAccountsV2').disabled, true);
  assert.equal(ui.node('btnSelectAllAccountsV2').disabled, true);
  await ui.context.selectAllAccounts('all'); // 并发保护：直接返回，不发请求
  assert.equal(ui.urls.length, 1);
  release();
  await first;
  assert.equal(ui.urls.length, 1);
  assert.equal(ui.node('btnSelectFilteredAccountsV2').disabled, false);
  assert.equal(ui.node('btnSelectAllAccountsV2').disabled, false);
});

test('全选途中清空选择不会回填已收集的 ID', async () => {
  let release;
  const ui = selectAllHarness({total: 6000});
  ui.setPending(() => new Promise(resolve => { release = resolve; }));
  const pending = ui.context.selectAllAccounts('filtered');
  ui.context.clearAccountSelection(); // 用户中途点了「清空选择」
  release({ok: true, ids: [1, 2, 3], total: 3});
  await pending;
  assert.deepEqual(ui.selected(), []);
  assert.equal(ui.context.ACCOUNT_SELECTED_ROWS.size, 0);
  assert.equal(ui.node('btnSelectFilteredAccountsV2').textContent, '选择全部筛选结果');
});

test('部分行缓存下不再本地预筛选，批量提交仍使用完整 ID 列表', async () => {
  const context = helperContext({
    ACCOUNTS: [{id: 1}],
    ACCOUNT_SELECTED: new Set([1, 2, 3]),
    ACCOUNT_SELECTED_ROWS: new Map([[1, {id: 1}]]),
  });
  assert.equal(context.accountRowsCoverSelection([1, 2]), false);
  assert.equal(context.accountRowsCoverSelection([1]), true);

  const calls = [];
  context.api = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push([...body.account_ids]);
    return {ok: true, deleted: body.account_ids.map(id => ({id})), deleted_count: body.account_ids.length, skipped: []};
  };
  const merged = plain(await context.postAccountBatches(
    '/api/accounts/delete-bulk', {}, Array.from(context.ACCOUNT_SELECTED), 2,
  ));
  assert.deepEqual(calls, [[1, 2], [3]]);
  assert.equal(merged.deleted_count, 3);
});

test('真实批量操作（删除）超过后端上限时自动分片，每批不超过上限', async () => {
  const ids = Array.from({length: 5002}, (_, index) => index + 1);
  const context = helperContext({
    ACCOUNTS: [{id: 1, email: 'one@example.com'}],
    ACCOUNT_SELECTED: new Set(ids),
    ACCOUNT_SELECTED_ROWS: new Map(),
  });
  const calls = [], toasts = [];
  context.$ = () => ({textContent: '', disabled: false});
  context.confirm = () => true;
  context.showToast = message => toasts.push(message);
  context.loadAccounts = () => {};
  context.loadSummary = () => {};
  context.updateAccountSelectionUi = () => {};
  context.api = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push({url, ids: [...body.account_ids]});
    return {ok: true, deleted: body.account_ids.map(id => ({id})), deleted_count: body.account_ids.length, skipped: []};
  };
  vm.runInContext(functionSource('deleteSelectedAccounts'), context);

  await context.deleteSelectedAccounts();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(call => call.ids.length), [5000, 2]);
  assert.ok(calls.every(call => call.ids.length <= context.accountBatchLimit('delete')));
  assert.deepEqual(calls.map(call => call.url), ['/api/accounts/delete-bulk', '/api/accounts/delete-bulk']);
  // 合并后的 deleted 结果继续驱动原有渲染逻辑：选中项被移除，提示使用合并后的计数。
  assert.equal(context.ACCOUNT_SELECTED.size, 0);
  assert.match(toasts.at(-1), /已删除 5002 个/);
});
