// Run: node --test tests/test_plus_activation_ui.js
// Exercise the real UI with an isolated DOM and mocked network; no paid services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/extract-links.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const providers = [
  {id: 1, name: 'Extract A', provider_type: 'extract', enabled: true, cdks: [{id: 11, enabled: true, display_suffix: 'ext-1234'}, {id: 12, enabled: false, display_suffix: 'disabled'}]},
  {id: 2, name: 'Lumen B', provider_type: 'lumen', enabled: true, cdks: [{id: 22, display_suffix: 'lum-5678'}]},
  {id: 3, name: 'UPI C', provider_type: 'upi_git5', enabled: true, cdks: [{id: 33, display_suffix: 'upi-9012'}]},
  {id: 0, name: 'Environment', provider_type: 'extract', has_configured_cdk: true},
  {id: 4, name: 'Disabled', provider_type: 'extract', enabled: false},
];
const accepted = ids => ({status: 202, data: {ok: true, started: ids.map(id => ({id, status: 'queued'})), busy: [], skipped: [], failed: [], started_count: ids.length, busy_count: 0, skipped_count: 0, failed_count: 0}});

function harness(respond = call => accepted(call.body.account_ids), accounts = [], catalogue = () => ({data: {items: providers}}), groupCatalogue = () => ({data: {groups: [{group_name: '已开通', total: 3}]}})) {
  let body;
  class Element {
    constructor(tag) { Object.assign(this, {tagName: tag, children: [], attrs: {}, events: {}, value: '', disabled: false, hidden: false, required: false, _text: '', parent: null}); }
    append(...nodes) { for (const node of nodes) { node.remove(); node.parent = this; this.children.push(node); } }
    replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this._text = ''; this.append(...nodes); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(' '); }
    setAttribute(key, value) { this.attrs[key] = String(value); if (key === 'required') this.required = true; }
    removeAttribute(key) { delete this.attrs[key]; }
    addEventListener(event, callback) { (this.events[event] ||= []).push(callback); }
    emit(event) { for (const callback of this.events[event] || []) callback({preventDefault() {}, target: this, currentTarget: this}); }
    click() { if (!this.disabled) this.emit('click'); }
    focus() { document.activeElement = this; }
    get isConnected() { return this === body || !!this.parent?.isConnected; }
    get parentElement() { return this.parent; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit('close'); }
    matches(selector) {
      if (selector.startsWith('.')) return (this.className || '').split(' ').includes(selector.slice(1));
      const match = /^(\w+)(?:\[([\w-]+)(?:=["']?([^"'\]]+)["']?)?\])?$/.exec(selector);
      return !!match && this.tagName === match[1] && (!match[2] || (match[3] === undefined ? this.attrs[match[2]] !== undefined : this.attrs[match[2]] === match[3]));
    }
    querySelectorAll(selector) {
      const output = [];
      const visit = node => { for (const child of node.children) { if (selector.split(',').some(part => child.matches(part.trim()))) output.push(child); visit(child); } };
      visit(this); return output;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    reportValidity() { return this.querySelectorAll('input, select, textarea').every(input => !input.required || input.disabled || !!input.value); }
  }
  body = new Element('body');
  const document = {body, activeElement: body, createElement: tag => new Element(tag), addEventListener() {}};
  const calls = [], timers = new Map();
  let timerId = 0, reloads = 0;
  const storage = new Proxy({}, {get() { throw new Error('Credentials must never use browser storage'); }, set() { throw new Error('Credentials must never use browser storage'); }});
  const context = {
    document, window: {}, ACCOUNTS: accounts, ACCOUNT_SELECTED_ROWS: new Map(), renderAccounts() {}, loadAccounts: async () => { reloads++; },
    URL, AbortController, Date, Math, crypto: {randomUUID() { throw new Error('Activation must not create payment idempotency keys'); }},
    setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; }, clearTimeout: id => timers.delete(id),
    localStorage: storage, sessionStorage: storage,
    fetch: async (url, options) => {
      const call = {url, options, body: options.body ? JSON.parse(options.body) : undefined}; calls.push(call);
      const result = url === '/api/extract-link/providers' ? await catalogue(call) : url === '/api/account-groups' ? await groupCatalogue(call) : await respond(call);
      return {ok: (result.status || 200) < 400, status: result.status || 200, json: async () => result.data};
    },
  };
  vm.runInNewContext(source, context);
  return {
    calls, timers, context,
    get dialog() { return body.querySelector('dialog'); },
    get text() { return body.textContent; },
    get reloads() { return reloads; },
    get submissions() { return calls.filter(call => call.options.method === 'POST' && call.url === '/api/accounts/activate-plus'); },
    get paymentKey() { return this.dialog.querySelectorAll('input').find(input => input.attrs.type === 'password' && !input.attrs['aria-label']); },
    control(label) { return this.dialog.querySelectorAll('input, select, textarea').find(node => node.attrs['aria-label'] === label); },
    select(label, value) { const node = this.control(label); assert.ok(node, label); node.value = value; node.emit('change'); return node; },
    candidate(kind, index = 0) {
      const list = this.dialog.querySelectorAll('.el-activation-candidates').find(node => node.attrs['aria-label'] === `${kind}候选列表`);
      const node = list?.children[index]; assert.ok(node, `${kind} candidate ${index + 1}`);
      return {
        node,
        control(label) { return node.querySelectorAll('input, select, textarea').find(input => input.attrs['aria-label'] === label); },
        select(label, value) { const input = this.control(label); assert.ok(input, label); input.value = value; input.emit('change'); return input; },
        get key() { return node.querySelectorAll('input').find(input => input.attrs.type === 'password' && !input.attrs['aria-label']); },
        button(action) { return node.querySelectorAll('button').find(button => button.textContent === action); },
      };
    },
    button(label) { return this.dialog.querySelectorAll('button').find(node => node.textContent === label); },
    async open(ids) { await context.window.ExtractLinks.activatePlus(ids); return this.dialog; },
    configure(provider = '1', cdk = 'saved:11') { this.select('提链服务商', provider); this.select('提链 CDK 来源', cdk); this.paymentKey.value = 'payment-secret'; },
    submit() { this.dialog.querySelector('form').emit('submit'); },
  };
}

