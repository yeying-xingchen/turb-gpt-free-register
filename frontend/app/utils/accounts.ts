export interface Account {
  id: number;
  email: string;
  has_access_token?: boolean;
  has_password?: boolean;
  totp_enabled?: boolean;
  codex_agent_has_token?: boolean;
  /** 是否已被兑换码领取（数据来自 redeem_claims）。 */
  redeemed?: boolean;
  redeemed_at?: string;
  [key: string]: any;
}
export interface AccountGroup {
  group_name: string;
  total: number;
  redeemable: number;
  redeem_prefix?: string;
  public_stock?: boolean;
}
export const accountActionLabels: Record<string, string> = {
  overview: "账号详情与操作",
  live: "查活 / 刷新 AT",
  plan: "查询套餐",
  totp: "开启 2FA",
  email: "换绑邮箱",
  extract: "提取支付链接",
  activate: "开通 Plus",
  pay: "提交扫码支付",
  "pay-query": "查询支付结果",
  agent: "生成 Codex Agent",
  upload: "上传 Agent 到 sub2api",
  retry: "补跑 Codex 授权",
  stop: "停止 Codex 补跑",
  note: "编辑备注",
  group: "设置分组",
  archive: "归档账号",
  restore: "取消归档",
  export: "复制 / 导出",
  log: "查看任务日志",
  "extract-task": "管理提链任务",
};
export const accountStatusLabels: Record<string, string> = {
  queued: "排队中",
  pending: "等待中",
  running: "进行中",
  retrying: "补跑中",
  success: "成功",
  succeeded: "已成功",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  skipped: "已跳过",
  deactivated: "已停用",
  alive: "正常",
  active: "正常",
  valid: "有效",
  expired: "已过期",
  unknown: "待核实",
  interrupted: "已中断",
  checking: "检查资格",
  extracting: "正在提链",
  paying: "正在支付",
  verifying: "核验套餐",
  needs_attention: "需要核实",
  awaiting_blik: "等待 BLIK",
  submission_pending: "提交待确认",
  submitted: "已提交",
  processing: "处理中",
  cancelled: "已取消",
  canceled: "已取消",
  matched: "已匹配",
  assigned: "已分配",
  scanning: "扫码中",
  waiting: "等待中",
};
export function accountStatus(value: unknown) {
  const status = String(value || "");
  return accountStatusLabels[status] || status || "未执行";
}
export function accountPlan(account: Account) {
  const plan = String(account.current_plan_type || account.plan_type || "");
  return plan
    ? `${accountPlanLabel(plan)}${account.plus_trial_eligible ? " · 可试用 Plus" : ""}`
    : "尚未查询";
}

export interface AccountPlanPromo {
  key: string;
  name: string;
  offer: string;
  duration: string;
  summary: string;
  id: string;
  details: string[];
}

export interface AccountPlanInfo {
  name: string;
  /** 标签文本：沿用后端原始小写套餐名（free/plus/pro…），与旧版 pill 一致。 */
  label: string;
  normalized: string;
  paid: boolean;
  trialEligible: boolean;
  eligibilityPending: boolean;
  promos: AccountPlanPromo[];
  lines: string[];
  /** 订阅接口返回的有效期；与 lines 里的付费到期分开，供详情视图展示。 */
  activeUntilLabel: string;
  queryLabel: string;
  queryKind: "pending" | "running" | "failed" | "";
  error: string;
  lastSuccess: string;
  checkedLabel: string;
  networkLabel: string;
  graceLabel: string;
  graceTitle: string;
  subscriptionError: string;
}

const accountPlanLabels: Record<string, string> = {
  free: "Free",
  plus: "Plus",
  pro: "Pro",
  team: "Team",
  business: "Business",
  go: "Go",
};

export function accountPlanLabel(value: unknown) {
  const raw = String(value || "").trim();
  const normalized = raw.toLowerCase().replace(/[\s_-]/g, "");
  const known: Record<string, string> = {
    chatgptplusplan: "Plus",
    chatgptproplan: "Pro",
    chatgptteamplan: "Team",
    chatgptbusinessplan: "Business",
    chatgptgoplan: "Go",
    ...accountPlanLabels,
  };
  return (
    known[normalized] ||
    raw.replace(/^chatgpt/i, "").replace(/plan$/i, "") ||
    "尚未查询"
  );
}

function promoDuration(value: unknown) {
  if (!value || typeof value !== "object") return "";
  const duration = value as Record<string, any>;
  if (duration.num_periods == null) return "";
  const period = String(duration.period || "").toLowerCase();
  const unit: Record<string, string> = {
    month: "个月",
    year: "年",
    week: "周",
    day: "天",
  };
  return `${duration.num_periods}${unit[period] || period}`;
}

function promoOffer(metadata: Record<string, any>, campaign: any) {
  const percentage = metadata.discount?.percentage;
  if (percentage !== undefined && percentage !== null && percentage !== "") {
    const numeric = Number(percentage);
    if (!Number.isNaN(numeric)) return numeric === 100 ? "免费" : `${percentage}%折扣`;
    return `${percentage}%折扣`;
  }
  return String(
    metadata.promotion_type_label ||
      metadata.title ||
      campaign?.id ||
      "有优惠",
  );
}

