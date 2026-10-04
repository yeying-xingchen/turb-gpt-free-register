<script setup lang="ts">
const props = defineProps<{
  providers: any[];
  activation?: boolean;
  disabled?: boolean;
}>();
const providerId = ref("");
const credential = ref("");
const cdk = ref("");
const linkType = ref("upi");
const proxy = ref("");
const entries = ref("");
const paymentProvider = ref("");
const promo = ref("default");
const campaign = ref("");
const types: Record<string, string[]> = {
  extract: ["pix", "upi", "kakao_pay", "ideal"],
  lumen: [
    "ideal",
    "upi",
    "pix",
    "paypal",
    "kakao_pay",
    "momo",
    "blik",
    "twint",
    "gcash",
    "gopay",
  ],
  upi_git5: ["upi"],
};
const provider = computed(() =>
  props.providers.find((item) => String(item.id) === providerId.value),
);
const cdks = computed(() =>
  (provider.value?.cdks || []).filter(
    (item: any) => item.enabled !== false && item.enabled !== 0,
  ),
);
watch(providerId, () => {
  credential.value = "";
  cdk.value = proxy.value = entries.value = "";
  paymentProvider.value = "";
  linkType.value = props.activation
    ? "upi"
    : provider.value?.default_link_type || "upi";
});
watch(credential, () => {
  cdk.value = "";
});
function clear() {
  cdk.value = proxy.value = entries.value = "";
}
function validateProxy(value: string, index = 1) {
  try {
    const url = new URL(value);
    if (
      url.hostname &&
      [
        "http:",
        "https:",
        "socks4:",
        "socks4a:",
        "socks5:",
        "socks5h:",
      ].includes(url.protocol) &&
      !/\s/.test(value)
    )
      return;
  } catch {
    /* Do not include credentials in validation errors. */
  }
  throw new Error(`第 ${index} 个代理必须是有效的 HTTP(S) 或 SOCKS URL`);
}
function payload(): Record<string, any> {
  const item = provider.value;
  if (!item) throw new Error("请选择提链服务商");
  const result: Record<string, any> = {
    provider_id: Number(item.id),
    link_type: props.activation ? "upi" : linkType.value,
    payment_amount: 0,
  };
  if (credential.value === "raw") {
    if (!cdk.value.trim()) throw new Error("请输入提链 CDK");
    result.cdk = cdk.value.trim();
  } else if (credential.value.startsWith("saved:")) {
    result.cdk_id = Number(credential.value.slice(6));
    if (!cdks.value.some((saved: any) => Number(saved.id) === result.cdk_id))
      throw new Error("请选择当前服务商的有效 CDK");
  } else if (!(
    credential.value === "configured" &&
    Number(item.id) === 0 &&
    item.has_configured_cdk
  )) {
    throw new Error("请选择提链 CDK 来源");
  }
  if (item.provider_type === "lumen" && proxy.value.trim()) {
    validateProxy(proxy.value.trim());
    result.proxy_url = proxy.value.trim();
  }
  if (item.provider_type === "upi_git5") {
    result.entry_proxies = entries.value
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!result.entry_proxies.length)
      throw new Error("UPI-GIT5 必须填写服务端可连接的入口代理");
    result.entry_proxies.forEach((value: string, index: number) =>
      validateProxy(value, index + 1),
    );
    if (!props.activation && paymentProvider.value)
      result.payment_provider_id = paymentProvider.value;
  }
  if (!props.activation && promo.value !== "default")
    result.use_promo = promo.value === "yes";
  if (!props.activation && campaign.value.trim())
    result.promo_campaign = campaign.value.trim();
  return result;
}
defineExpose({ payload, clear });
</script>

<template>
  <fieldset class="extraction-fields form-grid" :disabled="disabled">
    <label class="field"
      >提链服务商
      <select v-model="providerId" class="select" required>
        <option value="" disabled>请选择服务商</option>
        <option
          v-for="item in providers"
          :key="item.id"
          :value="String(item.id)"
        >
          {{ item.name }} · {{ item.provider_type }}
        </option>
      </select>
    </label>
    <label class="field"
      >提链 CDK 来源
      <select v-model="credential" class="select" required>
        <option value="" disabled>请选择 CDK</option>
        <option v-for="item in cdks" :key="item.id" :value="`saved:${item.id}`">
          {{
            item.masked_cdk ||
            item.cdk_masked ||
            "尾号 " +
              (item.display_suffix ||
                item.cdk_suffix ||
                item.suffix ||
                item.id)
          }}{{ item.memo ? " · " + item.memo : "" }}
        </option>
        <option
          v-if="Number(provider?.id) === 0 && provider?.has_configured_cdk"
          value="configured"
        >
          服务器环境配置
        </option>
        <option v-if="provider" value="raw">输入本次临时 CDK</option>
      </select>
    </label>
    <label v-if="credential === 'raw'" class="field"
      >本次提链 CDK
      <input
        v-model="cdk"
        class="input"
        type="password"
        autocomplete="new-password"
        required
      />
    </label>
    <label v-if="!activation" class="field"
      >支付方式
      <select v-model="linkType" class="select">
        <option
          v-for="type in types[provider?.provider_type] || ['upi']"
          :key="type"
          :value="type"
        >
          {{ type.toUpperCase() }}
        </option>
      </select>
    </label>
    <label v-if="provider?.provider_type === 'lumen'" class="field"
      >代理 URL（可选）
      <input
        v-model="proxy"
        class="input"
        autocomplete="off"
        placeholder="留空使用默认连接"
      />
    </label>
    <label v-if="provider?.provider_type === 'upi_git5'" class="field full"
      >UPI 入口代理（每行一个）
      <textarea
        v-model="entries"
        class="textarea mono"
        rows="3"
        autocomplete="off"
        spellcheck="false"
        required
        placeholder="http://user:password@host:port"
      />
      <small class="muted"
        >填写 UPI 服务端可连接的 HTTP(S) / SOCKS 代理。</small
      >
    </label>
    <template v-if="!activation && provider?.provider_type === 'upi_git5'">
      <p class="muted">使用零元 Checkout。</p>
      <label class="field"
        >供应商内置支付平台（可选）<select
          v-model="paymentProvider"
          class="select"
        >
          <option value="">仅提链</option>
          <option value="foarge">Foarge</option>
          <option value="xxsyun">XXSYun</option>
          <option value="astrascan">AstraScan</option>
        </select></label
      >
    </template>
    <template v-if="!activation">
      <label class="field"
        >促销活动<select v-model="promo" class="select">
          <option value="default">使用默认设置</option>
          <option value="yes">启用促销</option>
          <option value="no">不使用促销</option>
        </select></label
      >
      <label class="field"
        >活动标识（可选）<input
          v-model="campaign"
          class="input"
          placeholder="promo_campaign"
      /></label>
    </template>
  </fieldset>
</template>

<style scoped>
.extraction-fields {
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}
.full {
  grid-column: 1 / -1;
}
</style>