test('one request contains all unique selected IDs, including accounts on other pages', async () => {
  const account = {id: 1, email: 'one@example.test'};
  const ui = harness(undefined, [account]);
  await ui.open([1, 999, 1, 0, -1, 'bad']);
  assert.equal(ui.submissions.length, 0);
  assert.match(ui.text, /已选择 2 个账号/); assert.match(ui.text, /UPI · 0 元 Checkout/);
  ui.configure(); ui.submit(); await settle();
  assert.equal(ui.submissions.length, 1);
  assert.deepEqual(ui.submissions[0].body, {account_ids: [1, 999], success_group: '', extraction: {provider_id: 1, cdk_id: 11}, payment: {provider: 'v1', auth_mode: 'key', cdk: 'payment-secret'}});
  assert.equal(ui.paymentKey.value, ''); assert.equal(ui.button('开始开通 Plus').disabled, true);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1);
  assert.match(ui.text, /已入队 2/); assert.equal(account.plus_activation_status, 'queued');
  assert.equal(ui.reloads, 1); assert.equal(ui.timers.size, 0);
  assert.ok(ui.calls.every(call => ['/api/extract-link/providers', '/api/account-groups', '/api/accounts/activate-plus'].includes(call.url)));
});

test('all enabled providers remain selectable and no provider/CDK is automatically chosen', async () => {
  const ui = harness(); await ui.open([2]);
  assert.deepEqual(ui.control('提链服务商').children.map(option => option.value), ['', '1', '2', '3', '0']);
  ui.paymentKey.value = 'payment-secret'; ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
  ui.select('提链服务商', '1');
  assert.deepEqual(ui.control('提链 CDK 来源').children.map(option => option.value), ['', 'saved:11', 'raw']);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
  assert.match(ui.text, /请选择提链 CDK 来源/);
});

for (const [provider, cdk, extraction] of [
  ['0', 'configured', {provider_id: 0}],
  ['2', 'raw', {provider_id: 2, cdk: 'extract-secret', proxy_url: 'http://user:pass@proxy.test:8080'}],
  ['3', 'saved:33', {provider_id: 3, cdk_id: 33, entry_proxies: ['http://entry.test:8080', 'socks5://user:pass@entry.test:1080']}],
]) test(`provider ${provider} uses matching configured, temporary or saved CDK and proxies`, async () => {
  const ui = harness(); await ui.open([5, 6]); ui.configure(provider, cdk);
  if (cdk === 'raw') ui.control('本次提链 CDK').value = 'extract-secret';
  if (extraction.proxy_url) ui.control('提链代理 URL').value = extraction.proxy_url;
  if (extraction.entry_proxies) ui.control('UPI 入口代理').value = extraction.entry_proxies.join('\n');
  ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.extraction, extraction);
  assert.equal(ui.control('本次提链 CDK').value, ''); assert.equal(ui.control('提链代理 URL').value, ''); assert.equal(ui.control('UPI 入口代理').value, '');
  assert.ok(!ui.text.includes('extract-secret')); assert.ok(!ui.text.includes('user:pass'));
});

test('UPI entry proxies validate before submission and never echo invalid credentials', async () => {
  const ui = harness(); await ui.open([1]); ui.configure('3', 'saved:33');
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
  ui.control('UPI 入口代理').value = 'file://secret'; ui.submit(); await settle();
  assert.equal(ui.submissions.length, 0); assert.match(ui.text, /第 1 个入口代理/); assert.ok(!ui.text.includes('file://secret'));
});

for (const provider of ['v1', 'masi', 'orderhub']) test(`${provider} submits the chosen platform and key exactly once`, async () => {
  const ui = harness(); await ui.open([1]); ui.configure(); ui.select('支付服务商', provider);
  assert.equal(ui.paymentKey.value, ''); ui.paymentKey.value = provider + '-secret'; ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.payment, {provider, auth_mode: 'key', cdk: provider + '-secret'});
  assert.equal('access_token' in ui.submissions[0].body, false);
});

test('OrderHub numeric login submits session auth without payment CDK or login password', async () => {
  const ui = harness(call => call.url.endsWith('/session') ? {data: {ok: true, logged_in: false}} : call.url.endsWith('/login') ? {data: {ok: true, user: {username: '123456'}}} : accepted(call.body.account_ids));
  await ui.open([7, 8]); ui.configure(); ui.select('支付服务商', 'orderhub'); ui.select('支付验证方式', 'session'); await settle();
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
  ui.control('OrderHub 数字账号').value = '123456'; ui.control('OrderHub 登录密码').value = 'login-secret';
  ui.button('登录 OrderHub').click(); await settle(); assert.equal(ui.control('OrderHub 登录密码').value, '');
  assert.match(ui.text, /OrderHub 已登录/); ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.payment, {provider: 'orderhub', auth_mode: 'session'});
  assert.equal(ui.timers.size, 0); assert.ok(!ui.submissions[0].options.body.includes('login-secret'));
});

test('closing in flight clears credentials and never aborts or starts client payment/extraction calls', async () => {
  let release;
  const ui = harness(call => new Promise(resolve => { release = () => resolve(accepted(call.body.account_ids)); }));
  await ui.open([1, 2]); ui.configure('2', 'raw');
  const raw = ui.control('本次提链 CDK'), proxy = ui.control('提链代理 URL'), payment = ui.paymentKey, username = ui.control('OrderHub 数字账号');
  raw.value = 'extract-secret'; proxy.value = 'http://user:pass@proxy.test'; username.value = '123456';
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1);
  ui.button('关闭').click();
  for (const input of [raw, proxy, payment, username]) assert.equal(input.value, '');
  assert.equal(ui.submissions[0].options.signal, undefined);
  release(); await settle(); assert.equal(ui.reloads, 1); assert.equal(ui.timers.size, 0);
  await ui.open([1, 2]); assert.equal(ui.paymentKey.value, ''); assert.equal(ui.control('本次提链 CDK').value, '');
});