export function accountPlanPromos(account: Account): AccountPlanPromo[] {
  const campaigns = account.eligible_promo_campaigns;
  if (!campaigns || typeof campaigns !== "object" || Array.isArray(campaigns))
    return [];
  return Object.entries(campaigns).map(([key, campaign]) => {
    const item = (
      campaign && typeof campaign === "object" ? campaign : {}
    ) as Record<string, any>;
    const metadata =
      item.metadata && typeof item.metadata === "object" ? item.metadata : {};
    const rawName = String(metadata.plan_name || key || "套餐").trim();
    const name = accountPlanLabel(rawName);
    const offer = promoOffer(metadata, item);
    const duration = promoDuration(metadata.duration);
    const id = String(item.id || "");
    const summary = `${name}：${offer}${duration ? `/${duration}` : ""}`;
    const details = [
      id ? `活动 ID：${id}` : "",
      metadata.title && metadata.title !== rawName
        ? `活动名称：${metadata.title}`
        : "",
      metadata.promotion_type_label
        ? `优惠类型：${metadata.promotion_type_label}`
        : "",
    ].filter(Boolean);
    return { key, name, offer, duration, summary, id, details };
  });
}

function billingLabel(value: unknown) {
  const raw = String(value || "");
  const normalized = raw.toLowerCase();
  if (normalized === "monthly") return "月付";
  if (["yearly", "annual", "annually"].includes(normalized)) return "年付";
  return raw;
}

function discountLabel(account: Account) {
  const amount = account.discount_amount;
  if (amount === undefined || amount === null || amount === "") return "";
  const type = String(account.discount_type || "").toLowerCase();
  const numeric = Number(amount);
  if (type === "percentage" && !Number.isNaN(numeric)) return `${amount}%折扣`;
  return `${amount}折扣`;
}

function planNetworkLabel(account: Account) {
  const route = String(account.plan_check_network_route || "");
  if (route === "proxy")
    return `代理${account.plan_check_proxy_used ? `（${account.plan_check_proxy_used}）` : ""}`;
  if (route === "direct_fallback")
    return `直连回退${account.plan_check_proxy_fallback_reason ? `（${account.plan_check_proxy_fallback_reason}）` : ""}`;
  if (route === "direct") return "直连";
  return "";
}

export function accountPlanInfo(account: Account): AccountPlanInfo {
  const rawPlan = String(account.current_plan_type || account.plan_type || "").trim();
  const normalized = rawPlan.toLowerCase();
  const paid = ["plus", "pro", "team", "business", "go"].some((value) =>
    normalized.includes(value),
  );
  const promos = accountPlanPromos(account);
  const status = String(account.plan_check_status || "").toLowerCase();
  const checking = status === "queued" || status === "running";
  const failed = Boolean(account.plan_check_error) || status === "failed";
  const queryKind = checking
    ? status === "queued"
      ? "pending"
      : "running"
    : failed
      ? "failed"
      : "";
  const automatic = account.plan_check_trigger === "registration_auto";
  const queryLabel = checking
    ? `${automatic ? "自动" : "套餐"}${status === "queued" ? "查询排队中" : "查询中"}`
    : failed
      ? "查询失败"
      : "";
  const expiresAt =
    account.subscription_active_until ||
    account.plan_expires_at ||
    account.expires_at ||
    account.plan_renews_at ||
    account.renews_at;
  const renewsAt = account.plan_renews_at || account.renews_at;
  // 与旧版一致：账期、币种、折扣只对已开通套餐的账号展示；
  // 免费账号的到期字段可能来自挽留期，混在一起会造成误读。
  const lines = paid
    ? [
        billingLabel(
          account.billing_period || account.subscription_billing_period,
        ),
        account.billing_currency || account.subscription_billing_currency
          ? String(
              account.billing_currency || account.subscription_billing_currency,
            )
          : "",
        expiresAt ? `到期 ${accountDate(expiresAt)}` : "",
        renewsAt && renewsAt !== expiresAt ? `续费 ${accountDate(renewsAt)}` : "",
        discountLabel(account),
      ].filter(Boolean)
    : [];
  const graceStart = account.subscription_became_delinquent_at;
  const graceEnd = account.subscription_grace_period_end_at;
  const graceRange = [
    graceStart ? `开始 ${accountDate(graceStart)}` : "",
    graceEnd ? `结束 ${accountDate(graceEnd)}` : "",
  ]
    .filter(Boolean)
    .join(" / ");
  const graceLabel = graceRange ? `挽留期：${graceRange}` : "";
  const graceTitle = [
    graceStart ? `挽留期开始：${graceStart}` : "",
    graceEnd ? `挽留期结束：${graceEnd}` : "",
  ]
    .filter(Boolean)
    .join("；");
  const networkLabel = planNetworkLabel(account);
  const checkedLabel = account.plan_checked_at
    ? `查询于 ${accountDate(account.plan_checked_at)}`
    : "";
  const checkedOk =
    account.plan_check_ok === true || status === "success";
  // 旧版对未确认资格的 free 账号显示“待查资格”，避免把未知当成不可试用。
  // 已经确认可试用（plus_trial_eligible）时不再提示，两种状态互斥。
  const eligibilityPending =
    !paid &&
    normalized === "free" &&
    !checkedOk &&
    !checking &&
    !failed &&
    !account.plus_trial_eligible;
  return {
    name: rawPlan ? accountPlanLabel(rawPlan) : "尚未查询",
    label: rawPlan ? rawPlan.toLowerCase() : "尚未查询",
    normalized,
    paid,
    trialEligible: Boolean(account.plus_trial_eligible),
    eligibilityPending,
    promos,
    lines,
    activeUntilLabel: account.subscription_active_until
      ? accountDate(account.subscription_active_until)
      : "",
    queryLabel,
    queryKind,
    // 失败的查询不一定带回原因；旧版会明确提示未返回原因，这里保持一致。
    error: String(
      account.plan_check_error ||
        (failed ? "套餐查询失败，未返回具体原因" : ""),
    ),
    lastSuccess: account.plan_last_success_at
      ? accountDate(account.plan_last_success_at)
      : "",
    checkedLabel,
    networkLabel,
    graceLabel,
    graceTitle,
    subscriptionError: String(account.subscription_error || ""),
  };
}

