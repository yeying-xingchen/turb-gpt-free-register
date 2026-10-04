// Run: node --test tests/test_scan_payment_ui.js
// This harness exercises user actions against the real shared UI, with no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../webui/static/extract-links.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness(respond, accounts = []) {
  let body;
  class Element {
    constructor(tag) {
      Object.assign(this, {tagName: tag, children: [], attrs: {}, events: {}, value: '', disabled: false, hidden: false, required: false, _text: '', parent: null});
    }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    set textContent(value) { this._text = String(value); this.children = []; }
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
      visit(this);
      return output;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    reportValidity() { return this.querySelectorAll('input').every(input => !input.required || input.disabled || !!input.value); }
  }
  body = new Element('body');
  const document = {body, activeElement: body, createElement: tag => new Element(tag), addEventListener() {}};
  const timers = new Map();
  let timerId = 0, now = 10000, uuids = 0;
  const calls = [];
  const forbiddenStorage = new Proxy({}, {get() { throw new Error('Payment credentials must not access browser storage'); }});
  const context = {
    document, window: {}, ACCOUNTS: accounts, ACCOUNT_SELECTED_ROWS: new Map(), renderAccounts() {}, loadAccounts: async () => {},
    URL, AbortController, Date: class extends Date { static now() { return now; } }, Math, crypto: {randomUUID: () => `test-${++uuids}`},
    setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; }, clearTimeout: id => timers.delete(id),
    localStorage: forbiddenStorage, sessionStorage: forbiddenStorage,
    fetch: async (url, options) => {
      const call = {url, options, body: options.body ? JSON.parse(options.body) : undefined};
      calls.push(call);
      const result = await respond(call, calls.length);
      return {ok: (result.status || 200) < 400, status: result.status || 200, json: async () => result.data};
    },
  };
  vm.runInNewContext(source, context);
  return {
    calls, context, timers,
    get dialog() { return body.querySelector('dialog'); },
    get text() { return body.textContent; },
    get uuidCount() { return uuids; },
    get secret() { return this.dialog.querySelectorAll('input').find(input => input.attrs.type === 'password' && !input.attrs['aria-label']); },
    input(label) { return this.dialog.querySelectorAll('input').find(input => input.attrs['aria-label'] === label); },
    select(label, value) { const node = this.dialog.querySelectorAll('select').find(input => input.attrs['aria-label'] === label); node.value = value; node.emit('change'); return node; },
    advance(delay) { now += delay; },
    fire(delay) { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry, `expected timer ${delay}`); timers.delete(entry[0]); now += delay; entry[1].callback(); },
    async open(mode, ids) { await context.window.ExtractLinks[mode](ids); return this.dialog; },
    submit(secret = 'test-cdk') { if (secret !== null) this.secret.value = secret; this.dialog.querySelector('form').emit('submit'); },
    button(label) { return this.dialog.querySelectorAll('button').find(button => button.textContent.includes(label)); },
  };
}
const response = (id, category, status, extra = {}) => ({data: {ok: true, [category]: [{id, status, ...extra}]}});
const waitingFetch = call => new Promise((_, reject) => call.options.signal.addEventListener('abort', () => reject(new Error('aborted'))));

test('cross-page IDs submit sequentially; pending remains distinct and polling ends at terminal states', async () => {
  let release;
  const ui = harness((call, count) => count === 1 ? new Promise(resolve => { release = resolve; }) : count === 2 ? {status: 202, data: {ok: true, pending: [{id: 99, status: 'submission_pending'}]}} : response(call.body.account_ids[0], 'items', 'succeeded'));
  await ui.open('submitPayment', [1, 99, 1]); ui.submit(); await settle();
  assert.equal(ui.calls.length, 1); assert.equal(ui.secret.value, '');
  ui.dialog.querySelector('form').emit('submit'); assert.equal(ui.calls.length, 1);
  release(response(1, 'created', 'queued', {task_id: 't1'})); await settle();
  assert.deepEqual(ui.calls.map(call => call.body.account_ids), [[1], [99]]);
  assert.equal(ui.calls[0].body.provider, 'v1');
  assert.equal(ui.calls[0].body.idempotency_key, ui.calls[1].body.idempotency_key);
  assert.match(ui.text, /提交结果待确认/); assert.equal(ui.dialog.querySelectorAll('pre').length, 0);
  ui.fire(5000); await settle(); assert.equal(ui.calls.length, 4);
  assert.ok(ui.calls.slice(2).every(call => call.url.endsWith('/query') && call.body.cdk === 'test-cdk' && !('provider' in call.body)));
  assert.equal(ui.timers.size, 0);
});