test('mixed 202 results render every category and refresh remains available', async () => {
  const ui = harness(() => ({status: 202, data: {ok: true, started: [{id: 1, status: 'queued'}], busy: [{id: 2, status: 'paying'}], skipped: [{id: 3, reason: '已是 Plus'}], failed: [{id: 4, error: '无 AT'}]}}));
  await ui.open([1, 2, 3, 4]); ui.configure(); ui.submit(); await settle();
  for (const message of ['已入队 1', '执行中 1', '已跳过 1', '提交失败 1', '已是 Plus', '无 AT']) assert.ok(ui.text.includes(message), message);
  const refresh = ui.button('刷新账号列表'); assert.equal(refresh.disabled, false); assert.equal(refresh.parent.hidden, false);
  refresh.click(); await settle(); assert.equal(ui.reloads, 2);
});

test('uncertain response cannot submit again or poll payment APIs', async () => {
  const ui = harness(() => { throw new Error('offline'); });
  await ui.open([1, 2]); ui.configure(); ui.select('成功后转入分组', '已开通'); ui.submit(); await settle();
  assert.match(ui.text, /提交结果待核实/); assert.equal(ui.button('开始开通 Plus').disabled, true);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：已开通/);
  assert.equal(ui.control('成功后转入分组').disabled, true); assert.equal(ui.button('重新加载分组').disabled, true);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1); assert.equal(ui.timers.size, 0);
});

test('definitive rejection clears secrets and requires fresh credentials to correct configuration', async () => {
  let count = 0;
  const ui = harness(call => ++count === 1 ? {status: 400, data: {ok: false, error: 'CDK payment-secret / extract-secret invalid'}} : accepted(call.body.account_ids));
  await ui.open([1]); ui.configure('2', 'raw'); ui.control('本次提链 CDK').value = 'extract-secret'; ui.submit(); await settle();
  assert.ok(!ui.text.includes('payment-secret')); assert.ok(!ui.text.includes('extract-secret'));
  assert.equal(ui.paymentKey.value, ''); assert.equal(ui.control('本次提链 CDK').value, '');
  assert.equal(ui.control('成功后转入分组').disabled, false); assert.equal(ui.button('重新加载分组').disabled, false);
  ui.select('成功后转入分组', '已开通');
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1);
  ui.paymentKey.value = 'new-payment'; ui.control('本次提链 CDK').value = 'new-extract'; ui.submit(); await settle(); assert.equal(ui.submissions.length, 2);
  assert.equal(ui.submissions[1].body.success_group, '已开通');
});

test('activation status uses only backend activation state and escapes messages', () => {
  const ui = harness();
  const cell = ui.context.window.ExtractLinks.activationCell;
  assert.equal(cell({scan_request_status: 'succeeded', extract_link_status: 'success'}), '');
  for (const status of ['queued', 'checking', 'extracting', 'paying', 'verifying', 'succeeded', 'failed', 'needs_attention', 'interrupted']) {
    const html = cell({plus_activation_status: status, plus_activation_message: '<img onerror=x>', plus_activation_updated_at: '<script>'});
    assert.ok(!html.includes('<img')); assert.ok(!html.includes('<script>')); assert.match(html, /&lt;img/);
    assert.equal(html.includes('status-success'), status === 'succeeded');
  }
  assert.match(cell({plus_activation_status: 'verifying'}), /核验真实 Plus/);
  assert.match(cell({plus_activation_status: 'needs_attention'}), /待核实/);
});

test('activation timestamps format backend seconds and ISO strings; invalid values are omitted', () => {
  const ui = harness();
  const cell = value => ui.context.window.ExtractLinks.activationCell({plus_activation_status: 'verifying', plus_activation_updated_at: value});
  const timestamp = 1735689600.125;
  const expected = new Date(timestamp * 1000).toLocaleString();
  for (const value of [timestamp, String(timestamp), new Date(timestamp * 1000).toISOString()]) assert.ok(cell(value).includes(`更新：${expected}`));
  for (const value of [undefined, null, '', 'invalid', '<img onerror=x>', 0, -1, Number.NaN, Number.POSITIVE_INFINITY, 1e99]) {
    const html = cell(value);
    assert.ok(!html.includes('更新：'), String(value));
    assert.ok(!html.includes('Invalid Date')); assert.ok(!html.includes('<img'));
  }
});

test('provider catalogue completing during OrderHub session check restores extraction controls', async () => {
  let releaseCatalogue, releaseSession;
  const ui = harness(() => new Promise(resolve => { releaseSession = resolve; }), [], () => new Promise(resolve => { releaseCatalogue = resolve; }));
  const opening = ui.open([1]);
  ui.select('支付服务商', 'orderhub'); ui.select('支付验证方式', 'session');
  releaseCatalogue({data: {items: providers}}); await opening;
  releaseSession({data: {ok: true, logged_in: true}}); await settle();
  assert.equal(ui.dialog.querySelector('fieldset').disabled, false);
  assert.equal(ui.button('开始开通 Plus').disabled, false);
});

