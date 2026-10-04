// Run: node --test tests/test_redeem_recovery_ui.js
// Execute the complete inline redemption controller against an offline DOM/fetch harness.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createHash, webcrypto} = require('node:crypto');
const test = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../webui/templates/redeem.html'), 'utf8');
const script = html.match(/<script>([^]*?)<\/script>/)[1];
const CDK = 'PRIVATE-CDK-123';
const CREDENTIALS = ['user@example.test---private-password---private-totp'];
const KEY = 'redeem_recovery_v1';
const storageKey = cdk => 'redeem_recovery_v2:' + createHash('sha256').update(String(cdk).replace(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/g, '').toUpperCase()).digest('hex');
const settle = () => new Promise(resolve => setImmediate(resolve));
let entropySequence = 0;

function storage() {
  return {
    values: new Map(), writes: [], failGet: false, failSet: false, dropWrites: false,
    getItem(key) { if (this.failGet) throw new Error('Storage blocked'); return this.values.get(key) ?? null; },
    setItem(key, value) {
      if (this.failSet) throw new Error('Quota exceeded');
      this.writes.push([key, value]);
      if (!this.dropWrites) this.values.set(key, value);
    },
  };
}
function success(extra = {}) {
  return {status: 200, body: {ok: true, count: 1, remaining: 0, credentials: CREDENTIALS,
    download_url: '/api/redeem/download/original-batch', filename: 'credentials.txt', ...extra}};
}
function harness({saved = storage(), respond = async () => success(), cryptoAvailable = true, storageAvailable = true, nativeDigest = null, textEncoderAvailable = true} = {}) {
  class Element {
    constructor(id = '') {
      Object.assign(this, {id, value: '', disabled: false, readOnly: false, className: '', attrs: {}, events: {}, children: [], text: '', html: ''});
      this.classList = {
        contains: name => this.className.split(' ').includes(name),
        toggle: (name, on) => { const names = new Set(this.className.split(' ').filter(Boolean)); if (on) names.add(name); else names.delete(name); this.className = [...names].join(' '); },
      };
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(' '); }
    set innerHTML(value) { this.html = String(value); this.children = []; }
    get innerHTML() { return this.html; }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    removeAttribute(key) { delete this.attrs[key]; }
    append(...nodes) { this.children.push(...nodes); }
    prepend(node) { this.children.unshift(node); }
    addEventListener(name, fn) { this.events[name] = fn; }
    focus() { this.focused = true; }
  }
  const ids = ['redeemForm', 'cdk', 'quantity', 'submit', 'submitLabel', 'result', 'redeemedResults', 'publicStockList', 'refreshStock', 'stockUpdated'];
  const nodes = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  nodes.quantity.value = '1'; nodes.submitLabel.textContent = '立即兑换'; nodes.result.className = 'result';
  const calls = [];
  let randomCalls = 0;
  const win = {location: {origin: 'http://localhost:5000'}, crypto: cryptoAvailable ? {
    getRandomValues(bytes) { assert.equal(bytes.length, 16); randomCalls++; const entropy = ++entropySequence; for (let i = 0; i < bytes.length; i++) bytes[i] = (entropy + i) % 256; return bytes; },
  } : undefined};
  if (cryptoAvailable && nativeDigest) win.crypto.subtle = {digest: nativeDigest};
  Object.defineProperty(win, 'sessionStorage', {get() { if (!storageAvailable) throw new Error('Storage denied'); return saved; }});
  const context = {
    window: win, URL, Uint8Array, TypeError, TextEncoder: textEncoderAvailable ? TextEncoder : undefined,
    localStorage: new Proxy({}, {get() { throw new Error('Redemption must not use localStorage'); }}),
    document: {getElementById: id => nodes[id], querySelectorAll: () => [new Element(), new Element(), new Element()], createElement: () => new Element()},
    fetch: async (url, opts) => {
      if (url === '/api/redeem/public-stock') return {ok: true, json: async () => ({groups: []})};
      assert.equal(url, '/api/redeem');
      const body = JSON.parse(opts.body);
      const state = JSON.parse(saved.values.get(storageKey(body.cdk)));
      assert.equal(state.request_id, body.request_id, 'request ID must be durable before sending');
      assert.equal(state.completed, false);
      const call = {url, opts, body}; calls.push(call);
      const reply = await respond(call, calls.length);
      return {ok: reply.status >= 200 && reply.status < 300, status: reply.status, json: async () => reply.body};
    },
  };
  vm.runInNewContext(script, context);
  return {
    context, nodes, calls, saved, get randomCalls() { return randomCalls; },
    submit(cdk = CDK, quantity = 1) { nodes.cdk.value = cdk; nodes.quantity.value = String(quantity); return nodes.redeemForm.events.submit({preventDefault() {}}); },
    input(cdk) { nodes.cdk.value = cdk; nodes.cdk.events.input(); },
    get state() { return JSON.parse(saved.values.get(storageKey(CDK))); },
    stateFor(cdk) { return JSON.parse(saved.values.get(storageKey(cdk))); },
    get resultText() { return nodes.result.textContent; },
    get batchText() { return nodes.redeemedResults.textContent; },
  };
}