for (const provider of ['v1', 'orderhub']) {
  test(`${provider} transport failure stops batch and explicit retry keeps original key`, async () => {
    const ui = harness((call, count) => { if (count === 1) throw new Error('transport'); return response(call.body.account_ids[0], 'duplicated', 'queued', {provider, task_id: `t${count}`}); });
    await ui.open('submitPayment', [1, 2]); ui.select('支付服务商', provider); ui.submit(); await settle();
    assert.equal(ui.calls.length, 1); assert.equal(ui.timers.size, 0);
    assert.equal(ui.button('原请求').hidden, false); ui.button('原请求').click(); await settle();
    assert.equal(ui.calls.length, 2); assert.equal(ui.calls[0].body.idempotency_key, ui.calls[1].body.idempotency_key); assert.equal(ui.uuidCount, 1);
    ui.dialog.querySelector('form').emit('submit'); await settle();
    assert.equal(ui.calls.length, 3); assert.deepEqual(ui.calls[2].body.account_ids, [2]);
    assert.equal(ui.calls[2].body.idempotency_key, ui.calls[0].body.idempotency_key);
    ui.dialog.close();
  });
}

test('Masi unknown never enables resubmission, even with retry flag', async () => {
  const ui = harness(call => response(call.body.account_ids[0], 'unknown', 'unknown', {provider: 'masi', can_retry_same_key: true}));
  await ui.open('submitPayment', [4, 5]); ui.select('支付服务商', 'masi'); ui.submit(); await settle();
  assert.equal(ui.calls.length, 1); assert.equal(ui.calls[0].body.provider, 'masi');
  assert.equal(ui.button('原请求').hidden, true); assert.equal(ui.timers.size, 0);
  assert.equal('access_token' in ui.calls[0].body, false);
});

test('queries accept selected IDs absent from local rows and without task IDs', async () => {
  const ui = harness(call => response(call.body.account_ids[0], 'unknown', 'unknown', {message: '没有任务 ID', can_retry_same_key: true}));
  await ui.open('queryPayment', [888]); ui.submit(); await settle();
  assert.deepEqual(ui.calls[0].body.account_ids, [888]); assert.match(ui.calls[0].url, /query$/);
  assert.match(ui.text, /没有任务 ID/); assert.equal(ui.timers.size, 0);
});

test('query errors preserve the last persisted remote task status', async () => {
  const account = {id: 1, scan_request_status: 'queued', scan_request_task_id: 'old', scan_request_provider: 'v1'};
  const ui = harness(() => response(1, 'failed', 'failed', {error: 'CDK 不匹配'}), [account]);
  await ui.open('queryPayment', [1]); ui.submit(); await settle();
  assert.equal(account.scan_request_status, 'queued'); assert.equal(account.scan_request_task_id, 'old');
  assert.match(ui.text, /上次保存状态/); assert.equal(ui.timers.size, 0);
});

test('closing clears credentials, stops remaining submissions and requires input on reopening', async () => {
  const ui = harness(waitingFetch);
  await ui.open('submitPayment', [1, 2]); ui.submit(); await settle();
  const input = ui.secret; ui.dialog.close(); await settle();
  assert.equal(input.value, ''); assert.equal(ui.calls.length, 1); assert.equal(ui.timers.size, 0);
  await ui.open('queryPayment', [1]); ui.dialog.querySelector('form').emit('submit'); await settle();
  assert.equal(ui.calls.length, 1);
});

test('request timeout only ends local waiting and permits same-key retry', async () => {
  const ui = harness(waitingFetch);
  await ui.open('submitPayment', [1, 2]); ui.submit(); await settle(); ui.fire(45000); await settle();
  assert.equal(ui.calls.length, 1); assert.match(ui.text, /服务器任务没有取消/); assert.equal(ui.button('原请求').hidden, false);
});

