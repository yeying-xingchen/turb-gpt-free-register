<script setup lang="ts">
import {
  checkResult,
  downloadFile,
  errorMessage,
  formatTime,
  resultMessage,
} from "~/components/operations/helpers";
import type {
  ActionPrompt,
  OperationRow,
} from "~/components/operations/helpers";
useHead({ title: "Codex 授权" });
const { request } = useApi();
const toast = useToast();
const view = ref<"accounts" | "credentials">("accounts");
const rows = ref<OperationRow[]>([]);
const summary = ref<OperationRow>({});
const summaryError = ref("");
const selected = ref<string[]>([]);
const search = ref("");
const query = ref("");
const status = ref("");
const archived = ref("0");
const dateFrom = ref("");
const dateTo = ref("");
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const loading = ref(true);
const loaded = ref(false);
const busy = ref(false);
const auto = ref(true);
const error = ref("");
const workers = ref(1);
const confirmation = shallowRef<ActionPrompt | null>(null);
const logEmail = ref("");
const logOpen = ref(false);
const actionDetails = ref<OperationRow[]>([]);
const key = (row: OperationRow) =>
  view.value === "accounts" ? String(row.id) : row.filename;
const selectedRows = computed(() =>
  rows.value.filter((row) => selected.value.includes(key(row))),
);
const allSelected = computed(
  () =>
    rows.value.length > 0 &&
    rows.value.every((row) => selected.value.includes(key(row))),
);
const canRetry = (row: OperationRow) =>
  row.codex_status !== "retrying" && row.live_check_status !== "deactivated";
