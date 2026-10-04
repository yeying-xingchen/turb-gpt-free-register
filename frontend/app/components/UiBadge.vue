<script setup lang="ts">
const props = defineProps<{ value?: string | boolean | null }>();
const labels: Record<string, string> = {
  stopping: "停止中",
  partial_success: "部分成功",
  needs_attention: "待核实",
  deactivated: "账号已废",
  missing: "缺少凭证",
  skipped: "已跳过",
  in_use: "使用中",
  exhausted: "已领完",
  unknown: "待核实",
  awaiting_blik: "等待验证码",
  interrupted: "已中断",
  pending: "等待中",
  queued: "排队中",
  running: "运行中",
  processing: "处理中",
  verifying: "核验中",
  submitting: "提交中",
  submission_pending: "提交结果待确认",
  success: "成功",
  succeeded: "成功",
  completed: "已完成",
  failed: "失败",
  rejected: "已拒绝",
  not_activated: "未成功激活",
  timeout: "已超时",
  error: "异常",
  cancelled: "已取消",
  canceled: "已取消",
  paused: "已暂停",
  stopped: "已停止",
  available: "可用",
  used: "已使用",
  free: "Free",
  plus: "Plus",
  pro: "Pro",
  team: "Team",
  enabled: "已启用",
  disabled: "已停用",
  active: "有效",
  revoked: "已撤销",
  expired: "已过期",
  retrying: "重试中",
  ok: "正常",
  redeemed: "已兑换",
  unredeemed: "未兑换",
};
const text = computed(() =>
  props.value == null || props.value === ""
    ? "未检测"
    : labels[String(props.value)] || String(props.value),
);
const kind = computed(() => {
  const value = String(props.value);
  if (
    [
      "partial_success",
      "needs_attention",
      "unknown",
      "awaiting_blik",
      "interrupted",
      "submission_pending",
    ].includes(value)
  )
    return "warning";
  if (
    [
      "failed",
      "error",
      "revoked",
      "expired",
      "deactivated",
      "rejected",
      "not_activated",
      "timeout",
    ].includes(value)
  )
    return "danger";
  if (
    [
      "success",
      "succeeded",
      "completed",
      "available",
      "active",
      "enabled",
      "ok",
      "plus",
      "true",
    ].includes(value)
  )
    return "success";
  if (
    [
      "running",
      "pending",
      "queued",
      "retrying",
      "stopping",
      "in_use",
      "redeemed",
      "processing",
      "verifying",
      "submitting",
    ].includes(value)
  )
    return "info";
  return "neutral";
});
</script>
<template>
  <span class="badge" :class="`badge-${kind}`"
    ><span class="badge-dot" />{{ text }}</span
  >
</template>