test('success groups default to no transfer, deduplicate names and render names as text', async () => {
  const name = '<img src=x onerror=alert(1)> & "Plus"';
  const ui = harness(undefined, [], undefined, () => ({data: {groups: [{group_name: name}, {group_name: '默认分组'}, {group_name: name}]}}));
  await ui.open([1]);
  const group = ui.control('成功后转入分组');
  assert.equal(group.value, ''); assert.equal(group.required, false);
  assert.deepEqual(group.children.map(node => node.value), ['', '默认分组', name]);
  assert.equal(group.children[2].textContent, name); assert.equal(group.children[2].children.length, 0);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：不自动转移/);
  ui.select('成功后转入分组', name);
  assert.ok(ui.dialog.querySelector('.el-feedback').textContent.includes(`成功后转入分组：${name}`));
  assert.equal(ui.dialog.querySelectorAll('img').length, 0);
  assert.match(ui.text, /真实 Plus 套餐成功后才转入/); assert.match(ui.text, /失败或待核实的账号保留原组/);
  assert.equal(ui.calls.find(call => call.url === '/api/account-groups').options.method, 'GET');
  ui.configure(); ui.submit(); await settle();
  assert.equal(ui.submissions[0].body.success_group, name);
});

test('one selected destination applies to the entire batch and remains locked in flight', async () => {
  let release;
  const account = {id: 1, group_name: '原组'};
  const ui = harness(call => new Promise(resolve => { release = () => resolve(accepted(call.body.account_ids)); }), [account]);
  await ui.open([1, 999, 1]); ui.configure(); ui.select('成功后转入分组', '已开通');
  const group = ui.control('成功后转入分组'), retry = ui.button('重新加载分组');
  ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.account_ids, [1, 999]);
  assert.equal(ui.submissions[0].body.success_group, '已开通');
  assert.equal(group.disabled, true); assert.equal(retry.disabled, true);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：已开通/);
  retry.click(); ui.submit(); await settle();
  assert.equal(ui.calls.filter(call => call.url === '/api/account-groups').length, 1);
  assert.equal(ui.submissions.length, 1); assert.equal(account.group_name, '原组');
  release(); await settle();
  assert.equal(group.disabled, true); assert.equal(account.group_name, '原组');
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：已开通.*已入队 2/);
  ui.dialog.close(); await ui.open([1]);
  assert.equal(ui.control('成功后转入分组').value, '');
});

for (const [label, groupCatalogue] of [
  ['network failure', () => { throw new Error('offline'); }],
  ['HTTP failure', () => ({status: 503, data: {error: '<img onerror=x> 分组暂不可用'}})],
  ['missing group list', () => ({data: {}})],
  ['malformed group entry', () => ({data: {groups: [{group_name: '已开通'}, {group_name: 123}]}})],
]) test(`${label} clears unverified destinations and allows an explicit no-transfer submission`, async () => {
  const ui = harness(undefined, [], undefined, groupCatalogue);
  await ui.open([1]); ui.configure();
  const group = ui.control('成功后转入分组');
  assert.deepEqual(group.children.map(node => node.value), ['', '默认分组']);
  assert.equal(group.value, ''); assert.equal(group.disabled, false);
  assert.equal(ui.button('重新加载分组').disabled, false);
  assert.equal(ui.button('开始开通 Plus').disabled, false);
  assert.match(ui.text, /分组加载失败/); assert.equal(ui.dialog.querySelectorAll('img').length, 0);
  ui.submit(); await settle(); assert.equal(ui.submissions[0].body.success_group, '');
});

test('the default group is selectable even when no groups are returned or loading fails', async () => {
  for (const groupCatalogue of [() => ({data: {groups: []}}), () => { throw new Error('offline'); }]) {
    const ui = harness(undefined, [], undefined, groupCatalogue);
    await ui.open([1]); ui.configure(); ui.select('成功后转入分组', '默认分组'); ui.submit(); await settle();
    assert.equal(ui.submissions[0].body.success_group, '默认分组');
  }
});

test('failed group loading retries without duplicate requests and restores available choices', async () => {
  let count = 0, release;
  const ui = harness(undefined, [], undefined, () => ++count === 1 ? Promise.reject(new Error('offline')) : new Promise(resolve => { release = resolve; }));
  await ui.open([1]); ui.configure();
  const retry = ui.button('重新加载分组'); retry.click(); retry.click();
  assert.equal(count, 2); assert.equal(retry.disabled, true);
  assert.equal(ui.control('成功后转入分组').value, '');
  release({data: {groups: [{group_name: '恢复分组'}]}}); await settle();
  assert.equal(retry.disabled, false); assert.ok(!ui.text.includes('分组加载失败'));
  assert.equal(ui.control('成功后转入分组').value, '');
  ui.select('成功后转入分组', '恢复分组'); ui.submit(); await settle();
  assert.equal(ui.submissions[0].body.success_group, '恢复分组');
});

test('refresh clears a previous custom choice explicitly and failure cannot reuse stale groups', async () => {
  let count = 0;
  const ui = harness(undefined, [], undefined, () => ++count === 1 ? {data: {groups: [{group_name: '旧分组'}]}} : count === 2 ? Promise.reject(new Error('offline')) : {data: {groups: [{group_name: '新分组'}]}});
  await ui.open([1]); ui.configure(); ui.select('成功后转入分组', '旧分组');
  ui.button('重新加载分组').click(); await settle();
  assert.equal(ui.control('成功后转入分组').value, '');
  assert.deepEqual(ui.control('成功后转入分组').children.map(node => node.value), ['', '默认分组']);
  assert.match(ui.text, /原分组选择已清除/); assert.match(ui.text, /分组加载失败/);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：不自动转移/);
  // A forged or stale value must fail validation, never silently become no-transfer.
  ui.select('成功后转入分组', '旧分组'); ui.submit(); await settle();
  assert.equal(ui.submissions.length, 0); assert.match(ui.text, /目标分组不可用/);
  ui.button('重新加载分组').click(); await settle();
  assert.deepEqual(ui.control('成功后转入分组').children.map(node => node.value), ['', '默认分组', '新分组']);
  assert.equal(ui.control('成功后转入分组').value, '');
  ui.select('成功后转入分组', '新分组'); ui.submit(); await settle();
  assert.equal(ui.submissions[0].body.success_group, '新分组');
});

