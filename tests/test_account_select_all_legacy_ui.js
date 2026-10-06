// Run: node --test tests/test_account_select_all_legacy_ui.js
// 历史版顶部导航控制台（index_legacy.html）「选择全部筛选结果 / 全选所有账号」与分批提交工具。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const legacy = fs.readFileSync(path.join(root, 'webui/templates/index_legacy.html'), 'utf8');

// 从 index_legacy.html 的内联脚本里抽取真实的函数/常量声明，保证测试跟着实现走。
function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(legacy);
  assert.ok(match, `Missing function ${name}`);
  return legacy.slice(match.index, legacy.indexOf('\n}', match.index) + 2);
}
function declarationSource(name) {
  const match = new RegExp(`^(?:const|let|var) ${name}\\b[\\s\\S]*?;\\s*$`, 'm').exec(legacy);
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
      'accountBatchProgress', 'accountRowsCoverSelection', 'getSelectedAccountRows',
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

function selectAllHarness({total = 12000, pageSize = 5000, maxPages = null} = {}) {
  const nodes = new Map(), urls = [], toasts = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  node('qAccounts').value = 'needle';
  let pending = null;
  const context = {
    ACCOUNT_SELECTED: new Set(), ACCOUNT_SELECTED_ROWS: new Map(), accountSelectionRevision: 0,
    ACCOUNTS: [{id: 1, email: 'one@example.com'}], ACCOUNTS_TOTAL: total,
    SHOW_ARCHIVED_ACCOUNTS: false,
    renders: 0, uiUpdates: 0,
    $: selector => node(String(selector).replace(/^#/, '')),
    document: {querySelectorAll: () => []},
    getAccountsPlanFilter: () => 'plus',
    getAccountsCodexFilter: () => 'success',
    getAccountsTotpFilter: () => 'enabled',
    getAccountsAtStatusFilter: () => 'expired',
    getAccountsLiveStatusFilter: () => 'failed',
    getAccountsGroupFilter: () => '5',
    getAccountsRedemptionFilter: () => 'unredeemed',
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
  const prologue = [
    declarationSource('ACCOUNT_SELECT_ALL_PAGE_SIZE'),
    // 允许测试替换安全页数上限，用来验证「total 异常时不会死循环」。
    maxPages == null ? declarationSource('ACCOUNT_SELECT_ALL_MAX_PAGES') : `const ACCOUNT_SELECT_ALL_MAX_PAGES = ${maxPages};`,
    declarationSource('accountSelectAllBusy'),
  ].join('\n');
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

test('历史版账号工具栏提供两个全选按钮、说明文案与 scope 绑定', () => {
  assert.match(legacy, /<button type="button" class="btn" id="btnSelectFilteredAccounts"[^>]*>选择全部筛选结果<\/button>/);
  assert.match(legacy, /<button type="button" class="btn" id="btnSelectAllAccounts"[^>]*>全选所有账号<\/button>/);
  // 标题里要说明选择数量不限、超限自动分批。
  assert.match(legacy, /id="btnSelectFilteredAccounts" title="[^"]*选择数量不限[^"]*自动分批提交[^"]*"/);
  assert.match(legacy, /id="btnSelectAllAccounts" title="[^"]*选择数量不限[^"]*自动分批提交[^"]*"/);
  assert.match(legacy, /id="accountsSelectAllHint"[^>]*>[^<]*筛选结果 = 当前筛选条件[^<]*所有账号 = 忽略筛选且包含已兑换与已归档[^<]*</);
  // 每页全选复选框保持原样。
  assert.match(legacy, /<input type="checkbox" id="accountsSelectAll"[^>]*>/);
  assert.match(legacy, /\$\('#btnSelectFilteredAccounts'\)\.addEventListener\('click', \(\) => selectAllAccounts\('filtered'\)\)/);
  assert.match(legacy, /\$\('#btnSelectAllAccounts'\)\.addEventListener\('click', \(\) => selectAllAccounts\('all'\)\)/);
  assert.match(legacy, /\/api\/accounts\/ids\?/);
});

test('全选按页收集到 total：filtered 带当前筛选，all 忽略筛选', async () => {
  const ui = selectAllHarness({total: 15000});
  await ui.context.selectAllAccounts('filtered');
  assert.equal(ui.urls.length, 3);
  assert.ok(ui.urls.every(url => url.startsWith('/api/accounts/ids?') && url.includes('scope=filtered')));
  assert.match(ui.urls[0], /page=1/);
  assert.match(ui.urls[1], /page=2/);
  assert.match(ui.urls[2], /page=3/);
  assert.ok(ui.urls.every(url => url.includes('page_size=5000')));
  // 使用与账号列表一致的筛选快照（归档/套餐/Codex/2FA/AT/查活/分组/兑换/关键词）。
  assert.match(ui.urls[0], /archived=0/);
  assert.match(ui.urls[0], /plan=plus/);
  assert.match(ui.urls[0], /codex_status=success/);
  assert.match(ui.urls[0], /totp_status=enabled/);
  assert.match(ui.urls[0], /at_status=expired/);
  assert.match(ui.urls[0], /live_status=failed/);
  assert.match(ui.urls[0], /group=5/);
  assert.match(ui.urls[0], /redemption=unredeemed/);
  assert.match(ui.urls[0], /q=needle/);
  // 选择数量不受 5000 上限限制：按页收集到服务端 total。
  assert.equal(ui.context.ACCOUNT_SELECTED.size, 15000);
  assert.deepEqual(ui.selected().slice(0, 3), [1, 2, 3]);
  assert.equal(ui.context.renders, 1);
  assert.equal(ui.context.uiUpdates, 3); // clearAccountSelection 一次 + renderAccounts 内部一次 + 兜底一次
  assert.match(ui.toasts.at(-1), /已选中筛选结果中的 15000 个账号/);
  assert.ok(ui.node('btnSelectFilteredAccounts').labels.includes('选择中… 5000/15000'));
  assert.equal(ui.node('btnSelectFilteredAccounts').textContent, '选择全部筛选结果');
  assert.equal(ui.node('btnSelectFilteredAccounts').disabled, false);
  // 行缓存只保留当前页已知的行。
  assert.equal(ui.context.ACCOUNT_SELECTED_ROWS.size, 1);

  ui.urls.length = 0;
  await ui.context.selectAllAccounts('all');
  assert.equal(ui.urls.length, 3);
  assert.ok(ui.urls.every(url => url.includes('scope=all')));
  assert.ok(ui.urls.every(url => !url.includes('redemption=') && !url.includes('q=') && !url.includes('at_status=')));
  assert.match(ui.toasts.at(-1), /已选中账号库全部 15000 个账号（含已兑换与已归档）/);
});

test('全选运行中并发点击只发出一轮请求，运行中禁用两个按钮', async () => {
  let release;
  const ui = selectAllHarness({total: 6000});
  ui.setPending(() => new Promise(resolve => { release = () => resolve({ok: true, ids: [1, 2], total: 6000}); }));
  const first = ui.context.selectAllAccounts('filtered');
  assert.equal(ui.node('btnSelectFilteredAccounts').disabled, true);
  assert.equal(ui.node('btnSelectAllAccounts').disabled, true);
  await ui.context.selectAllAccounts('all'); // 并发保护：直接返回，不发请求
  assert.equal(ui.urls.length, 1);
  release();
  await first;
  assert.equal(ui.urls.length, 1);
  assert.equal(ui.node('btnSelectFilteredAccounts').disabled, false);
  assert.equal(ui.node('btnSelectAllAccounts').disabled, false);
});

test('全选途中清空选择不会回填已收集的 ID', async () => {
  let release;
  const ui = selectAllHarness({total: 6000});
  ui.setPending(() => new Promise(resolve => { release = resolve; }));
  const pending = ui.context.selectAllAccounts('filtered');
  ui.context.clearAccountSelection(); // 用户中途改筛选/清空选择
  release({ok: true, ids: [1, 2, 3], total: 3});
  await pending;
  assert.deepEqual(ui.selected(), []);
  assert.equal(ui.context.ACCOUNT_SELECTED_ROWS.size, 0);
  assert.equal(ui.node('btnSelectFilteredAccounts').textContent, '选择全部筛选结果');
});

test('服务端 total 异常时按安全页数上限停止，不会死循环', async () => {
  const ui = selectAllHarness({total: 1e9, maxPages: 3});
  await ui.context.selectAllAccounts('all');
  assert.equal(ui.urls.length, 3);
  assert.equal(ui.context.ACCOUNT_SELECTED.size, 15000);
});

test('真实批量操作（删除）超过后端上限时自动分片，合并结果驱动原有提示', async () => {
  const ids = Array.from({length: 5002}, (_, index) => index + 1);
  const context = helperContext({
    ACCOUNTS: [{id: 1, email: 'one@example.com'}],
    ACCOUNT_SELECTED: new Set(ids),
    ACCOUNT_SELECTED_ROWS: new Map(),
  });
  const calls = [], toasts = [];
  const btn = makeNode();
  context.$ = () => btn;
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
  assert.ok(btn.labels.includes('删除中… 5000/5002'));
});

test('行缓存不完整时不再本地预筛选，批量提交仍使用完整 ID 列表', async () => {
  const context = helperContext({
    ACCOUNTS: [{id: 1, codex_status: 'retrying'}],
    ACCOUNT_SELECTED: new Set([1, 2, 3]),
    ACCOUNT_SELECTED_ROWS: new Map([[1, {id: 1, codex_status: 'retrying'}]]),
  });
  assert.equal(context.accountRowsCoverSelection([1]), true);
  assert.equal(context.accountRowsCoverSelection([1, 2]), false);

  const calls = [], toasts = [];
  const btn = makeNode();
  context.$ = () => btn;
  context.confirm = () => true;
  context.showToast = message => toasts.push(message);
  context.loadAccounts = () => {};
  context.updateAccountSelectionUi = () => {};
  context.retryLogEmail = null;
  context.pollRetryLog = () => {};
  context.api = async (url, options) => {
    const body = JSON.parse(options.body);
    calls.push([...body.account_ids]);
    return {ok: true, stopped: body.account_ids.map(id => ({id})), stopped_count: body.account_ids.length, skipped: []};
  };
  vm.runInContext(functionSource('stopSelectedCodex'), context);

  await context.stopSelectedCodex();
  // 只有 1 个账号的行数据，但不能因此丢掉另外 2 个 ID；服务端会跳过不需要停止的账号。
  assert.deepEqual(calls, [[1, 2, 3]]);
  assert.match(toasts.at(-1), /已停止 3 个/);
});

test('历史版所有批量接口分片提交，ZIP 下载保留单次上限提示', () => {
  const batched = [
    '/api/accounts/secret-bulk',
    '/api/accounts/totp-setup-bulk',
    '/api/accounts/codex-agent-bulk',
    '/api/accounts/codex-agent/upload-sub2-bulk',
    '/api/accounts/extract-link-bulk',
    '/api/accounts/check-plan-bulk',
    '/api/accounts/check-live-bulk',
    '/api/accounts/archive-bulk',
    '/api/accounts/delete-bulk',
    '/api/accounts/note-bulk',
    '/api/codex/stop-bulk',
    '/api/codex/retry-bulk',
  ];
  for (const endpoint of batched) {
    const escaped = endpoint.replace(/[/.]/g, '\\$&');
    assert.match(legacy, new RegExp(`postAccountBatches\\('${escaped}'[\\s\\S]{0,120}?accountBatchLimit\\(`), endpoint);
  }
  // 换邮箱单个账号走单账号接口，批量走 change-email-bulk 分片。
  assert.match(legacy, /postAccountBatches\('\/api\/accounts\/change-email-bulk', \{source: source\.trim\(\)\}, ids, accountBatchLimit\('email'\)/);
  assert.match(legacy, /\/api\/accounts\/\$\{ids\[0\]\}\/change-email/);
  // 设置分组通过分组选择器注入的 request 包装分片。
  assert.match(legacy, /path !== '\/api\/accounts\/group-bulk'/);
  assert.match(legacy, /postAccountBatches\(path, rest, groupIds, accountBatchLimit\('group'\)\)/);

  // ZIP 打包下载每次只返回一个文件，保持单次请求 + 明确的上限提示。
  assert.equal((legacy.match(/ZIP 下载单次最多 1000 个账号，请缩小选择范围/g) || []).length, 2);
  assert.match(legacy, /await api\('\/api\/accounts\/download-cpa-bulk'/);
  assert.match(legacy, /form\.action = '\/api\/accounts\/codex-agent\/download-bulk'/);
  assert.ok(!legacy.includes("postAccountBatches('/api/accounts/download-cpa-bulk'"));
  assert.ok(!legacy.includes("postAccountBatches('/api/accounts/codex-agent/download-bulk'"));
  // 单账号操作保持单账号接口。
  assert.match(legacy, /await api\('\/api\/accounts\/codex-agent', \{/);
  assert.match(legacy, /await api\(`\/api\/accounts\/\$\{encodeURIComponent\(id\)\}\/codex-agent\/upload-sub2`/);
});