const retryRows = computed(() => selectedRows.value.filter(canRetry));
const stopRows = computed(() =>
  selectedRows.value.filter((row) => row.codex_status === "retrying"),
);
const validWorkers = computed(
  () =>
    Number.isInteger(workers.value) &&
    workers.value >= 1 &&
    workers.value <= 16,
);
let revision = 0;
async function load() {
  const current = ++revision;
  const accountView = view.value === "accounts";
  loading.value = true;
  try {
    const result = checkResult(
      await request(accountView ? "/api/accounts" : "/api/codex", {
        query: {
          paged: 1,
          page: page.value,
          page_size: pageSize.value,
          q: query.value,
          archived: archived.value,
          ...(accountView ? { codex_status: status.value || undefined } : {}),
          date_from: dateFrom.value || undefined,
          date_to: dateTo.value || undefined,
        },
      }),
    );
    if (current !== revision) return;
    rows.value = (accountView ? result.items : result.accounts) || [];
    total.value = Number(result.total || 0);
    if (!accountView) {
      summary.value = result.summary || {};
      summaryError.value = "";
    }
    selected.value = selected.value.filter((value) =>
      rows.value.some((row) => key(row) === value),
    );
    loaded.value = true;
    error.value = "";
    const last = Math.max(1, Math.ceil(total.value / pageSize.value));
    if (page.value > last) page.value = last;
  } catch (cause) {
    if (current === revision) error.value = errorMessage(cause);
  } finally {
    if (current === revision) loading.value = false;
  }
}
async function loadSummary() {
  try {
    const result = checkResult(
      await request("/api/codex", {
        query: { paged: 1, page: 1, page_size: 1 },
      }),
    );
    summary.value = result.summary || {};
    summaryError.value = "";
  } catch (cause) {
    summaryError.value = errorMessage(cause);
  }
}
async function refresh() {
  await Promise.allSettled([
    load(),
    ...(view.value === "accounts" ? [loadSummary()] : []),
  ]);
}
usePolling(() => {
  if (auto.value && !busy.value) return refresh();
});
onBeforeUnmount(() => {
  revision++;
});
watch(view, () => {
  rows.value = [];
  selected.value = [];
  loaded.value = false;
  total.value = 0;
  archived.value = "0";
  dateFrom.value = "";
  dateTo.value = "";
  if (page.value !== 1) page.value = 1;
  else void refresh();
});
watch(page, () => {
  selected.value = [];
  void load();
});
watch([status, archived, query, pageSize, dateFrom, dateTo], () => {
  selected.value = [];
  if (page.value !== 1) page.value = 1;
  else void load();
});
function applySearch() {
  const next = search.value.trim();
  if (next === query.value) void load();
  else query.value = next;
}
function toggleAll() {
  selected.value = allSelected.value ? [] : rows.value.map(key);
}
function openLog(row: OperationRow) {
  logEmail.value = row.email;
  logOpen.value = true;
}
async function perform(path: string, body: OperationRow, message: string) {
  if (busy.value) return;
  busy.value = true;
  actionDetails.value = [];
  try {
    const result = checkResult(await request(path, { method: "POST", body }));
    actionDetails.value = Array.isArray(result.skipped) ? result.skipped : [];
    toast.success(resultMessage(result, message));
    selected.value = [];
    await refresh();
    return result;
  } finally {
    busy.value = false;
  }
}
function authorize(targets: OperationRow[], bulk = false) {
  const eligible = targets.filter(canRetry);
  if (!eligible.length || !validWorkers.value) return;
  const ids = eligible.map((row) => Number(row.id));
  const email = eligible[0]?.email;
  confirmation.value = {
    title: `${bulk ? "批量" : ""}开始 Codex 授权`,
    label: "开始授权",
    description: bulk
      ? `将为 ${ids.length} 个账号补跑 Codex 授权，并发 ${workers.value}。执行进度可在日志中查看。`
      : `将为 ${email} ${eligible[0]?.codex_status === "success" ? "重新" : ""}执行 Codex 授权。`,
    run: () =>
      perform(
        bulk ? "/api/codex/retry-bulk" : "/api/codex/retry",
        bulk ? { account_ids: ids, workers: workers.value } : { email },
        "已开始授权",
      ),
  };
}
function stop(targets: OperationRow[], bulk = false) {
  const emails = targets
    .filter((row) => row.codex_status === "retrying")
    .map((row) => row.email);
  if (!emails.length) return;
  confirmation.value = {
    title: "停止 Codex 授权",
    description: `将请求停止 ${emails.length} 个正在补跑的账号，停止结果会同步到列表和日志。`,
    label: "停止授权",
    run: () =>
      perform(
        bulk ? "/api/codex/stop-bulk" : "/api/codex/stop",
        bulk ? { emails } : { email: emails[0] },
        "停止请求已提交",
      ),
  };
}
function resetRetry(row: OperationRow) {
  confirmation.value = {
    title: "重置补跑状态",
    description: `${row.email}\n仅在任务线程已经结束、状态仍停留在补跑中时使用。重置为失败后可重新授权。`,
    run: () =>
      perform(
        "/api/codex/reset-retrying",
        { email: row.email, status: "failed" },
        "补跑状态已重置",
      ),
  };
}
function archive(targets: OperationRow[], value: boolean, bulk = false) {
  const filenames = targets.map((row) => row.filename);
  if (!filenames.length) return;
  const label = value ? "归档" : "恢复";
  const run = () =>
    perform(
      bulk ? "/api/codex/archive-bulk" : "/api/codex/archive",
      bulk
        ? { filenames, archived: value }
        : { filename: filenames[0], archived: value },
      `已${label}`,
    );
  if (bulk)
    confirmation.value = {
      title: `${label} ${filenames.length} 个凭证`,
      description: value
        ? "归档后可在「仅归档」视图中查找和恢复这些凭证。"
        : "这些凭证将恢复到未归档列表中。",
      run,
    };
  else void run().catch((cause) => toast.error(errorMessage(cause)));
}
function deleteCredentials(targets: OperationRow[], bulk = false) {
  const filenames = targets.map((row) => row.filename);
  if (!filenames.length) return;
  confirmation.value = {
    title: `删除 ${filenames.length} 个凭证`,
    danger: true,
    label: "删除凭证",
    description: `${filenames.length === 1 ? filenames[0] + "\n" : ""}将删除所选本地凭证记录及导出标记，此操作无法撤销。`,
    run: () =>
      perform(
        bulk ? "/api/codex/delete-bulk" : "/api/codex/delete",
        bulk ? { filenames } : { filename: filenames[0] },
        "已删除",
      ),
  };
}
function resetExport(row: OperationRow) {
  confirmation.value = {
    title: "重置导出标记",
    description: `将 ${row.filename} 重新标记为未导出。`,
    run: () =>
      perform(
        "/api/codex/reset-export",
        { filename: row.filename },
        "导出标记已重置",
      ),
  };
}
async function download(targets: OperationRow[], cpa: boolean, bulk = false) {
  if (busy.value || !targets.length) return;
  busy.value = true;
  try {
    const filenames = targets.map((row) => row.filename);
    const path = bulk
      ? cpa
        ? "/api/codex/download-bulk-from-cpa"
        : "/api/codex/download-bulk"
      : `/api/codex/${cpa ? "download-from-cpa" : "download"}/${encodeURIComponent(filenames[0])}`;
    await downloadFile(
      path,
      bulk ? `codex-${Date.now()}.${cpa ? "zip" : "json"}` : filenames[0],
      bulk ? { filenames } : undefined,
    );
    toast.success(
      cpa && bulk ? "下载已开始，处理详情见压缩包内的清单" : "下载已开始",
    );
    selected.value = [];
    await refresh();
  } finally {
    busy.value = false;
  }
}
function downloadSelected(cpa: boolean) {
  const targets = selectedRows.value.slice();
  if (!targets.length) return;
  confirmation.value = {
    title: `${cpa ? "从 CPA 下载" : "备份"} ${targets.length} 个凭证`,
    label: "开始下载",
    description: cpa
      ? "将按邮箱匹配 CPA 凭证并打包为 ZIP。解压后的单个 JSON 可用于 CPA，未成功项会记录在压缩包内的清单中。"
      : "将生成一个聚合 JSON 文件，适合备份与迁移。CPA 需要单个凭证文件，请使用逐条下载或「从 CPA 下载」。",
    run: () => download(targets, cpa, true),
  };
}
async function downloadOne(row: OperationRow, cpa: boolean) {
  try {
    await download([row], cpa);
  } catch (cause) {
    toast.error(errorMessage(cause));
  }
}
</script>

