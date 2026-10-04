<script setup lang="ts">
import {
  normalizeRedeemCode,
  recoveryKey,
  readRecovery,
  saveRecovery,
  newRecovery,
  type Recovery,
} from "~/utils/redemption";
definePageMeta({ layout: false });
useHead({
  title: "兑换中心 · Registrator",
  meta: [{ name: "referrer", content: "no-referrer" }],
});
const api = useApi(),
  toast = useToast();
const code = ref(""),
  quantity = ref(1),
  busy = ref(false),
  error = ref(""),
  stockError = ref(""),
  loading = ref(false);
const stock = ref<any[]>([]),
  results = ref<any[]>([]);
const completed = ref<{ code: string; id: string; remaining: number } | null>(
  null,
);
watch(code, () => {
  if (completed.value?.code !== normalizeRedeemCode(code.value))
    completed.value = null;
});
async function loadStock() {
  loading.value = true;
  stockError.value = "";
  try {
    stock.value = (await api.request("/api/redeem/public-stock")).groups || [];
  } catch (e: any) {
    stockError.value = e.message;
  } finally {
    loading.value = false;
  }
}
function downloadUrl(value: string) {
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin &&
      url.pathname.startsWith("/api/redeem/download/")
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
async function submit(nextBatch = false) {
  if (busy.value) return;
  error.value = "";
  if (!code.value.trim()) {
    error.value = "请输入兑换码";
    return;
  }
  if (
    !Number.isInteger(quantity.value) ||
    quantity.value < 1 ||
    quantity.value > 1000
  ) {
    error.value = "兑换数量须为 1–1000 的整数";
    return;
  }
  busy.value = true;
  let key = "";
  let state: Recovery | null = null;
  try {
    key = recoveryKey(code.value);
    state = readRecovery(key);
    if (nextBatch) {
      if (
        !completed.value ||
        completed.value.code !== normalizeRedeemCode(code.value) ||
        state?.request_id !== completed.value.id ||
        !state.completed
      )
        throw new Error("恢复记录已变化，请先恢复原结果。");
      state = newRecovery();
    }
    state ??= newRecovery();
    state.completed = false;
    saveRecovery(key, state);
    completed.value = null;
    const result = await api.request("/api/redeem", {
      method: "POST",
      body: {
        cdk: code.value.trim(),
        quantity: quantity.value,
        request_id: state.request_id,
      },
    });
    if (!result.download_url || !Array.isArray(result.credentials))
      throw new Error(
        "未收到完整兑换结果。请使用相同兑换码和数量恢复本批结果。",
      );
    const requestId = state.request_id;
    results.value = [
      { ...result, request_id: requestId },
      ...results.value.filter((r) => r.request_id !== requestId),
    ];
    try {
      saveRecovery(key, { ...state, completed: true });
      if (result.remaining > 0)
        completed.value = {
          code: normalizeRedeemCode(code.value),
          id: state.request_id,
          remaining: result.remaining,
        };
    } catch {
      error.value =
        "兑换已成功，但浏览器未能保存完成状态。请先保存下方凭据；再次提交将恢复本批结果。";
    }
    void loadStock();
  } catch (e: any) {
    error.value = e.message;
    if (
      state &&
      key &&
      e.status === 410 &&
      e.data?.code === "delivery_expired" &&
      e.data.remaining > 0
    ) {
      try {
        saveRecovery(key, { ...state, completed: true });
        completed.value = {
          code: normalizeRedeemCode(code.value),
          id: state.request_id,
          remaining: e.data.remaining,
        };
        error.value += `。原批次已过期，请联系管理员恢复；剩余 ${e.data.remaining} 个名额可继续兑换新批次。`;
      } catch {
        error.value += "。恢复信息保存失败，请联系管理员核对后再兑换。";
      }
    } else if (!e.status || e.status >= 500)
      error.value +=
        "。结果可能已受理，请使用相同兑换码和数量重试恢复，勿重复领取。";
  } finally {
    busy.value = false;
  }
}
async function copy(lines: string[]) {
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    toast.success("凭据已复制");
  } catch {
    toast.error("复制失败，请手动选中凭据复制");
  }
}
onMounted(loadStock);
</script>
<template>
  <div class="public-shell">
    <header class="public-header">
      <NuxtLink class="brand" to="/redeem"
        ><img
          src="/brand.svg"
          width="34"
          height="34"
          alt=""
        />Registrator</NuxtLink
      ><NuxtLink to="/login" class="muted">管理员登录 ↗</NuxtLink>
    </header>
    <main class="redeem-main">
      <div class="redeem-heading">
        <div class="eyebrow">READY WHEN YOU ARE</div>
        <h1>开启你的下一步。</h1>
        <p>输入兑换码，领取属于你的账号。</p>
      </div>
      <div class="redeem-columns">
        <section class="card">
          <div class="card-header">
            <h2>账号兑换</h2>
            <span class="badge badge-success"
              ><UiIcon name="shield" :size="12" />安全交付</span
            >
          </div>
          <form class="card-body stack" @submit.prevent="submit(false)">
            <label class="field"
              >兑换码<input
                v-model="code"
                class="input"
                autocomplete="off"
                placeholder="输入你的 CDK 兑换码"
                required
                :readonly="busy" /></label
            ><label class="field"
              >本次领取数量<input
                v-model.number="quantity"
                class="input"
                type="number"
                min="1"
                max="1000"
                required
                :readonly="busy"
              /><small>支持分次领取，每次最多 1000 个账号。</small></label
            >
            <div v-if="error" class="alert alert-error" role="alert">
              {{ error }}
            </div>
            <button class="btn btn-primary" :disabled="busy">
              {{ busy ? "正在准备你的账号…" : "兑换 / 恢复原结果"
              }}<UiIcon name="arrow" /></button
            ><button
              v-if="completed && completed.remaining > 0"
              type="button"
              class="btn"
              :disabled="busy"
              @click="submit(true)"
            >
              继续兑换下一批（剩余 {{ completed.remaining }} 个）
            </button>
            <p class="redeem-hint">
              重复提交会恢复上次结果。领取下一批请使用“继续兑换下一批”。兑换完成后请及时保存凭据。
            </p>
          </form>
        </section>
        <section class="card stock-card">
          <div class="card-header">
            <h2>可兑换库存</h2>
            <button
              class="icon-btn"
              :disabled="loading"
              aria-label="刷新库存"
              @click="loadStock"
            >
              <UiIcon name="refresh" />
            </button>
          </div>
          <div v-if="stockError" class="card-body muted" role="alert">
            库存暂时无法获取，不影响兑换。<button
              class="btn btn-sm"
              @click="loadStock"
            >
              重试
            </button>
          </div>
          <div v-else-if="stock.length" class="stock-list">
            <div v-for="item in stock" :key="item.group_name" class="stock-row">
              <span>{{ item.group_name }}</span
              ><span
                ><i class="status-dot" />{{ item.redeemable }}
                <small>个可用</small></span
              >
            </div>
          </div>
          <UiEmpty
            v-else
            :title="loading ? '正在查询库存…' : '暂无公开库存'"
            description="未公开的分组仍可凭有效兑换码领取。"
          />
          <p class="stock-note">库存以实际兑换结果为准</p>
        </section>
      </div>
      <section
        v-for="result in results"
        :key="result.request_id"
        class="card result-card"
      >
        <div class="card-header">
          <h2>
            {{ result.resumed ? "已恢复原批次" : "兑换成功" }} ·
            {{ result.count }} 个账号
          </h2>
          <UiBadge value="success" />
        </div>
        <div class="card-body">
          <p class="muted">
            {{ result.group_name }} · 剩余 {{ result.remaining }} 个名额 ·
            下载有效期至 {{ result.expires_at }}
          </p>
          <p class="muted">格式：邮箱---密码---2FA 密钥</p>
          <pre class="credentials" tabindex="0">{{
            result.credentials.join("\n")
          }}</pre>
          <div class="inline">
            <a
              v-if="downloadUrl(result.download_url)"
              :href="downloadUrl(result.download_url)"
              class="btn btn-primary"
              :download="result.filename"
              ><UiIcon name="download" />下载凭据</a
            ><button class="btn" @click="copy(result.credentials)">
              复制凭据
            </button>
          </div>
        </div>
      </section>
    </main>
    <footer class="public-footer">Registrator · 简单、可靠的账号交付</footer>
  </div>