export function accountDate(value: unknown) {
  if (!value) return "—";
  const input =
    typeof value === "number"
      ? value < 1e11
        ? value * 1000
        : value
      : String(value);
  const date = new Date(input);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}
export function accountError(error: unknown) {
  const item = error as any;
  return String(item?.data?.error || item?.message || "请求失败，请重试");
}
export function accountSafeUrl(value: unknown, image = false) {
  const text = String(value || "");
  if (image && /^data:image\/(png|jpeg|webp);base64,[a-z\d+/=\s]+$/i.test(text))
    return text;
  try {
    const url = new URL(text);
    if (
      ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
    )
      return url.href;
  } catch {
    /* Ignore malformed provider links. */
  }
  return "";
}
export async function copyAccountText(value: string) {
  if (!value) throw new Error("没有可复制的内容");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
  } catch {
    /* Older browsers may need the selection fallback. */
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.style.cssText = "position:fixed;left:-9999px;top:0";
  const previous = document.activeElement as HTMLElement | null;
  document.body.appendChild(field);
  field.select();
  try {
    if (!document.execCommand("copy"))
      throw new Error("剪贴板权限被拒绝，请使用下载功能");
  } finally {
    field.remove();
    previous?.focus();
  }
}
export function saveAccountFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function downloadAccountFile(
  path: string,
  body?: Record<string, any>,
  filename = "accounts.zip",
) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    ...(body
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  if (
    !response.ok ||
    response.headers.get("content-type")?.includes("text/html")
  ) {
    const result = await response.json().catch(() => ({}));
    throw new Error(
      result.error || `下载失败（HTTP ${response.status}），请检查登录状态`,
    );
  }
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  saveAccountFile(await response.blob(), match?.[1] || filename);
}
export function accountResult(result: any, fallback = "操作已完成") {
  const labels: Record<string, string> = {
    started: "已入队",
    updated: "已更新",
    deleted: "已删除",
    uploaded: "已上传",
    stopped: "已停止",
    busy: "进行中",
    skipped: "已跳过",
    failed: "失败",
    inserted: "已导入",
    created: "已创建",
    duplicate: "已受理",
    pending: "等待确认",
    unknown: "待核实",
    count: "已查询",
  };
  const summary: string[] = [];
  for (const [key, label] of Object.entries(labels)) {
    const value =
      result?.[`${key}_count`] ??
      (Array.isArray(result?.[key])
        ? result[key].length
        : typeof result?.[key] === "number"
          ? result[key]
          : undefined);
    if (value !== undefined) summary.push(`${label} ${value} 个`);
  }
  return summary.join("，") || result?.message || fallback;
}
export function accountResultDetails(result: any): string[] {
  const lines: string[] = [];
  for (const key of [
    "failed",
    "skipped",
    "busy",
    "errors",
    "details",
    "user_name_warnings",
    "items",
    "results",
    "created",
    "duplicated",
    "pending",
    "unknown",
  ]) {
    if (!Array.isArray(result?.[key])) continue;
    for (const item of result[key]) {
      const status = item.status || item.task?.status;
      const message =
        item.error || item.reason || item.message || item.task?.message || "";
      lines.push(
        [
          item.email ||
            (item.id || item.account_id
              ? `#${item.id || item.account_id}`
              : item.line
                ? `第 ${item.line} 行`
                : ""),
          status ? accountStatus(status) : "",
          message,
        ]
          .filter(Boolean)
          .join(" · "),
      );
    }
  }
  return [...new Set(lines.filter(Boolean))];
}
