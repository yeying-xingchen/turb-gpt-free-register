// Run: node --test frontend/tests/registration.test.mjs
// Execute the real Vue setup functions with Vue reactivity and mocked API/lifecycle.
// Every request is local test data; no browser, server, or network is used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { parse, compileScript } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const app = new URL("../app/", import.meta.url);
function loadModule(source, globals = {}, imports = {}) {
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  const allowedGlobals = Object.fromEntries(
    Object.entries(globals).filter(([name]) => /^[$A-Z_a-z][$\w]*$/.test(name)),
  );
  new Function("exports", "require", ...Object.keys(allowedGlobals), code)(
    exports,
    (name) => {
      if (name === "vue") return vue;
      assert.ok(name in imports, `Unexpected import: ${name}`);
      return imports[name];
    },
    ...Object.values(allowedGlobals),
  );
  return exports;
}
function source(path) {
  return readFileSync(new URL(path, app), "utf8");
}
const helpers = loadModule(source("components/operations/helpers.ts"));
const { descriptor } = parse(source("pages/index.vue"));
const dashboardCode = compileScript(descriptor, { id: "registration-test" }).content;
const deferred = () => Promise.withResolvers();
const settle = async () => { await vue.nextTick(); await setImmediate(); };
const fields = (manual = false, email = "manual@example.test") => [
  { key: "USE_EMAIL_SERVICE", value: !manual },
  { key: "REGISTER_EMAIL", value: email },
  { key: "REGISTRATION_DRIVER", value: "cloak" },
];
const jobsResult = (id = 1, active = 0) => ({
  ok: true, items: [{ id, status: "failed", retryable: true }], total: 60,
  status_counts: { active },
});
function dashboard(t, handle = () => undefined) {
  const calls = [], messages = [], mounted = [], unmounted = [];
  let poll;
  const request = async (path, options = {}) => {
    calls.push({ path, ...options });
    const result = handle(path, options);
    if (result !== undefined) return result;
    if (path === "/api/config") return fields();
    if (path === "/api/summary") return { accounts: 2 };
    if (options.method === "POST")
      return { ok: true, submitted: options.body.count || 1, workers: options.body.workers || 3 };
    if (path === "/api/jobs") return jobsResult();
    assert.fail(`Unexpected request: ${path}`);
  };
  const component = loadModule(dashboardCode, {
    ...vue,
    useHead() {},
    useApi: () => ({ request }),
    useToast: () => Object.fromEntries(["success", "error", "info"].map((kind) => [kind, (message) => messages.push({ kind, message })])),
    onMounted: (fn) => mounted.push(fn),
    onBeforeUnmount: (fn) => unmounted.push(fn),
    usePolling: (fn) => { poll = fn; },
  }, { "~/components/operations/helpers": helpers }).default;
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup({}, { expose() {} }));
  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    unmounted.forEach((fn) => fn());
    scope.stop();
  };
  t.after(dispose);
  return {
    state, calls, messages, dispose,
    posts: () => calls.filter((call) => call.method === "POST"),
    poll: () => poll(),
    mount: async () => { mounted.forEach((fn) => fn()); await settle(); },
  };
}

test("registration waits for config, reports failure, and allows a config retry", async (t) => {
  const pending = deferred();
  let configCalls = 0;
  const ui = dashboard(t, (path) => path === "/api/config" && ++configCalls === 1 ? pending.promise : undefined);
  await ui.mount();
  assert.equal(ui.state.validForm.value, false);
  await ui.state.createJobs();
  await ui.state.loadConfig();
  assert.equal(configCalls, 1, "repeated config loads do not overlap");
  assert.equal(ui.posts().length, 0);
  pending.reject(new Error("配置服务不可用"));
  await settle();
  assert.equal(ui.state.configError.value, "配置服务不可用");
  assert.equal(ui.state.validForm.value, false);
  await ui.state.loadConfig();
  assert.equal(ui.state.validForm.value, true);
  assert.equal(ui.state.configError.value, "");
});

test("incomplete config cannot enable registration", async (t) => {
  const ui = dashboard(t, (path) => path === "/api/config" ? [] : undefined);
  await ui.mount();
  await ui.state.createJobs();
  assert.equal(ui.posts().length, 0);
  assert.match(ui.state.configError.value, /配置响应不完整/);
});

test("manual config preserves edits and validates count and required email", async (t) => {
  let config = fields(true, "");
  const ui = dashboard(t, (path) => path === "/api/config" ? config : undefined);
  ui.state.count.value = 7;
  await ui.mount();
  assert.equal(ui.state.count.value, 7, "loading config must not overwrite the count draft");
  await ui.state.createJobs();
  assert.match(ui.state.formIssue.value, /每次只能提交 1/);
  ui.state.count.value = 1;
  await ui.state.createJobs();
  assert.match(ui.state.formIssue.value, /手动注册邮箱/);
  assert.equal(ui.posts().length, 0);
  config = fields(true);
  await ui.state.loadConfig();
  assert.equal(ui.state.validForm.value, true);
});

