<script setup lang="ts">
import { accountError } from "~/utils/accounts";
const props = defineProps<{
  disabled?: boolean;
  queryOnly?: boolean;
}>();
const { request } = useApi();
const provider = ref("v1");
const authMode = ref("key");
const cdk = ref("");
const username = ref("");
const password = ref("");
const loggedIn = ref(false);
const busy = ref(false);
const message = ref("");
const error = ref("");
const platforms = ref<any[]>([]);
const credentialMode = ref<"saved" | "raw">("raw");
const savedCdkId = ref<number | null>(null);
const session = computed(
  () => provider.value === "orderhub" && authMode.value === "session",
);
const current = computed(() =>
  platforms.value.find((item) => item.provider_type === provider.value),
);
const savedCdks = computed(() =>
  (current.value?.cdks || []).filter((item: any) => item.enabled),
);
const useSaved = computed(
  () => !session.value && credentialMode.value === "saved",
);
onMounted(() => loadPlatforms());
watch(provider, () => {
  authMode.value = "key";
  clear();
  syncCredentialMode();
});
watch(authMode, () => {
  clear();
  loggedIn.value = false;
});
async function loadPlatforms() {
  try {
    const data = await request("/api/payment-providers");
    platforms.value = data.items || [];
  } catch {
    platforms.value = [];
  }
  syncCredentialMode();
}
/** 有已保存凭据时默认选它，避免每次重复粘贴；没有则回到手工输入。 */
function syncCredentialMode() {
  if (session.value) return;
  if (savedCdks.value.length) {
    credentialMode.value = "saved";
    if (
      savedCdkId.value == null ||
      !savedCdks.value.some((item: any) => item.id === savedCdkId.value)
    )
      savedCdkId.value = savedCdks.value[0].id;
  } else {
    credentialMode.value = "raw";
    savedCdkId.value = null;
  }
}
function clear() {
  cdk.value = password.value = username.value = "";
}
function credentialLabel() {
  return provider.value === "orderhub"
    ? "OrderHub API Key"
    : provider.value === "seashore"
      ? "发布者 CDK（PBK-…）"
      : "本次支付 CDK";
}
async function sessionAction(action: "login" | "session" | "logout") {
  busy.value = true;
  error.value = "";
  try {
    if (
      action === "login" &&
      (!/^\d+$/.test(username.value.trim()) || !password.value)
    )
      throw new Error("请输入 OrderHub 数字账号和密码");
    const result = await request(
      `/api/payments/orderhub/${action}`,
      action === "session"
        ? undefined
        : {
            method: "POST",
            body:
              action === "login"
                ? { username: username.value.trim(), password: password.value }
                : {},
          },
    );
    loggedIn.value =
      action === "login" || (action === "session" && result.logged_in === true);
    message.value = loggedIn.value
      ? "OrderHub 服务器会话已登录"
      : "尚未登录 OrderHub";
  } catch (cause) {
    loggedIn.value = false;
    error.value = accountError(cause);
  } finally {
    password.value = "";
    busy.value = false;
  }
}
function balanceText(result: any) {
  const data = result?.data || {};
  const parts: string[] = [];
  if (data.remaining_uses !== undefined)
    parts.push(`剩余次数 ${data.remaining_uses}/${data.uses_total ?? "?"}`);
  if (data.uses_held) parts.push(`暂扣 ${data.uses_held}`);
  if (data.pending_orders !== undefined)
    parts.push(`全站待接单 ${data.pending_orders}`);
  if (data.capacity?.recommended !== undefined)
    parts.push(`建议一次发布 ${data.capacity.recommended} 单`);
  if (data.available_uses !== undefined) parts.push(`可用 ${data.available_uses}`);
  if (data.balance !== undefined && data.remaining_uses === undefined)
    parts.push(`余额 ${data.balance}`);
  return parts.join(" · ");
}
async function verify() {
  busy.value = true;
  error.value = "";
  try {
    const result = await request("/api/payments/verify", {
      method: "POST",
      body: payload(),
    });
    const detail = balanceText(result);
    message.value = `凭据验证完成${detail ? "：" + detail : ""}`;
  } catch (cause) {
    error.value = accountError(cause);
  } finally {
    busy.value = false;
  }
}
function payload(): Record<string, any> {
  if (session.value) {
    if (!loggedIn.value)
      throw new Error("请先登录 OrderHub，或检查服务器登录状态");
    return { provider: provider.value, auth_mode: "session" };
  }
  if (useSaved.value) {
    if (!savedCdkId.value)
      throw new Error("请选择已保存的支付 CDK，或改为输入临时凭据");
    return {
      provider: provider.value,
      auth_mode: "key",
      cdk_id: savedCdkId.value,
    };
  }
  if (!cdk.value.trim()) throw new Error("请输入支付 CDK / API Key");
  return {
    provider: provider.value,
    auth_mode: "key",
    cdk: cdk.value.trim(),
  };
}
defineExpose({ payload, clear, provider });
</script>

