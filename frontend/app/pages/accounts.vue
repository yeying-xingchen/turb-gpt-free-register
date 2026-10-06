<script setup lang="ts">
import ImportModal from "~/components/accounts/ImportModal.vue";
import OperationsModal from "~/components/accounts/OperationsModal.vue";
import GroupsModal from "~/components/accounts/GroupsModal.vue";
import PlanInfo from "~/components/accounts/PlanInfo.vue";
import QuotaInfo from "~/components/accounts/QuotaInfo.vue";
import QuotaUsage from "~/components/accounts/QuotaUsage.vue";
import PromoDetailsModal from "~/components/accounts/PromoDetailsModal.vue";
import {
  accountActionLabels,
  accountBatchLimit,
  accountDate,
  accountError,
  accountPlanInfo,
  accountResult,
  accountResultDetails,
  accountStatus,
  chunkAccountIds,
  copyAccountText,
  mergeAccountResults,
  type Account,
  type AccountGroup,
} from "~/utils/accounts";

const { request } = useApi();
const toast = useToast();
const rows = ref<Account[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const error = ref("");
const groupsError = ref("");
const groups = ref<AccountGroup[]>([]);
// 选中状态以 ID 集合为准（可以容纳整个账号库），行缓存只保存确实加载过的账号。
const selectedIds = ref(new Set<number>());
const selectedRows = ref(new Map<number, Account>());
const allAccountsTotal = ref(0);
const selectBusy = ref("");
const selectProgress = ref("");
const filters = reactive({
  q: "",
  archived: "0",
  plan: "",
  codex_status: "",
  totp_status: "",
  at_status: "",
  live_status: "",
  redemption: "unredeemed",
  group: "",
  date_from: "",
  date_to: "",
  promo_type: "",
  promo_discount: "",
});
// 默认隐藏已被兑换码领取的账号，点工具栏按钮后再一起显示。
const applied = ref<Record<string, string>>({
  archived: "0",
  redemption: "unredeemed",
});
const advancedFilters = ref(false);
const autoRefresh = ref(true);
const lastUpdated = ref("");
const importOpen = ref(false);
const groupsOpen = ref(false);
const promoAccount = ref<Account | null>(null);
const promoOpen = ref(false);
const lookupOpen = ref(false);
const lookupText = ref("");
const lookupBusy = ref(false);
const lookupError = ref("");
const lookupMissing = ref<string[]>([]);
const lookupSummary = ref("");
const deleting = ref(false);
const copyBusy = ref("");
const action = ref("");
const operationSeed = ref<{ ids: number[]; rows: Account[]; count: number } | null>(
  null,
);
const operationRevision = ref(0);
const notice = ref("");
const noticeDetails = ref<string[]>([]);
const operationLogOpen = ref(false);
const operationLogQuery = ref<Record<string, string>>({});
const operationLogTitle = ref("任务日志");
let operationLogRevision = 0;
let loadRevision = 0;
let selectionRevision = 0;
let mounted = true;
let initialized = false;
const selectedCount = computed(() => selectedIds.value.size);
const selectedIdList = computed(() => Array.from(selectedIds.value));
const selectedRowList = computed(() =>
  Array.from(selectedRows.value.values()),
);
const allPageSelected = computed(
  () =>
    rows.value.length > 0 &&
    rows.value.every((row) => selectedIds.value.has(row.id)),
);
const somePageSelected = computed(() =>
  rows.value.some((row) => selectedIds.value.has(row.id)),
);
// 只要当前没有限定「未兑换」，就说明已兑换账号正在一起显示。
const showRedeemed = computed(
  () => applied.value.redemption !== "unredeemed",
);
const operationRows = computed(() =>
  (operationSeed.value?.rows || []).map(
    (account) =>
      rows.value.find((row) => row.id === account.id) ||
      selectedRows.value.get(account.id) ||
      account,
  ),
);
const pageStats = computed(() => {
  const infos = rows.value.map((row) => accountPlanInfo(row));
  return {
    plus: infos.filter((info) => info.paid).length,
    trial: infos.filter((info) => info.trialEligible).length,
    promo: infos.filter((info) => info.promos.length > 0).length,
    checking: infos.filter((info) => info.queryKind && info.queryKind !== "failed")
      .length,
    failed: infos.filter((info) => info.queryKind === "failed").length,
    twofa: rows.value.filter((row) => row.totp_enabled).length,
  };
});
const batchGroups = [
  {
    name: "检查与安全",
    values: ["live", "plan", "quota", "totp", "email"],
  },
  { name: "订阅与支付", values: ["activate", "extract", "pay", "pay-query"] },
  {
    name: "Codex 与导出",
    values: ["agent", "upload", "retry", "stop", "export"],
  },
  {
    name: "整理账号",
    values: ["note", "group", "archive", "restore", "delete"],
  },
];
const batchLabels: Record<string, string> = {
  live: "查活",
  plan: "查套餐",
  quota: "查额度",
  totp: "开 2FA",
  email: "换邮箱",
  activate: "开通 Plus",
  extract: "提链",
  pay: "提交支付",
  "pay-query": "查询支付",
  agent: "生成 Agent",
  upload: "上传 sub2",
  retry: "补跑",
  stop: "停止",
  export: "导出",
  note: "备注",
  group: "设置分组",
  archive: "归档",
  restore: "取消归档",
  delete: "删除",
};
const batchTitles: Record<string, string> = {
  live: "重新登录选中账号，成功且未封号则标记正常，并刷新最新 AT",
  plan: "查询选中账号的当前套餐、试用资格与到期时间",
  quota:
    "查询选中账号的额度余额、Codex 5 小时/周用量和「银行重置」券张数与到期时间；只读接口，不消耗额度",
  totp: "为选中账号开启 2FA，完成后写回 TOTP 密钥；已启用或正在处理的会跳过",
  email: "为选中账号从指定来源领取新邮箱并换绑",
  activate: "配置提链服务商、CDK 与支付平台后提交，后台核验真实 Plus",
  extract: "给选中的 free（可 Plus 试用）账号批量提链，成功会消耗 CDK 次数",
  pay: "为已提链的选中账号提交支付，支付平台可能消耗额度",
  "pay-query": "用原支付凭据查询选中账号已有的支付任务，不会重新提交",
  agent: "用选中账号的 AT 生成 Codex Agent 凭据并保存到本地",
  upload: "把已生成的 Agent 凭据上传到系统配置的 sub2api",
  retry: "给选中账号批量补跑 Codex 授权，会消耗邮箱 OTP 和接码短信",
  stop: "停止选中账号中正在补跑的 Codex 授权",
  export: "读取并复制或导出选中账号的凭据，仅在执行后读取",
  note: "为选中账号统一设置备注，留空可清空备注",
  group: "把选中账号移入指定分组，可输入新分组名",
  archive: "归档选中账号，默认列表不再显示，可随时取消归档",
  restore: "把选中账号从归档列表恢复到正常列表",
  delete: "永久删除选中账号的本地记录，无法撤销；邮箱池状态不变",
};
function query() {
  return {
    ...applied.value,
    paged: 1,
    page: page.value,
    page_size: pageSize.value,
  };
}
async function loadAccounts(quiet = false) {
  const revision = ++loadRevision;
  if (!quiet) loading.value = true;
  try {
    const result = await request("/api/accounts", { query: query() });
    if (!mounted || revision !== loadRevision) return;
    total.value = Number(result.total || 0);
    const lastPage = Math.max(1, Math.ceil(total.value / pageSize.value));
    if (page.value > lastPage) {
      page.value = lastPage;
      return;
    }
    rows.value = result.items || [];
    for (const row of rows.value)
      if (selectedIds.value.has(row.id)) selectedRows.value.set(row.id, row);
    lastUpdated.value = new Date().toLocaleTimeString("zh-CN", {
      hour12: false,
    });
    error.value = "";
  } catch (cause) {
    if (mounted && revision === loadRevision) error.value = accountError(cause);
  } finally {
    if (revision === loadRevision) loading.value = false;
  }
}
async function loadGroups() {
  try {
    const result = await request("/api/account-groups");
    if (!mounted) return;
    groups.value = result.groups || [];
    groupsError.value = "";
  } catch (cause) {
    if (mounted) groupsError.value = accountError(cause);
  }
}
async function loadAllAccountsTotal() {
  try {
    const result = await request("/api/accounts/ids", {
      query: { scope: "all", page: 1, page_size: 1 },
    });
    if (mounted) allAccountsTotal.value = Number(result.total || 0);
  } catch {
    // 总数只用于按钮提示，失败不影响账号列表使用。
  }
}
async function refresh() {
  await Promise.all([loadAccounts(), loadGroups(), loadAllAccountsTotal()]);
}
function clearSelection() {
  selectedIds.value = new Set();
  selectedRows.value = new Map();
  selectionRevision++;
}
function applyFilters() {
  if (
    filters.date_from &&
    filters.date_to &&
    filters.date_from > filters.date_to
  ) {
    toast.error("开始日期不能晚于结束日期");
    return;
  }
  const { promo_type, promo_discount, ...values } = filters;
  if (values.plan === "promo" && (promo_type || promo_discount))
    values.plan = `promo:${promo_type || "*"}:${promo_discount}`;
  applied.value = { ...values, q: values.q.trim() };
  page.value = 1;
  clearSelection();
}
function resetFilters() {
  Object.assign(filters, {
    q: "",
    archived: "0",
    plan: "",
    codex_status: "",
    totp_status: "",
    at_status: "",
    live_status: "",
    redemption: "unredeemed",
    group: "",
    date_from: "",
    date_to: "",
    promo_type: "",
    promo_discount: "",
  });
  applyFilters();
}
function toggleRedeemed() {
  const redemption = showRedeemed.value ? "unredeemed" : "";
  filters.redemption = redemption;
  applied.value = { ...applied.value, redemption };
  page.value = 1;
  clearSelection();
}
watch([page, pageSize, applied], () => {
  if (initialized) void loadAccounts();
});
function resizePage() {
  page.value = 1;
}
usePolling(async () => {
  if (!initialized) {
    initialized = true;
    await refresh();
    return;
  }
  if (autoRefresh.value && !loading.value && !document.hidden)
    await loadAccounts(true);
}, 8000);
onBeforeUnmount(() => {
  mounted = false;
  loadRevision++;
  selectionRevision++;
});
function selectRow(row: Account) {
  selectedIds.value.add(row.id);
  selectedRows.value.set(row.id, row);
}
function deselectRow(id: number) {
  selectedIds.value.delete(id);
  selectedRows.value.delete(id);
}
function toggleRow(row: Account) {
  selectionRevision++;
  if (selectedIds.value.has(row.id)) deselectRow(row.id);
  else selectRow(row);
}
function togglePage() {
  selectionRevision++;
  if (allPageSelected.value) rows.value.forEach((row) => deselectRow(row.id));
  else rows.value.forEach(selectRow);
}
/**
 * 全选：scope=filtered 按当前筛选收集 ID；scope=all 忽略筛选，包含已兑换与已归档。
 * 只按页收集 ID（每页 5000），再取第一页做行缓存，因此不受账号总数限制。
 */
async function selectAllAccounts(scope: "filtered" | "all") {
  if (selectBusy.value) return;
  const revision = ++selectionRevision;
  const filterSnapshot = scope === "all" ? {} : { ...applied.value };
  selectBusy.value = scope;
  selectProgress.value = "正在统计…";
  try {
    const ids = new Set<number>();
    let expected = 0;
    for (let index = 1; ; index += 1) {
      const result = await request("/api/accounts/ids", {
        query: {
          ...filterSnapshot,
          scope,
          page: index,
          page_size: 5000,
        },
      });
      if (!mounted || revision !== selectionRevision) return;
      expected = Number(result.total || 0);
      const pageIds = result.ids || [];
      for (const raw of pageIds) {
        const id = Number(raw);
        if (Number.isInteger(id) && id > 0) ids.add(id);
      }
      selectProgress.value = `已选 ${ids.size}/${expected}`;
      if (!pageIds.length || ids.size >= expected) break;
    }
    const preview = await request("/api/accounts", {
      query: { ...filterSnapshot, paged: 1, page: 1, page_size: 50 },
    });
    if (!mounted || revision !== selectionRevision) return;
    const cache = new Map<number, Account>();
    for (const row of preview.items || [])
      if (ids.has(row.id)) cache.set(row.id, row);
    selectedIds.value = ids;
    selectedRows.value = cache;
    toast.success(
      scope === "all"
        ? `已选中账号库全部 ${ids.size} 个账号（含已兑换与已归档）`
        : `已选中筛选结果中的 ${ids.size} 个账号`,
    );
  } catch (cause) {
    toast.error(accountError(cause));
  } finally {
    selectBusy.value = "";
    selectProgress.value = "";
  }
}
async function lookup() {
  const emails = [
    ...new Set(
      lookupText.value
        .split(/[\s,;，；]+/)
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean),
    ),
  ];
  lookupError.value = lookupSummary.value = "";
  lookupMissing.value = [];
  if (!emails.length) {
    lookupError.value = "请输入至少 1 个邮箱";
    return;
  }
  lookupBusy.value = true;
  const revision = selectionRevision;
  try {
    const { q, ...lookupFilters } = applied.value;
    const matches: Account[] = [];
    const missing: string[] = [];
    // 后端单次最多查 5000 个邮箱，超过时自动分批。
    for (let index = 0; index < emails.length; index += 5000) {
      const result = await request("/api/accounts/lookup", {
        method: "POST",
        body: { emails: emails.slice(index, index + 5000), ...lookupFilters },
      });
      if (!mounted || revision !== selectionRevision) return;
      matches.push(...((result.matches || []) as Account[]));
      missing.push(...((result.not_found || []) as string[]));
    }
    for (const row of matches) selectRow(row);
    lookupMissing.value = missing;
    lookupSummary.value = `匹配 ${matches.length} 个，未匹配 ${missing.length} 个；当前共选中 ${selectedCount.value} 个。`;
    toast.info(lookupSummary.value);
  } catch (cause) {
    lookupError.value = accountError(cause);
  } finally {
    lookupBusy.value = false;
  }
}
function openOperation(value: string, picked?: Account[]) {
  const ids = picked ? picked.map((account) => account.id) : selectedIdList.value;
  if (!ids.length) {
    toast.info("请先选择账号");
    return;
  }
  operationSeed.value = {
    ids,
    rows: picked ? [...picked] : selectedRowList.value,
    count: ids.length,
  };
  operationRevision.value += 1;
  action.value = value;
}
function openPromos(account: Account) {
  promoAccount.value = account;
  promoOpen.value = true;
}
function redeemTitle(row: Account) {
  if (!row.redeemed) return "尚未被兑换码领取，仍在可兑换库存中";
  return row.redeemed_at
    ? `已被兑换码领取：${accountDate(row.redeemed_at)}`
    : "已被兑换码领取";
}
function runBatch(value: string) {
  if (!selectedCount.value) {
    toast.info("请先选择账号");
    return;
  }
  if (value === "delete") void deleteAccounts();
  else openOperation(value);
}
async function copySecret(account: Account, field: string) {
  if (copyBusy.value) return;
  copyBusy.value = `${account.id}:${field}`;
  try {
    const result = await request(`/api/accounts/${account.id}/secret`, {
      query: { field },
    });
    if (!result.value || result.value === "未设置")
      throw new Error("该账号尚未设置此字段");
    await copyAccountText(result.value);
    toast.success(
      field === "totp_code"
        ? "当前 2FA 验证码已复制，请及时使用"
        : "已复制到剪贴板",
    );
  } catch (cause) {
    toast.error(accountError(cause));
  } finally {
    copyBusy.value = "";
  }
}
/** 删除选中账号：超过后端单次上限时按 5000 个一批自动分批提交。 */
async function deleteAccounts(picked?: { ids: number[]; rows: Account[] }) {
  const target = picked ? picked.ids : selectedIdList.value;
  const knownRows = picked ? picked.rows : selectedRowList.value;
  if (!target.length || deleting.value) return;
  const preview = knownRows
    .slice(0, 3)
    .map((account) => account.email)
    .join(
      "\n",
    );
  if (
    !confirm(
      `确定永久删除 ${target.length} 个账号的本地记录？\n\n${preview}${
        target.length > 3 ? "\n…" : ""
      }\n\n本地保存的凭据将被删除，无法撤销；邮箱池状态不会改变。`,
    )
  )
    return;
  deleting.value = true;
  try {
    const results: any[] = [];
    for (const chunk of chunkAccountIds(target, accountBatchLimit("delete"))) {
      results.push(
        await request("/api/accounts/delete-bulk", {
          method: "POST",
          body: { account_ids: chunk },
        }),
      );
    }
    const result = mergeAccountResults(results);
    const deleted = new Set<number>(
      (result.deleted || []).map((item: any) =>
        Number(typeof item === "object" ? item.id : item),
      ),
    );
    for (const id of deleted) deselectRow(id);
    notice.value = accountResult(result);
    noticeDetails.value = accountResultDetails(result);
    toast.success(notice.value);
    action.value = "";
    await refresh();
  } catch (cause) {
    toast.error(accountError(cause));
  } finally {
    deleting.value = false;
  }
}
async function openSubmittedLog(payload: {
  accountIds: number[];
  jobType: string;
  title: string;
}) {
  const revision = ++operationLogRevision;
  action.value = "";
  const query = {
    account_ids: payload.accountIds.join(","),
    job_type: payload.jobType,
  };
  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (revision !== operationLogRevision) return;
    try {
      const result = await request("/api/accounts/tasks/latest", { query });
      const taskIds = (result.task_ids || []).filter(Boolean);
      if (taskIds.length) {
        operationLogTitle.value = payload.title;
        operationLogQuery.value = { task_ids: taskIds.join(",") };
        operationLogOpen.value = true;
        return;
      }
    } catch {
      // The queue may finish its account-state transaction just after the response.
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  if (revision === operationLogRevision)
    toast.info("任务已提交，可在任务中心打开日志查看执行过程");
}
async function afterOperation() {
  await refresh();
}
</script>

<template>
  <div class="accounts-page stack">
    <header class="page-header">
      <div>
        <p class="eyebrow">ACCOUNT WORKSPACE</p>
        <h1 class="page-title">账号管理</h1>
        <p class="page-description">
          管理账号、检查状态与订阅，集中处理日常操作。
        </p>
      </div>
      <div class="inline header-actions">
        <button class="btn" :disabled="loading" @click="refresh">
          <UiIcon name="refresh" />{{ loading ? "刷新中" : "刷新" }}</button
        ><button class="btn btn-primary" @click="importOpen = true">
          <UiIcon name="plus" />导入账号
        </button>
      </div>
    </header>

    <div class="stat-grid account-stats">
      <div class="stat-card">
        <span class="stat-label">筛选结果</span
        ><strong class="stat-value">{{ total.toLocaleString() }}</strong
        ><small class="muted">服务端实时分页</small>
      </div>
      <div class="stat-card">
        <span class="stat-label">已选账号</span
        ><strong class="stat-value">{{ selectedCount }}</strong
        ><small class="muted">支持跨页与全选批量操作</small>
      </div>
      <div class="stat-card">
        <span class="stat-label">本页 Plus / 可试用 / 有优惠</span
        ><strong class="stat-value stat-trio"
          >{{ pageStats.plus }} <span class="stat-slash">/</span>
          {{ pageStats.trial }} <span class="stat-slash">/</span>
          {{ pageStats.promo }}</strong
        ><small class="muted">共 {{ rows.length }} 个本页账号</small>
      </div>
      <div class="stat-card">
        <span class="stat-label">本页 2FA 已启用</span
        ><strong class="stat-value">{{ pageStats.twofa }}</strong
        ><small class="muted"
          >密钥仅按需读取<span v-if="pageStats.failed">
            · {{ pageStats.failed }} 个套餐查询失败</span
          ><span v-else-if="pageStats.checking">
            · {{ pageStats.checking }} 个正在查套餐</span
          ></small
        >
      </div>
    </div>

    <section class="card filter-card" aria-label="账号筛选">
      <form class="card-body stack" @submit.prevent="applyFilters">
        <div class="filter-main">
          <label class="field search-field"
            ><span>搜索账号</span>
            <div class="search-input">
              <UiIcon name="search" /><input
                v-model="filters.q"
                class="input"
                placeholder="搜索邮箱、用户名或备注"
                type="search"
              /></div
          ></label>
          <label class="field"
            >分组<select v-model="filters.group" class="select">
              <option value="">全部分组</option>
              <option
                v-for="group in groups"
                :key="group.group_name"
                :value="group.group_name"
              >
                {{ group.group_name }}（{{ group.total }}）
              </option>
            </select></label
          >
          <label class="field"
            >套餐<select v-model="filters.plan" class="select">
              <option value="">全部套餐</option>
              <option value="plus">Plus</option>
              <option value="plus_trial">Free（可试用 Plus）</option>
              <option value="free">全部 Free</option>
              <option value="promo">有促销资格</option>
              <option value="free_no_trial">Free（无试用）</option>
            </select></label
          >
          <label class="field"
            >归档状态<select v-model="filters.archived" class="select">
              <option value="0">正常账号</option>
              <option value="only">已归档</option>
              <option value="all">全部账号</option>
            </select></label
          >
          <button type="submit" class="btn btn-primary">应用筛选</button>
        </div>
        <div v-if="advancedFilters" class="form-grid advanced-filters">
          <label class="field"
            >Codex 状态<select v-model="filters.codex_status" class="select">
              <option value="">全部状态</option>
              <option value="success">成功</option>
              <option value="failed">失败</option>
              <option value="retrying">补跑中</option>
              <option value="stopped">已停止</option>
              <option value="skipped">已跳过</option>
              <option value="deactivated">已停用</option>
            </select></label
          >
          <label class="field"
            >2FA 状态<select v-model="filters.totp_status" class="select">
              <option value="">全部状态</option>
              <option value="enabled">已启用</option>
              <option value="disabled">未启用</option>
              <option value="pending">正在设置</option>
              <option value="failed">设置失败</option>
            </select></label
          >
          <label class="field"
            >AT 状态<select v-model="filters.at_status" class="select">
              <option value="">全部状态</option>
              <option value="expired">已过期</option>
              <option value="valid">未过期</option>
              <option value="unknown">信息不足</option>
            </select></label
          >
          <label class="field"
            >查活状态<select v-model="filters.live_status" class="select">
              <option value="">全部状态</option>
              <option value="failed">查活失败</option>
              <option value="success">查活正常</option>
              <option value="deactivated">已停用</option>
              <option value="checking">查活中</option>
              <option value="cancelled">已取消</option>
              <option value="never">未查活</option>
            </select></label
          >
          <label class="field"
            >兑换状态<select v-model="filters.redemption" class="select">
              <option value="">全部状态</option>
              <option value="redeemed">已兑换</option>
              <option value="unredeemed">未兑换</option>
            </select></label
          >
          <label class="field"
            >注册开始日期<input
              v-model="filters.date_from"
              type="date"
              class="input" /></label
          ><label class="field"
            >注册结束日期<input
              v-model="filters.date_to"
              type="date"
              class="input"
          /></label>
          <template v-if="filters.plan === 'promo'"
            ><label class="field"
              >促销套餐<select v-model="filters.promo_type" class="select">
                <option value="">全部促销套餐</option>
                <option value="plus">Plus</option>
                <option value="pro">Pro</option>
                <option value="team">Team</option>
                <option value="go">Go</option>
              </select></label
            ><label class="field"
              >折扣百分比<input
                v-model="filters.promo_discount"
                class="input"
                type="number"
                min="0"
                max="100"
                placeholder="全部折扣" /></label
          ></template>
        </div>
        <div class="filter-footer">
          <div class="inline">
            <button
              class="btn btn-sm"
              type="button"
              :aria-expanded="advancedFilters"
              @click="advancedFilters = !advancedFilters"
            >
              {{ advancedFilters ? "收起高级筛选" : "高级筛选" }}</button
            ><button class="btn btn-sm" type="button" @click="resetFilters">
              重置</button
            ><button
              class="btn btn-sm"
              type="button"
              @click="groupsOpen = true"
            >
              管理分组</button
            ><NuxtLink to="/providers">提链供应商</NuxtLink>
          </div>
          <label class="inline muted"
            ><input v-model="autoRefresh" type="checkbox" />自动刷新<span
              v-if="lastUpdated"
              >· {{ lastUpdated }}</span
            ></label
          >
        </div>
        <p v-if="groupsError" class="alert alert-error" role="alert">
          分组加载失败：{{ groupsError }}
          <button type="button" class="btn btn-sm" @click="loadGroups">
            重试
          </button>
        </p>
      </form>
    </section>

    <section
      class="card accounts-list"
      aria-label="账号列表"
      :aria-busy="loading"
    >
      <div class="card-header account-toolbar">
        <div class="selection-tools">
          <label class="inline"
            ><input
              type="checkbox"
              :checked="allPageSelected"
              :indeterminate="somePageSelected && !allPageSelected"
              :disabled="!rows.length || loading"
              aria-label="选择本页全部账号"
              @change="togglePage"
            />本页全选</label
          ><span class="selection-count">已选 {{ selectedCount }}</span
          ><button
            class="btn btn-sm"
            :disabled="!selectedCount"
            @click="clearSelection"
          >
            清空</button
          ><button
            class="btn btn-sm"
            :disabled="!!selectBusy || !total || loading"
            title="按当前筛选条件选中全部结果，不受 5000 个上限限制"
            @click="selectAllAccounts('filtered')"
          >
            {{
              selectBusy === "filtered"
                ? selectProgress || "选择中…"
                : `选择全部筛选结果（${total.toLocaleString()}）`
            }}</button
          ><button
            class="btn btn-sm"
            :disabled="!!selectBusy || !allAccountsTotal || loading"
            title="忽略当前筛选，选中账号库里的全部账号（含已兑换与已归档）"
            @click="selectAllAccounts('all')"
          >
            {{
              selectBusy === "all"
                ? selectProgress || "选择中…"
                : `全选所有账号（${allAccountsTotal.toLocaleString()}）`
            }}</button
          ><button class="btn btn-sm" @click="lookupOpen = true">
            按邮箱选中
          </button>
          <button
            class="btn btn-sm"
            type="button"
            :aria-pressed="showRedeemed"
            :title="
              showRedeemed
                ? '当前已兑换账号一起显示，点击后恢复默认隐藏'
                : '默认隐藏已被兑换码领取的账号，点击后一起显示'
            "
            @click="toggleRedeemed"
          >
            {{ showRedeemed ? "隐藏已兑换账号" : "显示已兑换账号" }}
          </button>
        </div>
      </div>
      <div class="account-actionbar" role="group" aria-label="批量操作">
        <div
          v-for="group in batchGroups"
          :key="group.name"
          class="action-group"
        >
          <span class="action-group-title">{{ group.name }}</span>
          <div class="action-group-buttons">
            <button
              v-for="value in group.values"
              :key="value"
              type="button"
              class="btn btn-sm"
              :class="{ 'btn-danger': value === 'delete' }"
              :disabled="!selectedCount || (value === 'delete' && deleting)"
              :title="batchTitles[value] || accountActionLabels[value]"
              @click="runBatch(value)"
            >
              {{ batchLabels[value] || accountActionLabels[value] }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="notice" class="account-notice" role="status">
        <div class="inline">
          <span>{{ notice }}</span
          ><button
            class="btn btn-sm"
            @click="
              notice = '';
              noticeDetails = [];
            "
          >
            关闭
          </button>
        </div>
        <details v-if="noticeDetails.length">
          <summary>查看处理详情</summary>
          <ul>
            <li v-for="(detail, index) in noticeDetails" :key="index">
              {{ detail }}
            </li>
          </ul>
        </details>
      </div>
      <div v-if="error" class="alert alert-error list-error" role="alert">
        账号加载失败：{{ error
        }}<button
          class="btn btn-sm"
          :disabled="loading"
          @click="loadAccounts()"
        >
          重试
        </button>
      </div>
      <div v-if="loading && !rows.length" class="loading-state" role="status">
        <span class="loading-dot" />正在加载账号…
      </div>
      <UiEmpty
        v-else-if="!rows.length && !error"
        title="没有找到账号"
        description="调整筛选条件，或导入已有账号开始管理。"
      />
      <div v-else-if="rows.length" class="table-wrap">
        <table class="data-table account-table">
          <thead>
            <tr>
              <th class="check-column"><span class="sr-only">选择</span></th>
              <th>账号</th>
              <th>套餐与订阅</th>
              <th>额度 / 用量</th>
              <th>银行重置</th>
              <th>2FA</th>
              <th>状态</th>
              <th>分组 / 备注</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.id"
              :class="{ 'is-selected': selectedIds.has(row.id) }"
            >
              <td class="check-column">
                <input
                  type="checkbox"
                  :checked="selectedIds.has(row.id)"
                  :aria-label="`选择 ${row.email}`"
                  @change="toggleRow(row)"
                />
              </td>
              <td class="identity-cell" data-label="账号">
                <button
                  class="email-button mono"
                  @click="openOperation('overview', [row])"
                >
                  {{ row.email }}
                </button>
                <div class="muted account-meta">
                  {{ row.user_name || "未设置用户名" }} · #{{ row.id
                  }}<span v-if="row.archived"> · 已归档</span>
                </div>
                <div
                  v-if="row.original_email"
                  class="muted original-email"
                  :title="row.original_email"
                >
                  原邮箱：{{ row.original_email }}
                </div>
                <div class="muted account-meta">
                  {{ accountDate(row.created_at) }}
                </div>
                <div class="inline secret-actions">
                  <button
                    class="btn btn-sm"
                    :disabled="!row.has_password || !!copyBusy"
                    @click="copySecret(row, 'password')"
                  >
                    {{
                      copyBusy === `${row.id}:password` ? "复制中…" : "复制密码"
                    }}</button
                  ><button
                    class="btn btn-sm"
                    :disabled="!row.has_access_token || !!copyBusy"
                    @click="copySecret(row, 'access_token')"
                  >
                    {{
                      copyBusy === `${row.id}:access_token`
                        ? "复制中…"
                        : "复制 AT"
                    }}
                  </button>
                </div>
              </td>
              <td class="plan-cell" data-label="套餐与订阅">
                <PlanInfo :account="row" @promos="openPromos" />
              </td>
              <td class="quota-cell" data-label="额度 / 用量">
                <QuotaUsage :account="row" />
              </td>
              <td class="reset-cell" data-label="银行重置">
                <QuotaInfo :account="row" field="reset" />
              </td>
              <td class="security-cell" data-label="2FA">
                <div class="security-status">
                  <span :class="row.totp_enabled ? 'security-on' : 'muted'">{{
                    row.totp_enabled ? "● 已启用" : "○ 未启用"
                  }}</span
                  ><button
                    v-if="row.totp_enabled"
                    class="btn btn-sm"
                    :disabled="!!copyBusy"
                    @click="copySecret(row, 'totp_code')"
                  >
                    复制验证码
                  </button>
                </div>
                <div
                  v-if="row.totp_setup_status && !row.totp_enabled"
                  class="muted account-meta"
                >
                  {{ accountStatus(row.totp_setup_status) }}
                </div>
              </td>
              <td data-label="状态">
                <div class="status-stack">
                  <div :title="redeemTitle(row)">
                    <span class="status-label">兑换</span
                    ><UiBadge
                      :value="row.redeemed ? 'redeemed' : 'unredeemed'"
                    />
                  </div>
                  <div>
                    <span class="status-label">查活</span
                    ><UiBadge :value="row.live_check_status || 'unknown'" />
                  </div>
                  <div
                    v-if="row.at_expired"
                    title="access_token 已过期或失效，需要重新查活刷新 AT"
                  >
                    <span class="status-label">AT</span
                    ><UiBadge value="expired" />
                  </div>
                  <div>
                    <span class="status-label">Codex</span
                    ><UiBadge :value="row.codex_status || 'unknown'" />
                  </div>
                  <div v-if="row.plus_activation_status">
                    <span class="status-label">Plus</span
                    ><UiBadge :value="row.plus_activation_status" />
                  </div>
                  <div v-else-if="row.scan_request_status">
                    <span class="status-label">支付</span
                    ><UiBadge :value="row.scan_request_status" />
                  </div>
                  <div v-else-if="row.extract_link_status">
                    <span class="status-label">提链</span
                    ><UiBadge :value="row.extract_link_status" />
                  </div>
                </div>
                <p
                  v-if="row.live_check_error || row.scan_request_error"
                  class="row-error"
                  :title="row.live_check_error || row.scan_request_error"
                >
                  {{ row.live_check_error || row.scan_request_error }}
                </p>
              </td>
              <td data-label="分组 / 备注">
                <button
                  class="group-tag"
                  @click="openOperation('group', [row])"
                >
                  {{ row.group_name || "默认分组" }}</button
                ><button
                  class="note-button muted"
                  :title="row.note || '添加备注'"
                  @click="openOperation('note', [row])"
                >
                  {{ row.note || "＋ 添加备注" }}
                </button>
              </td>
              <td data-label="操作">
                <div class="row-actions">
                  <button
                    class="btn btn-sm"
                    @click="openOperation('plan', [row])"
                  >
                    查套餐</button
                  ><button
                    class="btn btn-sm"
                    @click="openOperation('quota', [row])"
                  >
                    查额度</button
                  ><button
                    class="btn btn-sm"
                    @click="openOperation('live', [row])"
                  >
                    查活</button
                  ><button
                    class="btn btn-primary btn-sm"
                    @click="openOperation('overview', [row])"
                  >
                    <UiIcon name="more" />详情 / 操作
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="account-pagination">
        <label class="inline muted"
          >每页<select
            v-model.number="pageSize"
            class="select"
            @change="resizePage"
          >
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option></select
          >条</label
        ><UiPagination
          v-model:page="page"
          :total="total"
          :page-size="pageSize"
        />
      </div>
    </section>
    <p class="muted list-hint">
      列表只加载账号状态。密码、AT、2FA 密钥和 Agent 凭据仅在复制或导出时读取。默认隐藏已被兑换码领取的账号，可点「显示已兑换账号」一起查看。
    </p>

    <ImportModal
      v-if="importOpen"
      @close="importOpen = false"
      @imported="refresh"
    />
    <GroupsModal
      v-if="groupsOpen"
      :groups="groups"
      @close="groupsOpen = false"
      @changed="refresh"
    />
    <PromoDetailsModal
      v-model:open="promoOpen"
      :account="promoAccount"
      @update:open="
        !$event && (promoAccount = null);
        promoOpen = $event;
      "
    />
    <OperationsModal
      v-if="action"
      :key="`${action}:${operationRevision}`"
      :action="action"
      :accounts="operationRows"
      :account-ids="operationSeed?.ids || []"
      :account-count="operationSeed?.count || 0"
      :groups="groups"
      @close="action = ''"
      @changed="afterOperation"
      @submitted="openSubmittedLog"
      @delete="deleteAccounts"
    />
    <OperationsLogModal
      v-model:open="operationLogOpen"
      :endpoint="operationLogOpen ? '/api/tasks/logs' : ''"
      :query="operationLogQuery"
      :title="operationLogTitle"
    />
    <UiModal v-model:open="lookupOpen" title="按邮箱批量选中">
      <form id="accounts-lookup" class="stack" @submit.prevent="lookup">
        <p class="muted">
          每行一个邮箱，也可用空格、逗号或分号分隔。按当前归档、套餐、分组和日期筛选查找，支持原邮箱匹配；不会受搜索关键词限制。列表默认隐藏已兑换账号，需要匹配时先点工具栏的「显示已兑换账号」。
        </p>
        <label class="field"
          >邮箱列表<textarea
            v-model="lookupText"
            class="textarea mono"
            rows="7"
            :disabled="lookupBusy"
            placeholder="user@example.com"
          />
        </label>
        <p v-if="lookupError" class="alert alert-error" role="alert">
          {{ lookupError }}
        </p>
        <p v-if="lookupSummary" role="status">{{ lookupSummary }}</p>
        <details v-if="lookupMissing.length">
          <summary>未匹配邮箱（{{ lookupMissing.length }}）</summary>
          <pre class="missing-emails">{{ lookupMissing.join("\n") }}</pre>
        </details>
      </form>
      <template #footer
        ><button class="btn" :disabled="lookupBusy" @click="lookupOpen = false">
          关闭</button
        ><button
          class="btn btn-primary"
          type="submit"
          form="accounts-lookup"
          :disabled="lookupBusy"
        >
          {{ lookupBusy ? "正在查找…" : "查找并加入选择" }}
        </button></template
      >
    </UiModal>
  </div>