<template>
  <div class="stack codex-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">INTEGRATIONS / CODEX</p>
        <h1 class="page-title">Codex 授权</h1>
        <p class="page-description">跟进账号授权，妥善管理每一份访问凭证。</p>
      </div>
      <NuxtLink class="btn" to="/tasks">查看所有任务 ↗</NuxtLink>
    </header>
    <div v-if="summaryError" class="alert alert-error" role="alert">
      凭证统计加载失败：{{ summaryError }}
      <button class="btn btn-sm" @click="loadSummary">重试</button>
    </div>
    <section class="stat-grid codex-stats" aria-label="凭证统计">
      <div class="stat-card">
        <span class="stat-label">可管理凭证</span
        ><strong class="stat-value">{{ summary.total ?? "—" }}</strong
        ><span class="muted">未归档的授权凭证</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">已导出</span
        ><strong class="stat-value">{{ summary.exported ?? "—" }}</strong
        ><span class="muted">已下载过的凭证</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">待导出</span
        ><strong class="stat-value">{{ summary.pending ?? "—" }}</strong
        ><span class="muted">尚未下载的凭证</span>
      </div>
    </section>
    <section class="card">
      <div class="card-header codex-header">
        <div class="view-tabs" aria-label="Codex 视图">
          <button
            :class="{ active: view === 'accounts' }"
            :aria-pressed="view === 'accounts'"
            :disabled="busy"
            @click="view = 'accounts'"
          >
            账号授权</button
          ><button
            :class="{ active: view === 'credentials' }"
            :aria-pressed="view === 'credentials'"
            :disabled="busy"
            @click="view = 'credentials'"
          >
            凭证文件
          </button>
        </div>
        <div class="inline">
          <label class="inline refresh-label"
            ><input v-model="auto" type="checkbox" />每 5 秒刷新</label
          ><button class="btn btn-sm" :disabled="loading" @click="refresh">
            {{ loading ? "刷新中…" : "刷新" }}
          </button>
        </div>
      </div>
      <form class="toolbar filter-toolbar" @submit.prevent="applySearch">
        <input
          v-model="search"
          class="input search-input"
          type="search"
          :placeholder="
            view === 'accounts' ? '搜索账号邮箱…' : '搜索邮箱、凭证文件…'
          "
          aria-label="搜索 Codex"
        /><select
          v-if="view === 'accounts'"
          v-model="status"
          class="select"
          aria-label="授权状态"
        >
          <option value="">全部授权状态</option>
          <option value="success">授权成功</option>
          <option value="retrying">补跑中</option>
          <option value="failed">授权失败</option>
          <option value="missing">缺少凭证</option>
          <option value="skipped">已跳过</option>
          <option value="stopped">已停止</option>
          <option value="deactivated">账号已废</option></select
        ><select
          v-model="archived"
          class="select"
          :aria-label="view === 'accounts' ? '账号归档状态' : '凭证归档状态'"
        >
          <option value="0">未归档</option>
          <option value="only">仅归档</option>
          <option value="all">全部（含归档）</option></select
        ><button class="btn" type="submit" :disabled="loading">搜索</button>
      </form>
      <div class="toolbar date-toolbar">
        <label class="inline"
          >{{ view === "accounts" ? "注册日期" : "凭证日期"
          }}<input
            v-model="dateFrom"
            class="input"
            type="date"
            aria-label="开始日期"
            :max="dateTo || undefined" /></label
        ><span class="muted">至</span
        ><input
          v-model="dateTo"
          class="input"
          type="date"
          aria-label="结束日期"
          :min="dateFrom || undefined"
        /><button
          v-if="dateFrom || dateTo"
          class="btn btn-sm"
          @click="
            dateFrom = '';
            dateTo = '';
          "
        >
          清除日期</button
        ><span class="muted result-count"
          >共 {{ total }} 条 · 已选 {{ selected.length }} 条</span
        >
      </div>
      <div class="toolbar selection-toolbar">
        <template v-if="view === 'accounts'"
          ><label class="inline workers-label"
            >并发<input
              v-model.number="workers"
              class="input workers-input"
              type="number"
              min="1"
              max="16"
              step="1"
              :disabled="busy"
              aria-label="授权并发线程" /></label
          ><button
            class="btn btn-sm btn-primary"
            :disabled="busy || !retryRows.length || !validWorkers"
            @click="authorize(retryRows, true)"
          >
            授权选中（{{ retryRows.length }}）</button
          ><button
            class="btn btn-sm"
            :disabled="busy || !stopRows.length"
            @click="stop(stopRows, true)"
          >
            停止选中
          </button></template
        >
        <template v-else
          ><button
            class="btn btn-sm"
            :disabled="busy || !selected.length"
            @click="downloadSelected(false)"
          >
            下载本地备份</button
          ><button
            class="btn btn-sm"
            :disabled="busy || !selected.length"
            @click="downloadSelected(true)"
          >
            从 CPA 下载</button
          ><button
            class="btn btn-sm"
            :disabled="busy || !selected.length"
            @click="archive(selectedRows, archived !== 'only', true)"
          >
            {{ archived === "only" ? "恢复选中" : "归档选中" }}</button
          ><button
            class="btn btn-sm btn-danger"
            :disabled="busy || !selected.length"
            @click="deleteCredentials(selectedRows, true)"
          >
            删除选中
          </button></template
        >
        <label class="inline page-size"
          >每页<select
            v-model.number="pageSize"
            class="select"
            aria-label="每页条数"
          >
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option></select
          >条</label
        >
      </div>
      <div v-if="actionDetails.length" class="alert table-alert" role="status">
        <details>
          <summary>{{ actionDetails.length }} 项未执行，查看原因</summary>
          <ul>
            <li v-for="(item, index) in actionDetails" :key="index">
              {{ item.email || item.filename || item.id }}：{{ item.reason }}
            </li>
          </ul>
        </details>
      </div>
      <div v-if="error" class="alert alert-error table-alert" role="alert">
        {{ error }}
        <button class="btn btn-sm" :disabled="loading" @click="load">
          重新加载
        </button>
      </div>
      <div v-if="loading && !loaded" class="empty-state" role="status">
        正在加载{{ view === "accounts" ? "授权账号" : "凭证文件" }}…
      </div>
      <UiEmpty
        v-else-if="!rows.length && !error"
        :title="
          query || status || archived !== '0' || dateFrom || dateTo
            ? '没有匹配的记录'
            : view === 'accounts'
              ? '还没有可授权的账号'
              : '还没有 Codex 凭证'
        "
        :description="
          view === 'accounts'
            ? '注册或导入账号后，可以在这里发起授权；已有账号可尝试调整筛选。'
            : '授权成功后，凭证会显示在这里，也可以检查归档和日期筛选。'
        "
      />
      <div v-else-if="rows.length" class="table-wrap" :aria-busy="loading">
        <table class="data-table">
          <thead>
            <tr>
              <th class="check-cell">
                <input
                  type="checkbox"
                  :checked="allSelected"
                  :indeterminate="selected.length > 0 && !allSelected"
                  :disabled="busy || !rows.length"
                  aria-label="选择本页记录"
                  @change="toggleAll"
                />
              </th>
              <th>{{ view === "accounts" ? "账号" : "凭证" }}</th>
              <th>{{ view === "accounts" ? "授权状态" : "导出状态" }}</th>
              <th>套餐</th>
              <th>{{ view === "accounts" ? "授权结果" : "时间" }}</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="key(row)">
              <td>
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="key(row)"
                  :disabled="busy"
                  :aria-label="`选择 ${row.email || row.filename}`"
                />
              </td>
              <td>
                <strong class="account-email">{{ row.email || "—" }}</strong
                ><small
                  v-if="view === 'credentials'"
                  class="cell-sub mono filename"
                  :title="row.filename"
                  >{{ row.filename }}</small
                ><small v-else class="cell-sub"
                  >账号 #{{ row.id
                  }}<span v-if="row.archived"> · 已归档</span></small
                >
              </td>
              <td>
                <template v-if="view === 'accounts'"
                  ><UiBadge
                    :value="
                      row.live_check_status === 'deactivated'
                        ? 'deactivated'
                        : row.codex_status || '未授权'
                    " /></template
                ><template v-else
                  ><span
                    class="export-badge"
                    :class="{ exported: row.exported_count > 0 }"
                    >{{ row.exported_count > 0 ? "已导出" : "未导出" }}</span
                  ><small class="cell-sub"
                    >{{ row.archived ? "已归档 · " : "" }}已下载
                    {{ row.exported_count || 0 }} 次</small
                  ></template
                >
              </td>
              <td>
                {{ row.current_plan_type || row.plan || row.plan_type || "—" }}
              </td>
              <td v-if="view === 'accounts'">
                <span
                  class="result-message"
                  :title="row.codex_error || row.codex_error_message"
                  >{{
                    row.codex_error ||
                    row.codex_error_message ||
                    (row.codex_status === "success"
                      ? "授权已完成"
                      : row.codex_status === "retrying"
                        ? "正在执行授权，请查看日志"
                        : "—")
                  }}</span
                ><small class="cell-sub">{{
                  formatTime(row.codex_updated_at || row.created_at)
                }}</small>
              </td>
              <td v-else class="time-cell">
                {{ formatTime(row.mtime)
                }}<small class="cell-sub"
                  >到期 {{ formatTime(row.expired) }}</small
                >
              </td>
              <td>
                <div v-if="view === 'accounts'" class="row-actions">
                  <button
                    class="btn btn-sm"
                    :disabled="!row.email"
                    @click="openLog(row)"
                  >
                    日志</button
                  ><button
                    v-if="canRetry(row)"
                    class="btn btn-sm btn-primary"
                    :disabled="busy || !validWorkers"
                    @click="authorize([row])"
                  >
                    {{
                      row.codex_status === "success" ? "重新授权" : "补跑授权"
                    }}</button
                  ><button
                    v-if="row.codex_status === 'retrying'"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="stop([row])"
                  >
                    停止</button
                  ><button
                    v-if="row.codex_status === 'retrying'"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="resetRetry(row)"
                  >
                    重置状态
                  </button>
                </div>
                <div v-else class="row-actions">
                  <button
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="downloadOne(row, false)"
                  >
                    本地下载</button
                  ><button
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="downloadOne(row, true)"
                  >
                    CPA 下载</button
                  ><button
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="archive([row], !row.archived)"
                  >
                    {{ row.archived ? "恢复" : "归档" }}</button
                  ><button
                    v-if="row.exported_count > 0"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="resetExport(row)"
                  >
                    重置导出</button
                  ><button
                    class="btn btn-sm btn-danger"
                    :disabled="busy"
                    @click="deleteCredentials([row])"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiPagination v-model:page="page" :total="total" :page-size="pageSize" />
    </section>
    <p v-if="view === 'credentials'" class="muted download-note">
      下载会记录导出次数。本地下载保留原始凭证或回执；CPA 下载按邮箱获取实际授权
      JSON。
    </p>
    <OperationsActionDialog v-model:action="confirmation" />
    <OperationsLogModal
      v-model:open="logOpen"
      endpoint="/api/codex/retry-log"
      :query="{ email: logEmail }"
      :title="`${logEmail} · 授权日志`"
    />
  </div>
