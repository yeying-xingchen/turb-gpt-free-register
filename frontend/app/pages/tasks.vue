<script setup lang="ts">
import {
  checkResult,
  errorMessage,
  formatTime,
} from "~/components/operations/helpers";
import type {
  ActionPrompt,
  OperationRow,
} from "~/components/operations/helpers";
import {
  taskFilterActive,
  taskFilterOptions,
  taskFilterQuery,
  taskPoolByName,
  taskPoolDirty,
  taskPoolHint,
  taskPoolMax,
  taskPoolWorkers,
  taskPoolWorkersValid,
} from "~/utils/tasks";
import type { TaskFilterOption } from "~/utils/tasks";
useHead({ title: "任务中心" });
const { request } = useApi();
const toast = useToast();
const view = ref<"active" | "history">("active");
const active = shallowRef<OperationRow[]>([]);
const history = shallowRef<OperationRow[]>([]);
const counts = ref<OperationRow>({});
const activeTotal = ref(0);
const historyTotal = ref(0);
const activePage = ref(1);
const historyPage = ref(1);
const pageSize = ref(20);
const loading = ref(true);
const activeLoaded = ref(false);
const historyLoaded = ref(false);
const busy = ref(false);
const auto = ref(true);
const error = ref("");
const updated = ref("");
const confirmation = shallowRef<ActionPrompt | null>(null);
const logOpen = ref(false);
const logTask = ref<OperationRow | null>(null);
type ManualVerification = {
  account_id: number;
  email: string;
  waiting_since: string;
};
const manualVerifications = shallowRef<ManualVerification[]>([]);
const manualLoaded = ref(false);
const manualError = ref("");
type VerificationFrame = {
  image: string;
  width: number;
  height: number;
  revision: string;
};
const verificationOpen = ref(false);
const verificationAccount = shallowRef<ManualVerification | null>(null);
const verificationFrame = shallowRef<VerificationFrame | null>(null);
const verificationDisplayedFrame = shallowRef<VerificationFrame | null>(null);
const verificationPending = ref(true);
const verificationError = ref("");
const verificationInputBusy = ref(false);
let verificationGeneration = 0;
let verificationTimer: ReturnType<typeof setTimeout> | undefined;
let verificationController: AbortController | undefined;
function cleanupVerification() {
  verificationGeneration++;
  clearTimeout(verificationTimer);
  verificationTimer = undefined;
  verificationController?.abort();
  verificationController = undefined;
  verificationFrame.value = null;
  verificationDisplayedFrame.value = null;
  verificationInputBusy.value = false;
}
function verificationCurrent(accountId: number, generation: number) {
  return verificationOpen.value &&
    verificationAccount.value?.account_id === accountId &&
    verificationGeneration === generation;
}
function verificationConflict(cause: unknown) {
  return typeof cause === "object" && cause !== null &&
    "status" in cause && cause.status === 409;
}
async function pollVerificationFrame(accountId: number, generation: number) {
  if (!verificationCurrent(accountId, generation)) return;
  const controller = new AbortController();
  verificationController = controller;
  let retry = true;
  try {
    const result = await request<{
      ok: boolean;
      pending: boolean;
      frame?: VerificationFrame;
    }>(`/api/tasks/manual-verifications/${accountId}/frame`, {
      signal: controller.signal,
    });
    if (!verificationCurrent(accountId, generation)) return;
    verificationError.value = "";
    verificationPending.value = result.pending;
    verificationFrame.value = result.frame || null;
  } catch (cause) {
    if (!verificationCurrent(accountId, generation)) return;
    verificationError.value = errorMessage(cause);
    if (verificationConflict(cause)) {
      retry = false;
      verificationPending.value = false;
      verificationFrame.value = null;
      verificationDisplayedFrame.value = null;
    }
  } finally {
    if (verificationCurrent(accountId, generation)) {
      verificationController = undefined;
      if (retry)
        verificationTimer = setTimeout(
          () => void pollVerificationFrame(accountId, generation), 1000,
        );
    }
  }
}
function openVerification(item: ManualVerification) {
  cleanupVerification();
  verificationAccount.value = item;
  verificationError.value = "";
  verificationPending.value = true;
  verificationOpen.value = true;
  void pollVerificationFrame(item.account_id, verificationGeneration);
}
watch(verificationOpen, (open) => {
  if (!open) cleanupVerification();
}, { flush: "sync" });
async function sendVerificationClick(event: MouseEvent) {
  const accountId = verificationAccount.value?.account_id;
  const frame = verificationDisplayedFrame.value;
  const image = event.currentTarget as HTMLImageElement;
  if (accountId == null || !verificationOpen.value || verificationInputBusy.value ||
      !frame || frame !== verificationFrame.value || !image.complete) return;
  const bounds = image.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;
  const x = Math.min(frame.width - 1, Math.max(0,
    Math.floor((event.clientX - bounds.left) * frame.width / bounds.width)));
  const y = Math.min(frame.height - 1, Math.max(0,
    Math.floor((event.clientY - bounds.top) * frame.height / bounds.height)));
  const generation = verificationGeneration;
  verificationInputBusy.value = true;
  try {
    await request(`/api/tasks/manual-verifications/${accountId}/input`, {
      method: "POST", body: { x, y, revision: frame.revision },
    });
    if (verificationCurrent(accountId, generation)) verificationError.value = "";
  } catch (cause) {
    if (!verificationCurrent(accountId, generation)) return;
    verificationError.value = errorMessage(cause);
  } finally {
    if (verificationCurrent(accountId, generation)) verificationInputBusy.value = false;
  }
}
// 任务筛选：表单值在提交后才进入 applied，轮询刷新不会打断正在编辑的条件。
const filters = reactive({ job_type: "", status: "", q: "" });
const applied = ref<{ job_type: string; status: string; q: string }>({
  job_type: "",
  status: "",
  q: "",
});
const filterOptions = ref<{
  jobTypes: TaskFilterOption[];
  statuses: TaskFilterOption[];
}>({ jobTypes: [], statuses: [] });
const filterActive = computed(() => taskFilterActive(applied.value));
const filterDirty = computed(
  () =>
    filters.job_type !== applied.value.job_type ||
    filters.status !== applied.value.status ||
    filters.q.trim() !== applied.value.q,
);
// 后台并发：每类任务一个线程池，运行中即可调整。
const pools = ref<OperationRow[]>([]);
const poolName = ref("");
const poolWorkers = ref(1);
const concurrencySaving = ref(false);
const currentPool = computed(() => taskPoolByName(pools.value, poolName.value));
const currentPoolMax = computed(() => taskPoolMax(currentPool.value));
function syncPoolWorkers() {
  if (!pools.value.length) return;
  if (!pools.value.some((pool) => pool.name === poolName.value))
    poolName.value = pools.value[0]?.name || "";
  // 轮询刷新时不要覆盖用户正在输入但尚未应用的并发数。
  if (!concurrencyDirty.value)
    poolWorkers.value = taskPoolWorkers(currentPool.value);
}
watch(pools, syncPoolWorkers, { deep: true });
watch(poolName, () => {
  poolWorkers.value = taskPoolWorkers(currentPool.value);
});
const concurrencyDirty = computed(() =>
  taskPoolDirty(poolWorkers.value, currentPool.value),
);
const concurrencyValid = computed(() =>
  taskPoolWorkersValid(poolWorkers.value, currentPool.value),
);
const concurrencyHint = computed(() => taskPoolHint(currentPool.value));
const page = computed({
  get: () => (view.value === "active" ? activePage.value : historyPage.value),
  set: (value) => {
    if (view.value === "active") activePage.value = value;
    else historyPage.value = value;
  },
});
const total = computed(() =>
  view.value === "active" ? active.value.length : historyTotal.value,
);
const rows = computed(() =>
  view.value === "active"
    ? active.value.slice(
        (activePage.value - 1) * pageSize.value,
        activePage.value * pageSize.value,
      )
    : history.value,
);
const loaded = computed(() =>
  view.value === "active" ? activeLoaded.value : historyLoaded.value,
);
const pendingRegistration = computed(
  () =>
    active.value.filter(
      (task) =>
        task.job_id != null &&
        task.status === "pending" &&
        task.capabilities?.cancel,
    ).length,
);
const labels: Record<string, string> = {
  registration: "账号注册",
  codex_retry: "Codex 补跑",
  plus_activation: "开通 Plus",
  live_check: "账号查活",
  plan_check: "套餐查询",
  quota_check: "额度/用量查询",
  extract_link: "提取链接",
  scan_payment: "扫码支付",
  totp_setup: "开启 2FA",
  email_change: "换绑邮箱",
  codex_agent: "Codex 授权",
};
// 后端会下发筛选下拉项；接口异常时回退到本地标签，筛选框依旧可用。
const statusLabels: Record<string, string> = {
  pending: "等待执行",
  running: "执行中",
  success: "已完成",
  failed: "失败",
  cancelled: "已取消",
  stopped: "已停止",
  needs_attention: "待核实",
  paused: "已暂停",
  stopping: "取消中",
};
const jobTypeOptions = computed<TaskFilterOption[]>(() =>
  filterOptions.value.jobTypes.length
    ? filterOptions.value.jobTypes
    : Object.entries(labels).map(([value, label]) => ({ value, label })),
);
const statusOptions = computed<TaskFilterOption[]>(() =>
  filterOptions.value.statuses.length
    ? filterOptions.value.statuses
    : Object.entries(statusLabels).map(([value, label]) => ({ value, label })),
);
const filterSummary = computed(() => {
  const parts: string[] = [];
  if (applied.value.job_type)
    parts.push(
      jobTypeOptions.value.find(
        (option) => option.value === applied.value.job_type,
      )?.label || applied.value.job_type,
    );
  if (applied.value.status)
    parts.push(
      statusOptions.value.find(
        (option) => option.value === applied.value.status,
      )?.label || applied.value.status,
    );
  if (applied.value.q) parts.push(`“${applied.value.q}”`);
  return parts.join(" · ");
});
function status(task: OperationRow) {
  return ["deactivated", "needs_attention"].includes(task.source_status)
    ? task.source_status
    : task.display_status || task.status;
}
function progress(task: OperationRow) {
  return task.status === "pending"
    ? 0
    : Math.max(
        0,
        Math.min(
          100,
          Number(task.progress) || (task.status === "success" ? 100 : 0),
        ),
      );
}
let revision = 0;
async function refresh() {
  const current = ++revision;
  const currentView = view.value;
  const targetPage = historyPage.value;
  const query = taskFilterQuery(applied.value);
  loading.value = true;
  const results = await Promise.allSettled([
    request("/api/tasks/active", { query }),
    request("/api/tasks/manual-verifications"),
    ...(currentView === "history"
      ? [
          request("/api/tasks/history", {
            query: { ...query, page: targetPage, page_size: pageSize.value },
          }),
        ]
      : []),
  ]);
  if (current !== revision) return;
  const errors: string[] = [];
  const live = results[0];
  if (live?.status === "fulfilled") {
    try {
      const result = checkResult(live.value);
      active.value = result.items || [];
      counts.value = result.status_counts || {};
      if (Array.isArray(result.pools)) pools.value = result.pools;
      const options = taskFilterOptions(result);
      if (options.jobTypes.length || options.statuses.length)
        filterOptions.value = options;
      activeTotal.value = Number(result.total || 0);
      activeLoaded.value = true;
      activePage.value = Math.min(
        activePage.value,
        Math.max(1, Math.ceil(active.value.length / pageSize.value)),
      );
    } catch (cause) {
      errors.push(errorMessage(cause));
    }
  } else if (live?.status === "rejected")
    errors.push(errorMessage(live.reason));
  const manual = results[1];
  manualError.value = "";
  if (manual?.status === "fulfilled") {
    try {
      const result = checkResult(manual.value);
      manualVerifications.value = result.items || [];
      manualLoaded.value = true;
    } catch (cause) {
      manualError.value = errorMessage(cause);
    }
  } else if (manual?.status === "rejected")
    manualError.value = errorMessage(manual.reason);
  const past = results[2];
  if (past?.status === "fulfilled") {
    try {
      const result = checkResult(past.value);
      history.value = result.items || [];
      historyTotal.value = Number(result.total || 0);
      historyLoaded.value = true;
      const last = Math.max(1, Math.ceil(historyTotal.value / pageSize.value));
      if (historyPage.value > last) historyPage.value = last;
    } catch (cause) {
      errors.push(errorMessage(cause));
    }
  } else if (past?.status === "rejected")
    errors.push(errorMessage(past.reason));
  error.value = errors.join("；");
  if (!errors.length)
    updated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  loading.value = false;
}
usePolling(() => {
  if (auto.value && !busy.value) return refresh();
});
function applyFilters() {
  activePage.value = 1;
  historyPage.value = 1;
  applied.value = {
    job_type: filters.job_type,
    status: filters.status,
    q: filters.q.trim(),
  };
}
function resetFilters() {
  filters.job_type = "";
  filters.status = "";
  filters.q = "";
  applyFilters();
}
watch([view, historyPage, applied], () => void refresh());
watch(pageSize, () => {
  activePage.value = 1;
  if (historyPage.value !== 1) historyPage.value = 1;
  else if (view.value === "history") void refresh();
});
onBeforeUnmount(() => {
  revision++;
  cleanupVerification();
});
function openLog(task: OperationRow) {
  logTask.value = task;
  logOpen.value = true;
}
// 手动验证码模式下，正在运行的注册任务需要用户在任务旁提交邮箱验证码。
// 输入值按任务 ID 存在独立映射里，轮询刷新任务列表时不会丢失正在输入的内容。
const otpCodes = reactive<Record<string, string>>({});
const otpBusy = ref<string | null>(null);
function needsManualOtp(task: OperationRow) {
  return Boolean(
    task.manual_otp_required &&
      task.job_id != null &&
      status(task) === "running",
  );
}
function otpValid(task: OperationRow) {
  return /^\d{4,8}$/.test((otpCodes[task.id] || "").trim());
}
async function submitOtp(task: OperationRow) {
  if (otpBusy.value || !needsManualOtp(task) || !otpValid(task)) return;
  otpBusy.value = task.id;
  try {
    const result = checkResult(
      await request("/api/manual-otp", {
        method: "POST",
        body: {
          job_id: task.job_id,
          email: task.email,
          code: (otpCodes[task.id] || "").trim(),
        },
      }),
    );
    toast.success(result.message || "验证码已提交");
    otpCodes[task.id] = "";
  } catch (cause) {
    toast.error(errorMessage(cause));
  } finally {
    otpBusy.value = null;
  }
}
async function act(task: OperationRow, action: string) {
  if (busy.value || !task.capabilities?.[action]) return;
  busy.value = true;
  try {
    const result = checkResult(
      await request(`/api/tasks/${encodeURIComponent(task.id)}/${action}`, {
        method: "POST",
        body: {},
      }),
    );
    toast.success(result.message || "操作已提交");
    await refresh();
  } finally {
    busy.value = false;
  }
}
function taskAction(task: OperationRow, action: string) {
  if (action === "cancel")
    confirmation.value = {
      title: `取消任务 ${task.id}`,
      description:
        task.job_id == null
          ? "排队任务会直接取消；运行中的任务会在当前检查点停止，已提交的远端操作不会回滚。"
          : "排队任务会直接取消，运行中的任务会在当前检查点停止。",
      label: "取消任务",
      run: () => act(task, action),
    };
  else
    void act(task, action).catch((cause) => toast.error(errorMessage(cause)));
}
async function applyConcurrency() {
  if (concurrencySaving.value || !currentPool.value || !concurrencyValid.value)
    return;
  const workers = Number(poolWorkers.value);
  if (workers === Number(currentPool.value.workers)) return;
  concurrencySaving.value = true;
  try {
    const result = checkResult(
      await request("/api/tasks/concurrency", {
        method: "POST",
        body: { job_type: poolName.value, workers },
      }),
    );
    toast.success(result.message || `并发已调整为 ${workers}`);
    if (result.warning) toast.info(result.warning);
    if (Array.isArray(result.pools)) pools.value = result.pools;
    await refresh();
  } catch (cause) {
    toast.error(errorMessage(cause));
  } finally {
    concurrencySaving.value = false;
  }
}
function cancelPending() {
  confirmation.value = {
    title: "取消排队注册任务",
    description: `将取消 ${pendingRegistration.value} 个排队注册任务，包含注册队列中的 Codex 补跑。`,
    run: async () => {
      if (busy.value) return;
      busy.value = true;
      try {
        const result = checkResult(
          await request("/api/jobs/cancel-pending", {
            method: "POST",
            body: {},
          }),
        );
        toast.success(`已取消 ${result.cancelled} 个排队任务`);
        await refresh();
      } finally {
        busy.value = false;
      }
    },
  };
}
</script>

