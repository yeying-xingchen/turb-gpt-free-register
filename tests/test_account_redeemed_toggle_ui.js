// Run: node --test tests/test_account_redeemed_toggle_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webui/templates/index.html'), 'utf8');

function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `Missing function ${name}`);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}

function harness() {
  const nodes = new Map();
  const urls = [];
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        value: '', textContent: '', title: '', disabled: false, checked: false,
        attrs: {}, classes: new Set(),
        classList: {
          toggle(name, on) { if (on) this.owner.classes.add(name); else this.owner.classes.delete(name); },
          remove(name) { this.owner.classes.delete(name); },
        },
        setAttribute(name, value) { this.attrs[name] = value; },
        getAttribute(name) { return this.attrs[name]; },
      });
      nodes.get(id).classList.owner = nodes.get(id);
    }
    return nodes.get(id);
  };
  const context = {
    SHOW_ARCHIVED_ACCOUNTS: false,
    SHOW_REDEEMED_ACCOUNTS: false,
    SHOW_PLUS_ACCOUNTS_ONLY: false,
    SHOW_PLUS_TRIAL_ACCOUNTS_ONLY: false,
    SHOW_FREE_ACCOUNTS_ONLY: false,
    accountsLoading: false,
    ACCOUNTS: [], ACCOUNTS_TOTAL: 0,
    PAGERS: {accounts: {page: 3, size: 20}},
    selectionCleared: 0, renders: 0, polls: 0,
    $: selector => node(selector.slice(1)),
    document: {getElementById: node},
    getAccountsPlanFilter: () => '', getAccountsCodexFilter: () => '',
    getAccountsTotpFilter: () => '', getAccountsGroupFilter: () => '',
    getAccountsAtStatusFilter: () => '', getAccountsLiveStatusFilter: () => '',
    getAccountsQuery: () => '',
    api: async url => { urls.push(url); return {items: [], total: 0}; },
    clearAccountSelection: () => { context.selectionCleared++; },
    pollAccountPlanStatuses: async () => { context.polls++; },
    renderAccounts: () => { context.renders++; },
    showToast: message => { throw new Error(message); },
  };
  vm.createContext(context);
  const names = [
    'setAccountsFilterToggle', 'getAccountsRedemptionFilter', 'getAccountLookupFilters',
    'loadAccounts', 'applyAccountsRedeemedFilter',
  ];
  vm.runInContext(names.map(functionSource).join('\n'), context);
  return {context, node, urls};
}

test('已兑换筛选默认打开，按钮只切换这一个条件', async () => {
  const ui = harness();
  const {context} = ui;
  const button = ui.node('showRedeemedAccountsV2');

  assert.equal(context.getAccountsRedemptionFilter(), 'unredeemed');
  assert.equal(context.getAccountLookupFilters().redemption, 'unredeemed');

  await context.loadAccounts();
  assert.match(ui.urls.at(-1), /[?&]redemption=unredeemed&/);

  await context.applyAccountsRedeemedFilter(true);
  assert.equal(context.SHOW_REDEEMED_ACCOUNTS, true);
  assert.equal(context.getAccountsRedemptionFilter(), '');
  assert.equal(context.getAccountLookupFilters().redemption, '');
  // 不限制兑换状态时不带 redemption 参数，等价于「全部状态」。
  assert.match(ui.urls.at(-1), /[?&]redemption=&/);
  assert.equal(button.attrs['aria-pressed'], 'true');
  assert.ok(button.classes.has('is-active'));
  // 切换筛选后回到第一页、清空跨页选择，并重新拉取列表与轻量状态。
  assert.equal(context.PAGERS.accounts.page, 1);
  assert.equal(context.selectionCleared, 1);
  assert.equal(context.polls, 1);

  await context.applyAccountsRedeemedFilter(false);
  assert.equal(context.SHOW_REDEEMED_ACCOUNTS, false);
  assert.match(ui.urls.at(-1), /[?&]redemption=unredeemed&/);
  assert.equal(button.attrs['aria-pressed'], 'false');
  assert.equal(button.classes.has('is-active'), false);
});

test('旧版账号列表提供「显示已兑换」按钮并标记已兑换账号', () => {
  assert.match(
    html,
    /<button type="button" class="acc-v2-toggle" id="showRedeemedAccountsV2" aria-pressed="false" title="[^"]*">显示已兑换<\/button>/,
  );
  assert.match(source, /getAccountsRedemptionFilter/);
  assert.match(source, /showRedeemedAccountsV2/);
  // 显示已兑换账号时，行内用「已兑换」标记区分它们。
  assert.match(
    source,
    /r\.redeemed \? ` <span class="pill status-used" title="已被兑换码领取/,
  );
});

test('历史版顶部导航账号列表默认隐藏已兑换账号', () => {
  const legacy = fs.readFileSync(path.join(root, 'webui/templates/index_legacy.html'), 'utf8');
  assert.match(
    legacy,
    /<input id="showRedeemedAccounts" type="checkbox"[^>]*> 显示已兑换\n/,
  );
  assert.match(legacy, /let SHOW_REDEEMED_ACCOUNTS = false;/);
  assert.match(legacy, /function getAccountsRedemptionFilter\(\) \{\n[^}]*SHOW_REDEEMED_ACCOUNTS \? '' : 'unredeemed';/);
  // 列表与套餐状态轮询使用同一兑换状态条件。
  assert.equal((legacy.match(/&redemption=\$\{encodeURIComponent\(redemption\)\}/g) || []).length, 2);
  assert.match(legacy, /\$\('#showRedeemedAccounts'\)\.addEventListener\('change'/);
  assert.match(
    legacy,
    /r\.redeemed \? ` <span class="pill status-used" title="已被兑换码领取/,
  );
});
