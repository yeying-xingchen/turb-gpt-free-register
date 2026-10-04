<script setup lang="ts">
import { accountError } from "~/utils/accounts";
const api = useApi();
const toast = useToast();
const providers = ref<any[]>([]);
const loading = ref(true);
const busy = ref(false);
const error = ref("");
const activeId = ref<number | null>(null);
const active = computed(() =>
  providers.value.find((item) => item.id === activeId.value),
);
const providerOpen = ref(false);
const cdkOpen = ref(false);
const editingId = ref<number | null>(null);
const editingCdk = ref<number | null>(null);
const actionError = ref("");
const validation = ref("");
const resultOpen = ref(false);
const providerForm = reactive({
  name: "",
  provider_type: "seashore",
  api_base: "",
  enabled: true,
  is_default: false,
  note: "",
});
const cdkForm = reactive({ cdk: "", memo: "", enabled: true });
watch([providerOpen, cdkOpen], () => {
  actionError.value = "";
});
const types = [
  { value: "v1", label: "Astra Scan Workbench" },
  { value: "masi", label: "Masi" },
  { value: "orderhub", label: "UPI OrderHub" },
  { value: "seashore", label: "seashore 发布者 API" },
];
const apiHint = computed(() =>
  providerForm.provider_type === "v1"
    ? "https://scan-qr.hixinghai.com/api/v1"
    : providerForm.provider_type === "masi"
      ? "https://masi.cc.cd"
      : providerForm.provider_type === "orderhub"
        ? "https://upi.xxsyun.xyz/api/v1"
        : "https://seashore.lol/api/publisher",
);
async function load() {
  loading.value = true;
  error.value = "";
  try {
    providers.value = (await api.request("/api/payment-providers")).items || [];
    if (!providers.value.some((item) => item.id === activeId.value))
      activeId.value = providers.value[0]?.id ?? null;
  } catch (cause) {
    error.value = accountError(cause);
  } finally {
    loading.value = false;
  }
}
function openProvider(item?: any) {
  editingId.value = item?.id ?? null;
  Object.assign(providerForm, {
    name: item?.name || "",
    provider_type: item?.provider_type || "seashore",
    api_base: item?.api_base || "",
    enabled: item?.enabled ?? true,
    is_default: item?.is_default ?? false,
    note: item?.note || "",
  });
  providerOpen.value = true;
}
async function saveProvider() {
  busy.value = true;
  try {
    const data = await api.request(
      `/api/payment-providers${editingId.value == null ? "" : `/${editingId.value}`}`,
      { method: editingId.value == null ? "POST" : "PUT", body: providerForm },
    );
    activeId.value = data.item.id;
    providerOpen.value = false;
    toast.success("支付平台已保存");
    await load();
  } catch (cause) {
    actionError.value = accountError(cause);
    toast.error(actionError.value);
  } finally {
    busy.value = false;
  }
}
async function removeProvider() {
  if (
    !active.value ||
    !window.confirm(`删除支付平台「${active.value.name}」及其已保存的 CDK？`)
  )
    return;
  busy.value = true;
  try {
    await api.request(`/api/payment-providers/${active.value.id}`, {
      method: "DELETE",
    });
    toast.success("支付平台已删除");
    await load();
  } catch (cause) {
    actionError.value = accountError(cause);
    toast.error(actionError.value);
  } finally {
    busy.value = false;
  }
}
function openCdk(item?: any) {
  editingCdk.value = item?.id ?? null;
  Object.assign(cdkForm, {
    cdk: "",
    memo: item?.memo || "",
    enabled: item?.enabled ?? true,
  });
  cdkOpen.value = true;
}
watch(cdkOpen, (open) => {
  if (!open) cdkForm.cdk = "";
});
async function saveCdk() {
  if (!active.value) return;
  busy.value = true;
  try {
    const body: Record<string, any> = {
      memo: cdkForm.memo,
      enabled: cdkForm.enabled,
    };
    if (cdkForm.cdk) body.cdk = cdkForm.cdk;
    await api.request(
      editingCdk.value == null
        ? `/api/payment-providers/${active.value.id}/cdks`
        : `/api/payment-cdks/${editingCdk.value}`,
      { method: editingCdk.value == null ? "POST" : "PUT", body },
    );
    cdkOpen.value = false;
    toast.success("支付 CDK 已保存");
    await load();
  } catch (cause) {
    actionError.value = accountError(cause);
    toast.error(actionError.value);
  } finally {
    busy.value = false;
  }
}
async function cdkAction(item: any, action: string) {
  if (action === "delete" && !window.confirm("删除这条支付 CDK？")) return;
  busy.value = true;
  try {
    const data = await api.request(
      `/api/payment-cdks/${item.id}${action === "validate" ? "/validate" : ""}`,
      { method: action === "delete" ? "DELETE" : "POST" },
    );
    if (action === "validate") {
      validation.value = JSON.stringify(data.result?.data ?? data.result, null, 2);
      resultOpen.value = true;
    } else {
      toast.success("支付 CDK 已删除");
      await load();
    }
  } catch (cause) {
    actionError.value = accountError(cause);
    toast.error(actionError.value);
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>

<template>
  <div>
    <div v-if="error" class="alert alert-error" role="alert">
      {{ error }} <button class="btn btn-sm" @click="load">重试</button>
    </div>
    <div class="providers-layout">
      <section class="card providers-list">
        <div class="card-header">
          <h2>支付平台</h2>
          <button
            class="icon-btn"
            :disabled="loading"
            aria-label="刷新支付平台"
            @click="load"
          >
            <UiIcon name="refresh" />
          </button>
        </div>
        <button
          v-for="item in providers"
          :key="item.id"
          class="provider-item"
          :class="{ active: item.id === activeId }"
          @click="activeId = item.id"
        >
          <span class="provider-icon"><UiIcon name="link" /></span
          ><span
            ><strong>{{ item.name }}</strong
            ><small
              >{{ item.label }} ·
              {{ item.enabled ? "已启用" : "已停用" }} ·
              {{ item.cdks?.length || 0 }} 条 CDK</small
            ></span
          ><span v-if="item.is_default" class="default-mark">默认</span></button
        ><UiEmpty
          v-if="!providers.length"
          :title="loading ? '加载中…' : '暂无支付平台'"
          description="默认会创建 Astra、Masi、OrderHub 和 seashore 四个平台。"
        />
      </section>
      <section v-if="active" class="card provider-detail">
        <div class="card-header">
          <div>
            <h2>{{ active.name }}</h2>
            <p class="muted">{{ active.effective_api_base }}</p>
          </div>
          <div class="inline">
            <UiBadge :value="active.enabled ? 'enabled' : 'disabled'" /><button
              class="btn btn-sm"
              @click="openProvider(active)"
            >
              编辑
            </button>
          </div>
        </div>
        <div class="provider-meta">
          <div>
            <span>平台标识</span><strong>{{ active.provider_type }}</strong>
          </div>
          <div>
            <span>已保存 CDK</span
            ><strong>{{ active.cdks?.length || 0 }}</strong>
          </div>
          <div>
            <span>登录会话</span
            ><strong>{{ active.supports_session ? "支持" : "不支持" }}</strong>
          </div>
        </div>
        <div v-if="active.note" class="card-body muted">{{ active.note }}</div>
        <div class="toolbar">
          <strong>已保存的支付 CDK</strong
          ><span class="muted">仅展示掩码尾号，明文只留在服务端</span
          ><button
            class="btn btn-sm"
            style="margin-left: auto"
            @click="openCdk()"
          >
            <UiIcon name="plus" />添加 CDK
          </button>
        </div>
        <div v-if="active.cdks?.length" class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>CDK</th>
                <th>备注</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in active.cdks" :key="item.id">
                <td class="mono">{{ item.masked || `•••• ${item.display_suffix}` }}</td>
                <td>{{ item.memo || "—" }}</td>
                <td>
                  <UiBadge :value="item.enabled ? 'enabled' : 'disabled'" />
                </td>
                <td>
                  <div class="inline">
                    <button
                      class="btn btn-sm"
                      :disabled="busy"
                      @click="cdkAction(item, 'validate')"
                    >
                      查询额度</button
                    ><button
                      class="btn btn-sm"
                      :disabled="busy"
                      @click="openCdk(item)"
                    >
                      编辑</button
                    ><button
                      class="btn btn-sm btn-danger"
                      :disabled="busy"
                      @click="cdkAction(item, 'delete')"
                    >
                      删除
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <UiEmpty
          v-else
          title="尚未保存 CDK"
          description="添加后可在提交支付、开通 Plus 窗口里直接选择，不必每次粘贴。"
        />
        <div class="card-body inline">
          <NuxtLink v-if="active.provider_type === 'seashore'" to="/settings" class="btn"
            >打开支付提交配置</NuxtLink
          ><button
            class="btn btn-danger btn-sm"
            :disabled="busy"
            style="margin-left: auto"
            @click="removeProvider"
          >
            删除支付平台
          </button>
        </div>
      </section>
    </div>
    <UiModal
      v-model:open="providerOpen"
      :title="editingId == null ? '添加支付平台' : '编辑支付平台'"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="payment-provider-form" class="stack" @submit.prevent="saveProvider">
        <div class="form-grid">
          <label class="field"
            >名称<input
              v-model="providerForm.name"
              class="input"
              required
              maxlength="120" /></label
          ><label class="field"
            >平台<select v-model="providerForm.provider_type" class="select">
              <option v-for="item in types" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select></label
          >
        </div>
        <label class="field"
          >API 地址<input
            v-model="providerForm.api_base"
            class="input"
            type="url"
            :placeholder="apiHint" /></label
        ><label class="field"
          >备注<input
            v-model="providerForm.note"
            class="input"
            maxlength="2000"
        /></label>
        <div class="inline">
          <label
            ><input v-model="providerForm.enabled" type="checkbox" />
            启用平台</label
          ><label
            ><input v-model="providerForm.is_default" type="checkbox" />
            设为默认</label
          >
        </div>
      </form>
      <template #footer
        ><button class="btn" @click="providerOpen = false">取消</button
        ><button class="btn btn-primary" form="payment-provider-form" :disabled="busy">
          保存平台
        </button></template
      ></UiModal
    ><UiModal
      v-model:open="cdkOpen"
      :title="editingCdk == null ? '添加支付 CDK' : '编辑支付 CDK'"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="payment-cdk-form" class="stack" @submit.prevent="saveCdk">
        <label class="field"
          >CDK<input
            v-model="cdkForm.cdk"
            type="password"
            autocomplete="off"
            class="input"
            :required="editingCdk == null"
            :placeholder="
              editingCdk == null ? '例如 PBK-XXXX-XXXX-XXXX-XXXX' : '留空保持现有凭据'
            " /></label
        ><label class="field"
          >备注<input
            v-model="cdkForm.memo"
            class="input"
            maxlength="500" /></label
        ><label class="inline"
          ><input v-model="cdkForm.enabled" type="checkbox" />启用此凭据</label
        >
      </form>
      <template #footer
        ><button class="btn btn-primary" form="payment-cdk-form" :disabled="busy">
          保存 CDK
        </button></template
      ></UiModal
    ><UiModal v-model:open="resultOpen" title="支付额度 / 统计">
      <pre class="log">{{ validation }}</pre>
    </UiModal>
  </div>
</template>

<style scoped>
.providers-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.provider-detail {
  margin: 0 !important;
}
.provider-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 20px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: white;
  text-align: left;
}
.provider-item.active {
  background: #f2f9f6;
}
.provider-item strong {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: #445d56;
}
.provider-item small {
  display: block;
  color: #99aaa2;
  font-size: 10px;
  margin-top: 4px;
}
.provider-icon {
  display: flex;
  padding: 10px;
  background: #eaf2ee;
  border-radius: 8px;
  color: #6a9a88;
}
.default-mark {
  margin-left: auto;
  font-size: 9px;
  color: var(--accent);
}
.provider-meta {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  padding: 24px;
  gap: 18px;
  border-bottom: 1px solid var(--border);
}
.provider-meta span {
  display: block;
  font-size: 10px;
  color: #93a19a;
}
.provider-meta strong {
  display: block;
  font-size: 14px;
  font-weight: 500;
  margin-top: 6px;
}
.provider-detail .toolbar strong {
  font-size: 12px;
}
.provider-detail .card-header p {
  margin-top: 6px;
  overflow-wrap: anywhere;
}
@media (max-width: 1000px) {
  .providers-layout {
    grid-template-columns: 1fr;
  }
  .providers-list {
    display: flex;
    flex-wrap: wrap;
  }
  .providers-list > .card-header {
    width: 100%;
  }
  .provider-item {
    width: auto;
    flex: 1;
    min-width: 220px;
  }
}
</style>
