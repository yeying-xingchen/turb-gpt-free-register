<script setup lang="ts">
import {
  accountDate,
  accountPlanInfo,
  accountStatus,
  type Account,
  type AccountPlanPromo,
} from "~/utils/accounts";

const props = withDefaults(
  defineProps<{ account: Account; detailed?: boolean }>(),
  { detailed: false },
);
const emit = defineEmits<{ promos: [account: Account] }>();

const info = computed(() => accountPlanInfo(props.account));
// 标签底色区分三种语义：已开通、free 且有优惠/可试用、free 但资格未知。
const planKind = computed(() => {
  if (!info.value.normalized) return "unknown";
  if (info.value.paid) return "paid";
  return info.value.promos.length || info.value.trialEligible
    ? "free-active"
    : "free-idle";
});
const tooltip = computed(() =>
  [
    info.value.checkedLabel ||
      (info.value.queryKind ? "尚未完成首次查询" : "未查询实时套餐"),
    info.value.networkLabel ? `网络：${info.value.networkLabel}` : "",
    info.value.error,
    info.value.graceTitle,
    info.value.subscriptionError
      ? `订阅查询失败：${info.value.subscriptionError}`
      : "",
  ]
    .filter(Boolean)
    .join("\n"),
);
const visiblePromos = computed<AccountPlanPromo[]>(() =>
  props.detailed ? info.value.promos : info.value.promos.slice(0, 3),
);
const hiddenPromoCount = computed(() =>
  props.detailed ? 0 : Math.max(0, info.value.promos.length - 3),
);
</script>

<template>
  <div class="plan-info" :class="{ 'plan-info-detailed': detailed }">
    <div class="plan-head">
      <span class="plan-tag" :class="`plan-tag-${planKind}`" :title="tooltip">{{
        info.label
      }}</span>
      <span v-if="info.trialEligible" class="plan-chip chip-trial"
        >可试用 Plus</span
      >
      <span
        v-else-if="info.eligibilityPending"
        class="plan-chip chip-pending"
        title="尚未查询到 Plus 试用资格，可执行“查套餐”确认"
        >待查资格</span
      >
      <span
        v-if="info.queryKind"
        class="plan-chip"
        :class="`chip-${info.queryKind}`"
        :title="info.error || info.checkedLabel"
        >{{ info.queryLabel }}</span
      >
      <button
        v-if="info.promos.length"
        type="button"
        class="plan-chip chip-promo"
        :title="`查看 ${info.promos.length} 项可用套餐优惠详情`"
        @click="emit('promos', account)"
      >
        优惠详情（{{ info.promos.length }}）
      </button>
    </div>

    <ul v-if="visiblePromos.length" class="plan-promos">
      <li
        v-for="promo in visiblePromos"
        :key="promo.key"
        :title="[promo.summary, ...promo.details].join('\n')"
      >
        {{ promo.summary }}
      </li>
      <li v-if="hiddenPromoCount" class="muted">
        另有 {{ hiddenPromoCount }} 项，点击“优惠详情”查看
      </li>
    </ul>

    <p v-if="info.lines.length" class="plan-meta" :title="tooltip">
      {{ info.lines.join(" · ") }}
    </p>
    <p v-if="info.graceLabel" class="plan-grace" :title="info.graceTitle">
      {{ info.graceLabel }}
    </p>
    <p v-if="info.error" class="plan-error" :title="info.error">
      {{ info.error }}
    </p>

    <template v-if="detailed">
      <dl class="plan-facts">
        <div>
          <dt>套餐查询状态</dt>
          <dd>
            {{
              info.queryKind
                ? info.queryLabel
                : accountStatus(account.plan_check_status)
            }}
          </dd>
        </div>
        <div>
          <dt>最近查询</dt>
          <dd>{{ info.checkedLabel || "尚未查询" }}</dd>
        </div>
        <div>
          <dt>查询网络</dt>
          <dd>{{ info.networkLabel || "—" }}</dd>
        </div>
        <div>
          <dt>最近成功</dt>
          <dd>{{ info.lastSuccess || "—" }}</dd>
        </div>
        <div v-if="!info.paid && info.activeUntilLabel">
          <dt>订阅有效至</dt>
          <dd>{{ info.activeUntilLabel }}</dd>
        </div>
        <div v-if="account.subscription_checked_at">
          <dt>订阅核验</dt>
          <dd>{{ accountDate(account.subscription_checked_at) }}</dd>
        </div>
        <div v-if="account.subscription_plan_type">
          <dt>订阅套餐</dt>
          <dd>{{ account.subscription_plan_type }}</dd>
        </div>
        <div v-if="account.discount_expires_at">
          <dt>折扣结束</dt>
          <dd>{{ accountDate(account.discount_expires_at) }}</dd>
        </div>
        <div v-if="account.discount_promo_campaign_id">
          <dt>折扣活动</dt>
          <dd class="mono">{{ account.discount_promo_campaign_id }}</dd>
        </div>
      </dl>
      <p v-if="info.subscriptionError" class="plan-error">
        订阅查询失败：{{ info.subscriptionError }}
      </p>
    </template>
  </div>
