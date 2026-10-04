<script setup lang="ts">
import PaymentProviders from "~/components/accounts/PaymentProviders.vue";
useHead({ title: "服务与凭据 · Registrator" });
const api = useApi(),
  toast = useToast();
const kind = ref<"extract" | "payment">("extract");
const providers = ref<any[]>([]),
  loading = ref(true),
  busy = ref(false),
  error = ref(""),
  activeId = ref<number | null>(null);
const active = computed(() =>
  providers.value.find((p) => p.id === activeId.value),
);
const actionError = ref("");
const providerOpen = ref(false),
  cdkOpen = ref(false),
  editingId = ref<number | null>(null),
  editingCdk = ref<number | null>(null),
  validation = ref(""),
  resultOpen = ref(false);
watch([providerOpen, cdkOpen], () => {
  actionError.value = "";
});
const providerForm = reactive({
  name: "",
  provider_type: "lumen",
  api_base: "",
  default_link_type: "ideal",
  enabled: true,
  is_default: false,
  note: "",
});
const cdkForm = reactive({ cdk: "", memo: "", enabled: true });
const methods = computed(() =>
  providerForm.provider_type === "upi_git5"
    ? ["upi"]
    : providerForm.provider_type === "extract"
      ? ["pix", "upi", "kakao_pay", "ideal"]
      : [
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
);
watch(
  () => providerForm.provider_type,
  () => {
    if (!methods.value.includes(providerForm.default_link_type))
      providerForm.default_link_type = methods.value[0]!;
  },
);
async function load() {
  loading.value = true;
  error.value = "";
  try {
    providers.value =
      (await api.request("/api/extract-link/providers")).items || [];
    if (!providers.value.some((p) => p.id === activeId.value))
      activeId.value = providers.value[0]?.id ?? null;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
function openProvider(item?: any) {
  editingId.value = item?.id ?? null;
  Object.assign(providerForm, {
    name: item?.name || "",
    provider_type: item?.provider_type || "lumen",
    api_base: item?.api_base || "",
    default_link_type: item?.default_link_type || "ideal",
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
      `/api/extract-link/providers${editingId.value == null ? "" : `/${editingId.value}`}`,
      { method: editingId.value == null ? "POST" : "PUT", body: providerForm },
    );
    activeId.value = data.item.id;
    providerOpen.value = false;
    toast.success("供应商已保存");
    await load();
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
async function removeProvider() {
  if (
    !active.value ||
    !window.confirm(`删除供应商「${active.value.name}」及其 CDK？`)
  )
    return;
  busy.value = true;
  try {
    await api.request(`/api/extract-link/providers/${active.value.id}`, {
      method: "DELETE",
    });
    toast.success("供应商已删除");
    await load();
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
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
        ? `/api/extract-link/providers/${active.value.id}/cdks`
        : `/api/extract-link/cdks/${editingCdk.value}`,
      { method: editingCdk.value == null ? "POST" : "PUT", body },
    );
    cdkOpen.value = false;
    toast.success("CDK 已保存");
    await load();
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
async function cdkAction(item: any, action: string) {
  if (action === "delete" && !window.confirm("删除这条 CDK？")) return;
  busy.value = true;
  try {
    const data = await api.request(
      `/api/extract-link/cdks/${item.id}${action === "validate" ? "/validate" : ""}`,
      { method: action === "delete" ? "DELETE" : "POST" },
    );
    if (action === "validate") {
      validation.value = JSON.stringify(data.result, null, 2);
      resultOpen.value = true;
    } else {
      toast.success("CDK 已删除");
      await load();
    }
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>
<template>
  <div>
    <div class="page-header">
      <div>
        <div class="eyebrow">PROVIDERS & CREDENTIALS</div>
        <h1 class="page-title">服务与凭据</h1>
        <p class="page-description">
          配置提链供应商与支付平台，保存各自的 CDK，在账号管理中直接选用。
        </p>
      </div>
      <button
        v-if="kind === 'extract'"
        class="btn btn-primary"
        @click="openProvider()"
      >
        <UiIcon name="plus" />添加供应商
      </button>
    </div>
    <div class="inline provider-tabs" role="tablist">
      <button
        class="btn btn-sm"
        :class="{ 'btn-primary': kind === 'extract' }"
        role="tab"
        :aria-selected="kind === 'extract'"
        @click="kind = 'extract'"
      >
        提链供应商
      </button>
      <button
        class="btn btn-sm"
        :class="{ 'btn-primary': kind === 'payment' }"
        role="tab"
        :aria-selected="kind === 'payment'"
        @click="kind = 'payment'"
      >
        支付平台
      </button>
    </div>
    <PaymentProviders v-if="kind === 'payment'" />
    <template v-else>
      <div v-if="error" class="alert alert-error" role="alert">
        {{ error }} <button class="btn btn-sm" @click="load">重试</button>
      </div>
      <div class="providers-layout">
        <section class="card providers-list">
          <div class="card-header">
            <h2>服务供应商</h2>
            <button
              class="icon-btn"
              :disabled="loading"
              aria-label="刷新供应商"
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
                >{{ item.provider_type }} ·
                {{ item.enabled ? "已启用" : "已停用" }}</small
              ></span
            ><span v-if="item.is_default" class="default-mark">默认</span></button
          ><UiEmpty
            v-if="!providers.length"
            :title="loading ? '加载中…' : '暂无供应商'"
            description="添加一个供应商开始配置。"
          />
        </section>
      <section v-if="active" class="card provider-detail">
        <div class="card-header">
          <div>
            <h2>{{ active.name }}</h2>
            <p class="muted">{{ active.api_base }}</p>
          </div>
          <div class="inline">
            <UiBadge :value="active.enabled ? 'enabled' : 'disabled'" /><button
              v-if="active.id > 0"
              class="btn btn-sm"
              @click="openProvider(active)"
            >
              编辑
            </button>
          </div>
        </div>
        <div class="provider-meta">
          <div>
            <span>服务类型</span><strong>{{ active.provider_type }}</strong>
          </div>
          <div>
            <span>默认支付方式</span
            ><strong>{{ active.default_link_type }}</strong>
          </div>
          <div>
            <span>已保存 CDK</span
            ><strong>{{ active.cdks?.length || 0 }}</strong>
          </div>
        </div>
        <div v-if="active.note" class="card-body muted">{{ active.note }}</div>
        <div class="toolbar">
          <strong>服务凭据</strong><span class="muted">仅展示脱敏尾号</span
          ><button
            v-if="active.id > 0"
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
                <td class="mono">•••• {{ item.display_suffix }}</td>
                <td>{{ item.memo || "—" }}</td>
                <td>
                  <UiBadge :value="item.enabled ? 'enabled' : 'disabled'" />
                </td>
                <td>
                  <div class="inline">
                    <button
                      v-if="item.id > 0"
                      class="btn btn-sm"
                      :disabled="busy"
                      @click="cdkAction(item, 'validate')"
                    >
                      查询额度</button
                    ><button
                      v-if="item.id > 0"
                      class="btn btn-sm"
                      :disabled="busy"
                      @click="openCdk(item)"
                    >
                      编辑</button
                    ><button
                      v-if="item.id > 0"
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
          :description="
            active.id === 0
              ? '此兼容供应商由系统配置管理。'
              : '添加后可在账号操作中直接选择，无需反复输入。'
          "
        />
        <div class="card-body inline">
          <NuxtLink to="/accounts" class="btn"
            ><UiIcon name="users" />前往账号管理</NuxtLink
          ><NuxtLink v-if="active.id === 0" to="/settings" class="btn"
            >打开系统配置</NuxtLink
          ><button
            v-else
            class="btn btn-danger btn-sm"
            :disabled="busy"
            style="margin-left: auto"
            @click="removeProvider"
          >
            删除供应商
          </button>
        </div>
      </section>
    </div>
    </template>
    <UiModal
      v-model:open="providerOpen"
      :title="editingId == null ? '添加供应商' : '编辑供应商'"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="provider-form" class="stack" @submit.prevent="saveProvider">
        <div class="form-grid">
          <label class="field"
            >名称<input
              v-model="providerForm.name"
              class="input"
              required
              maxlength="120" /></label
          ><label class="field"
            >服务类型<select
              v-model="providerForm.provider_type"
              class="select"
            >
              <option value="lumen">Lumen Flow</option>
              <option value="extract">Extract</option>
              <option value="upi_git5">UPI-GIT5</option>
            </select></label
          >
        </div>
        <label class="field"
          >API 地址<input
            v-model="providerForm.api_base"
            class="input"
            type="url"
            placeholder="https://api.example.com"
            required /></label
        ><label class="field"
          >默认支付方式<select
            v-model="providerForm.default_link_type"
            class="select"
          >
            <option v-for="method in methods" :key="method" :value="method">
              {{ method }}
            </option>
          </select></label
        ><label class="field"
          >备注<input
            v-model="providerForm.note"
            class="input"
            maxlength="2000"
        /></label>
        <div class="inline">
          <label
            ><input v-model="providerForm.enabled" type="checkbox" />
            启用供应商</label
          ><label
            ><input v-model="providerForm.is_default" type="checkbox" />
            设为默认</label
          >
        </div>
      </form>
      <template #footer
        ><button class="btn" @click="providerOpen = false">取消</button
        ><button class="btn btn-primary" form="provider-form" :disabled="busy">
          保存供应商
        </button></template
      ></UiModal
    ><UiModal
      v-model:open="cdkOpen"
      :title="editingCdk == null ? '添加 CDK' : '编辑 CDK'"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="cdk-form" class="stack" @submit.prevent="saveCdk">
        <label class="field"
          >CDK<input
            v-model="cdkForm.cdk"
            type="password"
            autocomplete="off"
            class="input"
            :required="editingCdk == null"
            :placeholder="
              editingCdk == null ? '输入服务凭据' : '留空保持现有凭据'
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
        ><button class="btn btn-primary" form="cdk-form" :disabled="busy">
          保存 CDK
        </button></template
      ></UiModal
    ><UiModal v-model:open="resultOpen" title="CDK 查询结果">
      <pre class="log">{{ validation }}</pre>
    </UiModal>
  </div>
</template>
<style scoped>
.provider-tabs {
  margin-bottom: 16px;
}
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
