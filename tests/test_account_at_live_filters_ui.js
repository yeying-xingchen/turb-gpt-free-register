// Run: node --test tests/test_account_at_live_filters_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webui/templates/index.html'), 'utf8');
const legacy = fs.readFileSync(path.join(root, 'webui/templates/index_legacy.html'), 'utf8');
const accountsPage = fs.readFileSync(path.join(root, 'frontend/app/pages/accounts.vue'), 'utf8');

function functionSource(script, name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(script);
  assert.ok(match, `Missing function ${name}`);
  return script.slice(match.index, script.indexOf('\n}', match.index) + 2);
}

function harness({at = '', live = '', plan = '', codex = '', totp = '', group = '', redemption = ''} = {}) {
  const urls = [];
  const values = {
    atStatusFilterV2: at,
    liveStatusFilterV2: live,
    dateFromAccountsV2: '',
    dateToAccountsV2: '',
    groupFilterV2: group,
    codexStatusFilterV2: codex,
    totpStatusFilterV2: totp,
  };
  const context = {
    SHOW_ARCHIVED_ACCOUNTS: false,
    SHOW_REDEEMED_ACCOUNTS: false,
    ACCOUNTS: [], ACCOUNTS_TOTAL: 0,
    accountsLoading: false,
    PAGERS: {accounts: {page: 1, size: 20}},
    document: {getElementById: id => ({value: values[id] ?? ''})},
    getAccountsPlanFilter: () => plan,
    getAccountsCodexFilter: () => codex,
    getAccountsTotpFilter: () => totp,
    getAccountsGroupFilter: () => group,
    getAccountsRedemptionFilter: () => redemption,
    getAccountsQuery: () => '',
    api: async url => { urls.push(url); return {items: [], total: 0}; },
    clearAccountSelection: () => {},
    renderAccounts: () => {},
    showToast: message => { throw new Error(message); },
  };
  vm.createContext(context);
  const names = [
    'getAccountsAtStatusFilter', 'getAccountsLiveStatusFilter', 'getAccountLookupFilters', 'loadAccounts',
  ];
  vm.runInContext(names.map(name => functionSource(source, name)).join('\n'), context);
  return {context, urls};
}

test('旧版控制台提供 AT 与查活筛选，并带进列表、轻量状态和邮箱查找请求', async () => {
  const ui = harness({at: 'expired', live: 'failed'});

  assert.equal(ui.context.getAccountsAtStatusFilter(), 'expired');
  assert.equal(ui.context.getAccountsLiveStatusFilter(), 'failed');
  const lookupFilters = ui.context.getAccountLookupFilters();
  assert.equal(lookupFilters.at_status, 'expired');
  assert.equal(lookupFilters.live_status, 'failed');

  await ui.context.loadAccounts();
  assert.match(ui.urls.at(-1), /[?&]at_status=expired&/);
  assert.match(ui.urls.at(-1), /[?&]live_status=failed&/);

  // 未选择时不带条件值，等价于「全部状态」。
  const plain = harness();
  assert.equal(plain.context.getAccountsAtStatusFilter(), '');
  assert.equal(plain.context.getAccountsLiveStatusFilter(), '');
  await plain.context.loadAccounts();
  assert.match(plain.urls.at(-1), /[?&]at_status=&/);
  assert.match(plain.urls.at(-1), /[?&]live_status=&/);
});

test('轮询轻量状态使用与列表相同的 AT/查活筛选', () => {
  const start = source.indexOf('async function pollAccountPlanStatuses(');
  assert.ok(start >= 0, 'Missing pollAccountPlanStatuses');
  const body = source.slice(start, source.indexOf('\n}', start) + 2);
  assert.match(body, /getAccountsAtStatusFilter\(\)/);
  assert.match(body, /getAccountsLiveStatusFilter\(\)/);
  assert.match(body, /at_status=\$\{encodeURIComponent\(atStatus\)\}/);
  assert.match(body, /live_status=\$\{encodeURIComponent\(liveStatus\)\}/);
});

test('旧版控制台模板含 AT 与查活下拉，并在切换时刷新列表', () => {
  assert.match(html, /<select id="atStatusFilterV2">[\s\S]*?<option value="expired">AT：已过期<\/option>[\s\S]*?<option value="valid">AT：未过期<\/option>[\s\S]*?<option value="unknown">AT：信息不足<\/option>[\s\S]*?<\/select>/);
  assert.match(html, /<select id="liveStatusFilterV2">[\s\S]*?<option value="failed">查活：失败<\/option>[\s\S]*?<option value="success">查活：正常<\/option>[\s\S]*?<option value="never">查活：未查活<\/option>[\s\S]*?<\/select>/);
  assert.match(source, /\['atStatusFilterV2', 'liveStatusFilterV2'\]\.forEach/);
  // AT 过期账号在查活状态下方给出明确标记。
  assert.match(source, /r\.at_expired[\s\S]{0,160}AT: 已过期/);
});

test('历史版控制台同样支持 AT 与查活筛选和过期标记', () => {
  assert.match(legacy, /<select id="atStatusFilter"[^>]*>[\s\S]*?<option value="expired">已过期<\/option>[\s\S]*?<\/select>/);
  assert.match(legacy, /<select id="liveStatusFilter"[^>]*>[\s\S]*?<option value="failed">失败<\/option>[\s\S]*?<option value="never">未查活<\/option>[\s\S]*?<\/select>/);
  assert.match(legacy, /function getAccountsAtStatusFilter\(\)/);
  assert.match(legacy, /function getAccountsLiveStatusFilter\(\)/);
  assert.match(legacy, /at_status=\$\{encodeURIComponent\(atStatus\)\}&live_status=\$\{encodeURIComponent\(liveStatus\)\}/);
  assert.match(legacy, /\$\('#atStatusFilter'\)\.addEventListener\('change'/);
  assert.match(legacy, /\$\('#liveStatusFilter'\)\.addEventListener\('change'/);
  assert.match(legacy, /r\.at_expired[\s\S]{0,160}AT: 已过期/);
});

test('Nuxt 账号页提供 AT 与查活筛选并显示 AT 过期徽章', () => {
  assert.match(accountsPage, /filters = reactive\(\{[\s\S]*?at_status: "",\s*\n\s*live_status: "",/);
  assert.match(accountsPage, />AT 状态<select v-model="filters\.at_status"[\s\S]*?<option value="expired">已过期<\/option>[\s\S]*?<option value="valid">未过期<\/option>[\s\S]*?<option value="unknown">信息不足<\/option>/);
  assert.match(accountsPage, />查活状态<select v-model="filters\.live_status"[\s\S]*?<option value="failed">查活失败<\/option>[\s\S]*?<option value="never">未查活<\/option>/);
  // 重置筛选时要一起清空，否则「重置」后仍会带着旧条件。
  assert.match(accountsPage, /function resetFilters\(\)[\s\S]*?at_status: "",\s*\n\s*live_status: "",/);
  // 过期账号在状态列显示徽章。
  assert.match(accountsPage, /v-if="row\.at_expired"[\s\S]{0,200}<UiBadge value="expired"/);
});