test("invalid registration numbers never produce a POST", async (t) => {
  const ui = dashboard(t);
  await ui.mount();
  for (const invalid of ["", 0, -1, 1.5, 201, NaN, Infinity]) {
    ui.state.count.value = invalid;
    await ui.state.createJobs();
    assert.equal(ui.state.validForm.value, false, `invalid count: ${invalid}`);
    assert.match(ui.state.formIssue.value, /注册数量/, `error for count ${String(invalid)}: ${ui.state.formIssue.value}`);
  }
  ui.state.count.value = 2;
  for (const invalid of ["", 0, -1, 1.5, 17, NaN, Infinity]) {
    ui.state.workers.value = invalid;
    await ui.state.createJobs();
    assert.equal(ui.state.validForm.value, false, `invalid workers: ${invalid}`);
    assert.match(ui.state.formIssue.value, /并发线程/);
  }
  assert.equal(ui.posts().length, 0);
});

test("rapid registration submissions share the busy guard and recover after rejection", async (t) => {
  const pending = deferred();
  let attempt = 0;
  const ui = dashboard(t, (_path, options) => options.method === "POST" && ++attempt === 1 ? pending.promise : undefined);
  await ui.mount();
  ui.state.count.value = 2;
  const first = ui.state.createJobs();
  const second = ui.state.createJobs();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.state.busy.value, true);
  pending.reject(new Error("邮箱服务未配置"));
  await Promise.all([first, second]);
  assert.equal(ui.state.busy.value, false);
  assert.equal(ui.state.submitError.value, "邮箱服务未配置");
  assert.equal(ui.state.count.value, 2);
  assert.equal(ui.messages.filter((item) => item.kind === "success").length, 0);
  await ui.state.createJobs();
  assert.equal(ui.posts().length, 2);
  assert.equal(ui.state.submitError.value, "");
  assert.match(ui.state.notice.value, /已提交 2 个任务/);
});

test("confirmation snapshots quantity and workers and cannot be replaced by another submit", async (t) => {
  const ui = dashboard(t);
  await ui.mount();
  ui.state.counts.value = { active: 5 };
  ui.state.count.value = 2;
  ui.state.workers.value = 4;
  await ui.state.createJobs();
  const prompt = ui.state.confirmation.value;
  assert.match(prompt.description, /2 个任务，并发 4/);
  ui.state.count.value = 100;
  ui.state.workers.value = 9;
  await ui.state.createJobs();
  assert.equal(ui.state.confirmation.value, prompt);
  assert.equal(ui.posts().length, 0);
  await Promise.all([prompt.run(), prompt.run()]);
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body, { count: 2, workers: 4 });
});

test("confirmation rechecks config constraints and keeps the failure visible", async (t) => {
  const ui = dashboard(t);
  await ui.mount();
  ui.state.counts.value = { active: 1 };
  ui.state.count.value = 2;
  await ui.state.createJobs();
  ui.state.config.value.USE_EMAIL_SERVICE = false;
  await assert.rejects(ui.state.confirmation.value.run(), /每次只能提交 1/);
  assert.equal(ui.posts().length, 0);
  assert.match(ui.state.submitError.value, /每次只能提交 1/);
});

test("registration keeps both the acknowledgement and the backend warning", async (t) => {
  const ui = dashboard(t, (_path, options) => options.method === "POST"
    ? { ok: true, submitted: 1, workers: 3, warning: "手动 OTP 模式：请提交验证码" } : undefined);
  await ui.mount();
  await ui.state.createJobs();
  assert.match(ui.state.notice.value, /已提交 1 个任务/);
  assert.match(ui.state.notice.value, /请提交验证码/);
});

test("single and bulk retries reject invalid workers and snapshot valid workers", async (t) => {
  const ui = dashboard(t);
  await ui.mount();
  const job = ui.state.jobs.value[0];
  ui.state.selected.value = [job.id];
  ui.state.workers.value = 1.5;
  ui.state.jobAction(job, "retry");
  ui.state.bulkAction("retry");
  assert.equal(ui.state.confirmation.value, null);
  for (const bulk of [false, true]) {
    ui.state.workers.value = 4;
    if (bulk) {
      ui.state.selected.value = [job.id];
      ui.state.bulkAction("retry");
    } else ui.state.jobAction(job, "retry");
    const prompt = ui.state.confirmation.value;
    ui.state.workers.value = 11;
    await prompt.run();
    assert.equal(ui.posts().at(-1).body.workers, 4);
    ui.state.confirmation.value = null;
  }
});

test("pagination cancels old reads and ignores late success and failure responses", async (t) => {
  const reads = [];
  const ui = dashboard(t, (path, options) => {
    if (path === "/api/jobs" && !options.method) {
      const pending = deferred();
      reads.push({ ...pending, signal: options.signal });
      return pending.promise;
    }
  });
  await ui.mount();
  ui.state.page.value = 2;
  await settle();
  assert.equal(reads[0].signal.aborted, true);
  reads[1].resolve(jobsResult(22));
  await settle();
  reads[0].resolve(jobsResult(11));
  await settle();
  assert.equal(ui.state.jobs.value[0].id, 22);
  const old = ui.state.loadJobs();
  const latest = ui.state.loadJobs();
  reads[3].resolve(jobsResult(33));
  await latest;
  reads[2].reject(new Error("过期读取失败"));
  await old;
  assert.equal(ui.state.error.value, "");
  assert.equal(ui.state.jobs.value[0].id, 33);
});

