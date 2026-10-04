<script setup lang="ts">
import {
  checkResult,
  copyText,
  errorMessage,
  formatTime,
  resultMessage,
} from "~/components/operations/helpers";
import type {
  ActionPrompt,
  OperationRow,
} from "~/components/operations/helpers";
useHead({ title: "邮箱池" });
const { request } = useApi();
const toast = useToast();
const sourceLabels: Record<string, string> = {
  all: "全部来源",
  outlook: "Outlook",
  generic_api: "通用 API",
  imap: "通用 IMAP",
  cloudflare_domain: "域名邮箱",
};
const statusLabels: Record<string, string> = {
  available: "可用",
  used: "已使用",
  failed: "失败",
  disabled: "停用",
};
const source = ref("all");
const status = ref("");
const search = ref("");
const query = ref("");
const rows = ref<OperationRow[]>([]);
const selected = ref<string[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(true);
const loaded = ref(false);
const error = ref("");
const busy = ref(false);
const confirmation = shallowRef<ActionPrompt | null>(null);
const importOpen = ref(false);
const importSource = ref("outlook");
const importText = ref("");
const importRegistered = ref(false);
const imapServer = ref("");
const imapPort = ref(993);
const imapSsl = ref(true);
const importError = ref("");
const importResult = ref("");
const fileReading = ref(false);
const importVisible = computed({
  get: () => importOpen.value,
  set: (value) => {
    if (!busy.value && !fileReading.value) importOpen.value = value;
  },
});
const fileName = ref("");
const statusTargets = ref<OperationRow[]>([]);
const statusValue = ref("available");
const statusNote = ref("");
const statusError = ref("");
const statusOpen = computed({
  get: () => statusTargets.value.length > 0,
  set: (value) => {
    if (!value && !busy.value) statusTargets.value = [];
  },
});
const key = (row: OperationRow) => `${row.source}|${row.email}`;
const selectedRows = computed(() =>
  rows.value.filter((row) => selected.value.includes(key(row))),
);
const allSelected = computed(
  () =>
    rows.value.length > 0 &&
    rows.value.every((row) => selected.value.includes(key(row))),
);
const importHint = computed(() =>
  importSource.value === "imap"
    ? "邮箱----IMAP密码 或 邮箱:IMAP密码"
    : importSource.value === "generic_api"
      ? "邮箱----取码地址"
      : "邮箱----密码----clientId----refreshToken",
);
const importValid = computed(
  () =>
    importText.value.trim() &&
    (importSource.value !== "imap" ||
      (imapServer.value.trim() &&
        Number.isInteger(imapPort.value) &&
        imapPort.value >= 1 &&
        imapPort.value <= 65535)),
);
let revision = 0;
async function load() {
  const current = ++revision;
  loading.value = true;
  try {
    const result = checkResult(
      await request("/api/outlook", {
        query: {
          paged: 1,
          page: page.value,
          page_size: pageSize.value,
          source: source.value,
          status: status.value || undefined,
          q: query.value,
        },
      }),
    );
    if (current !== revision) return;
    rows.value = result.items || [];
    total.value = Number(result.total || 0);
    error.value = "";
    loaded.value = true;
    selected.value = selected.value.filter((value) =>
      rows.value.some((row) => key(row) === value),
    );
    const last = Math.max(1, Math.ceil(total.value / pageSize.value));
    if (page.value > last) page.value = last;
  } catch (cause) {
    if (current === revision) error.value = errorMessage(cause);
  } finally {
    if (current === revision) loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => {
  revision++;
});
watch(page, () => {
  selected.value = [];
  void load();
});
watch([source, status, query, pageSize], () => {
  selected.value = [];
  if (page.value !== 1) page.value = 1;
  else void load();
});
function applySearch() {
  const next = search.value.trim();
  if (next === query.value) void load();
  else query.value = next;
}
function toggleAll() {
  selected.value = allSelected.value ? [] : rows.value.map(key);
}
async function perform(path: string, body: OperationRow, message: string) {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = checkResult(await request(path, { method: "POST", body }));
    if (path === "/api/outlook/delete" && !result.deleted)
      throw new Error("邮箱不存在或来源不匹配");
    toast.success(resultMessage(result, message));
    selected.value = [];
    await load();
    return result;
  } finally {
    busy.value = false;
  }
}
function deleteRows(targets: OperationRow[]) {
  if (!targets.length) return;
  const items = targets.map((row) => ({
    email: row.email,
    source: row.source,
  }));
  confirmation.value = {
    title: `删除 ${items.length} 个邮箱`,
    danger: true,
    label: "删除邮箱",
    description: `${items.length === 1 ? items[0]?.email : `已选中 ${items.length} 个邮箱`}\n将从邮箱池移除这些记录，此操作无法撤销。`,
    run: () =>
      items.length === 1
        ? perform("/api/outlook/delete", items[0]!, "邮箱已删除")
        : perform(
            "/api/outlook/delete-bulk",
            { items, source: source.value },
            "已删除",
          ),
  };
}
function editStatus(targets: OperationRow[]) {
  statusTargets.value = targets.map((row) => ({
    email: row.email,
    source: row.source,
  }));
  statusValue.value =
    targets.length === 1 && statusLabels[targets[0]?.status]
      ? targets[0]?.status
      : "available";
  statusNote.value = "";
  statusError.value = "";
}
async function saveStatus() {
  const items = statusTargets.value;
  if (!items.length || busy.value) return;
  try {
    const payload = {
      status: statusValue.value,
      note:
        statusNote.value.trim() || `手动标记${statusLabels[statusValue.value]}`,
    };
    await perform(
      items.length === 1 ? "/api/outlook/status" : "/api/outlook/status-bulk",
      items.length === 1 ? { ...items[0], ...payload } : { items, ...payload },
      "状态已更新",
    );
    statusTargets.value = [];
  } catch (cause) {
    statusError.value = errorMessage(cause);
  }
}
function openImport() {
  importSource.value = ["outlook", "generic_api", "imap"].includes(source.value)
    ? source.value
    : "outlook";
  importError.value = "";
  importResult.value = "";
  importOpen.value = true;
}
async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  fileReading.value = true;
  importError.value = "";
  try {
    importText.value = await file.text();
    fileName.value = file.name;
  } catch (cause) {
    importError.value = `读取文件失败：${errorMessage(cause)}`;
  } finally {
    fileReading.value = false;
    input.value = "";
  }
}
async function submitImport() {
  if (!importValid.value || busy.value || fileReading.value) return;
  busy.value = true;
  importError.value = "";
  importResult.value = "";
  try {
    const payload: OperationRow = {
      source: importSource.value,
      text: importText.value,
      as_registered: importRegistered.value,
    };
    if (importSource.value === "imap")
      Object.assign(payload, {
        imap_server: imapServer.value.trim(),
        imap_port: imapPort.value,
        imap_ssl: imapSsl.value,
      });
    const result = checkResult(
      await request("/api/outlook/import", { method: "POST", body: payload }),
    );
    importResult.value = `导入完成：解析 ${result.parsed} 行，新增 ${result.inserted}，跳过 ${result.skipped}。${result.as_registered ? "已作为注册账号导入，可前往 Codex 页面补跑授权。" : ""}`;
    toast.success(`导入完成，新增 ${result.inserted} 条`);
    importText.value = "";
    fileName.value = "";
    selected.value = [];
    page.value = 1;
    source.value = importSource.value;
    await load();
  } catch (cause) {
    importError.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}
async function copy(value: string) {
  try {
    await copyText(value);
    toast.success("已复制");
  } catch (cause) {
    toast.error(errorMessage(cause));
  }
}
</script>

<template>
  <div class="stack mailbox-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">RESOURCES / MAILBOXES</p>
        <h1 class="page-title">邮箱池</h1>
        <p class="page-description">统一管理邮箱来源，为下一次注册做好准备。</p>
      </div>
      <button class="btn btn-primary" :disabled="busy" @click="openImport">
        <span aria-hidden="true">＋</span> 导入邮箱
      </button>
    </header>
    <section class="mailbox-intro">
      <div class="mailbox-mark" aria-hidden="true">@</div>
      <div>
        <strong>多个来源，一个邮箱池</strong>
        <p>
          支持 Outlook、通用 API、IMAP 与域名邮箱。导入后即可在注册任务中使用。
        </p>
      </div>
      <NuxtLink class="btn btn-sm" to="/">前往注册 ↗</NuxtLink>
    </section>
    <section class="card">
      <form class="toolbar filter-toolbar" @submit.prevent="applySearch">
        <label class="field search-field"
          ><span class="sr-only">搜索邮箱</span
          ><input
            v-model="search"
            class="input"
            type="search"
            placeholder="搜索邮箱、备注或来源…"
        /></label>
        <label class="field"
          ><span class="sr-only">邮箱来源</span
          ><select v-model="source" class="select" aria-label="邮箱来源">
            <option
              v-for="(label, value) in sourceLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
            </option>
          </select></label
        >
        <label class="field"
          ><span class="sr-only">邮箱状态</span
          ><select v-model="status" class="select" aria-label="邮箱状态">
            <option value="">全部状态</option>
            <option
              v-for="(label, value) in statusLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
            </option>
          </select></label
        >
        <button type="submit" class="btn" :disabled="loading">搜索</button
        ><button type="button" class="btn" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </form>
      <div class="toolbar selection-toolbar">
        <span class="muted"
          >共 {{ total }} 个邮箱 · 已选 {{ selected.length }} 个</span
        ><button
          class="btn btn-sm"
          :disabled="busy || !selected.length"
          @click="editStatus(selectedRows)"
        >
          批量修改状态</button
        ><button
          class="btn btn-sm btn-danger"
          :disabled="busy || !selected.length"
          @click="deleteRows(selectedRows)"
        >
          删除选中</button
        ><button
          class="btn btn-sm"
          :disabled="!rows.length"
          @click="
            copy(rows.map((row) => row.copy_line || row.email).join('\n'))
          "
        >
          复制本页素材</button
        ><label class="inline page-size"
          >每页<select
            v-model.number="pageSize"
            class="select"
            aria-label="每页邮箱数"
          >
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option></select
          >条</label
        >
      </div>
      <div v-if="error" class="alert alert-error table-alert" role="alert">
        {{ error }}
        <button class="btn btn-sm" :disabled="loading" @click="load">
          重新加载
        </button>
      </div>
      <div v-if="loading && !loaded" class="empty-state" role="status">
        正在加载邮箱池…
      </div>
      <UiEmpty
        v-else-if="!rows.length && !error"
        :title="
          query || status || source !== 'all'
            ? '没有匹配的邮箱'
            : '邮箱池还没有素材'
        "
        :description="
          query || status || source !== 'all'
            ? '试试其他搜索词或筛选条件。'
            : '导入文本或文件，开始管理你的邮箱。'
        "
      />
      <div v-else-if="rows.length" class="table-wrap" :aria-busy="loading">
        <table class="data-table">
          <thead>
            <tr>
              <th class="check-cell">
                <input
                  type="checkbox"
                  :checked="allSelected"
                  :indeterminate="selected.length > 0 && !allSelected"
                  :disabled="busy || !rows.length"
                  aria-label="选择本页邮箱"
                  @change="toggleAll"
                />
              </th>
              <th>邮箱</th>
              <th>来源</th>
              <th>状态</th>
              <th>导入时间</th>
              <th>使用时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="key(row)">
              <td>
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="key(row)"
                  :disabled="busy"
                  :aria-label="`选择 ${row.email}`"
                />
              </td>
              <td>
                <strong class="email">{{ row.email }}</strong
                ><small
                  v-if="row.note"
                  class="cell-sub note"
                  :title="row.note"
                  >{{ row.note }}</small
                >
              </td>
              <td>{{ sourceLabels[row.source] || row.source }}</td>
              <td><UiBadge :value="row.status" /></td>
              <td class="time-cell">
                {{ formatTime(row.imported_at || row.created_at) }}
              </td>
              <td class="time-cell">{{ formatTime(row.used_at) }}</td>
              <td>
                <div class="row-actions">
                  <button
                    class="btn btn-sm"
                    @click="copy(row.copy_line || row.email)"
                  >
                    复制素材</button
                  ><button
                    v-if="row.access_token"
                    class="btn btn-sm"
                    @click="copy(row.access_token)"
                  >
                    Token</button
                  ><button
                    v-if="row.account_copy_line"
                    class="btn btn-sm"
                    @click="copy(row.account_copy_line)"
                  >
                    复制账号</button
                  ><button
                    class="btn btn-sm"
                    :disabled="busy"
                    @click="editStatus([row])"
                  >
                    改状态</button
                  ><button
                    class="btn btn-sm btn-danger"
                    :disabled="busy"
                    @click="deleteRows([row])"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <UiPagination v-model:page="page" :total="total" :page-size="pageSize" />
    </section>
    <OperationsActionDialog v-model:action="confirmation" />
    <UiModal v-model:open="statusOpen" title="修改邮箱状态">
      <form id="mailbox-status-form" class="stack" @submit.prevent="saveStatus">
        <p class="muted">
          {{
            statusTargets.length === 1
              ? statusTargets[0]?.email
              : `已选择 ${statusTargets.length} 个邮箱`
          }}
        </p>
        <label class="field"
          >目标状态<select
            v-model="statusValue"
            class="select"
            :disabled="busy"
          >
            <option
              v-for="(label, value) in statusLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
            </option>
          </select></label
        ><label class="field"
          >备注（可选）<input
            v-model="statusNote"
            class="input"
            :disabled="busy"
            placeholder="记录本次变更的原因"
        /></label>
        <div v-if="statusError" class="alert alert-error" role="alert">
          {{ statusError }}
        </div>
      </form>
      <template #footer
        ><button class="btn" :disabled="busy" @click="statusOpen = false">
          取消</button
        ><button
          class="btn btn-primary"
          type="submit"
          form="mailbox-status-form"
          :disabled="busy"
        >
          {{ busy ? "保存中…" : "保存状态" }}
        </button></template
      >
    </UiModal>
    <UiModal v-model:open="importVisible" title="导入邮箱">
      <form
        id="mailbox-import-form"
        class="stack"
        @submit.prevent="submitImport"
      >
        <div class="form-grid">
          <label class="field"
            >邮箱类型<select
              v-model="importSource"
              class="select"
              :disabled="busy || fileReading"
            >
              <option value="outlook">Outlook</option>
              <option value="generic_api">通用 API</option>
              <option value="imap">通用 IMAP</option>
            </select></label
          ><label class="field"
            >从文件读取<input
              class="input file-input"
              type="file"
              accept=".txt,.csv,.log,text/plain,text/csv"
              :disabled="busy || fileReading"
              @change="readFile"
            /><small v-if="fileName" class="muted">{{ fileName }}</small></label
          >
        </div>
        <div v-if="importSource === 'imap'" class="form-grid">
          <label class="field"
            >IMAP 服务器<input
              v-model="imapServer"
              class="input"
              placeholder="imap.example.com"
              required
              :disabled="busy" /></label
          ><label class="field"
            >端口<input
              v-model.number="imapPort"
              class="input"
              type="number"
              min="1"
              max="65535"
              required
              :disabled="busy" /></label
          ><label class="inline"
            ><input v-model="imapSsl" type="checkbox" :disabled="busy" />使用
            SSL</label
          >
        </div>
        <label class="field"
          >邮箱内容<textarea
            v-model="importText"
            class="textarea mono import-text"
            rows="8"
            :placeholder="importHint"
            :disabled="busy || fileReading"
            required
          /><small class="muted"
            >每行一条，支持 ---- 或 ==== 分隔。{{ importHint }}。</small
          ></label
        >
        <label class="inline registered-option"
          ><input
            v-model="importRegistered"
            type="checkbox"
            :disabled="busy"
          />作为已注册账号导入，供后续补跑 Codex 授权</label
        >
        <div v-if="fileReading" class="alert" role="status">正在读取文件…</div>
        <div v-if="importError" class="alert alert-error" role="alert">
          {{ importError }}
        </div>
        <div v-if="importResult" class="alert" role="status">
          {{ importResult }}
          <NuxtLink v-if="importRegistered" to="/codex">前往 Codex →</NuxtLink>
        </div>
      </form>
      <template #footer
        ><button
          class="btn"
          :disabled="busy || fileReading"
          @click="importOpen = false"
        >
          关闭</button
        ><button
          class="btn btn-primary"
          type="submit"
          form="mailbox-import-form"
          :disabled="busy || fileReading || !importValid"
        >
          {{ busy ? "导入中…" : "确认导入" }}
        </button></template
      >
    </UiModal>
  </div>
</template>

<style scoped>
.mailbox-page {
  gap: 24px;
}
.mailbox-page > .page-header {
  margin-bottom: 0;
}
.mailbox-intro {
  display: flex;
  gap: 16px;
  align-items: center;
  padding: 22px 24px;
  background: var(--surface, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 14px;
}
.mailbox-mark {
  display: grid;
  place-items: center;
  width: 45px;
  height: 45px;
  flex-shrink: 0;
  background: var(--accent-light, #eaf6f2);
  color: var(--accent, #0f766e);
  border-radius: 12px;
  font-size: 25px;
}
.mailbox-intro strong {
  font-size: 14px;
}
.mailbox-intro p {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-secondary, #64748b);
  line-height: 1.7;
}
.mailbox-intro > .btn {
  margin-left: auto;
  flex-shrink: 0;
}
.filter-toolbar {
  padding: 20px;
  gap: 10px;
}
.search-field {
  flex: 1;
  min-width: 200px;
}
.selection-toolbar {
  padding: 12px 20px;
  gap: 8px;
  border-block: 1px solid var(--border, #e5e7eb);
  font-size: 12px;
}
.page-size {
  margin-left: auto;
}
.page-size select {
  width: 70px;
}
.table-alert {
  margin: 16px 20px;
}
.check-cell {
  width: 40px;
}
.email {
  font-weight: 550;
}
.cell-sub {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-secondary, #64748b);
}
.note {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.time-cell {
  font-size: 12px;
  white-space: nowrap;
}
.row-actions {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  min-width: 170px;
}
.import-text {
  min-height: 180px;
  resize: vertical;
  font-size: 12px;
}
.registered-option {
  font-size: 12px;
  line-height: 1.7;
}
.file-input {
  font-size: 12px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}
@media (max-width: 650px) {
  .mailbox-intro {
    flex-wrap: wrap;
  }
  .mailbox-intro > .btn {
    margin-left: 61px;
  }
  .page-size {
    margin-left: 0;
  }
}
</style>