<template>
  <div class="stack task-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">OPERATIONS / TASKS</p>
        <h1 class="page-title">任务中心</h1>
        <p class="page-description">
          注册、授权与账号操作，在一个地方掌握进度。
        </p>
      </div>
      <NuxtLink class="btn btn-primary" to="/"
        >创建注册任务 <span aria-hidden="true">＋</span></NuxtLink
      >
    </header>
    <section class="stat-grid" aria-label="任务统计">
      <div class="stat-card">
        <span class="stat-label">进行中</span
        ><strong class="stat-value">{{ counts.active ?? "—" }}</strong
        ><span class="muted">{{
          filterActive ? "当前筛选范围内的活跃任务" : "全部类型的活跃任务"
        }}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">正在执行</span
        ><strong class="stat-value">{{ counts.running ?? "—" }}</strong
        ><span class="muted">任务正在处理中</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">等待执行</span
        ><strong class="stat-value">{{ counts.pending ?? "—" }}</strong
        ><span class="muted">按队列顺序调度</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">已暂停</span
        ><strong class="stat-value">{{ counts.paused ?? "—" }}</strong
        ><span class="muted">恢复后继续执行</span>
      </div>
    </section>
    <section class="card" aria-labelledby="manual-verification-title">
      <div class="card-header task-header">
        <div>
          <h2 id="manual-verification-title">待人工验证</h2>
          <p class="muted">
            后台浏览器保持同一验证会话，无需桌面窗口。打开网页验证后，验证码请自己点击。
          </p>
        </div>
        <button class="btn btn-sm" :disabled="loading" @click="refresh">
          {{ loading ? "刷新中…" : "刷新列表" }}
        </button>
      </div>
      <div v-if="manualError" class="alert alert-error table-alert" role="alert">
        {{ manualError }}
      </div>
      <div v-if="loading && !manualLoaded" class="empty-state" role="status">
        正在加载待人工验证列表…
      </div>
      <div
        v-else-if="!manualVerifications.length && !manualError"
        class="empty-state"
      >
        当前没有待人工验证的账号。
      </div>
      <div v-if="manualVerifications.length" class="table-wrap" :aria-busy="loading">
        <table class="data-table">
          <thead>
            <tr><th>邮箱</th><th>等待开始时间</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in manualVerifications" :key="item.account_id">
              <td>{{ item.email || `账号 ${item.account_id}` }}</td>
              <td class="time-cell">{{ formatTime(item.waiting_since) }}</td>
              <td>
                <button
                  class="btn btn-sm btn-primary"
                  type="button"
                  @click="openVerification(item)"
                >
                  网页验证
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <section class="card">
      <div class="card-header task-header">
        <div class="view-tabs" aria-label="任务视图">
          <button
            :class="{ active: view === 'active' }"
            :aria-pressed="view === 'active'"
            @click="view = 'active'"
          >
            进行中 <span>{{ counts.active || 0 }}</span></button
          ><button
            :class="{ active: view === 'history' }"
            :aria-pressed="view === 'history'"
            @click="view = 'history'"
          >
            历史任务
          </button>
        </div>
        <div class="inline">
          <label class="inline refresh-label"
            ><input v-model="auto" type="checkbox" />自动刷新</label
          ><button class="btn btn-sm" :disabled="loading" @click="refresh">
            {{ loading ? "刷新中…" : "刷新" }}
          </button>
        </div>
      </div>
      <form
        class="toolbar task-filters"
        aria-label="任务筛选"
        @submit.prevent="applyFilters"
      >
        <label class="inline filter-field"
          >任务类型<select
            v-model="filters.job_type"
            class="select"
            aria-label="按任务类型筛选"
          >
            <option value="">全部类型</option>
            <option
              v-for="option in jobTypeOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        >
        <label class="inline filter-field"
          >状态<select
            v-model="filters.status"
            class="select"
            aria-label="按任务状态筛选"
          >
            <option value="">全部状态</option>
            <option
              v-for="option in statusOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select></label
        >
        <label class="inline filter-field filter-search"
          >关键词<input
            v-model="filters.q"
            class="input"
            type="search"
            placeholder="邮箱或任务 ID"
            aria-label="按邮箱或任务 ID 筛选"
        /></label>
        <button type="submit" class="btn btn-sm btn-primary">应用筛选</button>
        <button
          type="button"
          class="btn btn-sm"
          :disabled="!filterActive && !filterDirty"
          @click="resetFilters"
        >
          重置
        </button>
        <span v-if="filterActive" class="muted filter-summary"
          >已筛选：{{ filterSummary }}</span
        >
      </form>
      <div class="toolbar task-toolbar">
        <span class="muted">{{
          updated ? `更新于 ${updated}` : "正在同步任务状态"
        }}</span
        ><button
          v-if="view === 'active'"
          class="btn btn-sm"
          :disabled="busy || !pendingRegistration"
          @click="cancelPending"
        >
          取消排队注册任务（{{ pendingRegistration }}）</button
        ><span
          v-if="view === 'active' && pools.length"
          class="inline concurrency-group"
        >
          <label class="inline"
            >后台并发<select
              v-model="poolName"
              class="select pool-select"
              aria-label="选择任务类型"
            >
              <option v-for="pool in pools" :key="pool.name" :value="pool.name">
                {{ pool.label }}（{{ pool.workers }}）
              </option></select
            ></label
          ><button
            class="btn btn-sm btn-icon"
            type="button"
            aria-label="并发减一"
            :disabled="concurrencySaving || Number(poolWorkers) <= 1"
            @click="poolWorkers = Math.max(1, Number(poolWorkers) - 1)"
          >
            −</button
          ><input
            v-model.number="poolWorkers"
            class="input workers-input"
            type="number"
            min="1"
            :max="currentPoolMax"
            step="1"
            aria-label="并发线程数"
            :disabled="concurrencySaving"
          /><button
            class="btn btn-sm btn-icon"
            type="button"
            aria-label="并发加一"
            :disabled="
              concurrencySaving || Number(poolWorkers) >= currentPoolMax
            "
            @click="
              poolWorkers = Math.min(currentPoolMax, Number(poolWorkers) + 1)
            "
          >
            ＋</button
          ><button
            class="btn btn-sm"
            type="button"
            :disabled="
              concurrencySaving || !concurrencyDirty || !concurrencyValid
            "
            @click="applyConcurrency"
          >
            {{
              concurrencySaving
                ? "应用中…"
                : concurrencyDirty
                  ? "应用并发"
                  : "并发已生效"
            }}</button
          ><span v-if="currentPool" class="muted concurrency-hint">{{
            concurrencyHint
          }}</span>
        </span>
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
        <button class="btn btn-sm" :disabled="loading" @click="refresh">
          重新加载
        </button>
      </div>
      <div
        v-if="view === 'active' && activeTotal > active.length"
        class="alert table-alert"
      >
        当前展示最近 {{ active.length }} 个{{ filterActive ? "符合筛选条件的" : "" }}活跃任务，共
        {{ activeTotal }} 个。
      </div>
      <div v-if="loading && !loaded" class="empty-state" role="status">
        正在加载{{ view === "active" ? "进行中" : "历史" }}任务…
      </div>
      <UiEmpty
        v-else-if="!rows.length && !error"
        :title="
          filterActive
            ? '没有符合筛选条件的任务'
            : view === 'active'
              ? '当前没有进行中的任务'
              : '暂无历史任务'
        "
        :description="
          filterActive
            ? '换个任务类型、状态或关键词，或者重置筛选条件。'
            : view === 'active'
              ? '所有任务已经处理完毕，新任务将在这里显示。'
              : '任务完成后，执行结果和日志会保留在这里。'
        "
      />
      <div v-else-if="rows.length" class="table-wrap" :aria-busy="loading">
        <table class="data-table">
          <thead>
            <tr>
              <th>任务</th>
              <th>状态</th>
              <th>邮箱</th>
              <th>{{ view === "active" ? "执行进度" : "执行结果" }}</th>
              <th>{{ view === "active" ? "开始时间" : "结束时间" }}</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="task in rows" :key="task.id">
              <td>
                <strong>{{
                  task.label || labels[task.job_type] || "任务"
                }}</strong
                ><small class="cell-sub mono">{{ task.id }}</small>
              </td>
              <td><UiBadge :value="status(task)" /></td>
              <td>{{ task.email || "等待分配" }}</td>
              <td class="progress-cell">
                <div v-if="view === 'active'" class="progress-line">
                  <progress
                    :value="progress(task)"
                    max="100"
                    :aria-label="`${task.label || '任务'}进度`"
                  /><span>{{ progress(task) }}%</span>
                </div>
                <small
                  class="task-message"
                  :title="
                    task.error_message || task.progress_message || task.stage
                  "
                  >{{
                    task.error_message ||
                    task.progress_message ||
                    task.stage ||
                    "等待执行"
                  }}</small
                >
              </td>
              <td class="time-cell">
                {{
                  formatTime(
                    view === "active"
                      ? task.started_at || task.created_at
                      : task.completed_at || task.started_at || task.created_at,
                  )
                }}
              </td>
              <td>
                <div class="row-actions">
                  <div v-if="needsManualOtp(task)" class="otp-inline">
                    <input
                      v-model="otpCodes[task.id]"
                      class="input mono otp-input"
                      inputmode="numeric"
                      autocomplete="one-time-code"
                      maxlength="8"
                      placeholder="邮箱验证码"
                      :aria-label="`任务 ${task.id} 邮箱验证码`"
                      :disabled="otpBusy === task.id"
                      @keyup.enter="submitOtp(task)"
                    /><button
                      class="btn btn-sm btn-primary"
                      type="button"
                      :disabled="otpBusy === task.id || !otpValid(task)"
                      @click="submitOtp(task)"
                    >
                      {{ otpBusy === task.id ? "提交中…" : "提交验证码" }}
                    </button>
                  </div>
                  <button class="btn btn-sm" @click="openLog(task)">
                    {{ view === "active" ? "查看进度" : "日志" }}</button
                  ><button
                    v-if="task.capabilities?.pause"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="taskAction(task, 'pause')"
                  >
                    暂停</button
                  ><button
                    v-if="task.capabilities?.resume"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="taskAction(task, 'resume')"
                  >
                    恢复</button
                  ><button
                    v-if="task.capabilities?.cancel"
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="taskAction(task, 'cancel')"
                  >
                    取消</button
                  ><span v-if="task.status === 'stopping'" class="muted"
                    >停止中…</span
                  >
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiPagination v-model:page="page" :total="total" :page-size="pageSize" />
    </section>
    <UiModal v-model:open="verificationOpen" title="网页人工验证">
      <div class="stack">
        <p>{{ verificationAccount?.email || `账号 ${verificationAccount?.account_id}` }}</p>
        <p class="muted">后台浏览器保持同一验证会话，无需桌面窗口。验证码请自己点击。</p>
        <p v-if="verificationError" class="alert alert-error" role="alert">{{ verificationError }}</p>
        <p v-if="verificationPending" class="muted" role="status">验证画面准备中…</p>
        <img
          v-if="verificationFrame"
          :key="verificationFrame.revision"
          class="verification-image"
          :src="`data:image/jpeg;base64,${verificationFrame.image}`"
          alt="后台浏览器实时验证画面，点击验证码进行人工验证"
          :aria-busy="verificationInputBusy"
          draggable="false"
          @load="verificationDisplayedFrame = verificationFrame"
          @click="sendVerificationClick"
        />
        <p v-if="verificationInputBusy" class="muted" role="status">正在发送点击…</p>
      </div>
    </UiModal>
    <OperationsActionDialog v-model:action="confirmation" />
    <OperationsLogModal
      v-model:open="logOpen"
      :endpoint="
        logTask ? `/api/tasks/${encodeURIComponent(logTask.id)}/log` : ''
      "
      :title="`${logTask?.label || '任务'} · ${logTask?.id || ''}`"
    />
  </div>