test("polling skips manual refresh and mutation; pre-mutation reads cannot overwrite results", async (t) => {
  const staleJobs = deferred(), staleSummary = deferred(), submitted = deferred();
  let hold = false;
  const ui = dashboard(t, (path, options) => {
    if (options.method === "POST") return submitted.promise;
    if (hold && path === "/api/jobs") return staleJobs.promise;
    if (hold && path === "/api/summary") return staleSummary.promise;
  });
  await ui.mount();
  hold = true;
  const refreshing = ui.state.refresh();
  const reads = ui.calls.slice(-2);
  const callCount = ui.calls.length;
  await ui.poll();
  assert.equal(ui.calls.length, callCount);
  const creating = ui.state.createJobs();
  assert.ok(reads.every((call) => call.signal.aborted));
  await ui.poll();
  assert.equal(ui.posts().length, 1);
  hold = false;
  submitted.resolve({ ok: true, submitted: 1, workers: 3 });
  await creating;
  staleJobs.resolve(jobsResult(999));
  staleSummary.resolve({ accounts: 999 });
  await refreshing;
  assert.equal(ui.state.jobs.value[0].id, 1);
  assert.equal(ui.state.summary.value.accounts, 2);
  assert.equal(ui.state.error.value, "");
});

test("unmount aborts every read and ignores late config and summary responses", async (t) => {
  const pending = { "/api/config": deferred(), "/api/jobs": deferred(), "/api/summary": deferred() };
  const ui = dashboard(t, (path) => pending[path]?.promise);
  await ui.mount();
  ui.dispose();
  assert.ok(ui.calls.every((call) => call.signal.aborted));
  pending["/api/config"].resolve(fields());
  pending["/api/jobs"].resolve(jobsResult());
  pending["/api/summary"].resolve({ accounts: 9 });
  await settle();
  assert.equal(ui.state.configLoaded.value, false);
  assert.equal(ui.state.loaded.value, false);
  assert.deepEqual(ui.state.summary.value, {});
});

function api(response, pathname = "/") {
  const navigation = [], authenticated = { value: true };
  const module = loadModule(source("composables/useApi.ts"), {
    fetch: async () => response,
    window: { location: { origin: "http://example.test", pathname, search: "?page=2" } },
    useState: () => authenticated,
    navigateTo: async (destination) => navigation.push(destination),
  });
  return { ...module.useApi(), navigation, authenticated, ApiError: module.ApiError };
}

test("API rejects malformed successful JSON and HTML without claiming success", async () => {
  for (const body of ["<html>login</html>", "{broken", ""]) {
    const client = api(new Response(body, { status: 200, headers: { "Content-Type": "text/html" } }));
    await assert.rejects(client.request("/api/jobs", { method: "POST", body: {} }), (error) =>
      error instanceof client.ApiError && error.status === 200 && /JSON/.test(error.message));
  }
  const empty = api(new Response(null, { status: 204 }));
  await assert.rejects(empty.request("/api/jobs"), /204/);
});

test("API preserves JSON arrays, successful objects and structured error messages", async () => {
  for (const value of [fields(), { ok: true, submitted: 2, workers: 3 }]) {
    const client = api(Response.json(value));
    assert.deepEqual(await client.request("/api/config"), value);
  }
  for (const status of [200, 400]) {
    const client = api(Response.json({ ok: false, error: "手动邮箱未配置" }, { status }));
    await assert.rejects(client.request("/api/jobs"), (error) =>
      error.status === status && error.message === "手动邮箱未配置");
  }
});

test("API handles JSON/HTML 401 and redirected login pages, retaining public-page behavior", async () => {
  const responses = [
    Response.json({ error: "请先登录" }, { status: 401 }),
    new Response("Unauthorized", { status: 401 }),
    { redirected: true, url: "http://example.test/login?next=/", status: 200, ok: true, json: async () => { throw new SyntaxError(); } },
  ];
  for (const response of responses) {
    const client = api(response);
    await assert.rejects(client.request("/api/jobs"));
    assert.equal(client.authenticated.value, false);
    assert.deepEqual(client.navigation, [{ path: "/login", query: { next: "/?page=2" } }]);
  }
  for (const path of ["/login", "/redeem"]) {
    const client = api(Response.json({ error: "请先登录" }, { status: 401 }), path);
    await assert.rejects(client.request("/api/redeem"), /请先登录/);
    assert.deepEqual(client.navigation, []);
  }
});

test("aborting response parsing preserves the original cancellation", async () => {
  const controller = new AbortController();
  const aborted = new DOMException("Aborted", "AbortError");
  controller.abort();
  const client = api({ json: async () => { throw aborted; } });
  await assert.rejects(client.request("/api/jobs", { signal: controller.signal }), (cause) => cause === aborted);
});