test('slow group loading does not block no-transfer submission or unlock submitted controls', async () => {
  let release;
  const ui = harness(undefined, [], undefined, () => new Promise(resolve => { release = resolve; }));
  const opening = ui.open([1]); await settle(); ui.configure();
  assert.equal(ui.button('开始开通 Plus').disabled, false);
  ui.submit(); await settle();
  assert.equal(ui.submissions[0].body.success_group, '');
  release({data: {groups: [{group_name: '迟到分组'}]}}); await opening;
  assert.equal(ui.control('成功后转入分组').value, '');
  assert.equal(ui.control('成功后转入分组').disabled, true);
  assert.equal(ui.button('重新加载分组').disabled, true);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /成功后转入分组：不自动转移.*已入队 1/);
});

test('group loading completing during account refresh restores controls afterward', async () => {
  let releaseGroups, releaseRefresh;
  const ui = harness(undefined, [], undefined, () => new Promise(resolve => { releaseGroups = resolve; }));
  ui.context.loadAccounts = () => new Promise(resolve => { releaseRefresh = resolve; });
  const opening = ui.open([1]); await settle(); ui.button('刷新账号列表').click();
  releaseGroups({data: {groups: [{group_name: '已开通'}]}}); await opening;
  assert.equal(ui.control('成功后转入分组').disabled, true);
  releaseRefresh(); await settle();
  assert.equal(ui.control('成功后转入分组').disabled, false);
  assert.equal(ui.button('重新加载分组').disabled, false);
});

test('ordinary payment submission and query omit group controls, loading and success_group', async () => {
  for (const mode of ['submitPayment', 'queryPayment']) {
    const ui = harness(() => ({data: {ok: true, items: [{id: 1, status: 'succeeded'}]}}));
    ui.context.crypto.randomUUID = () => 'payment-only';
    await ui.context.window.ExtractLinks[mode]([1]);
    assert.equal(ui.control('成功后转入分组'), undefined);
    assert.equal(ui.button('重新加载分组'), undefined);
    ui.paymentKey.value = 'payment-secret'; ui.submit(); await settle();
    assert.equal(ui.calls.length, 1); assert.equal('success_group' in ui.calls[0].body, false);
    assert.match(ui.calls[0].url, /\/scan-requests(?:\/query)?$/);
  }
});

test('candidate lists explain ordered fallback and keep between one and twenty entries', async () => {
  const ui = harness(); await ui.open([1]);
  assert.match(ui.text, /仅在明确失败时自动切换下一候选/);
  assert.match(ui.text, /结果未知或超时继续查询、核实原任务/);
  assert.match(ui.text, /避免重复支付/);
  for (const kind of ['提链', '支付']) {
    const first = ui.candidate(kind);
    assert.equal(first.button('删除').disabled, true);
    first.button('删除').emit('click'); assert.equal(first.node.isConnected, true);
    assert.equal(first.button('上移').disabled, true); assert.equal(first.button('下移').disabled, true);
    const add = ui.button(`添加${kind}候选`);
    for (let index = 1; index < 20; index++) add.click();
    assert.equal(add.disabled, true);
    add.emit('click');
    assert.equal(first.node.parent.children.length, 20);
    assert.equal(ui.candidate(kind, 19).button('下移').disabled, true);
    assert.match(ui.candidate(kind, 19).node.querySelector('legend').textContent, /候选 20/);
    ui.candidate(kind, 9).button('删除').click();
    assert.equal(add.disabled, false); assert.equal(first.node.parent.children.length, 19);
    add.click(); assert.equal(first.node.parent.children.length, 20);
    for (let index = 0; index < 20; index++) {
      const candidate = ui.candidate(kind, index);
      if (kind === '提链') { candidate.select('提链服务商', '1'); candidate.select('提链 CDK 来源', 'saved:11'); }
      else candidate.key.value = `candidate-key-${index}`;
    }
  }
  ui.submit(); await settle();
  assert.equal(ui.submissions[0].body.extraction.length, 20); assert.equal(ui.submissions[0].body.payment.length, 20);
  assert.equal(ui.submissions[0].body.payment[19].cdk, 'candidate-key-19');
});

test('ordered arrays preserve same-platform independent CDKs, proxies and group after reordering', async () => {
  const ui = harness(); await ui.open([1, 999]); ui.configure('2', 'raw');
  const firstExtract = ui.candidate('提链'), firstPayment = ui.candidate('支付');
  firstExtract.control('本次提链 CDK').value = 'extract-first';
  firstExtract.control('提链代理 URL').value = 'https://user:first@proxy.test';
  ui.button('添加提链候选').click(); const secondExtract = ui.candidate('提链', 1);
  secondExtract.select('提链服务商', '2'); secondExtract.select('提链 CDK 来源', 'raw');
  secondExtract.control('本次提链 CDK').value = 'extract-second';
  secondExtract.control('提链代理 URL').value = 'https://user:second@proxy.test';
  ui.button('添加提链候选').click(); const upi = ui.candidate('提链', 2);
  upi.select('提链服务商', '3'); upi.select('提链 CDK 来源', 'saved:33');
  upi.control('UPI 入口代理').value = 'socks5://proxy:secret@entry.test:1080';
  ui.button('添加支付候选').click(); const secondPayment = ui.candidate('支付', 1); secondPayment.key.value = 'payment-second';
  secondExtract.button('上移').click(); firstPayment.button('下移').click();
  assert.equal(ui.candidate('提链').node, secondExtract.node); assert.equal(ui.candidate('支付').node, secondPayment.node);
  assert.match(secondExtract.node.querySelector('legend').textContent, /候选 1 · 优先尝试/);
  assert.equal(firstExtract.control('本次提链 CDK').value, 'extract-first'); assert.equal(firstPayment.key.value, 'payment-secret');
  ui.select('成功后转入分组', '已开通');
  const summary = ui.dialog.querySelector('.el-feedback').textContent;
  assert.match(summary, /提链顺序：1\..* → 2\..* → 3\./); assert.match(summary, /支付顺序：1\..* → 2\./);
  for (const secret of ['extract-first', 'extract-second', 'payment-secret', 'payment-second', 'user:first', 'proxy:secret']) assert.ok(!ui.text.includes(secret));
  ui.submit(); await settle();
  assert.equal(ui.submissions.length, 1);
  assert.deepEqual(ui.submissions[0].body, {
    account_ids: [1, 999], success_group: '已开通',
    extraction: [
      {provider_id: 2, cdk: 'extract-second', proxy_url: 'https://user:second@proxy.test', link_type: 'upi', payment_amount: 0},
      {provider_id: 2, cdk: 'extract-first', proxy_url: 'https://user:first@proxy.test', link_type: 'upi', payment_amount: 0},
      {provider_id: 3, cdk_id: 33, entry_proxies: ['socks5://proxy:secret@entry.test:1080'], link_type: 'upi', payment_amount: 0},
    ],
    payment: [{provider: 'v1', auth_mode: 'key', cdk: 'payment-second'}, {provider: 'v1', auth_mode: 'key', cdk: 'payment-secret'}],
  });
  for (const input of [firstExtract.control('本次提链 CDK'), secondExtract.control('本次提链 CDK'), firstExtract.control('提链代理 URL'), secondExtract.control('提链代理 URL'), upi.control('UPI 入口代理'), firstPayment.key, secondPayment.key]) assert.equal(input.value, '');
  assert.ok(ui.calls.every(call => ['/api/extract-link/providers', '/api/account-groups', '/api/accounts/activate-plus'].includes(call.url)));
});

