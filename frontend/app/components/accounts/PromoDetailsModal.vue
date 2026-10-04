<script setup lang="ts">
import { accountPlanLabel, type Account } from "~/utils/accounts";

const props = defineProps<{ open: boolean; account: Account | null }>();
const emit = defineEmits<{ "update:open": [open: boolean] }>();

interface CampaignField {
  label: string;
  value: string;
}

interface CampaignView {
  key: string;
  name: string;
  fields: CampaignField[];
  raw: string;
}

const campaigns = computed<CampaignView[]>(() => {
  const source = props.account?.eligible_promo_campaigns;
  if (!source || typeof source !== "object" || Array.isArray(source)) return [];
  return Object.entries(source).map(([key, value]) => {
    const campaign = (value && typeof value === "object" ? value : {}) as Record<
      string,
      any
    >;
    const metadata =
      campaign.metadata && typeof campaign.metadata === "object"
        ? campaign.metadata
        : {};
    const duration =
      metadata.duration && typeof metadata.duration === "object"
        ? [
            metadata.duration.num_periods,
            metadata.duration.period
              ? {
                  month: "个月",
                  year: "年",
                  week: "周",
                  day: "天",
                }[String(metadata.duration.period).toLowerCase()] ||
                metadata.duration.period
              : "",
          ]
            .filter((part: unknown) => part !== null && part !== undefined && part !== "")
            .join(" ")
        : "";
    const autoRenew =
      metadata.no_auto_renewal_at_discount_end === false
        ? "是"
        : metadata.no_auto_renewal_at_discount_end === true
          ? "否"
          : "";
    const fields: CampaignField[] = [
      { label: "活动 ID", value: campaign.id },
      { label: "标题", value: metadata.title },
      { label: "说明", value: metadata.summary },
      { label: "套餐", value: metadata.plan_name },
      {
        label: "优惠类型",
        value: metadata.promotion_type_label || metadata.promotion_type,
      },
      {
        label: "折扣比例",
        value:
          metadata.discount?.percentage !== undefined &&
          metadata.discount?.percentage !== null
            ? `${metadata.discount.percentage}%`
            : "",
      },
      { label: "优惠时长", value: duration },
      { label: "结束后自动续费", value: autoRenew },
      { label: "支付处理方", value: metadata.processor },
    ]
      .map((field) => ({ label: field.label, value: String(field.value ?? "") }))
      .filter((field) => field.value.trim());
    return {
      key,
      name: accountPlanLabel(metadata.plan_name || key),
      fields,
      raw: JSON.stringify(campaign, null, 2),
    };
  });
});
</script>

<template>
  <UiModal
    :open="open"
    :title="`可用套餐优惠 · ${account?.email || ''}`"
    @update:open="emit('update:open', $event)"
  >
    <div class="promo-details stack">
      <p class="muted">
        最近一次查套餐返回的活动资格，仅表示账号当前可领取，
        <strong>不代表已开通套餐</strong>。
      </p>
      <p v-if="!campaigns.length" class="muted">该账号当前没有可用的套餐优惠。</p>
      <section v-for="campaign in campaigns" :key="campaign.key">
        <h3>{{ campaign.name }}</h3>
        <dl>
          <div v-for="field in campaign.fields" :key="field.label">
            <dt>{{ field.label }}</dt>
            <dd>{{ field.value }}</dd>
          </div>
        </dl>
        <details>
          <summary>查看全部原始字段</summary>
          <pre class="mono">{{ campaign.raw }}</pre>
        </details>
      </section>
    </div>
  </UiModal>
</template>

<style scoped>
.promo-details section {
  border: 1px solid var(--border, #e2e8f0);
  border-radius: 10px;
  padding: 14px 16px;
}
.promo-details h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: #1d4ed8;
}
.promo-details dl {
  display: grid;
  gap: 8px;
  margin: 0;
}
.promo-details dl > div {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: 12px;
  font-size: 12px;
}
.promo-details dt {
  color: var(--muted, #84909f);
}
.promo-details dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.promo-details details {
  margin-top: 12px;
  border-top: 1px solid var(--border, #edf0f6);
  padding-top: 10px;
}
.promo-details summary {
  font-size: 12px;
  color: #5274c9;
  cursor: pointer;
}
.promo-details pre {
  max-height: 240px;
  overflow: auto;
  margin: 10px 0 0;
  padding: 12px;
  border-radius: 8px;
  background: #f4f6fa;
  color: #52617b;
  font-size: 11px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .promo-details dl > div {
    grid-template-columns: 1fr;
    gap: 3px;
  }
}
</style>
