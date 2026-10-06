<script setup lang="ts">
import {
  accountQuotaInfo,
  accountUsageInfo,
  type Account,
  type AccountUsageWindow,
} from "~/utils/accounts";

const props = defineProps<{ account: Account }>();
const quota = computed(() => accountQuotaInfo(props.account));
const usage = computed(() => accountUsageInfo(props.account));
const windows = computed<AccountUsageWindow[]>(() => [
  usage.value.fiveHour,
  usage.value.week,
]);
</script>

<template>
  <div class="quota-usage">
    <div
      class="quota-usage-balance"
      :class="`quota-${quota.balanceKind}`"
      :title="quota.tooltip"
    >
      {{ quota.balanceLabel }}
    </div>
    <div
      v-for="window in windows"
      :key="window.shortLabel"
      class="usage-row"
      :title="window.tooltip"
    >
      <span class="usage-row-label">{{ window.shortLabel }}</span>
      <span
        class="usage-row-track"
        role="progressbar"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-valuenow="window.barPercent ?? undefined"
        :aria-label="`${window.shortLabel} 已用 ${window.percentLabel}`"
      >
        <span
          class="usage-row-fill"
          :class="`usage-fill-${window.kind}`"
          :style="{ width: `${window.barPercent ?? 0}%` }"
        />
      </span>
      <span class="usage-row-value" :class="`usage-${window.kind}`">{{
        window.percentLabel
      }}</span>
      <span v-if="window.countdownLabel" class="usage-row-reset">{{
        window.countdownLabel
      }}</span>
    </div>
  </div>
</template>

<style scoped>
.quota-usage {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.quota-usage-balance {
  font-size: 12px;
  font-weight: 700;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.quota-value {
  color: #166534;
}
.quota-empty,
.quota-unknown {
  color: var(--muted, #84909f);
  font-weight: 600;
}
.quota-busy {
  color: #92600a;
}
.quota-failed {
  color: #b3261e;
}
/* 进度条行：短标签 + 轨道 + 已用百分比 + 紧凑倒计时。 */
.usage-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}
.usage-row-label {
  flex: 0 0 auto;
  min-width: 18px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted, #84909f);
}
.usage-row-track {
  flex: 1 1 56px;
  min-width: 48px;
  height: 6px;
  border-radius: 999px;
  background: #eef1f5;
  border: 1px solid #e3e7ec;
  overflow: hidden;
}
.usage-row-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: #34a06f;
  transition: width 0.2s ease;
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
.usage-row-value {
  flex: 0 0 auto;
  min-width: 34px;
  text-align: right;
  font-size: 11px;
  font-weight: 700;
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
.usage-row-reset {
  flex: 0 1 auto;
  font-size: 10px;
  line-height: 1.5;
  color: var(--muted, #84909f);
  overflow-wrap: anywhere;
}
</style>
