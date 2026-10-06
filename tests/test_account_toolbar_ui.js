// Run: node --test tests/test_account_toolbar_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../webui/static/console.js'), 'utf8');

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

function harness() {
  const pages = [
    [{id: 1, email: 'one@example.com', has_access_token: true}, {id: 2, email: 'two@example.com', has_access_token: false}],
    [{id: 3, email: 'three@example.com', has_access_token: true}],
  ];
  const nodes = new Map(), scopes = [{}, {}, {}, {}], requests = [], copied = [], toasts = [];
  let checkboxes = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', textContent: '', title: '', disabled: false, checked: false, classList: {remove() {}, toggle() {}}, focus() {}});
    return nodes.get(id);
  };
  const context = {
    ACCOUNT_SELECTED: new Set(), ACCOUNT_SELECTED_ROWS: new Map(), accountSelectionRevision: 0,
    ACCOUNTS: pages[0], ACCOUNTS_TOTAL: 3, SHOW_ARCHIVED_ACCOUNTS: false,
    PAGERS: {accounts: {page: 1, size: 2}},
    $: selector => node(selector.slice(1)),
    document: {getElementById: node, querySelectorAll: selector => selector === '[data-account-selection-scope]' ? scopes : checkboxes},
    getAccountLookupFilters: () => ({archived: '0'}),
    api: async () => ({matches: [pages[1][0]], not_found: []}),
    fetchAccountSecrets: async (ids, field) => { requests.push({ids: Array.from(ids), field}); return ids.map(id => `${field}:${id}`); },
    copyText: value => copied.push(value), showToast: value => toasts.push(value),
  };
  context.renderAccounts = () => {
    checkboxes = context.ACCOUNTS.map(row => ({checked: context.ACCOUNT_SELECTED.has(row.id)}));
    context.updateAccountSelectionUi();
  };
  context.loadAccounts = () => { context.ACCOUNTS = pages[context.PAGERS.accounts.page - 1] || []; context.renderAccounts(); };
  context.refreshJobs = context.loadOutlook = context.loadCodex = () => {};
  vm.createContext(context);
  const names = ['clearAccountSelection', 'updateAccountSelectionUi', 'syncAccountsSelectAll', 'onAccountsBodyChange', 'getSelectedAccountRows', 'pagerGo', 'pagerGoTo', 'pagerSetSize', '_reloadPagedList', 'parseAccountEmailInput', 'selectAccountsByEmail', 'clearAccountEmailSelectionInput', 'copySelectedAccountLines', 'copySelectedAccountTokens', 'copySelectedAccountEmails', 'copyCurrentPageLines', 'copyCurrentPageTokens'];
  const bulk = ['accountBatchLimit', 'chunkAccountIds', 'mergeBatchResults', 'postAccountBatches', 'accountBatchProgress', 'accountRowsCoverSelection'];
  vm.runInContext([declarationSource('ACCOUNT_BATCH_LIMITS'), declarationSource('ACCOUNT_EMAIL_LOOKUP_LIMIT'), ...names.map(functionSource), ...bulk.map(functionSource)].join('\n'), context);
  context.renderAccounts();
  return {
    context, node, requests, copied, scopes, toasts, pages,
    get checkboxes() { return checkboxes; },
    selected() { return [...context.ACCOUNT_SELECTED]; },
    choose(id, checked = true) { context.onAccountsBodyChange({target: {closest: () => ({dataset: {accountId: String(id)}, checked})}}); },
  };
}

test('changing pages keeps selection; deselecting the current page preserves other pages', () => {
  const ui = harness(), c = ui.context;
  ui.choose(1);
  c.pagerGo('accounts', 1);
  assert.deepEqual(ui.selected(), [1]);
  assert.equal(ui.node('accountsSelectionScopeV2').textContent, '本页选中 0 / 1 · 其他页已选 1');
  assert.equal(ui.node('accountsSelectAllV2').checked, false);
  c.syncAccountsSelectAll(true);
  assert.deepEqual(ui.selected(), [1, 3]);
  c.syncAccountsSelectAll(false);
  assert.deepEqual(ui.selected(), [1]);
  assert.deepEqual([...c.ACCOUNT_SELECTED_ROWS.keys()], [1]);
  c.pagerGoTo('accounts', 1);
  assert.equal(ui.checkboxes[0].checked, true);
  assert.equal(ui.node('accountsSelectAllV2').indeterminate, true);
  c.pagerSetSize('accounts', '20');
  assert.deepEqual(ui.selected(), [1]);
});