test('automatic polling stops after ten minutes without another network call', async () => {
  const ui = harness(() => response(1, 'pending', 'submission_pending'));
  await ui.open('submitPayment', [1]); ui.submit(); await settle(); ui.advance(600001); ui.fire(5000); await settle();
  assert.equal(ui.calls.length, 1); assert.equal(ui.timers.size, 0); assert.match(ui.text, /10 分钟/);
});

test('persisted payment status escapes untrusted text and does not require extraction state', () => {
  const ui = harness(() => { throw new Error('Unexpected network'); });
  const html = ui.context.window.ExtractLinks.paymentCell({scan_request_status: 'unknown', scan_request_task_id: '<script>', scan_request_message: '<img onerror=x>', scan_request_provider: 'masi'});
  assert.match(html, /&lt;script&gt;/); assert.match(html, /&lt;img onerror=x&gt;/); assert.match(html, /Masi/); assert.ok(!html.includes('<script>'));
});

test('OrderHub API key submission delegates account AT retrieval to backend', async () => {
  const ui = harness(call => response(call.body.account_ids[0], 'created', 'queued', {provider: 'orderhub', task_id: 'hub1'}));
  await ui.open('submitPayment', [7]); ui.select('支付服务商', 'orderhub'); ui.submit('ohk_test'); await settle();
  assert.equal(ui.calls[0].body.provider, 'orderhub'); assert.equal(ui.calls[0].body.auth_mode, 'key');
  assert.equal(ui.calls[0].body.cdk, 'ohk_test'); assert.equal('access_token' in ui.calls[0].body, false);
  ui.dialog.close();
});

test('OrderHub login clears password; session submit and polls omit CDK and use original task lookup', async () => {
  const ui = harness(call => call.url.endsWith('/session') ? {data: {ok: true, logged_in: false}} : call.url.endsWith('/login') ? {data: {ok: true, user: {username: '123456'}}} : response(call.body.account_ids[0], call.url.endsWith('/query') ? 'items' : 'created', call.url.endsWith('/query') ? 'succeeded' : 'queued', {provider: 'orderhub', task_id: 'hub-session'}));
  await ui.open('submitPayment', [7]); ui.select('支付服务商', 'orderhub'); ui.select('支付验证方式', 'session'); await settle();
  assert.equal(ui.calls[0].options.method, 'GET');
  ui.input('OrderHub 数字账号').value = '123456'; ui.input('OrderHub 登录密码').value = 'secret-password';
  ui.button('登录 OrderHub').click(); await settle();
  assert.equal(ui.input('OrderHub 登录密码').value, ''); assert.equal(ui.calls[1].body.password, 'secret-password');
  ui.submit(null); await settle();
  assert.equal(ui.calls[2].body.auth_mode, 'session'); assert.equal('cdk' in ui.calls[2].body, false); assert.equal(ui.calls[2].body.provider, 'orderhub');
  assert.equal(ui.button('退出 OrderHub').disabled, true);
  ui.fire(5000); await settle();
  assert.equal(ui.calls[3].body.auth_mode, 'session'); assert.equal('cdk' in ui.calls[3].body, false); assert.equal('provider' in ui.calls[3].body, false);
  assert.equal(ui.timers.size, 0); ui.dialog.close();
});


test('dynamic payment deadlines are displayed without inferring terminal status', async () => {
  const ui = harness(() => response(1, 'items', 'verifying', {provider: 'orderhub', task_id: 'task1', task: {paymentWindowEndsAt: 1, paymentExpiryKind: 'dynamic', verificationDeadline: 2}}));
  await ui.open('queryPayment', [1]); ui.submit('ohk_test'); await settle();
  assert.match(ui.text, /核验中/); assert.match(ui.text, /本轮支付期限/); assert.match(ui.text, /核验截止时间/);
  assert.match(ui.text, /以平台重新核验结果为准/);
  assert.ok([...ui.timers.values()].some(timer => timer.delay === 5000));
  ui.dialog.close();
});
