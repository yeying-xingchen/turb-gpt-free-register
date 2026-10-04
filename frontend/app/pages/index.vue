<script setup lang="ts">
import {
  checkResult,
  errorMessage,
  formatTime,
  resultMessage,
} from "~/components/operations/helpers";
import type {
  ActionPrompt,
  OperationRow,
} from "~/components/operations/helpers";
useHead({ title: "注册与概览" });
const { request } = useApi();
const toast = useToast();
const jobs = ref<OperationRow[]>([]);
const summary = ref<OperationRow>({});
const counts = ref<OperationRow>({});
const config = ref<OperationRow>({});
const configError = ref("");
const summaryError = ref("");
const error = ref("");
const loading = ref(true);
const busy = ref(false);
const auto = ref(true);
const loaded = ref(false);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const count = ref(1);
const workers = ref(3);
const notice = ref("");
const selected = ref<number[]>([]);
const confirmation = shallowRef<ActionPrompt | null>(null);
const logOpen = ref(false);
const logId = ref<number | null>(null);
const otpJob = ref<OperationRow | null>(null);
const otpCode = ref("");
const otpError = ref("");
const otpOpen = computed({
  get: () => !!otpJob.value,
  set: (value) => {
    if (!value && !busy.value) otpJob.value = null;
  },
});
const manual = computed(() => config.value.USE_EMAIL_SERVICE === false);
const validForm = computed(
  () =>
    Number.isInteger(count.value) &&
    count.value >= 1 &&
    count.value <= (manual.value ? 1 : 200) &&
    Number.isInteger(workers.value) &&
    workers.value >= 1 &&
    workers.value <= 16,
);
function bytes(value: unknown) {
  const count = Math.max(0, Number(value) || 0);
  return count < 1024
    ? `${count} B`
    : count < 1024 ** 2
      ? `${(count / 1024).toFixed(1)} KiB`
      : `${(count / 1024 ** 2).toFixed(2)} MiB`;
}
const deletable = (job: OperationRow) =>
  !["running", "paused", "stopping"].includes(job.status);