<template>
  <fieldset class="payment-fields stack" :disabled="disabled || busy">
    <div class="form-grid">
      <label class="field"
        >支付平台<select v-model="provider" class="select">
          <option value="v1">Astra Scan Workbench</option>
          <option value="masi">Masi</option>
          <option value="orderhub">UPI OrderHub</option>
          <option value="seashore">seashore 发布者 API</option>
        </select></label
      >
      <label v-if="provider === 'orderhub'" class="field"
        >验证方式<select v-model="authMode" class="select">
          <option value="key">API Key</option>
          <option value="session">账号登录</option>
        </select></label
      >
      <label v-if="!session && savedCdks.length" class="field"
        >支付凭据来源<select v-model="credentialMode" class="select">
          <option value="saved">使用已保存的 CDK</option>
          <option value="raw">输入临时凭据</option>
        </select></label
      >
      <label v-if="useSaved" class="field"
        >已保存的 CDK<select v-model="savedCdkId" class="select">
          <option v-for="item in savedCdks" :key="item.id" :value="item.id">
            {{ item.masked }} · {{ item.memo || "无备注" }}
          </option>
        </select></label
      >
      <label v-if="!session && !useSaved" class="field"
        >{{ credentialLabel()
        }}<input
          v-model="cdk"
          class="input"
          type="password"
          autocomplete="new-password"
          required
      /></label>
    </div>
    <p v-if="!session && !savedCdks.length" class="muted">
      还没有保存 {{
        current?.name || provider
      }} 的 CDK；可在
      <NuxtLink to="/providers">提链服务 · 支付平台</NuxtLink>
      里保存后直接选用，也可以在此输入本次临时凭据。
    </p>
    <template v-if="session">
      <div class="form-grid">
        <label class="field"
          >OrderHub 数字账号<input
            v-model="username"
            class="input"
            inputmode="numeric"
            autocomplete="off"
        /></label>
        <label class="field"
          >登录密码<input
            v-model="password"
            class="input"
            type="password"
            autocomplete="new-password"
        /></label>
      </div>
      <div class="inline">
        <button
          type="button"
          class="btn btn-sm"
          @click="sessionAction('login')"
        >
          登录</button
        ><button
          type="button"
          class="btn btn-sm"
          @click="sessionAction('session')"
        >
          检查登录状态</button
        ><button
          type="button"
          class="btn btn-sm"
          @click="sessionAction('logout')"
        >
          退出登录
        </button>
      </div>
    </template>
    <div class="inline">
      <button type="button" class="btn btn-sm" @click="verify">
        {{ busy ? "验证中…" : "验证凭据 / 额度" }}</button
      ><a
        v-if="provider === 'orderhub'"
        href="https://upi.xxsyun.xyz"
        target="_blank"
        rel="noopener noreferrer"
        >OrderHub 账号中心</a
      ><a
        v-else-if="provider === 'seashore'"
        href="https://seashore.lol"
        target="_blank"
        rel="noopener noreferrer"
        >发布者平台</a
      >
    </div>
    <p v-if="provider === 'seashore'" class="muted">
      发布者 API 由后台读取所选账号的邮箱与完整 AT，提交该账号的零元 UPI
      支付链；没有幂等键，结果待核实时只查询、不重提。
    </p>
    <p v-if="queryOnly" class="muted">
      查询时请使用原支付平台的凭据，服务端会核对原任务。
    </p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
  </fieldset>
</template>

<style scoped>
.payment-fields {
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}
</style>