test('payment candidates mix independent keys with the OrderHub server session', async () => {
  const ui = harness(call => call.url.endsWith('/session') ? {data: {ok: true, logged_in: true}} : accepted(call.body.account_ids));
  await ui.open([1]); ui.configure();
  ui.button('添加支付候选').click(); const key = ui.candidate('支付', 1);
  key.select('支付服务商', 'orderhub'); key.key.value = 'ohk_candidate';
  ui.button('添加支付候选').click(); const session = ui.candidate('支付', 2);
  session.select('支付服务商', 'orderhub'); session.key.value = 'unused-key';
  session.select('支付验证方式', 'session'); await settle();
  assert.equal(session.key.value, ''); assert.equal(session.key.required, false);
  assert.equal(key.key.value, 'ohk_candidate'); assert.match(ui.text, /所有 session 候选共用服务器当前 OrderHub 登录会话/);
  ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.extraction, {provider_id: 1, cdk_id: 11});
  assert.deepEqual(ui.submissions[0].body.payment, [{provider: 'v1', auth_mode: 'key', cdk: 'payment-secret'}, {provider: 'orderhub', auth_mode: 'key', cdk: 'ohk_candidate'}, {provider: 'orderhub', auth_mode: 'session'}]);
  assert.ok(!ui.submissions[0].options.body.includes('unused-key'));
});

test('OrderHub logout invalidates every session candidate without clearing other candidate keys', async () => {
  let loggedIn = true;
  const ui = harness(call => {
    if (call.url.endsWith('/logout')) { loggedIn = false; return {data: {ok: true}}; }
    if (call.url.endsWith('/session')) return {data: {ok: true, logged_in: loggedIn}};
    return accepted(call.body.account_ids);
  });
  await ui.open([1]); ui.configure();
  for (let index = 1; index <= 2; index++) {
    ui.button('添加支付候选').click(); const candidate = ui.candidate('支付', index);
    candidate.select('支付服务商', 'orderhub'); candidate.select('支付验证方式', 'session'); await settle();
  }
  ui.candidate('支付', 2).button('退出 OrderHub').click(); await settle();
  assert.equal(ui.paymentKey.value, 'payment-secret');
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
  assert.match(ui.text, /支付候选 2：请先登录 OrderHub/);
});

test('each candidate validates before the batch request and session auth is exclusive to OrderHub', async () => {
  const ui = harness(); await ui.open([1]); ui.configure();
  ui.button('添加提链候选').click(); const extract = ui.candidate('提链', 1);
  extract.select('提链服务商', '2'); extract.select('提链 CDK 来源', 'saved:11');
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0); assert.match(ui.text, /提链候选 2：请选择当前服务商可用的提链 CDK/);
  extract.select('提链 CDK 来源', 'saved:22');
  ui.button('添加支付候选').click(); const pay = ui.candidate('支付', 1);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0); assert.match(ui.text, /支付候选 2：请输入本次使用的支付 CDK/);
  pay.key.value = 'other-key'; pay.select('支付验证方式', 'session');
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0); assert.match(ui.text, /支付候选 2：仅 OrderHub 支持 session/);
  assert.equal(ui.calls.filter(call => call.url.includes('/orderhub/')).length, 0);
  pay.select('支付验证方式', 'key'); pay.key.value = 'valid-key'; ui.submit(); await settle();
  assert.equal(ui.submissions.length, 1);
});

test('deleting a candidate clears all its credentials and remaining singleton keeps the old object shape', async () => {
  const ui = harness(); await ui.open([1]); ui.configure();
  ui.button('添加提链候选').click(); const extract = ui.candidate('提链', 1);
  extract.select('提链服务商', '2'); extract.select('提链 CDK 来源', 'raw');
  const secrets = [extract.control('本次提链 CDK'), extract.control('提链代理 URL'), extract.control('UPI 入口代理')];
  ui.button('添加支付候选').click(); const pay = ui.candidate('支付', 1);
  secrets.push(pay.key, pay.control('OrderHub 数字账号'), pay.control('OrderHub 登录密码'));
  secrets.forEach(input => { input.value = 'remove-secret'; });
  extract.button('删除').click(); pay.button('删除').click();
  assert.equal(extract.node.isConnected, false); assert.equal(pay.node.isConnected, false);
  for (const input of secrets) assert.equal(input.value, '');
  assert.equal(ui.paymentKey.value, 'payment-secret'); assert.equal(ui.candidate('支付').button('删除').disabled, true);
  ui.submit(); await settle();
  assert.deepEqual(ui.submissions[0].body.extraction, {provider_id: 1, cdk_id: 11});
  assert.deepEqual(ui.submissions[0].body.payment, {provider: 'v1', auth_mode: 'key', cdk: 'payment-secret'});
  assert.ok(!ui.submissions[0].options.body.includes('remove-secret'));
});