function assertOnlyRecoveryState(saved) {
  for (const [key, value] of saved.writes) {
    assert.match(key, /^redeem_recovery_v2:[a-f0-9]{64}$/);
    const state = JSON.parse(value);
    assert.deepEqual(Object.keys(state).sort(), ['completed', 'request_id']);
    assert.match(state.request_id, /^[A-Za-z0-9_-]{32,128}$/);
    assert.equal(typeof state.completed, 'boolean');
    for (const secret of [CDK, ...CREDENTIALS, 'private-password', '/api/redeem/download/']) assert.ok(!value.includes(secret));
  }
}

for (const [value, expected] of [
  ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
  ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
  ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
]) {
  test(`SHA-256 HTTP fallback matches the standard ${value.length}-byte vector`, () => {
    const ui = harness();
    assert.equal(ui.context.sha256Fallback(new TextEncoder().encode(value)), expected);
  });
}

test('SHA-256 fallback matches Node across padding boundaries and UTF-8 input', () => {
  const ui = harness();
  for (const value of [...[55, 56, 63, 64, 65, 127, 128, 129].map(length => 'a'.repeat(length)), '分组-兑换-Äß-🔑']) {
    assert.equal(ui.context.sha256Fallback(new TextEncoder().encode(value)), createHash('sha256').update(value).digest('hex'));
  }
});

test('native WebCrypto is preferred, and native/fallback modes share the same index', async () => {
  const saved = storage(); let nativeCalls = 0;
  const native = harness({saved, nativeDigest: async (algorithm, bytes) => { nativeCalls++; return webcrypto.subtle.digest(algorithm, bytes); }});
  native.context.sha256Fallback = () => { throw new Error('Native digest should be preferred'); };
  await native.submit();
  assert.equal(nativeCalls, 1);
  const fallback = harness({saved});
  await fallback.submit();
  assert.equal(fallback.calls[0].body.request_id, native.calls[0].body.request_id);
  assert.equal(fallback.randomCalls, 0);
});

test('a rejected native digest and missing TextEncoder still use standard SHA-256', async () => {
  const ui = harness({nativeDigest: async () => { throw new Error('Digest unavailable'); }, textEncoderAvailable: false});
  const cdk = '分组-兑换-Äß-🔑';
  assert.equal(await ui.context.cdkRecoveryKey(cdk), storageKey(cdk));
  await ui.submit(cdk);
  assert.equal(ui.calls.length, 1);
  assertOnlyRecoveryState(ui.saved);
});

test('duplicate submit while native digest is suspended creates just one ID and POST', async () => {
  let release;
  const ui = harness({nativeDigest: (algorithm, bytes) => new Promise(resolve => { release = async () => resolve(await webcrypto.subtle.digest(algorithm, bytes)); })});
  const first = ui.submit();
  assert.equal(ui.nodes.submit.disabled, true);
  assert.equal(ui.nodes.cdk.readOnly, true);
  await ui.submit();
  await settle();
  assert.equal(ui.calls.length, 0);
  assert.equal(ui.randomCalls, 0);
  await release();
  await first;
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.randomCalls, 1);
  assert.equal(ui.nodes.submit.disabled, false);
  assert.equal(ui.nodes.cdk.readOnly, false);
});