test('explicit clear removes cross-page selections, cached rows and checked boxes immediately', () => {
  const ui = harness(), c = ui.context;
  c.syncAccountsSelectAll(true);
  c.pagerGo('accounts', 1);
  ui.choose(3);
  c.clearAccountSelection();
  assert.deepEqual(ui.selected(), []);
  assert.equal(c.ACCOUNT_SELECTED_ROWS.size, 0);
  assert.ok(ui.checkboxes.every(cb => !cb.checked));
  assert.equal(ui.node('accountsSelectedHintV2').textContent, '已选 0 个账号');
  for (const id of ['btnCheckSelectedLiveV2', 'btnCheckSelectedPlansV2', 'btnDeleteSelectedAccountsV2', 'btnClearAccountSelectionV2']) assert.equal(ui.node(id).disabled, true, id);
  assert.ok(ui.scopes.every(scope => scope.textContent === '请先选择账号'));
});

test('empty current page still permits operations on accounts selected elsewhere', () => {
  const ui = harness(), c = ui.context;
  ui.choose(1);
  c.ACCOUNTS = [];
  c.renderAccounts();
  assert.equal(ui.node('btnCheckSelectedLiveV2').disabled, false);
  assert.equal(ui.node('btnCopyAllTokensV2').disabled, true);
  assert.equal(ui.node('btnCopyAllLinesV2').disabled, true);
  assert.equal(ui.node('accountsSelectAllV2').disabled, true);
  assert.equal(ui.scopes[0].textContent, '作用于已选 1 个账号（含其他页 1 个）');
});

test('selected copies include other pages; page copies include only current rows and available tokens', async () => {
  const ui = harness(), c = ui.context;
  ui.choose(3); // Use the same cached row path as an email lookup outside this page.
  c.ACCOUNT_SELECTED_ROWS.set(3, ui.pages[1][0]);
  ui.choose(1);
  await c.copySelectedAccountLines();
  await c.copySelectedAccountTokens();
  await c.copyCurrentPageLines();
  await c.copyCurrentPageTokens();
  assert.deepEqual(ui.requests, [
    {ids: [3, 1], field: 'copy_line'}, {ids: [3, 1], field: 'access_token'},
    {ids: [1, 2], field: 'copy_line'}, {ids: [1], field: 'access_token'},
  ]);
  c.copySelectedAccountEmails();
  assert.equal(ui.copied.at(-1), 'three@example.com\none@example.com');
});

test('no selection disables batch actions without disabling current-page copies', async () => {
  const ui = harness(), c = ui.context;
  assert.equal(ui.node('btnCheckSelectedLiveV2').disabled, true);
  assert.equal(ui.node('btnCopyAllLinesV2').disabled, false);
  await c.copySelectedAccountTokens();
  assert.equal(ui.requests.length, 0);
  await c.copyCurrentPageLines();
  assert.deepEqual(ui.requests[0].ids, [1, 2]);
  c.ACCOUNTS = [ui.pages[0][1]];
  c.updateAccountSelectionUi();
  assert.equal(ui.node('btnCopyAllTokensV2').disabled, true);
  assert.equal(ui.node('btnCopyAllLinesV2').disabled, false);
});

test('email lookup appends cross-page matches; clearing its input keeps those selections', async () => {
  const ui = harness(), c = ui.context;
  ui.choose(1);
  ui.node('accountEmailsToSelectV2').value = 'three@example.com';
  await c.selectAccountsByEmail();
  assert.deepEqual(ui.selected(), [1, 3]);
  assert.equal(c.ACCOUNT_SELECTED_ROWS.get(3).email, 'three@example.com');
  c.clearAccountEmailSelectionInput();
  assert.equal(ui.node('accountEmailsToSelectV2').value, '');
  assert.deepEqual(ui.selected(), [1, 3]);
});

test('a finished liveness request cannot re-enable the batch button after selection is cleared', async () => {
  const ui = harness(), c = ui.context;
  let finishRequest;
  Object.assign(c, {
    getCodexBulkWorkers: () => 3, confirm: () => true,
    openLiveLog() {}, loadSummary() {},
    api: () => new Promise(resolve => { finishRequest = resolve; }),
  });
  vm.runInContext(functionSource('checkSelectedLive'), c);
  ui.choose(1);
  const pending = c.checkSelectedLive();
  c.clearAccountSelection();
  finishRequest({started_count: 1, started: [{email: 'one@example.com'}]});
  await pending;
  assert.equal(ui.node('btnCheckSelectedLiveV2').disabled, true);
  assert.deepEqual(ui.selected(), []);
});

test('a pending email lookup cannot restore selections after an explicit clear', async () => {
  const ui = harness(), c = ui.context;
  let resolveLookup;
  c.api = () => new Promise(resolve => { resolveLookup = resolve; });
  ui.choose(1);
  ui.node('accountEmailsToSelectV2').value = 'three@example.com';
  const pending = c.selectAccountsByEmail();
  c.clearAccountSelection();
  resolveLookup({matches: [ui.pages[1][0]], not_found: []});
  await pending;
  assert.deepEqual(ui.selected(), []);
  assert.equal(c.ACCOUNT_SELECTED_ROWS.size, 0);
  assert.equal(ui.node('btnCheckSelectedLiveV2').disabled, true);
  assert.equal(ui.node('btnSelectAccountsByEmailV2').disabled, false);
});