const selectable = computed(() => jobs.value.filter(deletable));
const allSelected = computed(
  () =>
    selectable.value.length > 0 &&
    selectable.value.every((job) => selected.value.includes(job.id)),
);
const selectedJobs = computed(() =>
  jobs.value.filter((job) => selected.value.includes(job.id)),
);
let jobRevision = 0;
async function loadJobs() {
  const revision = ++jobRevision;
  loading.value = true;
  try {
    const result = checkResult(
      await request("/api/jobs", {
        query: { paged: 1, page: page.value, page_size: pageSize.value },
      }),
    );
    if (revision !== jobRevision) return;
    jobs.value = result.items || [];
    total.value = Number(result.total || 0);
    counts.value = result.status_counts || {};
    selected.value = selected.value.filter((id) =>
      jobs.value.some((job) => job.id === id && deletable(job)),
    );
    loaded.value = true;
    error.value = "";
    const last = Math.max(1, Math.ceil(total.value / pageSize.value));
    if (page.value > last) page.value = last;
  } catch (cause) {
    if (revision === jobRevision) error.value = errorMessage(cause);
  } finally {
    if (revision === jobRevision) loading.value = false;
  }
}
async function loadSummary() {
  try {
    summary.value = checkResult(await request("/api/summary"));
    summaryError.value = "";
  } catch (cause) {
    summaryError.value = errorMessage(cause);
  }
}
async function loadConfig() {
  try {
    const fields = await request<OperationRow[]>("/api/config");
    const allowed = [
      "EMAIL_SOURCE",
      "USE_EMAIL_SERVICE",
      "REGISTER_EMAIL",
      "REGISTRATION_DRIVER",
      "CODEX_OAUTH_DRIVER",
      "ENABLE_CODEX_AUTO",
      "PROXY_POOL",
    ];
    config.value = Object.fromEntries(
      fields
        .filter((field) => allowed.includes(field.key))
        .map((field) => [
          field.key,
          field.key === "PROXY_POOL"
            ? Array.isArray(field.value)
              ? field.value.length
              : 0
            : field.value,
        ]),
    );
    configError.value = "";
    if (manual.value) count.value = 1;
  } catch (cause) {
    configError.value = errorMessage(cause);
  }
}
async function refresh() {
  await Promise.allSettled([loadJobs(), loadSummary()]);
}
usePolling(() => {
  if (auto.value && !busy.value) return refresh();
});
onMounted(loadConfig);
onBeforeUnmount(() => {
  jobRevision++;
});
watch(page, () => {
  selected.value = [];
  void loadJobs();
});
watch(pageSize, () => {
  selected.value = [];
  if (page.value !== 1) page.value = 1;
  else void loadJobs();
});
function toggleAll() {
  selected.value = allSelected.value
    ? []
    : selectable.value.map((job) => job.id);
}
function openLog(job: OperationRow) {
  logId.value = job.id;
  logOpen.value = true;
}
function openOtp(job: OperationRow) {
  otpJob.value = job;
  otpCode.value = "";
  otpError.value = "";
}
async function perform(path: string, body: OperationRow, message: string) {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = checkResult(await request(path, { method: "POST", body }));
    toast.success(resultMessage(result, message));
    selected.value = [];
    await refresh();
    return result;
  } finally {
    busy.value = false;
  }
}
function jobAction(job: OperationRow, action: string) {
  const labels: Record<string, string> = {
    pause: "暂停",
    resume: "恢复",
    stop: "停止",
    cancel: "取消",
    retry: job.retry_label || "重试",
    delete: "删除",
  };
  const label = labels[action] || action;
  const run = () =>
    perform(
      `/api/jobs/${job.id}/${action}`,
      action === "retry" ? { workers: workers.value } : {},
      `已${label}`,
    );
  if (["pause", "resume"].includes(action)) {
    void run().catch((cause) => toast.error(errorMessage(cause)));
    return;
  }
  confirmation.value = {
    title: `${label}任务 #${job.id}`,
    label,
    danger: action === "delete",
    description:
      action === "delete"
        ? "将删除任务记录和对应日志；排队任务删除后不会执行。此操作无法撤销。"
        : action === "retry"
          ? job.retry_action === "codex"
            ? "账号已经创建，本次仅补跑 Codex 授权。原任务记录与日志会保留。"
            : "将创建新的注册任务，原任务记录与日志会保留。"
          : "排队任务会直接取消；进行中的任务会在当前步骤的检查点停止。",
    run,
  };
}
function bulkAction(action: "retry" | "delete") {
  const ids = selectedJobs.value
    .filter((job) => (action === "retry" ? job.retryable : deletable(job)))
    .map((job) => job.id);
  if (!ids.length) return;
  confirmation.value = {
    title: `${action === "retry" ? "重试" : "删除"} ${ids.length} 个任务`,
    danger: action === "delete",
    description:
      action === "delete"
        ? "将删除所选任务及其日志，无法撤销。执行状态变化导致不能删除的任务将被跳过。"
        : "根据账号创建结果，后端将重新注册或仅补跑 Codex 授权。",
    run: () =>
      perform(
        `/api/jobs/${action}-bulk`,
        {
          job_ids: ids,
          ...(action === "retry" ? { workers: workers.value } : {}),
        },
        action === "retry" ? "已提交重试" : "已删除",
      ),
  };
}
function cancelPending() {
  confirmation.value = {
    title: "取消全部排队注册任务",
    description: `将取消 ${counts.value.pending || 0} 个排队注册任务，包含排队的 Codex 补跑任务。`,
    run: () => perform("/api/jobs/cancel-pending", {}, "已取消排队任务"),
  };
}
async function createJobs() {
  if (!validForm.value || busy.value) return;
  const run = async () => {
    const result = await perform(
      "/api/jobs",
      { count: count.value, workers: workers.value },
      "注册任务已提交",
    );
    if (result)
      notice.value =
        result.warning ||
        `已提交 ${result.submitted} 个任务，本次并发 ${result.workers}。`;
    if (page.value !== 1) page.value = 1;
  };
  if (counts.value.active > 0)
    confirmation.value = {
      title: "继续添加注册任务",
      description: `已有 ${counts.value.active} 个任务执行或排队。本次将再添加 ${count.value} 个任务。`,
      label: "添加任务",
      run,
    };
  else {
    try {
      await run();
    } catch (cause) {
      toast.error(errorMessage(cause));
    }
  }
}
async function submitOtp() {
  if (!otpJob.value || busy.value || !/^\d{4,8}$/.test(otpCode.value.trim()))
    return;
  busy.value = true;
  otpError.value = "";
  try {
    checkResult(
      await request("/api/manual-otp", {
        method: "POST",
        body: {
          job_id: otpJob.value.id,
          email: otpJob.value.email,
          code: otpCode.value.trim(),
        },
      }),
    );
    toast.success("验证码已提交");
    otpJob.value = null;
    otpCode.value = "";
  } catch (cause) {
    otpError.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="stack dashboard">
    <header class="page-header">
      <div>
        <p class="eyebrow">WORKSPACE / OVERVIEW</p>
        <h1 class="page-title">注册与概览</h1>
        <p class="page-description">从一封邮箱开始，让每一次注册都有迹可循。</p>
      </div>
      <NuxtLink to="/tasks" class="btn"
        >查看任务中心 <span aria-hidden="true">↗</span></NuxtLink
      >
    </header>
    <div v-if="summaryError" class="alert alert-error" role="alert">
      概览加载失败：{{ summaryError }}
      <button class="btn btn-sm" @click="loadSummary">重试</button>
    </div>
    <section class="stat-grid" aria-label="工作区统计">
      <div class="stat-card">
        <span class="stat-label">已注册账号</span
        ><strong class="stat-value">{{ summary.accounts ?? "—" }}</strong
        ><span class="muted">工作区累计账号</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">可用邮箱</span
        ><strong class="stat-value">{{
          summary.outlook_available ?? "—"
        }}</strong
        ><span class="muted">当前配置的邮箱来源</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">进行中任务</span
        ><strong class="stat-value">{{ counts.active ?? "—" }}</strong
        ><span class="muted"
          >{{ counts.running || 0 }} 运行 · {{ counts.pending || 0 }} 排队</span
        >
      </div>
      <div class="stat-card">
        <span class="stat-label">注册成功</span
        ><strong class="stat-value">{{
          counts.success ?? (loaded ? 0 : "—")
        }}</strong
        ><span class="muted">{{ counts.failed || 0 }} 个任务失败</span>
      </div>
    </section>
    <div class="registration-grid">
      <section class="card registration-card">
        <div class="card-header">
          <div>
            <h2>创建注册任务</h2>
            <p class="muted">按需设置任务数量与并发。</p>
          </div>
          <span class="registration-symbol" aria-hidden="true">↗</span>
        </div>
        <form class="card-body stack" @submit.prevent="createJobs">
          <div class="form-grid">
            <label class="field"
              >注册数量<input
                v-model.number="count"
                class="input"
                type="number"
                min="1"
                :max="manual ? 1 : 200"
                step="1"
                required
                :disabled="busy"
              /><small class="muted">{{
                manual ? "手动验证码模式每次 1 个" : "单次 1–200 个账号"
              }}</small></label
            >
            <label class="field"
              >并发线程<input
                v-model.number="workers"
                class="input"
                type="number"
                min="1"
                max="16"
                step="1"
                required
                :disabled="busy"
              /><small class="muted">1–16 个线程同时处理</small></label
            >
          </div>
          <div v-if="manual" class="alert">
            当前使用手动验证码，任务开始后请通过「验证码」按钮提交邮箱收到的 6
            位验证码。
          </div>
          <div v-if="notice" class="alert" role="status">{{ notice }}</div>
          <button
            class="btn btn-primary start-button"
            type="submit"
            :disabled="busy || !validForm"
          >
            {{ busy ? "正在处理…" : "开始注册" }}
            <span aria-hidden="true">→</span>
          </button>
        </form>
      </section>
      <section class="card config-card">
        <div class="card-header">
          <h2>当前配置</h2>
          <NuxtLink class="btn btn-sm" to="/settings">配置设置 ↗</NuxtLink>
        </div>
        <div class="card-body">
          <div v-if="configError" class="alert alert-error" role="alert">
            {{ configError }}
            <button class="btn btn-sm" @click="loadConfig">重试</button>
          </div>
          <dl v-else class="config-list">
            <div>
              <dt>邮箱来源</dt>
              <dd>{{ config.EMAIL_SOURCE || "—" }}</dd>
            </div>
            <div>
              <dt>验证码获取</dt>
              <dd>
                {{
                  config.USE_EMAIL_SERVICE === undefined
                    ? "—"
                    : manual
                      ? "手动提交"
                      : "自动收取"
                }}
              </dd>
            </div>
            <div v-if="manual">
              <dt>注册邮箱</dt>
              <dd>{{ config.REGISTER_EMAIL || "尚未配置" }}</dd>
            </div>
            <div>
              <dt>注册方式</dt>
              <dd>{{ config.REGISTRATION_DRIVER || "默认" }}</dd>
            </div>
            <div>
              <dt>自动授权</dt>
              <dd>
                {{
                  config.ENABLE_CODEX_AUTO === undefined
                    ? "—"
                    : config.ENABLE_CODEX_AUTO
                      ? "已开启"
                      : "已关闭"
                }}
              </dd>
            </div>
            <div>
              <dt>授权方式</dt>
              <dd>{{ config.CODEX_OAUTH_DRIVER || "默认" }}</dd>
            </div>
            <div>
              <dt>代理池</dt>
              <dd>{{ config.PROXY_POOL ?? "—" }} 个代理</dd>
            </div>
          </dl>
        </div>
      </section>
    </div>
    <section class="card" aria-labelledby="jobs-title">
      <div class="card-header">
        <div>
          <h2 id="jobs-title">
            注册任务 <span class="muted">{{ total }}</span>
          </h2>
          <p class="muted">实时查看执行进度、结果与日志。</p>
        </div>
        <div class="inline">
          <label class="inline refresh-label"
            ><input v-model="auto" type="checkbox" />每 5 秒刷新</label
          ><button class="btn btn-sm" :disabled="loading" @click="refresh">
            {{ loading ? "刷新中…" : "刷新" }}
          </button>
        </div>
      </div>
      <div class="toolbar jobs-toolbar">
        <span class="muted">已选 {{ selected.length }} 项</span>
        <button
          class="btn btn-sm"
          :disabled="
            busy ||
            !selectedJobs.some((job) => job.retryable) ||
            !Number.isInteger(workers) ||
            workers < 1 ||
            workers > 16
          "
          @click="bulkAction('retry')"
        >
          重试选中
        </button>
        <button
          class="btn btn-sm btn-danger"
          :disabled="busy || !selected.length"
          @click="bulkAction('delete')"
        >
          删除选中
        </button>
        <button
          class="btn btn-sm"
          :disabled="busy || !counts.pending"
          @click="cancelPending"
        >
          取消排队（{{ counts.pending || 0 }}）
        </button>
        <label class="inline page-size"
          >每页<select
            v-model.number="pageSize"
            class="select"
            aria-label="每页任务数"
          >
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option></select
          >条</label
        >
      </div>
      <div v-if="error" class="alert alert-error table-alert" role="alert">
        {{ error }}
        <button class="btn btn-sm" :disabled="loading" @click="loadJobs">
          重新加载
        </button>
      </div>
      <div v-if="loading && !loaded" class="empty-state" role="status">
        正在加载注册任务…
      </div>
      <UiEmpty
        v-else-if="!jobs.length && !error"
        title="还没有注册任务"
        description="设置数量与并发，创建你的第一个任务。"
      />
      <div v-else-if="jobs.length" class="table-wrap" :aria-busy="loading">
        <table class="data-table">
          <thead>
            <tr>
              <th class="check-cell">
                <input
                  type="checkbox"
                  :checked="allSelected"
                  :indeterminate="selected.length > 0 && !allSelected"
                  :disabled="busy || !selectable.length"
                  aria-label="选择本页可操作任务"
                  @change="toggleAll"
                />
              </th>
              <th>任务</th>
              <th>状态</th>
              <th>邮箱 / 执行进度</th>
              <th>时间</th>
              <th>流量</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in jobs" :key="job.id">
              <td>
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="job.id"
                  :disabled="busy || !deletable(job)"
                  :aria-label="`选择任务 ${job.id}`"
                />
              </td>
              <td>
                <strong class="mono">#{{ job.id }}</strong
                ><small v-if="job.parent_job_id" class="cell-sub"
                  >重试自 #{{ job.parent_job_id }}</small
                ><small class="cell-sub">{{
                  job.job_type === "codex_retry" ? "Codex 补跑" : "账号注册"
                }}</small>
              </td>
              <td><UiBadge :value="job.display_status || job.status" /></td>
              <td>
                <span>{{ job.email || "等待分配邮箱" }}</span
                ><small
                  class="cell-sub job-message"
                  :title="
                    job.error_message || job.progress_message || job.stage
                  "
                  >{{
                    job.error_message ||
                    job.progress_message ||
                    job.stage ||
                    "—"
                  }}</small
                >
              </td>
              <td class="time-cell">
                <span>{{ formatTime(job.started_at || job.created_at) }}</span
                ><small v-if="job.completed_at" class="cell-sub"
                  >完成 {{ formatTime(job.completed_at) }}</small
                >
              </td>
              <td class="mono muted">
                <span
                  v-if="job.network_traffic?.available"
                  :title="`上传 ${bytes(job.network_traffic.upload_bytes)} · 下载 ${bytes(job.network_traffic.download_bytes)} · ${job.network_traffic.request_count || 0} 个请求`"
                  >{{ bytes(job.network_traffic.total_bytes) }}</span
                ><span v-else>—</span>
              </td>
              <td>
                <div class="row-actions">
                  <button class="btn btn-sm" @click="openLog(job)">日志</button>
                  <button
                    v-if="['pending', 'running'].includes(job.status)"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="jobAction(job, 'pause')"
                  >
                    暂停
                  </button>
                  <button
                    v-if="job.status === 'paused'"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="jobAction(job, 'resume')"
                  >
                    恢复
                  </button>
                  <button
                    v-if="job.status === 'pending'"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="jobAction(job, 'cancel')"
                  >
                    取消
                  </button>
                  <button
                    v-if="
                      ['running', 'paused', 'stopping'].includes(job.status)
                    "
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="jobAction(job, 'stop')"
                  >
                    {{ job.status === "stopping" ? "再次停止" : "停止" }}
                  </button>
                  <button
                    v-if="job.retryable"
                    class="btn btn-sm"
                    :disabled="
                      busy ||
                      !Number.isInteger(workers) ||
                      workers < 1 ||
                      workers > 16
                    "
                    @click="jobAction(job, 'retry')"
                  >
                    {{ job.retry_label || "重试" }}
                  </button>
                  <button
                    v-if="
                      job.status === 'running' &&
                      job.email &&
                      job.manual_otp_required
                    "
                    class="btn btn-sm btn-primary"
                    :disabled="busy"
                    @click="openOtp(job)"
                  >
                    验证码
                  </button>
                  <button
                    v-if="deletable(job)"
                    class="btn btn-sm btn-danger"
                    :disabled="busy"
                    @click="jobAction(job, 'delete')"
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
    <OperationsLogModal
      v-model:open="logOpen"
      :endpoint="logId === null ? '' : `/api/jobs/${logId}/log`"
      :title="`注册任务 #${logId} · 日志`"
    />
    <OperationsActionDialog v-model:action="confirmation" />
    <UiModal v-model:open="otpOpen" title="提交邮箱验证码">
      <form id="manual-otp-form" class="stack" @submit.prevent="submitOtp">
        <p class="muted">{{ otpJob?.email }} · 任务 #{{ otpJob?.id }}</p>
        <label class="field"
          >验证码（通常 6 位）<input
            v-model="otpCode"
            class="input mono"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="8"
            pattern="[0-9]{4,8}"
            required
            :disabled="busy"
            placeholder="000000"
        /></label>
        <div v-if="otpError" class="alert alert-error" role="alert">
          {{ otpError }}
        </div>
      </form>
      <template #footer
        ><button class="btn" :disabled="busy" @click="otpOpen = false">
          取消</button
        ><button
          class="btn btn-primary"
          form="manual-otp-form"
          type="submit"
          :disabled="busy || !/^\d{4,8}$/.test(otpCode.trim())"
        >
          {{ busy ? "提交中…" : "提交验证码" }}
        </button></template
      >
    </UiModal>
  </div>
</template>

<style scoped>
.dashboard {
  gap: 24px;
}
.dashboard > .page-header {
  margin-bottom: 0;
}
.registration-grid > .card {
  margin-top: 0;
}
h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
}
.card-header p {
  margin: 7px 0 0;
  font-size: 12px;
}
.stat-card > .muted {
  font-size: 12px;
}
.registration-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: 24px;
}
.registration-symbol {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: var(--accent-light, #eaf6f2);
  color: var(--accent, #0f766e);
  font-size: 21px;
}
.start-button {
  width: 100%;
  justify-content: space-between;
}
.config-list {
  margin: 0;
  display: grid;
  gap: 18px;
  font-size: 13px;
}
.config-list > div {
  display: flex;
  justify-content: space-between;
  gap: 20px;
}
.config-list dt {
  color: var(--text-secondary, #64748b);
  flex-shrink: 0;
}
.config-list dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.jobs-toolbar {
  padding: 14px 20px;
  border-bottom: 1px solid var(--border, #e5e7eb);
  gap: 8px;
}
.page-size {
  margin-left: auto;
  font-size: 12px;
}
.page-size select {
  width: 70px;
}
.refresh-label {
  font-size: 12px;
}
.table-alert {
  margin: 16px 20px;
}
.cell-sub {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-secondary, #64748b);
}
.job-message {
  max-width: 300px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.time-cell {
  white-space: nowrap;
  font-size: 12px;
}
.check-cell {
  width: 40px;
}
.row-actions {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  min-width: 165px;
}
@media (max-width: 850px) {
  .registration-grid {
    grid-template-columns: 1fr;
  }
  .page-size {
    margin-left: 0;
  }
}
</style>
