// Run: node --test tests/test_account_password_ui.js
// Execute the actual modern/legacy renderers and delegated click handlers offline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const PASSWORD = '<img src=x onerror="private-password">&secret';
const MASK = '••••••••';
const sources = {
  modern: fs.readFileSync(path.join(__dirname, '../webui/static/console.js'), 'utf8'),
  legacy: fs.readFileSync(path.join(__dirname, '../webui/templates/index_legacy.html'), 'utf8'),
};

function declaration(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^\\}`, 'm'));
  assert.ok(match, `missing function ${name}`);
  return match[0];
}

function harness(mode, respond = async () => ({value: PASSWORD})) {
  const source = sources[mode];
  const calls = [], copied = [], toasts = [];
  let handler;
  const body = {innerHTML: '', addEventListener(event, callback) { if (event === 'click') handler = callback; }};
  const context = {
    ACCOUNTS: [{id: 7, email: 'user@example.test', has_password: true, password: PASSWORD}],
    ACCOUNTS_TOTAL: 1, ACCOUNT_SELECTED: new Set(), ACCOUNT_SELECTED_ROWS: new Map(),
    PAGERS: {accounts: {page: 1, size: 50}},
    $: () => body,
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    short: value => String(value ?? ''),
    updateAccountSelectionUi() {}, _renderPager() {},
    showToast: value => toasts.push(value), copyText: value => copied.push(value),
    api: async (url, opts) => { calls.push({url, opts}); return respond(); },
  };
  for (const name of ['_tokenCellV2', '_planCell', '_activationCell', '_extractLinkCell', '_paymentCell', '_totpCellV2', '_codexCellV2', '_accountsV2MoreMenu', '_liveStatusSub', '_totpAction', '_codexCell', '_planAction', '_extractLinkAction', '_codexAction']) {
    context[name] = () => '';
  }
  const functions = ['_passwordCell', 'renderAccounts', 'fetchOneAccountSecret', 'toggleAccountPassword'].map(name => declaration(source, name)).join('\n');
  vm.createContext(context);
  vm.runInContext(functions, context);
  if (mode === 'modern') {
    vm.runInContext(declaration(source, 'onAccountsBodyClick'), context);
    handler = context.onAccountsBodyClick;
  } else {
    const match = source.match(/\$\('#accountsBody'\)\.addEventListener\('click', async \(e\) => \{[^]*?^\}\);/m);
    assert.ok(match, 'missing legacy delegated account click handler');
    vm.runInContext(match[0], context);
  }
  const valueEl = {
    textContent: MASK,
    set innerHTML(_) { throw new Error('Password must be inserted as text, never HTML'); },
  };
  const cell = {querySelector: selector => selector === '[data-account-password-value]' ? valueEl : null};
  const show = {
    disabled: false, isConnected: true, textContent: '查看',
    dataset: {accountShowPassword: '7'}, attrs: {'aria-expanded': 'false'},
    getAttribute(key) { return this.attrs[key]; }, setAttribute(key, value) { this.attrs[key] = value; },
    closest: selector => selector === 'td' ? cell : selector === '[data-account-show-password]' ? show : null,
  };
  const copy = {
    disabled: false, dataset: {accountId: '7', accountCopySecret: 'password'},
    closest: selector => selector === '[data-account-copy-secret]' ? copy : null,
  };
  return {context, body, calls, copied, toasts, valueEl, show, copy, click: target => handler({target})};
}

for (const mode of Object.keys(sources)) {
  test(`${mode}: initial account DOM contains only masked password and explicit actions`, () => {
    const ui = harness(mode);
    ui.context.renderAccounts();
    assert.ok(!ui.body.innerHTML.includes('private-password'));
    assert.ok(!ui.body.innerHTML.includes('&lt;img'));
    assert.match(ui.body.innerHTML, /data-account-show-password="7"/);
    assert.match(ui.body.innerHTML, /data-account-copy-secret="password"/);
    assert.match(ui.body.innerHTML, /••••••••/);
    assert.equal(ui.calls.length, 0);
  });

  test(`${mode}: absent password renders no reveal or copy control`, () => {
    const ui = harness(mode);
    ui.context.ACCOUNTS[0].has_password = false;
    ui.context.renderAccounts();
    assert.ok(!ui.body.innerHTML.includes('data-account-show-password'));
    assert.ok(!ui.body.innerHTML.includes('data-account-copy-secret="password"'));
    assert.ok(!ui.body.innerHTML.includes('private-password'));
    assert.equal(ui.calls.length, 0);
  });

  test(`${mode}: reveal fetches on demand as text, hide discards the displayed value`, async () => {
    const ui = harness(mode);
    await ui.click(ui.show);
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.calls[0].url, '/api/accounts/7/secret?field=password');
    assert.equal(ui.calls[0].opts.cache, 'no-store');
    assert.equal(ui.valueEl.textContent, PASSWORD);
    assert.equal(ui.show.textContent, '隐藏');
    assert.equal(ui.show.getAttribute('aria-expanded'), 'true');
    assert.ok(!JSON.stringify(ui.show).includes('private-password'));
    await ui.click(ui.show);
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.valueEl.textContent, MASK);
    assert.equal(ui.show.getAttribute('aria-expanded'), 'false');
    assert.equal(ui.show.disabled, false);
    await ui.click(ui.show);
    assert.equal(ui.calls.length, 2, 'reveal again must fetch rather than retain a password cache');
  });

  test(`${mode}: copy fetches the secret without revealing it in the DOM`, async () => {
    const ui = harness(mode);
    await ui.click(ui.copy);
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.calls[0].url, '/api/accounts/7/secret?field=password');
    assert.deepEqual(ui.copied, [PASSWORD]);
    assert.equal(ui.valueEl.textContent, MASK);
    assert.equal(ui.copy.disabled, false);
    assert.deepEqual(ui.toasts, ['密码已复制']);
  });

  test(`${mode}: failed secret requests leave the value masked and controls usable`, async () => {
    const ui = harness(mode, async () => { throw new Error('未授权'); });
    await ui.click(ui.show);
    assert.equal(ui.valueEl.textContent, MASK);
    assert.equal(ui.show.disabled, false);
    assert.equal(ui.show.getAttribute('aria-expanded'), 'false');
    assert.deepEqual(ui.toasts, ['获取密码失败: 未授权']);
  });

  test(`${mode}: duplicate clicks and a replaced row cannot leak an in-flight result`, async () => {
    let finish;
    const ui = harness(mode, () => new Promise(resolve => { finish = resolve; }));
    const first = ui.click(ui.show);
    assert.equal(ui.show.disabled, true);
    await ui.click(ui.show);
    assert.equal(ui.calls.length, 1);
    ui.show.isConnected = false;
    finish({value: PASSWORD});
    await first;
    assert.equal(ui.valueEl.textContent, MASK);
    assert.equal(ui.show.disabled, false);
  });
}