for (const reload of [false, true]) {
  test(`unknown A survives successful B and B's next batch${reload ? ' across refresh' : ''}`, async () => {
    const saved = storage();
    const ui = harness({saved, respond: async (call, count) => {
      if (count === 1) throw new TypeError('A response lost');
      return success({remaining: 2, resumed: call.body.cdk === CDK});
    }});
    await ui.submit(CDK);
    const originalA = ui.state.request_id;
    ui.input('SECOND-CDK');
    await ui.submit('SECOND-CDK');
    const firstB = ui.stateFor('SECOND-CDK').request_id;
    await ui.submit('SECOND-CDK');
    const secondB = ui.stateFor('SECOND-CDK').request_id;
    assert.notEqual(secondB, firstB);
    assert.equal(ui.state.request_id, originalA);
    assert.equal(ui.state.completed, false);
    const returning = reload ? harness({saved, respond: async () => success({resumed: true, remaining: 2})}) : ui;
    returning.input(CDK);
    await returning.submit(CDK);
    assert.equal(returning.calls.at(-1).body.request_id, originalA);
    assert.equal(returning.stateFor('SECOND-CDK').request_id, secondB);
    assert.equal(returning.stateFor('SECOND-CDK').completed, true);
    assert.equal(returning.state.completed, true);
    if (reload) assert.equal(returning.randomCalls, 0);
    assertOnlyRecoveryState(saved);
  });
}

test('completed records for several codes are each recovered after refresh before allowing new batches', async () => {
  const saved = storage();
  const before = harness({saved, respond: async () => success({remaining: 2})});
  await before.submit(CDK);
  await before.submit('SECOND-CDK');
  const ids = [before.state.request_id, before.stateFor('SECOND-CDK').request_id];
  const after = harness({saved, respond: async () => success({remaining: 2, resumed: true})});
  await after.submit('SECOND-CDK');
  after.input(CDK);
  await after.submit(CDK);
  assert.deepEqual(after.calls.map(call => call.body.request_id), [ids[1], ids[0]]);
  assert.equal(after.randomCalls, 0);
});

test('case and internal Python whitespace variants reuse one code record', async () => {
  const ui = harness({respond: async (_, count) => { if (count === 1) throw new TypeError('Response lost'); return success({resumed: true}); }});
  const variants = [CDK, 'private-cdk-123', ' PRIVATE - CDK - 123 ', 'PRIVATE-\tCDK-\n123', 'PRIVATE-\u001c\u001dCDK-\u001e\u001f\u0085123', 'PRIVATE-\u00a0CDK-\u3000123'];
  for (const code of variants) await ui.submit(code);
  assert.equal(new Set(ui.calls.map(call => call.body.request_id)).size, 1);
  assert.equal(ui.saved.values.size, 1);
  assert.equal(ui.randomCalls, 1);
});

for (const completed of [false, true]) {
  test(`legacy v1 ${completed ? 'completed' : 'unknown'} credential remains a read-only migration fallback`, async () => {
    const saved = storage();
    const legacy = JSON.stringify({request_id: 'c'.repeat(32), completed}); saved.values.set(KEY, legacy);
    const ui = harness({saved, respond: async () => success({remaining: 2})});
    await ui.submit(CDK);
    await ui.submit('SECOND-CDK');
    assert.deepEqual(ui.calls.map(call => call.body.request_id), ['c'.repeat(32), 'c'.repeat(32)]);
    await ui.submit('SECOND-CDK');
    assert.notEqual(ui.stateFor('SECOND-CDK').request_id, 'c'.repeat(32));
    await ui.submit(CDK);
    assert.equal(ui.calls.at(-1).body.request_id, 'c'.repeat(32));
    await ui.submit('THIRD-CDK');
    assert.equal(ui.calls.at(-1).body.request_id, 'c'.repeat(32));
    assert.equal(saved.values.get(KEY), legacy);
    assertOnlyRecoveryState(saved);
  });
}

test('network retry reuses the durable random ID and explains recovery without storing credentials', async () => {
  const ui = harness({respond: async (_, count) => { if (count === 1) throw new TypeError('Network failure'); return success({resumed: true}); }});
  await ui.submit();
  const first = ui.state.request_id;
  assert.equal(ui.state.completed, false);
  assert.match(ui.resultText, /10 分钟/); assert.match(ui.resultText, /本标签页/);
  assert.equal(ui.nodes.cdk.value, CDK);
  await ui.submit();
  assert.deepEqual(ui.calls.map(call => call.body.request_id), [first, first]);
  assert.equal(ui.randomCalls, 1);
  assert.match(ui.batchText, /已恢复上次兑换/); assert.match(ui.batchText, /未重复扣减/);
  assert.match(ui.batchText, /下载链接在有效期内可重复使用/);
  assert.equal(ui.state.completed, true);
  assertOnlyRecoveryState(ui.saved);
});

