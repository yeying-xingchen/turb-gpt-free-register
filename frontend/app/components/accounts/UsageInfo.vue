<script setup lang="ts">
import {
  accountDate,
  accountStatus,
  accountUsageInfo,
  type Account,
  type AccountUsageWindow,
} from "~/utils/accounts";

const props = withDefaults(
  defineProps<{ account: Account; field?: "5h" | "week"; detailed?: boolean }>(),
  { field: "5h", detailed: false },
);
const info = computed(() => accountUsageInfo(props.account));
const window = computed<AccountUsageWindow>(() =>
  props.field === "week" ? info.value.week : info.value.fiveHour,
);
</script>

<template>
  <div class="usage-info" :class="{ 'usage-info-detailed': detailed }">
    <div class="usage-head">
      <span
        class="usage-percent"
        :class="`usage-${window.kind}`"
        :title="window.tooltip"
        >{{ window.percentLabel }}</span
      >
      <span v-if="window.windowLabel" class="usage-window">{{
        window.windowLabel
      }}</span>
    </div>
    <span
      class="usage-track"
      role="progressbar"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="window.barPercent ?? undefined"
      :aria-label="`${window.shortLabel} 已用 ${window.percentLabel}`"
      :title="window.tooltip"
    >
      <span
        class="usage-fill"
        :class="`usage-fill-${window.kind}`"
        :style="{ width: `${window.barPercent ?? 0}%` }"
      />
    </span>
    <p v-if="window.resetLabel" class="usage-meta" :title="window.tooltip">
      {{ window.resetLabel }}
    </p>
    <template v-if="detailed">
      <dl class="usage-facts">
        <div>
          <dt>用量查询状态</dt>
          <dd>{{ accountStatus(account.quota_check_status) }}</dd>
        </div>
        <div>
          <dt>最近查询</dt>
          <dd>{{ info.checkedLabel || "尚未查询" }}</dd>
        </div>
        <div v-if="account.usage_plan_type">
          <dt>套餐</dt>
          <dd>{{ account.usage_plan_type }}</dd>
        </div>
        <div v-if="account.usage_limit_reached_type">
          <dt>触发限流的窗口</dt>
          <dd>{{ account.usage_limit_reached_type }}</dd>
        </div>
        <div v-if="account.usage_allowed === false">
          <dt>当前请求</dt>
          <dd>已被限流</dd>
        </div>
      </dl>
      <p v-if="info.error" class="usage-error">{{ info.error }}</p>
    </template>
  </div>
</template>

<style scoped>
.usage-info {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.usage-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.usage-percent {
  font-size: 12px;
  font-weight: 700;
  line-height: 1.6;
  white-space: nowrap;
}
.usage-ok {
  color: #166534;
}
.usage-warn,
.usage-busy {
  color: #a16207;
}
.usage-limit,
.usage-failed {
  color: #b3261e;
}
.usage-idle,
.usage-unknown {
  color: var(--muted, #84909f);
  font-weight: 600;
}
.usage-window {
  border-radius: 999px;
  border: 1px solid #dbe1e9;
  background: #f6f7f9;
  color: #5b6b7f;
  padding: 0 6px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.7;
}
/* 与列表里的合并列使用同一套进度条配色。 */
.usage-track {
  display: block;
  height: 6px;
  border-radius: 999px;
  background: #eef1f5;
  border: 1px solid #e3e7ec;
  overflow: hidden;
}
.usage-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: #34a06f;
}
.usage-fill-ok {
  background: #34a06f;
}
.usage-fill-warn {
  background: #d99a2b;
}
.usage-fill-limit,
.usage-fill-failed {
  background: #d05353;
}
.usage-fill-idle {
  background: #b9c2cd;
}
.usage-fill-busy {
  background: #e0c07a;
}
.usage-fill-unknown {
  background: transparent;
}
.usage-meta {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--muted, #84909f);
  overflow-wrap: anywhere;
}
.usage-error {
  margin: 0;
  font-size: 11px;
  color: #b94b4b;
  overflow-wrap: anywhere;
}
.usage-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
}
.usage-facts dt {
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.usage-facts dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
@media (max-width: 800px) {
  .usage-facts {
    grid-template-columns: 1fr;
  }
}
</style>