</template>

<style scoped>
.task-page {
  gap: 24px;
}
.task-page > .page-header {
  margin-bottom: 0;
}
.stat-card > .muted {
  font-size: 12px;
}
.task-header {
  flex-wrap: wrap;
  gap: 12px;
}
.view-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  background: var(--surface-secondary, #f4f6fa);
  border-radius: 10px;
}
.view-tabs button {
  border: 0;
  background: transparent;
  color: var(--text-secondary, #64748b);
  padding: 9px 15px;
  border-radius: 7px;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}
.view-tabs button.active {
  background: var(--surface, #fff);
  color: var(--text, #1e293b);
  box-shadow: 0 1px 4px #14274b0c;
}
.view-tabs span {
  margin-left: 6px;
  font-size: 11px;
}
.task-toolbar {
  padding: 14px 20px;
  gap: 12px;
  font-size: 12px;
  border-bottom: 1px solid var(--border, #e5e7eb);
}
.task-filters {
  padding: 14px 20px;
  gap: 10px;
  font-size: 12px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border, #e5e7eb);
  background: var(--surface-secondary, #f8fafc);
}
.filter-field {
  gap: 6px;
  color: var(--text-secondary, #64748b);
}
.filter-field select {
  width: 150px;
}
.filter-search .input {
  width: 200px;
  min-height: 30px;
  padding: 2px 8px;
}
.filter-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}
.page-size {
  margin-left: auto;
}
.page-size select {
  width: 70px;
}
.concurrency-group {
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.pool-select {
  width: 150px;
  margin-left: 6px;
}
.workers-input {
  width: 64px;
  min-height: 28px;
  padding: 2px 6px;
  text-align: center;
}
.btn-icon {
  min-width: 28px;
  padding: 2px 6px;
  line-height: 1.2;
}
.concurrency-hint {
  white-space: nowrap;
}
.refresh-label {
  font-size: 12px;
}
.table-alert {
  margin: 16px 20px;
}
.cell-sub {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary, #64748b);
}
.time-cell {
  white-space: nowrap;
  font-size: 12px;
}
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-width: 145px;
}
.otp-inline {
  flex-basis: 100%;
  display: flex;
  gap: 4px;
}
.otp-input {
  width: 118px;
  min-height: 28px;
  padding: 2px 6px;
  font-size: 12px;
}
.progress-cell {
  min-width: 210px;
  max-width: 310px;
}
.progress-line {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  margin-bottom: 8px;
}
.progress-line progress {
  flex: 1;
  width: 130px;
  height: 5px;
  accent-color: var(--accent, #0f766e);
}
.task-message {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary, #64748b);
  font-size: 12px;
}
@media (max-width: 650px) {
  .page-size {
    margin-left: 0;
  }
}
</style>