test('refresh after a lost response recovers the same request with no CDK prefilled', async () => {
  const saved = storage();
  const before = harness({saved, respond: async () => { throw new TypeError('Response lost'); }});
  await before.submit(CDK, 2);
  const after = harness({saved, respond: async () => success({count: 2, resumed: true})});
  assert.equal(after.nodes.cdk.value, '');
  await after.submit(CDK, 2);
  assert.equal(after.calls[0].body.request_id, before.calls[0].body.request_id);
  assert.equal(after.randomCalls, 0);
  assert.match(after.batchText, /已恢复上次兑换/);
  assertOnlyRecoveryState(saved);
});

test('only the explicit next-batch action rotates ID; failures then retry that new batch', async () => {
  const ui = harness({respond: async (_, count) => { if (count === 2) throw new TypeError('Response lost'); return success({remaining: 2, resumed: count > 2}); }});
  await ui.submit();
  const original = ui.state.request_id;
  assert.equal(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  await ui.submit();
  const next = ui.state.request_id;
  assert.notEqual(next, original);
  assert.equal(ui.state.completed, false);
  assert.notEqual(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  await ui.submit();
  assert.deepEqual(ui.calls.map(call => call.body.request_id), [original, next, next]);
  assert.equal(ui.randomCalls, 2);
});

test('refresh after success first resumes the completed batch before allowing a new batch', async () => {
  const saved = storage();
  const before = harness({saved, respond: async () => success({remaining: 3})});
  await before.submit();
  const original = before.state.request_id;
  assert.equal(before.state.completed, true);
  const after = harness({saved, respond: async (_, count) => success({remaining: 3 - count, resumed: count === 1})});
  assert.notEqual(after.nodes.submitLabel.textContent, '继续兑换下一批');
  await after.submit();
  assert.equal(after.calls[0].body.request_id, original);
  assert.equal(after.randomCalls, 0);
  assert.equal(after.nodes.submitLabel.textContent, '继续兑换下一批');
  await after.submit();
  assert.notEqual(after.calls[1].body.request_id, original);
});

test('changing CDK creates an independent record without rotating the previous code request', async () => {
  const ui = harness({respond: async () => success({remaining: 3})});
  await ui.submit();
  const original = ui.state.request_id;
  ui.input('ANOTHER-CDK');
  assert.notEqual(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  await ui.submit('ANOTHER-CDK');
  assert.notEqual(ui.calls[1].body.request_id, original);
  assert.equal(ui.state.request_id, original);
  assert.equal(ui.state.completed, true);
  assert.equal(ui.stateFor('ANOTHER-CDK').completed, true);
  assert.equal(ui.randomCalls, 2);
  assertOnlyRecoveryState(ui.saved);
});

for (const failure of ['read', 'write', 'dropped-write', 'invalid-state', 'invalid-code-state', 'storage-access', 'randomness']) {
  test(`unavailable recovery ${failure} blocks the redemption POST`, async () => {
    const saved = storage();
    if (failure === 'read') saved.failGet = true;
    if (failure === 'write') saved.failSet = true;
    if (failure === 'dropped-write') saved.dropWrites = true;
    if (failure === 'invalid-state') saved.values.set(KEY, JSON.stringify({request_id: 'bad', completed: false}));
    if (failure === 'invalid-code-state') {
      saved.values.set(KEY, JSON.stringify({request_id: 'd'.repeat(32), completed: false}));
      saved.values.set(storageKey(CDK), JSON.stringify({request_id: 'bad', completed: false}));
    }
    const ui = harness({saved, cryptoAvailable: failure !== 'randomness', storageAvailable: failure !== 'storage-access'});
    await ui.submit();
    assert.equal(ui.calls.length, 0);
    assert.match(ui.resultText, /尚未提交/);
    assert.equal(ui.nodes.submit.disabled, false);
    assert.equal(ui.nodes.cdk.readOnly, false);
  });
}

test('a quantity conflict asks for the original quantity and preserves the same request ID', async () => {
  const saved = storage(); saved.values.set(KEY, JSON.stringify({request_id: 'a'.repeat(32), completed: false}));
  const ui = harness({saved, respond: async (_, count) => count === 1 ? {status: 409, body: {code: 'request_conflict', error: 'Quantity differs'}} : success({count: 2, resumed: true})});
  await ui.submit(CDK, 1);
  assert.match(ui.resultText, /上次提交的兑换数量/);
  assert.equal(ui.nodes.quantity.attrs['aria-invalid'], 'true');
  await ui.submit(CDK, 2);
  assert.deepEqual(ui.calls.map(call => call.body.request_id), ['a'.repeat(32), 'a'.repeat(32)]);
  assert.equal(ui.randomCalls, 0);
});

test('saving completion failure still displays received credentials but never enables another batch', async () => {
  const saved = storage();
  const ui = harness({saved, respond: async () => { saved.failSet = true; return success({remaining: 2}); }});
  await ui.submit(CDK, 3);
  assert.equal(ui.nodes.quantity.value, '3');
  assert.match(ui.batchText, /兑换成功/);
  assert.match(ui.batchText, /private-password/);
  assert.match(ui.resultText, /兑换已成功/);
  assert.ok(!ui.resultText.includes('尚未提交'));
  assert.notEqual(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  assert.equal(ui.state.completed, false);
});

test('in-flight duplicate submits cannot create a second request', async () => {
  let finish;
  const ui = harness({respond: () => new Promise(resolve => { finish = resolve; })});
  const first = ui.submit();
  await ui.submit();
  await settle();
  assert.equal(ui.calls.length, 1);
  finish(success());
  await first;
  assert.equal(ui.nodes.submit.disabled, false);
});

test('confirmed expired delivery with remaining quota waits for an explicit next-batch click', async () => {
  const saved = storage(); saved.values.set(KEY, JSON.stringify({request_id: 'b'.repeat(32), completed: true}));
  const ui = harness({saved, respond: async (_, count) => count === 1
    ? {status: 410, body: {code: 'delivery_expired', remaining: 3, error: 'Expired'}}
    : success({remaining: 2})});
  await ui.submit();
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.calls[0].body.request_id, 'b'.repeat(32));
  assert.equal(ui.randomCalls, 0);
  assert.match(ui.resultText, /原批次下载已过期/);
  assert.match(ui.resultText, /仍有 3 个名额/);
  assert.match(ui.resultText, /扣减剩余名额/);
  assert.equal(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  await ui.submit();
  assert.notEqual(ui.calls[1].body.request_id, 'b'.repeat(32));
  assert.equal(ui.randomCalls, 1);
});

for (const [status, code, remaining] of [[410, 'code_exhausted', 3], [410, 'code_revoked', 3], [410, 'code_expired', 3], [410, 'delivery_expired', 0], [500, 'delivery_expired', 3]]) {
  test(`${status} ${code} with ${remaining} remaining cannot silently rotate request ID`, async () => {
    const ui = harness({respond: async () => ({status, body: {code, remaining, error: 'Unavailable'}})});
    await ui.submit();
    const original = ui.state.request_id;
    assert.notEqual(ui.nodes.submitLabel.textContent, '继续兑换下一批');
    await ui.submit();
    assert.deepEqual(ui.calls.map(call => call.body.request_id), [original, original]);
    assert.equal(ui.randomCalls, 1);
  });
}

test('expiry cannot enable new allocation when persisting the confirmed state fails', async () => {
  const saved = storage();
  const ui = harness({saved, respond: async () => { saved.failSet = true; return {status: 410, body: {code: 'delivery_expired', remaining: 2}}; }});
  await ui.submit();
  assert.match(ui.resultText, /暂不能提交新批次/);
  assert.notEqual(ui.nodes.submitLabel.textContent, '继续兑换下一批');
  await ui.submit();
  assert.equal(ui.calls.length, 1);
});

test('an incomplete success response remains recoverable rather than clearing the CDK', async () => {
  const ui = harness({respond: async (_, count) => count === 1 ? {status: 200, body: {}} : success({resumed: true})});
  await ui.submit();
  assert.match(ui.resultText, /未能读取完整兑换结果/);
  assert.equal(ui.nodes.cdk.value, CDK);
  assert.equal(ui.state.completed, false);
  await ui.submit();
  assert.equal(ui.calls[1].body.request_id, ui.calls[0].body.request_id);
});