test('changing one candidate clears its own credentials without changing other candidates', async () => {
  const ui = harness(); await ui.open([1]); ui.configure('2', 'raw');
  ui.control('本次提链 CDK').value = 'keep-extract';
  ui.button('添加提链候选').click(); const extract = ui.candidate('提链', 1);
  extract.select('提链服务商', '2'); extract.select('提链 CDK 来源', 'raw');
  extract.control('本次提链 CDK').value = 'clear-extract'; extract.control('提链代理 URL').value = 'https://user:secret@proxy.test';
  extract.select('提链服务商', '1');
  assert.equal(extract.control('本次提链 CDK').value, ''); assert.equal(extract.control('提链代理 URL').value, '');
  assert.equal(ui.control('本次提链 CDK').value, 'keep-extract');
  ui.button('添加支付候选').click(); const pay = ui.candidate('支付', 1);
  pay.key.value = 'clear-pay'; pay.control('OrderHub 登录密码').value = 'clear-password';
  pay.select('支付服务商', 'masi');
  assert.equal(pay.key.value, ''); assert.equal(pay.control('OrderHub 登录密码').value, ''); assert.equal(ui.paymentKey.value, 'payment-secret');
});

test('closing before submission clears credentials in every candidate and reopening starts with one empty entry', async () => {
  const ui = harness(); await ui.open([1]);
  ui.button('添加提链候选').click(); ui.button('添加支付候选').click();
  const inputs = ui.dialog.querySelectorAll('input, textarea'); inputs.forEach(input => { input.value = 'close-secret'; });
  ui.button('关闭').click();
  for (const input of inputs) assert.equal(input.value, '');
  await ui.open([1]);
  assert.equal(ui.dialog.querySelectorAll('.el-activation-candidate').length, 2);
  assert.equal(ui.paymentKey.value, ''); assert.equal(ui.control('本次提链 CDK').value, ''); assert.equal(ui.submissions.length, 0);
});

test('in-flight array submission locks list editing and closing never aborts the backend batch', async () => {
  let release;
  const ui = harness(call => new Promise(resolve => { release = () => resolve(accepted(call.body.account_ids)); }));
  await ui.open([1, 2]); ui.configure();
  ui.button('添加支付候选').click(); const second = ui.candidate('支付', 1); second.key.value = 'second-secret';
  ui.submit(); await settle();
  assert.equal(second.key.value, ''); assert.equal(ui.paymentKey.value, '');
  for (const label of ['添加提链候选', '添加支付候选', '重新加载分组']) assert.equal(ui.button(label).disabled, true);
  for (const action of ['删除', '上移', '下移']) assert.equal(second.button(action).disabled, true);
  second.button('删除').emit('click'); assert.equal(second.node.isConnected, true);
  assert.equal(ui.submissions.length, 1); assert.equal(ui.submissions[0].options.signal, undefined);
  ui.button('关闭').click(); release(); await settle();
  assert.equal(ui.reloads, 1); assert.equal(ui.timers.size, 0); assert.equal(ui.submissions.length, 1);
});

test('errors redact every candidate secret, including overlapping values, and definitive rejection unlocks editing', async () => {
  const secrets = ['payment-secret', 'payment-secret-extended', 'extract-secret', 'extract-secret-extended', 'http://user:secret@proxy.test', 'socks5://user:other@proxy.test'];
  const ui = harness(() => ({status: 400, data: {ok: false, error: secrets.join(' / ')}}));
  await ui.open([1]); ui.configure('2', 'raw'); ui.control('本次提链 CDK').value = secrets[2]; ui.control('提链代理 URL').value = secrets[4];
  ui.button('添加提链候选').click(); const extract = ui.candidate('提链', 1);
  extract.select('提链服务商', '3'); extract.select('提链 CDK 来源', 'raw'); extract.control('本次提链 CDK').value = secrets[3]; extract.control('UPI 入口代理').value = secrets[5];
  ui.button('添加支付候选').click(); ui.candidate('支付', 1).key.value = secrets[1];
  ui.submit(); await settle();
  for (const secret of secrets) assert.ok(!ui.text.includes(secret));
  assert.ok(!ui.dialog.querySelector('.el-error').textContent.includes('-extended'));
  for (const input of ui.dialog.querySelectorAll('input, textarea')) assert.equal(input.value, '');
  assert.equal(ui.button('添加支付候选').disabled, false); assert.equal(ui.candidate('支付', 1).button('删除').disabled, false);
  assert.match(ui.dialog.querySelector('.el-feedback').textContent, /待输入/);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1);
});

for (const [label, result] of [
  ['HTTP 408', {status: 408, data: {ok: false, error: '等待超时'}}],
  ['HTTP 504', {status: 504, data: {ok: false, error: '网关超时'}}],
  ['null response', {data: null}],
  ['invalid array response', {data: []}],
  ['malformed account result', {data: {ok: true, started: [null]}}],
]) test(`${label} cannot switch or resubmit payment candidates`, async () => {
  const ui = harness(() => result); await ui.open([1]); ui.configure();
  ui.button('添加支付候选').click(); ui.candidate('支付', 1).key.value = 'backup-secret';
  ui.submit(); await settle();
  assert.match(ui.text, /提交结果待核实/); assert.equal(ui.button('开始开通 Plus').disabled, true);
  assert.equal(ui.button('添加支付候选').disabled, true); assert.equal(ui.candidate('支付', 1).button('删除').disabled, true);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1); assert.equal(ui.timers.size, 0);
  assert.ok(ui.calls.every(call => ['/api/extract-link/providers', '/api/account-groups', '/api/accounts/activate-plus'].includes(call.url)));
});