</template>

<style scoped>
.codex-page {
  gap: 24px;
}
.codex-page > .page-header {
  margin-bottom: 0;
}
.codex-stats {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.stat-card > .muted {
  font-size: 12px;
}
.codex-header {
  flex-wrap: wrap;
  gap: 12px;
}
.view-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  border-radius: 10px;
  background: var(--surface-secondary, #f4f6fa);
}
.view-tabs button {
  border: 0;
  padding: 9px 15px;
  border-radius: 7px;
  background: transparent;
  color: var(--text-secondary, #64748b);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}
.view-tabs button.active {
  background: var(--surface, #fff);
  color: var(--text, #1e293b);
  box-shadow: 0 1px 4px #14274b0c;
}
.refresh-label {
  font-size: 12px;
}
.filter-toolbar {
  padding: 20px 20px 12px;
  gap: 10px;
}
.search-input {
  flex: 1;
  min-width: 210px;
}
.filter-toolbar select {
  width: auto;
  min-width: 130px;
}
.date-toolbar {
  padding: 0 20px 18px;
  gap: 8px;
  font-size: 12px;
}
.date-toolbar input {
  width: 150px;
  font-size: 12px;
}
.result-count {
  margin-left: auto;
}
.selection-toolbar {
  padding: 12px 20px;
  border-block: 1px solid var(--border, #e5e7eb);
  gap: 8px;
}
.workers-label,
.page-size {
  font-size: 12px;
}
.workers-input {
  width: 64px;
}
.page-size {
  margin-left: auto;
}
.page-size select {
  width: 70px;
}
.table-alert {
  margin: 16px 20px;
}
.check-cell {
  width: 40px;
}
.account-email {
  font-weight: 550;
}
.cell-sub {
  display: block;
  margin-top: 5px;
  color: var(--text-secondary, #64748b);
  font-size: 11px;
}
.filename {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.time-cell {
  font-size: 12px;
  white-space: nowrap;
}
.row-actions {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  min-width: 200px;
  max-width: 320px;
}
.result-message {
  display: block;
  max-width: 250px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.export-badge {
  display: inline-flex;
  padding: 4px 9px;
  border-radius: 6px;
  font-size: 11px;
  background: #edf8f3;
  color: #23845d;
}
.export-badge.exported {
  background: #f1f4f9;
  color: #69788d;
}
.download-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.8;
}
@media (max-width: 650px) {
  .codex-stats {
    grid-template-columns: 1fr;
  }
  .page-size,
  .result-count {
    margin-left: 0;
  }
}
</style>