</template>

<style scoped>
.accounts-page {
  gap: 22px;
}
.header-actions {
  flex-shrink: 0;
}
.account-stats {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.stat-card small {
  display: block;
  margin-top: 8px;
  font-size: 11px;
}
.stat-slash {
  font-size: 20px;
  color: var(--text-muted, #94a3b8);
  font-weight: 400;
}
.stat-trio {
  font-size: 24px;
}
.filter-main {
  display: grid;
  grid-template-columns:
    minmax(200px, 1.8fr) repeat(3, minmax(130px, 1fr))
    auto;
  gap: 12px;
  align-items: end;
}
.search-input {
  position: relative;
}
.search-input > :first-child {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  width: 16px;
  color: var(--text-muted, #64748b);
}
.search-input input {
  padding-left: 36px;
  width: 100%;
}
.advanced-filters {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding-top: 4px;
}
.filter-footer,
.account-toolbar,
.account-pagination {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.filter-footer {
  font-size: 12px;
}
.filter-footer a {
  color: var(--accent, #2b66dc);
}
.account-toolbar {
  padding: 16px 18px;
}
.selection-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  font-size: 12px;
}
.selection-count {
  color: var(--accent, #2b66dc);
  font-weight: 600;
}
.account-actionbar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 12px 22px;
  padding: 12px 18px;
  border-top: 1px solid var(--border, #e7ebef);
  border-bottom: 1px solid var(--border, #e7ebef);
  background: #fafbfc;
}
.action-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.action-group-title {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--muted, #84909f);
}
.action-group-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.account-table {
  width: 100%;
}
.account-table th {
  white-space: nowrap;
}
.account-table td {
  vertical-align: top;
  padding-top: 18px;
  padding-bottom: 18px;
}
.account-table tr.is-selected {
  background: var(--accent-soft, #f0f6ff);
}
.check-column {
  width: 36px;
}
.account-table input[type="checkbox"],
.selection-tools input {
  accent-color: var(--accent, #316ade);
  width: 15px;
  height: 15px;
  cursor: pointer;
}
.identity-cell {
  min-width: 220px;
  max-width: 320px;
}
.email-button {
  background: none;
  border: 0;
  padding: 0;
  text-align: left;
  color: var(--text, #192a40);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.email-button:hover {
  color: var(--accent, #2b66dc);
}
.account-meta {
  font-size: 11px;
  margin-top: 5px;
}
.original-email {
  max-width: 270px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  margin-top: 4px;
}
.secret-actions {
  margin-top: 10px;
  gap: 5px;
}
.secret-actions .btn,
.security-status .btn {
  font-size: 10px;
  padding: 3px 7px;
  min-height: 24px;
}
.plan-cell {
  min-width: 240px;
  max-width: 360px;
}
/* 额度/用量列：余额一行 + 5h/周两条进度条；银行重置列只放张数与到期。 */
.quota-cell {
  min-width: 220px;
  max-width: 300px;
}
.reset-cell {
  min-width: 120px;
  max-width: 200px;
}
.security-cell {
  min-width: 104px;
}
.security-status {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 8px;
  font-size: 11px;
}
.security-on {
  color: #178261;
}
.status-stack {
  display: grid;
  gap: 7px;
}
.status-stack > div {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-label {
  color: var(--text-muted, #64748b);
  font-size: 10px;
  width: 36px;
}
.row-error {
  color: #b94b4b;
  max-width: 190px;
  font-size: 10px;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.group-tag {
  display: inline-block;
  border: 1px solid var(--border, #dde5f0);
  background: var(--surface-soft, #f8fafc);
  color: var(--text, #37465c);
  border-radius: 5px;
  padding: 4px 8px;
  font-size: 11px;
  cursor: pointer;
  max-width: 170px;
  overflow-wrap: anywhere;
}
.note-button {
  border: 0;
  padding: 0;
  background: transparent;
  font-size: 11px;
  line-height: 1.6;
  text-align: left;
  margin-top: 9px;
  cursor: pointer;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  max-width: 180px;
  min-width: 90px;
  overflow-wrap: anywhere;
}
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-width: 160px;
}
.row-actions .btn {
  font-size: 11px;
}
.account-pagination {
  padding: 16px 18px;
  border-top: 1px solid var(--border, #e5eaf1);
}
.account-pagination > label {
  font-size: 12px;
}
.account-pagination > label select {
  width: 75px;
}
.account-notice,
.list-error {
  margin: 12px 18px;
}
.account-notice {
  color: #166534;
  font-size: 12px;
}
.loading-state {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: center;
  min-height: 220px;
  color: var(--text-muted, #64748b);
}
.loading-dot {
  width: 14px;
  height: 14px;
  border: 2px solid #e1e8f3;
  border-top-color: #3b73e0;
  border-radius: 50%;
  animation: account-spin 0.8s linear infinite;
}
.list-hint {
  margin: -8px 0 0;
  font-size: 11px;
}
.missing-emails {
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
@keyframes account-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (max-width: 1200px) {
  .filter-main {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
  .search-field {
    grid-column: span 2;
  }
  .filter-main > button {
    grid-column: span 2;
  }
}
@media (max-width: 800px) {
  .account-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .filter-main,
  .advanced-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .account-table thead {
    display: none;
  }
  .account-table,
  .account-table tbody {
    display: block;
  }
  .account-table tr {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px 18px;
    padding: 18px;
    border-top: 1px solid var(--border, #e5eaf1);
    position: relative;
  }
  .account-table td {
    display: block;
    padding: 0;
    border: 0;
    min-width: 0;
    max-width: none;
  }
  .account-table td::before {
    content: attr(data-label);
    display: block;
    color: var(--text-muted, #64748b);
    font-size: 10px;
    margin-bottom: 6px;
  }
  .account-table .check-column {
    position: absolute;
    right: 18px;
    top: 21px;
    width: auto;
  }
  .account-table .identity-cell {
    grid-column: 1 / -1;
    padding-right: 28px;
  }
  .account-table .identity-cell::before {
    display: none;
  }
  .account-table td:last-child {
    grid-column: 1 / -1;
  }
  .account-table .plan-cell {
    grid-column: 1 / -1;
  }
  .row-actions {
    max-width: none;
  }
  .account-pagination {
    justify-content: center;
  }
}
@media (prefers-reduced-motion: reduce) {
  .loading-dot {
    animation: none;
  }
}
</style>