test('account refresh failure after acceptance cannot unlock or resubmit the batch', async () => {
  const ui = harness(); ui.context.loadAccounts = async () => { throw new Error('refresh failed'); };
  await ui.open([1]); ui.configure(); ui.button('添加支付候选').click(); ui.candidate('支付', 1).key.value = 'second-key';
  ui.submit(); await settle();
  assert.equal(ui.button('开始开通 Plus').disabled, true); assert.equal(ui.button('添加支付候选').disabled, true);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 1);
});

test('candidate OrderHub session checks retain the existing timeout and keep keys in other candidates', async () => {
  const ui = harness(call => new Promise((_, reject) => call.options.signal.addEventListener('abort', () => reject(new Error('aborted')))));
  await ui.open([1]); ui.configure(); ui.button('添加支付候选').click();
  const session = ui.candidate('支付', 1); session.select('支付服务商', 'orderhub'); session.select('支付验证方式', 'session');
  const timer = [...ui.timers.values()].find(timer => timer.delay === 45000); assert.ok(timer);
  timer.callback(); await settle();
  assert.match(ui.text, /登录状态待核实/); assert.equal(ui.paymentKey.value, 'payment-secret');
  assert.equal(ui.timers.size, 0); assert.equal(ui.button('添加支付候选').disabled, false);
  ui.submit(); await settle(); assert.equal(ui.submissions.length, 0);
});

test('one delayed catalogue populates every extraction candidate without making extra requests', async () => {
  let release;
  const ui = harness(undefined, [], () => new Promise(resolve => { release = resolve; }));
  const opening = ui.open([1]); ui.button('添加提链候选').click();
  release({data: {items: providers}}); await opening;
  for (let index = 0; index < 2; index++) {
    const candidate = ui.candidate('提链', index);
    assert.deepEqual(candidate.control('提链服务商').children.map(option => option.value), ['', '1', '2', '3', '0']);
    assert.equal(candidate.control('提链服务商').value, ''); assert.equal(candidate.node.querySelector('fieldset').disabled, false);
  }
  assert.equal(ui.calls.filter(call => call.url === '/api/extract-link/providers').length, 1);
});

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  const end = source.indexOf('\n}', start) + 2;
  return source.slice(start, end);
}
for (const legacy of [false, true]) test(`${legacy ? 'legacy' : 'modern'} page selection, click binding, rendering and lightweight status polling`, async () => {
  const html = fs.readFileSync(path.join(root, legacy ? 'webui/templates/index_legacy.html' : 'webui/templates/index.html'), 'utf8');
  const script = legacy ? html : fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');
  const buttonId = legacy ? 'btnActivateSelectedPlus' : 'btnActivateSelectedPlusV2';
  assert.match(html, new RegExp(`id="${buttonId}" disabled`));
  const nodes = new Map();
  const node = id => { if (!nodes.has(id)) nodes.set(id, {disabled: true, events: {}, addEventListener(name, fn) { this.events[name] = fn; }}); return nodes.get(id); };
  const selected = new Set(), account = {id: 1, plus_activation_status: 'queued'};
  let clicked = [], renders = 0, reloads = 0, snapshot = {revision: 'paying', total: 1, items: [{id: 1, plus_activation_status: 'paying', plus_activation_message: '付款中', plus_activation_updated_at: 'now'}]};
  const context = {
    $: selector => node(selector.slice(1)), document: {getElementById: node, querySelectorAll: () => []}, ACCOUNT_SELECTED: selected, ACCOUNTS: [account], ACCOUNTS_TOTAL: 1,
    SHOW_ARCHIVED_ACCOUNTS: false, planStatusLoading: false, accountsLoading: false, planStatusRevision: '', PAGERS: {accounts: {page: 1, size: 20}},
    getAccountsPlanFilter: () => '', getAccountsCodexFilter: () => '', getAccountsTotpFilter: () => '', getAccountsGroupFilter: () => '', getAccountsQuery: () => '',
    api: async url => { assert.match(url, /^\/api\/accounts\/plan-check-status\?/); return snapshot; },
    renderAccounts: () => { renders++; }, loadAccounts: async () => { reloads++; }, window: {ExtractLinks: {activatePlus: ids => { clicked = Array.from(ids); }}},
    showToast: message => { throw new Error(message); },
  };
  node('qAccounts').value = ''; node('dateFromAccountsV2').value = ''; node('dateToAccountsV2').value = '';
  vm.createContext(context);
  vm.runInContext(functionSource(script, 'updateAccountSelectionUi'), context);
  context.updateAccountSelectionUi(); assert.equal(node(buttonId).disabled, true);
  selected.add(1); selected.add(999); context.updateAccountSelectionUi(); assert.equal(node(buttonId).disabled, false);
  let binding;
  if (legacy) binding = script.slice(script.indexOf(`$('#${buttonId}').addEventListener`), script.indexOf("$('#btnExtractSelectedLinks').addEventListener"));
  else binding = script.slice(script.indexOf(`  bind('${buttonId}'`), script.indexOf("  bind('btnExtractSelectedLinksV2'"));
  context.bind = (id, fn) => node(id).addEventListener('click', fn);
  vm.runInContext(binding, context); node(buttonId).events.click(); assert.deepEqual(clicked, [1, 999]);
  assert.ok(script.includes('${_activationCell(r)}'));
  vm.runInContext('async ' + functionSource(script, 'pollAccountPlanStatuses'), context);
  await context.pollAccountPlanStatuses(); assert.equal(account.plus_activation_status, 'paying'); assert.equal(account.plus_activation_message, '付款中'); assert.equal(account.plus_activation_updated_at, 'now'); assert.equal(renders, 1); assert.equal(reloads, 0);
  snapshot = {revision: 'succeeded', total: 1, items: [{id: 1, plus_activation_status: 'succeeded'}]};
  await context.pollAccountPlanStatuses(); assert.equal(reloads, 1);
});
