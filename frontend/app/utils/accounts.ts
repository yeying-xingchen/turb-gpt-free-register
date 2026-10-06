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
  /** access_token 是否已过期/失效（后端按 token_expired、token_expires_at 或 JWT exp 判定）。 */
  at_expired?: boolean;
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
  quota: "查询额度、用量与重置券",
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

export interface AccountQuotaInfo {
  /** 额度单元格主文本：如「12.34 USD」「0.00 USD」「不限量」。 */
  balanceLabel: string;
  /** 额度单元格状态：value 有值、empty 明确为 0、busy 查询中、failed 查询失败、unknown 未查询。 */
  balanceKind: "value" | "empty" | "busy" | "failed" | "unknown";
  /** 「银行重置」券张数主文本：如「2 张」。 */
  resetLabel: string;
  resetKind: "value" | "empty" | "busy" | "failed" | "unknown";
  /** 最近一张重置券的到期时间文本，未查询或没有券时为空。 */
  resetExpiryLabel: string;
  checkedLabel: string;
  error: string;
  tooltip: string;
}
/** 额度数值：优先使用服务端原始字符串，避免小数点精度被前端改写。 */
function quotaBalanceText(account: Account): string {
  if (account.quota_unlimited === true || account.quota_unlimited === 1)
    return "不限量";
  const raw = account.quota_balance;
  let text = "";
  if (raw !== undefined && raw !== null && String(raw).trim() !== "")
    text = String(raw).trim();
  else if (typeof account.quota_balance_amount === "number")
    text = account.quota_balance_amount.toFixed(2);
  if (!text) return "";
  const currency = String(account.quota_currency || "").trim();
  return currency ? `${text} ${currency}` : text;
}
/**
 * 余额是否为 0。
 *
 * remaining_balance 返回的是「剩余」而不是「已用」：0 表示没有剩余，
 * 即已经用完或该账号本来就没有这类额度（两者接口本身不区分），
 * 因此不能按「有可用额度」的绿色展示，也不能读成「一次都没用过」。
 */
function quotaBalanceIsZero(account: Account, text: string): boolean {
  if (account.quota_unlimited === true || account.quota_unlimited === 1)
    return false;
  if (typeof account.quota_balance_amount === "number")
    return account.quota_balance_amount === 0;
  if (!text) return false;
  return /^0*(\.0+)?$/.test(text.replace(/[^0-9.]/g, ""));
}
/**
 * 额度与「银行重置」券展示信息。
 *
 * 两个接口分别独立返回：任一失败只影响自己那一列，另一列仍显示上次成功的数值，
 * 因此这里按端点分别判定状态，并把失败原因放进 tooltip。
 */