</template>

<style scoped>
.plan-info {
  display: grid;
  gap: 7px;
  min-width: 0;
}
.plan-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.plan-tag {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  border: 1px solid transparent;
  padding: 1px 9px;
  font-size: 11px;
  font-weight: 700;
  line-height: 1.75;
  white-space: nowrap;
}
/* 已开通套餐：绿色底 */
.plan-tag-paid {
  color: #13745a;
  background: #eafaf1;
  border-color: #bfe6d1;
}
/* free 且有优惠或可试用 Plus：绿色底 */
.plan-tag-free-active {
  color: #166534;
  background: #eafaf1;
  border-color: #bfe6d1;
}
/* free 但资格未知：中性灰底，表示状态未确认 */
.plan-tag-free-idle {
  color: #5b6b7f;
  background: #eef1f5;
  border-color: #dbe1e9;
}
/* 尚未查询过套餐：虚线浅底 */
.plan-tag-unknown {
  color: var(--muted, #84909f);
  background: #f6f7f9;
  border-color: #e3e7ec;
  border-style: dashed;
  font-weight: 600;
}
.plan-chip {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  border: 1px solid transparent;
  padding: 1px 8px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.7;
  white-space: nowrap;
}
.chip-trial {
  color: #166534;
  background: #eafaf1;
  border-color: #bfe6d1;
}
.chip-pending,
.chip-running {
  color: #92600a;
  background: #fff7e6;
  border-color: #f3d99b;
}
.chip-failed {
  color: #b3261e;
  background: #fff1f0;
  border-color: #f7c9c5;
}
.chip-promo {
  color: #1d4ed8;
  background: #eef4ff;
  border-color: #c3d4fb;
  cursor: pointer;
}
.chip-promo:hover {
  background: #dde8ff;
}
.plan-promos {
  display: grid;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.plan-promos li {
  color: #166534;
  background: #f0fbf5;
  border: 1px solid #bfe6d1;
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 11px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.plan-meta,
.plan-grace,
.plan-error {
  margin: 0;
  font-size: 11px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.plan-meta {
  color: var(--muted, #84909f);
}
.plan-grace {
  color: #92600a;
  background: #fff7e6;
  border: 1px solid #f3d99b;
  border-radius: 6px;
  padding: 2px 8px;
}
.plan-error {
  color: #b94b4b;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.plan-info-detailed .plan-error {
  display: block;
  -webkit-line-clamp: none;
  overflow: visible;
}
.plan-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
}
.plan-facts dt {
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.plan-facts dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
@media (max-width: 800px) {
  .plan-facts {
    grid-template-columns: 1fr;
  }
}
</style>
