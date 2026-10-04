<script setup lang="ts">
import ExtractionConfig from "./ExtractionConfig.vue";
import PaymentConfig from "./PaymentConfig.vue";
import PlanInfo from "./PlanInfo.vue";
import PromoDetailsModal from "./PromoDetailsModal.vue";
import {
  accountActionLabels,
  accountDate,
  accountError,
  accountResult,
  accountResultDetails,
  accountSafeUrl,
  accountStatus,
  copyAccountText,
  downloadAccountFile,
  saveAccountFile,
  type Account,
  type AccountGroup,
} from "~/utils/accounts";
const props = defineProps<{
  action: string;
  accounts: Account[];
  groups: AccountGroup[];
}>();
const emit = defineEmits<{
  close: [];
  changed: [];
  submitted: [
    payload: { accountIds: number[]; jobType: string; title: string },
  ];
  delete: [accounts: Account[]];
}>();
const { request } = useApi();
const toast = useToast();
const mode = ref(props.action);
const busy = ref(false);
const error = ref("");
const feedback = ref("");
const details = ref<string[]>([]);
const submitted = ref(false);
const providers = ref<any[]>([]);
const loadingProviders = ref(false);
const extractionIds = ref([0]);
const paymentIds = ref([0]);
let sequence = 0;
const extractionRefs = new Map<number, any>();
const paymentRefs = new Map<number, any>();
const note = ref(
  props.accounts.length === 1 ? props.accounts[0]?.note || "" : "",
);
const group = ref(
  props.accounts.length === 1
    ? props.accounts[0]?.group_name || "默认分组"
    : "",
);
const source = ref("outlook");
const workers = ref(3);
const verifyTask = ref(true);
const successGroup = ref("");
const exportField = ref("login_credentials");
const exportMethod = ref("copy");
const proxy = ref("");
const taskAction = ref("refresh");
const taskCdk = ref("");
const blikCode = ref("");
const logType = ref("live");
const log = ref("");
const logRunning = ref(false);
const logLoading = ref(false);
const image = ref("");
const qrShown = ref(false);
const promoAccount = ref<Account | null>(null);
const promoOpen = ref(false);
let live = true;
let scanKey = "";
let logRevision = 0;
const open = computed({
  get: () => true,
  set: (value: boolean) => {
    if (!value && !busy.value) emit("close");
  },
});
const ids = computed(() => props.accounts.map((account) => account.id));
const first = computed(() => props.accounts[0]);
const isCheckout = computed(() =>
  ["extract", "activate", "pay", "pay-query"].includes(mode.value),
);
const needsExtraction = computed(() =>
  ["extract", "activate"].includes(mode.value),
);
const needsPayment = computed(() =>
  ["activate", "pay", "pay-query"].includes(mode.value),
);
const actionLimit = computed(() =>
  ["note", "group", "archive", "restore", "export"].includes(mode.value)
    ? 5000
    : 500,
);
const descriptions: Record<string, string> = {
  live: "按系统配置的查活驱动重新登录，按需完成密码、邮箱验证码或 2FA 验证；取得新 AT 后标记正常。任务在后台排队执行。",
  plan: "查询当前套餐、试用资格和订阅到期时间。未填写代理时使用服务器查套餐网络策略。",
  totp: "为账号开启 2FA，成功后将密钥保存到账号记录。已启用、缺少 AT 或正在处理的账号会跳过。",
  email:
    "从所选邮箱来源获取新邮箱并换绑。请先在系统配置中设置该来源；后台任务结束后更新账号邮箱。",
  agent: "使用账号 AT 生成 Codex Agent 凭据并保存到本地账号记录。",
  upload: "将已生成的 Agent 凭据上传到系统配置的 sub2api。",
  retry:
    "补跑 Codex 授权，会按账号消耗邮箱 OTP 和接码短信。正在补跑和已停用账号会跳过。",
  stop: "向正在补跑的 Codex 任务发送停止信号。",
  archive: "将这些账号移入归档列表，可以随时取消归档。",
  restore: "将这些账号从归档列表恢复到正常账号列表。",
  note: "本次输入会覆盖所有目标账号的原备注；留空会清除备注。",
  group: "输入已有分组或新的分组名称。默认分组名为“默认分组”。",
  extract: "仅支持已确认可试用 Plus 的免费账号。成功提链可能消耗 CDK 次数。",
  activate:
    "后台依次检查资格、提取零元 UPI 链接、提交支付并核验 Plus。候选按顺序尝试；结果不确定时等待核实原任务。",
  pay: "提交已经成功提取的 UPI 支付链接。支付平台可能消耗额度；提交后可查询原任务。",
  "pay-query": "使用原支付凭据查询已有任务。关闭窗口不会取消服务器任务。",
  export: "仅在点击执行后读取所选字段。复制 / 导出的凭据请妥善保存。",
};
const taskTypeByMode: Record<string, { type: string; title: string }> = {
  live: { type: "live_check", title: "查活任务日志" },
  plan: { type: "plan_check", title: "套餐查询日志" },
  totp: { type: "totp_setup", title: "2FA 设置日志" },
  email: { type: "email_change", title: "邮箱换绑日志" },
  agent: { type: "codex_agent", title: "Codex Agent 日志" },
  retry: { type: "codex_retry", title: "Codex 补跑日志" },
  stop: { type: "codex_retry", title: "Codex 补跑日志" },
  extract: { type: "extract_link", title: "提链任务日志" },
  "extract-task": { type: "extract_link", title: "提链任务日志" },
  activate: { type: "plus_activation", title: "Plus 激活日志" },
  pay: { type: "scan_payment", title: "扫码支付日志" },
  "pay-query": { type: "scan_payment", title: "扫码支付日志" },
};
const actionSections = [
  { label: "账号与安全", actions: ["live", "plan", "totp", "email", "export"] },
  {
    label: "订阅与支付",
    actions: ["activate", "extract", "extract-task", "pay", "pay-query"],
  },
  { label: "Codex", actions: ["agent", "upload", "retry", "stop", "log"] },
  { label: "整理", actions: ["note", "group", "archive", "restore"] },
];
const statusRows = computed(() =>
  first.value
    ? [
        ["查活", first.value.live_check_status, first.value.live_check_error],
        [
          "套餐查询",
          first.value.plan_check_status,
          first.value.plan_check_error,
        ],
        [
          "2FA 设置",
          first.value.totp_setup_status,
          first.value.totp_setup_error || first.value.totp_setup_message,
        ],
        [
          "邮箱换绑",
          first.value.email_change_status,
          first.value.email_change_error || first.value.email_change_new_email,
        ],
        ["Codex", first.value.codex_status, first.value.codex_error],
        [
          "Codex Agent",
          first.value.codex_agent_status,
          first.value.codex_agent_message,
        ],
        [
          "提链",
          first.value.extract_link_status,
          first.value.extract_link_error || first.value.extract_link_message,
        ],
        [
          "扫码支付",
          first.value.scan_request_status,
          first.value.scan_request_error || first.value.scan_request_message,
        ],
        [
          "Plus 激活",
          first.value.plus_activation_status,
          first.value.plus_activation_message,
        ],
      ]
    : [],
);
function changeMode(value: string) {
  clearCredentials();
  mode.value = value;
  error.value = feedback.value = "";
  details.value = [];
  submitted.value = false;
  qrShown.value = false;
  image.value = "";
}
function clearCredentials() {
  for (const field of extractionRefs.values()) field?.clear();
  for (const field of paymentRefs.values()) field?.clear();
  taskCdk.value = blikCode.value = "";
}
async function loadProviders() {
  loadingProviders.value = true;
  error.value = "";
  try {
    const result = await request("/api/extract-link/providers");
    if (!live) return;
    providers.value = (result.items || []).filter(
      (item: any) => item.enabled !== false && item.enabled !== 0,
    );
    if (!providers.value.length)
      error.value = "尚无可用提链服务商，请先到供应商页面配置。";
  } catch (cause) {
    if (live) error.value = accountError(cause);
  } finally {
    loadingProviders.value = false;
  }
}
watch(
  mode,
  (value) => {
    logRevision++;
    log.value = "";
    if (["extract", "activate"].includes(value) && !providers.value.length)
      void loadProviders();
    if (value === "log") void loadLog();
  },
  { immediate: true },
);
watch(logType, () => {
  logRevision++;
  log.value = "";
  void loadLog();
});
async function loadLog() {
  if (mode.value !== "log" || !first.value || logLoading.value) return;
  const revision = logRevision;
  logLoading.value = true;
  try {
    const paths: Record<string, string> = {
      live: "/api/accounts/live-check-log",
      totp: "/api/accounts/totp-setup-log",
      codex: "/api/codex/retry-log",
      email: `/api/accounts/${first.value.id}/change-email-log`,
    };
    const result = await request(paths[logType.value]!, {
      query: logType.value === "email" ? {} : { email: first.value.email },
    });
    if (!live || revision !== logRevision) return;
    log.value = result.log || "暂无日志";
    logRunning.value = !!result.running;
    error.value = "";
  } catch (cause) {
    if (live && revision === logRevision) error.value = accountError(cause);
  } finally {
    logLoading.value = false;
  }
}
usePolling(async () => {
  if (mode.value === "log") await loadLog();
}, 4000);
onBeforeUnmount(() => {
  live = false;
  logRevision++;
  clearCredentials();
});
function fieldPayloads(refs: Map<number, any>, values: number[]) {
  return values.map((id) => {
    const field = refs.get(id);
    if (!field) throw new Error("表单仍在加载，请稍候");
    return field.payload();
  });
}
async function performExport() {
  if (exportField.value === "agent_zip") {
    if (ids.value.length > 1000) throw new Error("单次最多下载 1000 个 Agent");
    await downloadAccountFile(
      "/api/accounts/codex-agent/download-bulk",
      { account_ids: ids.value },
      "codex-agents.zip",
    );
    feedback.value = "已开始下载 Agent ZIP，部分失败详情见包内 manifest.json。";
    return;
  }
  if (exportField.value === "cpa_zip") {
    if (ids.value.length > 1000)
      throw new Error("单次最多下载 1000 个 CPA 凭据");
    const result = await request("/api/accounts/download-cpa-bulk", {
      method: "POST",
      body: { account_ids: ids.value, prepare: true },
    });
    const url = new URL(result.download_url || "", window.location.origin);
    if (
      url.origin !== window.location.origin ||
      !url.pathname.startsWith("/api/downloads/")
    )
      throw new Error("服务端返回的下载地址无效");
    const link = document.createElement("a");
    link.href = url.href;
    link.download = result.filename || "accounts-cpa.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    feedback.value = `已开始下载 CPA ZIP${result.error_count ? `，${result.error_count} 个失败详情见包内 manifest.json` : ""}`;
    return;
  }
  let values: string[];
  if (exportField.value === "email") {
    values = props.accounts.map((account) => account.email);
  } else {
    const result = await request("/api/accounts/secret-bulk", {
      method: "POST",
      body: { account_ids: ids.value, field: exportField.value },
    });
    values = (result.values || [])
      .map((item: any) => item.value)
      .filter(Boolean);
    details.value = accountResultDetails(result);
  }
  if (!values.length) throw new Error("所选账号没有可导出的内容");
  const text = values.join("\n");
  if (exportMethod.value === "copy") await copyAccountText(text);
  else
    saveAccountFile(
      new Blob(["\ufeff" + text + "\n"], { type: "text/plain;charset=utf-8" }),
      `accounts-${exportField.value}-${Date.now()}.txt`,
    );
  feedback.value = `已${exportMethod.value === "copy" ? "复制" : "下载"} ${values.length} 条记录${details.value.length ? "，部分账号跳过，见下方详情" : ""}`;
}
async function submit() {
  if (
    busy.value ||
    (submitted.value &&
      mode.value !== "export" &&
      mode.value !== "pay-query" &&
      mode.value !== "extract-task")
  )
    return;
  busy.value = true;
  error.value = feedback.value = "";
  details.value = [];
  let dispatched = false;
  try {
    if (!ids.value.length) throw new Error("请先选择账号");
    if (ids.value.length > actionLimit.value)
      throw new Error(
        `本操作单次最多处理 ${actionLimit.value} 个账号，请缩小选择范围`,
      );
    if (mode.value === "export") {
      await performExport();
      toast.success(feedback.value);
      return;
    }
    const body: Record<string, any> = { account_ids: ids.value };
    const routes: Record<string, string> = {
      live: "check-live-bulk",
      plan: "check-plan-bulk",
      totp: "totp-setup-bulk",
      email: "change-email-bulk",
      agent: "codex-agent-bulk",
      upload: "codex-agent/upload-sub2-bulk",
      note: "note-bulk",
      group: "group-bulk",
      archive: "archive-bulk",
      restore: "archive-bulk",
      extract: "extract-link-bulk",
      activate: "activate-plus",
      pay: "scan-requests",
      "pay-query": "scan-requests/query",
    };
    let path = `/api/accounts/${routes[mode.value]}`;
    if (mode.value === "note") body.note = note.value;
    if (mode.value === "group") {
      if (!group.value.trim()) throw new Error("请填写目标分组");
      body.group_name = group.value.trim();
    }
    if (mode.value === "archive" || mode.value === "restore")
      body.archived = mode.value === "archive";
    if (mode.value === "email") body.source = source.value;
    if (mode.value === "agent") body.verify_task = verifyTask.value;
    if (mode.value === "plan") {
      body.timezone_offset_min = String(new Date().getTimezoneOffset());
      if (proxy.value.trim()) body.proxy = proxy.value.trim();
    }
    if (mode.value === "retry" || mode.value === "stop") {
      path = `/api/codex/${mode.value === "retry" ? "retry" : "stop"}-bulk`;
      if (mode.value === "retry")
        body.workers = Math.max(1, Math.min(16, Number(workers.value) || 3));
    }
    if (mode.value === "extract") {
      const blocked = props.accounts.filter((account) =>
        [
          "queued",
          "running",
          "awaiting_blik",
          "unknown",
          "interrupted",
        ].includes(account.extract_link_status),
      );
      if (blocked.length)
        throw new Error(
          `所选账号中有 ${blocked.length} 个提链任务仍在执行或待核实，请先通过“管理提链任务”查询原任务`,
        );
      Object.assign(
        body,
        fieldPayloads(extractionRefs, [extractionIds.value[0]!])[0],
      );
    }
    if (mode.value === "activate") {
      body.extraction = fieldPayloads(extractionRefs, extractionIds.value);
      body.payment = fieldPayloads(paymentRefs, paymentIds.value);
      body.success_group = successGroup.value;
    }
    if (mode.value === "pay" || mode.value === "pay-query") {
      Object.assign(
        body,
        fieldPayloads(paymentRefs, [paymentIds.value[0]!])[0],
      );
      if (mode.value === "pay") {
        if (!scanKey)
          scanKey = `nuxt-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
        body.idempotency_key = scanKey;
      }
    }
    if (mode.value === "extract-task") {
      if (ids.value.length !== 1)
        throw new Error("提链任务管理请一次选择一个账号");
      path = `/api/accounts/${ids.value[0]}/extract-link/${taskAction.value}`;
      delete body.account_ids;
      if (taskCdk.value.trim()) body.cdk = taskCdk.value.trim();
      if (taskAction.value === "blik-code") {
        if (!/^\d{6}$/.test(blikCode.value.trim()))
          throw new Error("请输入六位 BLIK 验证码");
        body.blik_code = blikCode.value.trim();
      }
    }
    dispatched = true;
    const result = await request(path, { method: "POST", body });
    if (result.ok === false)
      throw new Error(result.error || result.message || "操作未被接受");
    if (!live) return;
    submitted.value = !["pay-query", "extract-task"].includes(mode.value);
    feedback.value = accountResult(
      result,
      mode.value === "pay-query"
        ? "支付任务查询完成"
        : "操作已提交，后台任务状态将自动更新",
    );
    details.value = accountResultDetails(result);
    if (mode.value === "extract-task")
      image.value = accountSafeUrl(
        result.image_url_png || result.qr_url || result.result?.image_url_png,
        true,
      );
    if (isCheckout.value) clearCredentials();
    toast.success(feedback.value);
    emit("changed");
    const task = taskTypeByMode[mode.value];
    if (task) {
      const startedIds = Array.isArray(result.started)
        ? result.started
            .map((item: any) => Number(item?.id ?? item?.account_id))
            .filter((id: number) => Number.isInteger(id) && id > 0)
        : [];
      const accountIds = startedIds.length
        ? startedIds
        : Number(result.account_id) > 0
          ? [Number(result.account_id)]
          : ids.value;
      if (accountIds.length)
        emit("submitted", {
          accountIds,
          jobType: task.type,
          title: task.title,
        });
    }
  } catch (cause) {
    if (live) {
      error.value = accountError(cause);
      if (
        ["pay", "activate"].includes(mode.value) &&
        dispatched &&
        (!(cause as any)?.status || (cause as any).status >= 500)
      ) {
        submitted.value = true;
        error.value +=
          "；如已发出请求，请先刷新列表并查询原任务，核对受理情况。";
      }
      toast.error(error.value);
    }
  } finally {
    busy.value = false;
  }
}
async function copy(value: string) {
  try {
    await copyAccountText(value);
    toast.success("已复制");
  } catch (cause) {
    toast.error(accountError(cause));
  }
}
</script>

<template>
  <UiModal v-model:open="open" :title="accountActionLabels[mode] || '账号操作'">
    <div class="account-operations stack">
      <div class="scope">
        <strong>{{
          accounts.length === 1
            ? first?.email
            : `已选择 ${accounts.length} 个账号（包含跨页选择）`
        }}</strong>
        <details v-if="accounts.length > 1">
          <summary>查看本次操作账号</summary>
          <div class="scope-list">
            <span v-for="account in accounts" :key="account.id"
              >#{{ account.id }} · {{ account.email }}</span
            >
          </div>
        </details>
      </div>
      <template v-if="mode === 'overview' && first">
        <section class="plan-section stack">
          <div class="inline plan-section-head">
            <strong>套餐与订阅</strong
            ><span class="muted">最近一次查套餐的真实结果</span>
          </div>
          <PlanInfo :account="first" detailed @promos="promoAccount = $event" />
        </section>
        <dl class="account-facts">
          <div>
            <dt>用户名</dt>
            <dd>{{ first.user_name || "—" }}</dd>
          </div>
          <div>
            <dt>分组</dt>
            <dd>{{ first.group_name || "默认分组" }}</dd>
          </div>
          <div>
            <dt>2FA</dt>
            <dd>{{ first.totp_enabled ? "已启用" : "未启用" }}</dd>
          </div>
          <div>
            <dt>兑换</dt>
            <dd>
              {{
                first.redeemed
                  ? `已兑换${first.redeemed_at ? " · " + accountDate(first.redeemed_at) : ""}`
                  : "未兑换"
              }}
            </dd>
          </div>
          <div>
            <dt>注册时间</dt>
            <dd>{{ accountDate(first.created_at) }}</dd>
          </div>
          <div v-if="first.original_email">
            <dt>原邮箱</dt>
            <dd>{{ first.original_email }}</dd>
          </div>
          <div>
            <dt>备注</dt>
            <dd>{{ first.note || "暂无备注" }}</dd>
          </div>
        </dl>
        <div class="status-list">
          <div v-for="row in statusRows" :key="row[0]" class="status-row">
            <span>{{ row[0] }}</span
            ><UiBadge :value="row[1] || 'unknown'" /><span class="muted">{{
              row[2] || accountStatus(row[1])
            }}</span>
          </div>
        </div>
        <section
          v-if="
            first.extract_link_long_url ||
            first.extract_link_hosted_instructions_url ||
            first.extract_link_copy_paste
          "
          class="stack"
        >
          <strong>提链结果</strong
          ><template
            v-for="key in [
              'extract_link_hosted_instructions_url',
              'extract_link_long_url',
              'extract_link_copy_paste',
            ]"
            :key="key"
            ><div v-if="first[key]" class="link-result">
              <a
                v-if="accountSafeUrl(first[key])"
                :href="accountSafeUrl(first[key])"
                target="_blank"
                rel="noopener noreferrer"
                >打开{{
                  key === "extract_link_hosted_instructions_url"
                    ? "支付说明页"
                    : "支付链接"
                }}</a
              ><span v-else class="mono">{{
                String(first[key]).slice(0, 100)
              }}</span
              ><button class="btn btn-sm" @click="copy(String(first[key]))">
                复制
              </button>
            </div></template
          ><small class="muted"
            >到期：{{ accountDate(first.extract_link_expires_at) }}</small
          >
        </section>
        <div v-if="accountSafeUrl(first.extract_link_image_url_png, true)">
          <button class="btn btn-sm" @click="qrShown = !qrShown">
            {{ qrShown ? "收起二维码" : "显示支付二维码" }}</button
          ><img
            v-if="qrShown"
            :src="accountSafeUrl(first.extract_link_image_url_png, true)"
            class="payment-qr"
            alt="支付二维码"
            referrerpolicy="no-referrer"
          />
        </div>
        <section
          v-for="section in actionSections"
          :key="section.label"
          class="stack operation-section"
        >
          <h3>{{ section.label }}</h3>
          <div class="action-grid">
            <button
              v-for="actionName in section.actions"
              :key="actionName"
              class="btn btn-sm"
              @click="changeMode(actionName)"
            >
              {{ accountActionLabels[actionName] }}
            </button>
          </div>
        </section>
        <button class="btn btn-danger" @click="emit('delete', accounts)">
          删除此账号
        </button>
      </template>
      <template v-else-if="mode === 'log'">
        <div class="inline">
          <label class="field"
            >日志类型<select v-model="logType" class="select">
              <option value="live">查活</option>
              <option value="totp">2FA 设置</option>
              <option value="email">换绑邮箱</option>
              <option value="codex">Codex 补跑</option>
            </select></label
          ><button class="btn btn-sm" :disabled="logLoading" @click="loadLog">
            刷新日志</button
          ><span class="muted">{{
            logLoading
              ? "加载中…"
              : logRunning
                ? "运行中，每 4 秒刷新"
                : "任务未在运行"
          }}</span>
        </div>
        <pre class="task-log">{{ log || "正在读取日志…" }}</pre>
      </template>
      <form
        v-else
        id="account-operation-form"
        class="stack"
        @submit.prevent="submit"
      >
        <p v-if="descriptions[mode]" class="muted">{{ descriptions[mode] }}</p>
        <p v-if="accounts.length > actionLimit" class="alert alert-error">
          本操作最多 {{ actionLimit }} 个账号，当前选择
          {{ accounts.length }} 个。
        </p>
        <fieldset
          class="operation-fields stack"
          :disabled="busy || (submitted && mode !== 'export')"
        >
          <label v-if="mode === 'note'" class="field"
            >备注<textarea
              v-model="note"
              class="textarea"
              rows="4"
              maxlength="2000"
            /><small class="muted">{{ note.length }} / 2000</small></label
          >
          <label v-if="mode === 'group'" class="field"
            >目标分组<input
              v-model="group"
              list="account-group-options"
              class="input"
              maxlength="60"
              required /><datalist id="account-group-options">
              <option
                v-for="item in groups"
                :key="item.group_name"
                :value="item.group_name"
              /></datalist
          ></label>
          <label v-if="mode === 'plan'" class="field"
            >查询代理（可选）<input
              v-model="proxy"
              class="input"
              autocomplete="off"
              placeholder="留空使用服务器网络策略"
          /></label>
          <label v-if="mode === 'email'" class="field"
            >新邮箱来源<select v-model="source" class="select">
              <option value="outlook">Outlook 邮箱池</option>
              <option value="generic_api">通用邮箱 API</option>
              <option value="imap">IMAP 邮箱池</option>
              <option value="cloudflare_domain">Cloudflare 域名池</option>
              <option value="cloudflare">Cloudflare</option>
              <option value="gptmail">GPTMail</option>
              <option value="mailnest">MailNest</option>
              <option value="cloudmail">CloudMail</option>
              <option value="remail">ReMail</option>
            </select></label
          >
          <label v-if="mode === 'retry'" class="field"
            >并发线程数<input
              v-model.number="workers"
              type="number"
              class="input"
              min="1"
              max="16"
              required
          /></label>
          <label v-if="mode === 'agent'" class="inline"
            ><input v-model="verifyTask" type="checkbox" />生成后验证 Agent
            任务</label
          >
          <template v-if="needsExtraction">
            <div class="inline">
              <strong>提链配置</strong
              ><NuxtLink to="/providers" @click="emit('close')"
                >管理供应商 / CDK</NuxtLink
              ><button
                type="button"
                class="btn btn-sm"
                :disabled="loadingProviders"
                @click="loadProviders"
              >
                {{ loadingProviders ? "加载中…" : "重载供应商" }}
              </button>
            </div>
            <section
              v-for="(id, index) in extractionIds"
              :key="id"
              class="candidate"
            >
              <div v-if="mode === 'activate'" class="candidate-header">
                <strong>提链候选 {{ index + 1 }}</strong
                ><button
                  v-if="extractionIds.length > 1"
                  type="button"
                  class="btn btn-sm"
                  @click="
                    extractionRefs.get(id)?.clear();
                    extractionRefs.delete(id);
                    extractionIds.splice(index, 1);
                  "
                >
                  移除
                </button>
              </div>
              <ExtractionConfig
                :ref="
                  (element: any) => {
                    if (element) extractionRefs.set(id, element);
                  }
                "
                :providers="providers"
                :activation="mode === 'activate'"
                :disabled="busy || loadingProviders"
              />
            </section>
            <button
              v-if="mode === 'activate' && extractionIds.length < 20"
              type="button"
              class="btn btn-sm"
              @click="extractionIds.push(++sequence)"
            >
              ＋ 添加提链候选
            </button>
          </template>
          <template v-if="needsPayment">
            <strong>支付配置</strong>
            <section
              v-for="(id, index) in paymentIds"
              :key="id"
              class="candidate"
            >
              <div v-if="mode === 'activate'" class="candidate-header">
                <strong>支付候选 {{ index + 1 }}</strong
                ><button
                  v-if="paymentIds.length > 1"
                  type="button"
                  class="btn btn-sm"
                  @click="
                    paymentRefs.get(id)?.clear();
                    paymentRefs.delete(id);
                    paymentIds.splice(index, 1);
                  "
                >
                  移除
                </button>
              </div>
              <PaymentConfig
                :ref="
                  (element: any) => {
                    if (element) paymentRefs.set(id, element);
                  }
                "
                :disabled="busy"
                :query-only="mode === 'pay-query'"
              />
            </section>
            <button
              v-if="mode === 'activate' && paymentIds.length < 20"
              type="button"
              class="btn btn-sm"
              @click="paymentIds.push(++sequence)"
            >
              ＋ 添加支付候选
            </button>
          </template>
          <label v-if="mode === 'activate'" class="field"
            >Plus 核验成功后转入分组<select
              v-model="successGroup"
              class="select"
            >
              <option value="">保留原分组</option>
              <option
                v-for="item in groups"
                :key="item.group_name"
                :value="item.group_name"
              >
                {{ item.group_name }}
              </option></select
            ><small class="muted"
              >仅在服务端确认真实 Plus 套餐生效后移动；每组候选最多 20
              项。</small
            ></label
          >
          <template v-if="mode === 'export'">
            <label class="field"
              >导出内容<select v-model="exportField" class="select">
                <option value="email">邮箱列表</option>
                <option value="login_credentials">
                  登录凭据（邮箱---密码---2FA）
                </option>
                <option value="password">密码</option>
                <option value="access_token">Access Token</option>
                <option value="totp_secret">2FA 密钥</option>
                <option value="totp_code">当前 2FA 验证码</option>
                <option value="copy_line">原始账号整行</option>
                <option value="full_export">完整账号导出</option>
                <option value="codex_agent_token">Codex Agent Token</option>
                <option value="agent_zip">Agent 凭据 ZIP</option>
                <option value="cpa_zip">从 CPA 下载凭据 ZIP</option>
              </select></label
            >
            <label v-if="!exportField.endsWith('_zip')" class="field"
              >输出方式<select v-model="exportMethod" class="select">
                <option value="copy">复制到剪贴板</option>
                <option value="download">下载 TXT</option>
              </select></label
            >
            <p v-if="exportField === 'totp_code'" class="muted">
              验证码按点击时刻生成，通常每 30 秒更新，请及时使用。
            </p>
          </template>
          <template v-if="mode === 'extract-task'">
            <p class="muted">
              当前状态：{{ accountStatus(first?.extract_link_status) }} ·
              {{
                first?.extract_link_message || first?.extract_link_error || ""
              }}
            </p>
            <label class="field"
              >任务操作<select v-model="taskAction" class="select">
                <option value="refresh">查询 / 刷新原提链任务</option>
                <option
                  v-if="first?.extract_link_provider_type === 'upi_git5'"
                  value="qr"
                >
                  获取支付二维码
                </option>
                <option
                  v-if="first?.extract_link_provider_type === 'lumen'"
                  value="blik-code"
                >
                  提交 BLIK 验证码
                </option>
                <option value="cancel">取消提链任务</option>
              </select></label
            ><label class="field"
              >原提链 CDK（可选）<input
                v-model="taskCdk"
                class="input"
                type="password"
                autocomplete="new-password"
                placeholder="留空使用服务器保存的 CDK" /></label
            ><label v-if="taskAction === 'blik-code'" class="field"
              >六位 BLIK 验证码<input
                v-model="blikCode"
                class="input"
                inputmode="numeric"
                pattern="[0-9]{6}"
                maxlength="6"
                required
            /></label>
          </template>
        </fieldset>
      </form>
      <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
      <div v-if="feedback" class="operation-feedback" role="status">
        <strong>{{ feedback }}</strong>
        <p v-if="isCheckout" class="muted">
          关闭窗口后后台继续运行。支付状态与 Plus 套餐核验分别显示在账号详情中。
        </p>
      </div>
      <ul v-if="details.length" class="result-details">
        <li v-for="(detail, index) in details" :key="index">{{ detail }}</li>
      </ul>
      <img
        v-if="image"
        :src="image"
        class="payment-qr"
        alt="支付二维码"
        referrerpolicy="no-referrer"
      />
      <button
        v-if="mode === 'pay' && submitted"
        class="btn"
        @click="changeMode('pay-query')"
      >
        查询原支付任务
      </button>
    </div>
    <PromoDetailsModal
      v-model:open="promoOpen"
      :account="promoAccount"
      @update:open="
        !$event && (promoAccount = null);
        promoOpen = $event;
      "
    />
    <template #footer
      ><button
        v-if="action === 'overview' && mode !== 'overview'"
        class="btn"
        :disabled="busy"
        @click="changeMode('overview')"
      >
        返回详情</button
      ><button class="btn" :disabled="busy" @click="emit('close')">
        {{ submitted ? "完成" : "关闭" }}</button
      ><button
        v-if="!['overview', 'log'].includes(mode)"
        class="btn btn-primary"
        type="submit"
        form="account-operation-form"
        :disabled="
          busy ||
          loadingProviders ||
          accounts.length > actionLimit ||
          (submitted && mode !== 'export')
        "
      >
        {{
          busy
            ? "处理中…"
            : submitted && mode !== "export"
              ? "已提交"
              : mode === "export"
                ? "执行复制 / 下载"
                : "确认" + (accountActionLabels[mode] || "操作")
        }}
      </button></template
    >
  </UiModal>
</template>

<style scoped>
.account-operations {
  min-width: 0;
}
.scope {
  background: var(--surface-soft, #f5f7fa);
  padding: 12px 14px;
  border-radius: 10px;
  overflow-wrap: anywhere;
}
.scope details {
  margin-top: 8px;
}
.scope-list {
  display: grid;
  gap: 4px;
  max-height: 140px;
  overflow: auto;
  margin-top: 8px;
  font-size: 12px;
}
.account-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin: 0;
}
.plan-section {
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--border, #e2e8f0);
  border-radius: 10px;
}
.plan-section-head {
  justify-content: space-between;
}
.account-facts dt {
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.account-facts dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.status-list {
  display: grid;
  gap: 8px;
}
.status-row {
  display: grid;
  grid-template-columns: 85px auto 1fr;
  align-items: start;
  gap: 10px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.action-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.operation-section h3 {
  font-size: 13px;
  margin: 0;
}
.operation-fields {
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}
.candidate {
  padding: 14px;
  border: 1px solid var(--border, #e2e8f0);
  border-radius: 10px;
}
.candidate-header,
.link-result {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
  overflow-wrap: anywhere;
}
.operation-feedback {
  padding: 12px;
  border-radius: 10px;
  background: #ecfdf5;
  color: #166534;
}
.result-details {
  max-height: 240px;
  overflow: auto;
  padding-left: 20px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.task-log {
  padding: 16px;
  border-radius: 10px;
  background: #101b2d;
  color: #dce7f5;
  max-height: 440px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.payment-qr {
  display: block;
  width: min(100%, 280px);
  margin: 12px auto;
  border-radius: 8px;
}
@media (max-width: 600px) {
  .action-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .status-row {
    grid-template-columns: 76px 1fr;
  }
  .status-row > .muted {
    grid-column: 1 / -1;
  }
}
</style>