export function accountQuotaInfo(account: Account): AccountQuotaInfo {
  const status = String(account.quota_check_status || "").toLowerCase();
  const checking = status === "queued" || status === "running";
  const balanceText = quotaBalanceText(account);
  const balanceError = String(account.quota_error || "");
  const creditsError = String(account.reset_credits_error || "");
  const creditsAvailable =
    typeof account.reset_credits_available === "number"
      ? account.reset_credits_available
      : null;
  const creditsApplicable =
    typeof account.reset_credits_applicable === "number"
      ? account.reset_credits_applicable
      : null;
  // 查询过但没有可识别数值（例如上游换了字段名）时显示「无数据」，区别于从未查询。
  const balanceChecked = Boolean(account.quota_checked_at);
  const creditsChecked = Boolean(account.reset_credits_checked_at);
  const balanceZero = quotaBalanceIsZero(account, balanceText);
  // wham/usage 的 credits.has_credits：false 表示账号本来就没有 credits 额度。
  const hasCredits =
    account.quota_has_credits === true || account.quota_has_credits === 1
      ? true
      : account.quota_has_credits === false || account.quota_has_credits === 0
        ? false
        : null;
  const noCreditsEntitlement =
    hasCredits === false && (!balanceText || balanceZero);
  // 状态优先级：查询中 > 有数值（0 也算有明确数值，只是标成中性色）> 无权益 > 失败 > 未查询。
  const balanceKind: AccountQuotaInfo["balanceKind"] = checking
    ? "busy"
    : balanceText && !noCreditsEntitlement
      ? balanceZero
        ? "empty"
        : "value"
      : noCreditsEntitlement
        ? "empty"
        : Boolean(balanceError) || status === "failed"
          ? "failed"
          : "unknown";
  const resetKind: AccountQuotaInfo["resetKind"] = checking
    ? "busy"
    : creditsAvailable !== null
      ? creditsAvailable === 0
        ? "empty"
        : "value"
      : Boolean(creditsError) || status === "failed"
        ? "failed"
        : "unknown";
  const balanceLabel = checking
    ? "查询中…"
    : noCreditsEntitlement
      ? "无额度权益"
      : balanceKind === "value" || balanceKind === "empty"
        ? balanceText
        : balanceKind === "failed"
          ? "查询失败"
          : balanceChecked
            ? "无数据"
            : "未查询";
  const resetLabel = checking
    ? "查询中…"
    : resetKind === "value" || resetKind === "empty"
      ? `${creditsAvailable} 张`
      : resetKind === "failed"
        ? "查询失败"
        : creditsChecked
          ? "无数据"
          : "未查询";
  const checkedLabel = account.quota_checked_at
    ? `查询于 ${accountDate(account.quota_checked_at)}`
    : "";
  const detail = Array.isArray(account.reset_credits_detail)
    ? account.reset_credits_detail
    : [];
  const expiryLines = detail
    .filter((item: any) => item && item.expires_at)
    .map(
      (item: any) =>
        `${item.expires_at}${item.status ? `（${item.status}）` : ""}`,
    );
  const tooltip = [
    checkedLabel || (checking ? "额度查询进行中" : "尚未查询额度"),
    balanceError ? `额度查询失败：${balanceError}` : "",
    creditsError ? `重置券查询失败：${creditsError}` : "",
    account.quota_check_error ? `额度查询任务失败：${account.quota_check_error}` : "",
    // credits.has_credits 来自 wham/usage，能把余额 0 的两种含义分开。
    hasCredits === false
      ? "该账号没有 credits 额度（不是用完了）"
      : balanceZero && hasCredits === true
        ? "余额已用完（credits.has_credits=true 且剩余为 0）"
        : balanceZero
          ? "余额为 0：可能已经用完，也可能该账号没有这类额度（接口只返回剩余值）"
          : "",
    account.quota_balance_fallback
      ? "余额数值来自 wham/usage 的 credits.balance"
      : "",
    creditsAvailable === 0
      ? "没有可用重置券：未发放、已用完或已过期"
      : "",
    creditsAvailable !== null && creditsApplicable !== null &&
    creditsApplicable !== creditsAvailable
      ? `当前可应用 ${creditsApplicable} 张`
      : "",
    ...expiryLines.map((line: string) => `重置券到期：${line}`),
    account.quota_last_success_at
      ? `最近成功：${accountDate(account.quota_last_success_at)}`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
  return {
    balanceLabel,
    balanceKind,
    resetLabel,
    resetKind,
    resetExpiryLabel: account.reset_credits_expires_at
      ? `最近到期 ${accountDate(account.reset_credits_expires_at)}`
      : "",
    checkedLabel,
    error: balanceError || creditsError,
    tooltip,
  };
}

export interface AccountUsageWindow {
  /** 已用百分比主文本：如「22%」「已用尽」；无数据时为空。 */
  percentLabel: string;
  /** 展示语气：ok 正常、warn 接近上限、limit 已用尽、idle 未使用、busy 查询中、failed 查询失败、unknown 无数据。 */
  kind: "ok" | "warn" | "limit" | "idle" | "busy" | "failed" | "unknown";
  /** 窗口长度文本：5 小时 / 周 / 月。 */
  windowLabel: string;
  /** 进度条标签用的短文本：5h / 周 / 月。 */
  shortLabel: string;
  /** 进度条填充比例（0–100，已用）；没有数据时为 null（渲染空槽）。 */
  barPercent: number | null;
  /** 紧凑倒计时文本：如「4 小时后重置」；无法计算时为空。 */
  countdownLabel: string;
  /** 重置时间或倒计时文本，无数据时为空。 */
  resetLabel: string;
  tooltip: string;
}
export interface AccountUsageInfo {
  fiveHour: AccountUsageWindow;
  week: AccountUsageWindow;
  limitReached: boolean;
  checkedLabel: string;
  error: string;
  tooltip: string;
}
/** 窗口长度文本：上游用 limit_window_seconds 报告真实长度，按它显示周/月。 */
function usageWindowLabel(seconds: unknown): string {
  if (typeof seconds !== "number" || seconds <= 0) return "";
  if (seconds <= 6 * 3600) return "5 小时";
  if (seconds <= 8 * 24 * 3600) return "周";
  if (seconds <= 32 * 24 * 3600) return "月";
  return `${Math.round(seconds / 86400)} 天`;
}
/** 进度条上的短标签：5 小时窗口写「5h」，长窗口按真实长度写「周」「月」。 */
function usageShortLabel(windowSeconds: unknown, fallback: string): string {
  if (typeof windowSeconds !== "number" || windowSeconds <= 0) return fallback;
  if (windowSeconds <= 6 * 3600) return "5h";
  if (windowSeconds <= 8 * 24 * 3600) return "周";
  if (windowSeconds <= 32 * 24 * 3600) return "月";
  return `${Math.round(windowSeconds / 86400)}天`;
}
/** 重置时刻：优先 reset_at，其次用 usage_checked_at + reset_after_seconds 换算。 */
function usageResetTargetMs(account: Account, prefix: string): number {
  const resetAt = account[`${prefix}_reset_at`];
  if (resetAt) {
    const parsed = new Date(String(resetAt)).getTime();
    if (!Number.isNaN(parsed)) return parsed;
  }
  const after = account[`${prefix}_reset_after_seconds`];
  const base = account.usage_checked_at
    ? new Date(String(account.usage_checked_at)).getTime()
    : Number.NaN;
  if (typeof after === "number" && after >= 0 && !Number.isNaN(base))
    return base + after * 1000;
  return Number.NaN;
}
/** 紧凑倒计时：进度条行内展示，完整时间在悬停提示里。 */
function usageCountdownLabel(account: Account, prefix: string): string {
  const target = usageResetTargetMs(account, prefix);
  if (Number.isNaN(target)) return "";
  const seconds = Math.max(0, Math.round((target - Date.now()) / 1000));
  if (seconds <= 60) return "即将重置";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟后重置`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时后重置`;
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  return rest ? `${days} 天 ${rest} 小时后重置` : `${days} 天后重置`;
}
function usageResetLabel(account: Account, prefix: string): string {
  const resetAt = account[`${prefix}_reset_at`];
  if (resetAt) return `重置 ${accountDate(resetAt)}`;
  const after = account[`${prefix}_reset_after_seconds`];
  if (typeof after !== "number" || after < 0) return "";
  const base = account.usage_checked_at
    ? new Date(String(account.usage_checked_at)).getTime()
    : Number.NaN;
  if (Number.isNaN(base))
    return after > 0 ? `约 ${Math.max(1, Math.round(after / 60))} 分钟后重置` : "";
  return `重置 ${accountDate(new Date(base + after * 1000).toISOString())}`;
}
function usageWindowInfo(
  account: Account,
  options: {
    prefix: string;
    checking: boolean;
    failed: boolean;
    error: string;
    limitReached: boolean;
    fallbackLabel: string;
  },
): AccountUsageWindow {
  const percent = account[`${options.prefix}_percent`];
  const windowSeconds = account[`${options.prefix}_window_seconds`];
  const hasPercent = typeof percent === "number";
  // 上游用 started=false 表示「窗口还没开始用」：0% + 剩余时间仍是整个窗口。
  const started = account[`${options.prefix}_started`] !== false;
  const windowLabel = usageWindowLabel(windowSeconds);
  const shortLabel = usageShortLabel(windowSeconds, options.fallbackLabel);
  const resetLabel = usageResetLabel(account, options.prefix);
  const countdownLabel = usageCountdownLabel(account, options.prefix);
  let kind: AccountUsageWindow["kind"];
  if (options.checking) kind = "busy";
  else if (hasPercent) {
    if (percent >= 100) kind = "limit";
    else if (percent >= 80) kind = "warn";
    else if (percent <= 0 && !started) kind = "idle";
    else kind = "ok";
  } else kind = options.failed ? "failed" : "unknown";
  const percentLabel = options.checking
    ? "查询中…"
    : hasPercent
      ? percent >= 100
        ? "已用尽"
        : `${Math.round(percent)}%`
      : options.failed
        ? "查询失败"
        : "无数据";
  const remain =
    hasPercent && percent < 100 ? `剩 ${Math.max(0, Math.round(100 - percent))}%` : "";
  const tooltip = [
    hasPercent
      ? `${windowLabel || "限流"}窗口已用 ${Math.round(percent)}%${remain ? `，${remain}` : ""}`
      : options.checking
        ? "用量查询进行中"
        : options.failed
          ? "用量查询失败"
          : "尚未查询到该窗口用量",
    windowSeconds ? `窗口长度 ${windowLabel}` : "",
    hasPercent && percent <= 0 && !started ? "本窗口尚未开始使用" : "",
    resetLabel,
    options.limitReached ? "账号当前已被限流（rate_limit.limit_reached）" : "",
    options.error || "",
  ]
    .filter(Boolean)
    .join("\n");
  return {
    percentLabel,
    kind,
    windowLabel,
    shortLabel,
    barPercent: hasPercent ? Math.max(0, Math.min(100, percent)) : null,
    countdownLabel,
    resetLabel,
    tooltip,
  };
}
/**
 * 5 小时 / 周(月) 用量窗口展示信息（wham/usage 的 rate_limit）。
 *
 * 上游按窗口真实长度报告：短窗口是 5 小时、长窗口是周（团队套餐可能是整月），
 * 因此这里按 usage_*_window_seconds 生成标签，不假设 primary 一定是 5h。
 * 百分比是「已用」：0% 表示本窗口没用过，100% 表示已经用尽。
 */
export function accountUsageInfo(account: Account): AccountUsageInfo {
  const status = String(account.quota_check_status || "").toLowerCase();
  const checking = status === "queued" || status === "running";
  const error = String(account.usage_error || "");
  const failed = Boolean(error) || status === "failed";
  const limitReached =
    account.usage_limit_reached === true || account.usage_limit_reached === 1;
  const checkedLabel = account.usage_checked_at
    ? `用量查询于 ${accountDate(account.usage_checked_at)}`
    : "";
  const fiveHour = usageWindowInfo(account, {
    prefix: "usage_5h",
    checking,
    failed,
    error,
    limitReached,
    fallbackLabel: "5h",
  });
  const week = usageWindowInfo(account, {
    prefix: "usage_week",
    checking,
    failed,
    error,
    limitReached,
    fallbackLabel: "周",
  });
  const tooltip = [
    checkedLabel || (checking ? "用量查询进行中" : "尚未查询用量"),
    account.usage_plan_type ? `套餐 ${account.usage_plan_type}` : "",
    error ? `用量查询失败：${error}` : "",
    account.usage_limit_reached_type
      ? `触发限流的窗口：${account.usage_limit_reached_type}`
      : "",
    limitReached ? "账号当前已被限流" : "",
  ]
    .filter(Boolean)
    .join("\n");
  return { fiveHour, week, limitReached, checkedLabel, error, tooltip };
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
/** 后端账号批量接口的单次上限，与 webui/app.py 的校验保持一致。 */
export const accountBatchLimits: Record<string, number> = {
  live: 500,
  plan: 500,
  quota: 500,
  totp: 500,
  email: 500,
  agent: 500,
  upload: 500,
  retry: 500,
  stop: 500,
  extract: 500,
  activate: 500,
  pay: 500,
  "pay-query": 500,
  note: 5000,
  group: 5000,
  archive: 5000,
  restore: 5000,
  export: 5000,
  secret: 5000,
  delete: 5000,
  download: 1000,
};
export function accountBatchLimit(action: string) {
  const limit = Number(accountBatchLimits[action]);
  return Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : 500;
}
/** 把选中的账号 ID 切成不超过后端单次上限的批次，供自动分批提交使用。 */
export function chunkAccountIds(
  ids: Iterable<number>,
  size = 500,
): number[][] {
  const chunkSize = Math.max(1, Math.floor(Number(size) || 0));
  const chunks: number[][] = [];
  let current: number[] = [];
  for (const raw of ids) {
    const id = Number(raw);
    if (!Number.isInteger(id) || id <= 0) continue;
    current.push(id);
    if (current.length >= chunkSize) {
      chunks.push(current);
      current = [];
    }
  }
  if (current.length) chunks.push(current);
  return chunks;
}
/**
 * 合并分批提交的响应：数组合并、`*_count` 重新按合并后的数组长度或求和计算，
 * 标量保留首个非空值（如 idempotency_key、ok），对象做浅合并。
 * 标识与分页类数值（id、*_id、page、total 等）不做累加，避免拼出错误的 ID。
 */
export function mergeAccountResults(results: any[]): any {
  const merged: Record<string, any> = {};
  const arrayKeys = new Set<string>();
  for (const result of results) {
    if (!result || typeof result !== "object") continue;
    for (const [key, value] of Object.entries(result)) {
      if (Array.isArray(value)) {
        merged[key] = [...(merged[key] || []), ...value];
        arrayKeys.add(key);
      } else if (typeof value === "number") {
        if (mergeAccountScalarKeys.has(key) || key.endsWith("_id")) {
          if (merged[key] === undefined) merged[key] = value;
          continue;
        }
        merged[key] = (typeof merged[key] === "number" ? merged[key] : 0) + value;
      } else if (value && typeof value === "object") {
        merged[key] = { ...(merged[key] || {}), ...value };
      } else if (merged[key] === undefined && value !== undefined && value !== null)
        merged[key] = value;
    }
  }
  for (const key of arrayKeys) {
    const countKey = `${key}_count`;
    if (countKey in merged) merged[countKey] = merged[key].length;
  }
  return merged;
}
const mergeAccountScalarKeys = new Set([
  "id",
  "page",
  "page_size",
  "offset",
  "limit",
  "total",
  "revision",
]);
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
