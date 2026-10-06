<script setup lang="ts">
import {
  accountDate,
  accountQuotaInfo,
  accountStatus,
  type Account,
} from "~/utils/accounts";

const props = withDefaults(
  defineProps<{ account: Account; field?: "balance" | "reset"; detailed?: boolean }>(),
  { field: "balance", detailed: false },
);
const info = computed(() => accountQuotaInfo(props.account));
const kind = computed(() =>
  props.field === "reset" ? info.value.resetKind : info.value.balanceKind,
);
const label = computed(() =>
  props.field === "reset" ? info.value.resetLabel : info.value.balanceLabel,
);
const hint = computed(() => {
  if (props.detailed) return info.value.checkedLabel;
  if (props.field === "reset") {
    return (
      info.value.resetExpiryLabel ||
      info.value.checkedLabel ||
      info.value.error
    );
  }
  return info.value.error || info.value.checkedLabel;
});
const applicableNote = computed(() => {
  if (props.field !== "reset") return "";
  const available = props.account.reset_credits_applicable;
  const total = props.account.reset_credits_available;
  if (typeof available !== "number" || typeof total !== "number") return "";
  return available === total ? "" : `可应用 ${available} 张`;
});
</script>

<template>
  <div class="quota-info" :class="{ 'quota-info-detailed': detailed }">
    <span
      class="quota-value"
      :class="`quota-${kind}`"
      :title="info.tooltip || label"
      >{{ label }}</span
    >
    <p v-if="hint" class="quota-meta" :title="info.tooltip || hint">
      {{ hint }}
    </p>
    <p v-if="applicableNote" class="quota-meta" :title="info.tooltip">
      {{ applicableNote }}
    </p>
    <template v-if="detailed">
      <dl class="quota-facts">
        <div>
          <dt>额度查询状态</dt>
          <dd>{{ accountStatus(account.quota_check_status) }}</dd>
        </div>
        <div>
          <dt>最近查询</dt>
          <dd>{{ info.checkedLabel || "尚未查询" }}</dd>
        </div>
        <div v-if="account.quota_currency">
          <dt>币种</dt>
          <dd>{{ account.quota_currency }}</dd>
        </div>
        <div v-if="account.quota_last_success_at">
          <dt>最近成功</dt>
          <dd>{{ accountDate(account.quota_last_success_at) }}</dd>
        </div>
        <div v-if="account.reset_credits_applicable != null">
          <dt>当前可应用重置</dt>
          <dd>{{ account.reset_credits_applicable }} 张</dd>
        </div>
      </dl>
      <p v-if="info.error" class="quota-error">{{ info.error }}</p>
    </template>
  </div>
</template>

<style scoped>
.quota-info {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.quota-value {
  font-size: 12px;
  font-weight: 700;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.quota-value-value {
  color: #166534;
}
.quota-value-empty,
.quota-value-unknown {
  color: var(--muted, #84909f);
  font-weight: 600;
}
.quota-value-busy {
  color: #92600a;
}
.quota-value-failed {
  color: #b3261e;
}
.quota-meta {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--muted, #84909f);
  overflow-wrap: anywhere;
}
.quota-info-detailed .quota-meta {
  color: var(--text-muted, #6b7280);
}
.quota-error {
  margin: 0;
  font-size: 11px;
  color: #b94b4b;
  overflow-wrap: anywhere;
}
.quota-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
}
.quota-facts dt {
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.quota-facts dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
@media (max-width: 800px) {
  .quota-facts {
    grid-template-columns: 1fr;
  }
}
</style>
