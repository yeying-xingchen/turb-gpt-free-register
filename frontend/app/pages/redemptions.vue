<script setup lang="ts">
useHead({ title: "兑换管理 · Registrator" });
const api = useApi(),
  toast = useToast();
const codes = ref<any[]>([]),
  groups = ref<any[]>([]),
  stock = ref<any>({}),
  loading = ref(true),
  error = ref(""),
  busy = ref(false);
const search = ref(""),
  page = ref(1),
  createOpen = ref(false),
  groupOpen = ref(false),
  details = ref<any>(null);
const actionError = ref(""),
  editingGroup = ref(false);
watch([createOpen, groupOpen], () => {
  actionError.value = "";
});
const form = reactive({
  account_group: "",
  quantity: 1,
  expires_in_days: 0,
  note: "",
});
const groupForm = reactive({
  group_name: "",
  redeem_prefix: "",
  public_stock: false,
});
const filtered = computed(() =>
  codes.value.filter((c) =>
    `${c.code} ${c.account_group} ${c.note}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
);
const rows = computed(() =>
  filtered.value.slice((page.value - 1) * 30, page.value * 30),
);
watch(search, () => {
  page.value = 1;
});
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.request("/api/redeem/codes", {
      query: { limit: "all" },
    });
    codes.value = data.items;
    groups.value = data.groups;
    stock.value = data.stock;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function create() {
  busy.value = true;
  try {
    await api.request("/api/redeem/codes", { method: "POST", body: form });
    createOpen.value = false;
    toast.success("兑换码已生成");
    await load();
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
async function revoke(item: any) {
  if (!window.confirm(`撤销兑换码 ${item.code}？未兑换名额将不可使用。`))
    return;
  busy.value = true;
  try {
    await api.request(`/api/redeem/codes/${item.id}/revoke`, {
      method: "POST",
    });
    toast.success("兑换码已撤销");
    await load();
  } catch (e: any) {
    actionError.value = e.message;
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
async function copy(value: string) {
  try {
    await navigator.clipboard.writeText(value);
    toast.success("已复制");
  } catch {
    toast.error("复制失败，请手动选中复制");
  }
}
function editGroup(item?: any) {
  editingGroup.value = !!item;
  Object.assign(groupForm, {
    group_name: item?.group_name || "",
    redeem_prefix: item?.redeem_prefix || "",
    public_stock: item?.public_stock || false,
  });
  groupOpen.value = true;
}
async function saveGroup() {
  busy.value = true;
  try {
    await api.request("/api/account-groups/meta", {
      method: "POST",
      body: groupForm,
    });
    groupOpen.value = false;
    toast.success("分组配置已保存");
    await load();
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
        <div class="eyebrow">REDEMPTION & DELIVERY</div>
        <h1 class="page-title">兑换管理</h1>
        <p class="page-description">
          为账号分组生成兑换码，管理库存与交付记录。
        </p>
      </div>
      <div class="inline">
        <NuxtLink class="btn" to="/redeem"
          ><UiIcon name="gift" />公开兑换页</NuxtLink
        ><button class="btn btn-primary" @click="createOpen = true">
          <UiIcon name="plus" />生成兑换码
        </button>
      </div>
    </div>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">可兑换账号<UiIcon name="users" /></div>
        <strong class="stat-value">{{ stock.available ?? "—" }}</strong
        ><small>当前可交付库存</small>
      </div>
      <div class="stat-card">
        <div class="stat-label">账号分组<UiIcon name="grid" /></div>
        <strong class="stat-value">{{ groups.length }}</strong
        ><small>独立管理库存与前缀</small>
      </div>
      <div class="stat-card">
        <div class="stat-label">有效兑换码<UiIcon name="gift" /></div>
        <strong class="stat-value">{{
          codes.filter((c) => c.status === "active").length
        }}</strong
        ><small>可继续领取的兑换码</small>
      </div>
      <div class="stat-card">
        <div class="stat-label">已交付账号<UiIcon name="check" /></div>
        <strong class="stat-value">{{
          codes.reduce((n, c) => n + c.redeemed_count, 0)
        }}</strong
        ><small>累计兑换成功</small>
      </div>
    </div>
    <div v-if="error" class="alert alert-error" role="alert">
      {{ error }}<button class="btn btn-sm" @click="load">重试</button>
    </div>
    <section class="card">
      <div class="card-header">
        <h2>兑换码</h2>
        <button class="btn btn-sm" :disabled="loading" @click="load">
          <UiIcon name="refresh" />刷新
        </button>
      </div>
      <div class="toolbar">
        <input
          v-model="search"
          class="input"
          placeholder="搜索兑换码、分组或备注"
          aria-label="搜索兑换码"
        />
      </div>
      <UiEmpty
        v-if="!rows.length"
        :title="loading ? '正在加载…' : '暂无兑换码'"
        description="先创建账号分组，再生成绑定分组的兑换码。"
      />
      <div v-else class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>兑换码</th>
              <th>分组</th>
              <th>状态</th>
              <th>已领取 / 总额</th>
              <th>有效期</th>
              <th>备注</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in rows" :key="item.id">
              <td>
                <button
                  class="code-button mono"
                  title="复制兑换码"
                  @click="copy(item.code)"
                >
                  {{ item.code }}
                </button>
              </td>
              <td>{{ item.account_group }}</td>
              <td><UiBadge :value="item.status" /></td>
              <td>
                {{ item.redeemed_count }}
                <span class="muted">/ {{ item.quantity }}</span>
              </td>
              <td class="muted">{{ item.expires_at || "永久有效" }}</td>
              <td class="wrap muted">{{ item.note || "—" }}</td>
              <td>
                <div class="inline">
                  <button class="btn btn-sm" @click="details = item">
                    记录</button
                  ><button
                    class="btn btn-sm btn-danger"
                    :disabled="busy || item.status !== 'active'"
                    @click="revoke(item)"
                  >
                    撤销
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiPagination
        v-model:page="page"
        :total="filtered.length"
        :page-size="30"
      />
    </section>
    <section class="card">
      <div class="card-header">
        <h2>分组与公开库存</h2>
        <button class="btn btn-sm" @click="editGroup()">
          <UiIcon name="plus" />创建分组
        </button>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>分组名称</th>
              <th>账号数</th>
              <th>可兑换</th>
              <th>CDK 前缀</th>
              <th>公开库存</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="group in groups" :key="group.group_name">
              <td>{{ group.group_name }}</td>
              <td>{{ group.total }}</td>
              <td>{{ group.redeemable }}</td>
              <td class="mono">{{ group.redeem_prefix || "CDK" }}</td>
              <td>{{ group.public_stock ? "公开" : "不展示" }}</td>
              <td>
                <button class="btn btn-sm" @click="editGroup(group)">
                  编辑
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiEmpty
        v-if="!groups.length"
        title="还没有分组"
        description="创建分组后，可在账号页将账号分配到对应分组。"
      />
    </section>
    <UiModal v-model:open="createOpen" title="生成兑换码"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="create-code" class="stack" @submit.prevent="create">
        <label class="field"
          >兑换分组<select v-model="form.account_group" class="select" required>
            <option value="" disabled>请选择分组</option>
            <option
              v-for="g in groups"
              :key="g.group_name"
              :value="g.group_name"
            >
              {{ g.group_name }}（可用 {{ g.redeemable }}）
            </option>
          </select></label
        >
        <div class="form-grid">
          <label class="field"
            >兑换名额<input
              v-model.number="form.quantity"
              type="number"
              min="1"
              max="1000"
              required
              class="input" /></label
          ><label class="field"
            >有效天数<input
              v-model.number="form.expires_in_days"
              type="number"
              min="0"
              max="3650"
              required
              class="input"
            /><small>0 表示永久有效</small></label
          >
        </div>
        <label class="field"
          >备注<input v-model="form.note" class="input" maxlength="200"
        /></label>
      </form>
      <template #footer
        ><button class="btn" @click="createOpen = false">取消</button
        ><button
          class="btn btn-primary"
          form="create-code"
          :disabled="busy || !form.account_group"
        >
          {{ busy ? "正在生成…" : "生成兑换码" }}
        </button></template
      ></UiModal
    ><UiModal v-model:open="groupOpen" title="分组配置"
      ><p v-if="actionError" class="alert alert-error" role="alert">
        {{ actionError }}
      </p>
      <form id="group-form" class="stack" @submit.prevent="saveGroup">
        <label class="field"
          >分组名称<input
            v-model="groupForm.group_name"
            class="input"
            required
            :readonly="editingGroup" /></label
        ><label class="field"
          >兑换码前缀<input
            v-model="groupForm.redeem_prefix"
            class="input"
            maxlength="16"
            placeholder="留空使用 CDK" /></label
        ><label class="inline"
          ><input
            v-model="groupForm.public_stock"
            type="checkbox"
          />在公开兑换页展示库存</label
        >
      </form>
      <template #footer
        ><button class="btn btn-primary" form="group-form" :disabled="busy">
          保存配置
        </button></template
      ></UiModal
    ><UiModal
      :open="!!details"
      title="交付记录"
      @update:open="!$event && (details = null)"
      ><template v-if="details"
        ><p class="mono">{{ details.code }}</p>
        <div
          v-for="claim in details.redeemed_accounts"
          :key="claim.id"
          class="claim"
        >
          <span>{{ claim.email }}</span
          ><small class="muted">{{ claim.claimed_at }}</small>
        </div>
        <UiEmpty
          v-if="!details.redeemed_accounts?.length"
          title="尚未领取"
          description="用户兑换成功后会在此显示交付记录。" /></template
    ></UiModal>
  </div>
</template>
<style scoped>
.code-button {
  border: 0;
  background: none;
  padding: 0;
  color: #3b7e70;
  text-align: left;
}
.code-button:hover {
  text-decoration: underline;
}
.claim {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 0;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  font-size: 12px;
}
</style>