</template>
<style scoped>
.redeem-main {
  width: 100%;
  max-width: 900px;
  margin: 35px auto 50px;
  padding: 0 24px;
}
.redeem-heading {
  text-align: center;
  margin: 15px 0 38px;
}
.redeem-heading h1 {
  font-size: 35px;
  letter-spacing: -1px;
  font-weight: 600;
}
.redeem-heading p {
  color: #91a299;
  font-size: 13px;
  margin-top: 12px;
}
.redeem-columns {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 22px;
  align-items: start;
}
.redeem-columns > .card {
  margin: 0;
}
.redeem-hint {
  font-size: 11px;
  color: #97a39d;
  line-height: 1.8;
}
.stock-list {
  padding: 5px 22px;
}
.stock-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 17px 0;
  border-bottom: 1px solid #eff3f0;
  font-size: 12px;
}
.stock-row:last-child {
  border: 0;
}
.stock-row span:last-child {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #34856a;
}
.stock-row small {
  font-size: 10px;
  color: #93a297;
}
.stock-note {
  padding: 15px 22px;
  font-size: 10px;
  color: #a3afa7;
  border-top: 1px solid #eff3f0;
}
.result-card {
  margin-top: 24px;
}
.redeem-main .alert {
  margin: 0;
}
.redeem-main .input {
  padding: 12px;
}
.redeem-main .btn-primary {
  padding: 12px;
}
.stock-card .empty-state {
  padding: 38px 18px;
}
@media (max-width: 680px) {
  .redeem-columns {
    grid-template-columns: 1fr;
  }
  .redeem-heading h1 {
    font-size: 28px;
  }
  .redeem-main {
    padding: 0 18px;
    margin-top: 20px;
  }
}
</style>
