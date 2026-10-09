<script setup lang="ts">
useHead({ title: "系统配置 · Registrator" });
interface Field {
  key: string;
  group: string;
  label: string;
  help: string;
  type: string;
  secret?: boolean;
  value: any;
  choices?: { value: string; label: string }[];
}
const api = useApi(),
  toast = useToast();
const fields = ref<Field[]>([]),
  values = reactive<Record<string, any>>({}),
  saved = ref<Record<string, any>>({});
const activeGroup = ref(""),
  search = ref(""),
  loading = ref(true),
  busy = ref(false),
  error = ref("");
const groups = computed(() => [...new Set(fields.value.map((f) => f.group))]);
const shown = computed(() =>
  fields.value.filter((f) =>
    search.value
      ? `${f.label} ${f.key} ${f.help} ${f.group}`
          .toLowerCase()
          .includes(search.value.toLowerCase())
      : f.group === activeGroup.value,
  ),
);
const changed = computed(() =>
  fields.value.filter(
    (f) => JSON.stringify(values[f.key]) !== JSON.stringify(saved.value[f.key]),
  ),
);
function asInput(field: Field) {
  return Array.isArray(field.value)
    ? field.value.join("\n")
    : (field.value ?? (field.type === "bool" ? false : ""));
}
function isSecret(field: Field) {
  return (
    field.secret || /PASSWORD|SECRET|TOKEN|API_KEY|AUTH_CODE/.test(field.key)
  );
}
async function load() {
  loading.value = true;
  error.value = "";
  try {
    fields.value = await api.request<Field[]>("/api/config");
    for (const field of fields.value) values[field.key] = asInput(field);
    saved.value = { ...values };
    if (!activeGroup.value) activeGroup.value = groups.value[0] || "";
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function save() {
  busy.value = true;
  try {
    const updates: Record<string, any> = {};
    for (const f of changed.value) {
      const value = values[f.key];
      if (
        ["int", "float"].includes(f.type) &&
        (value === "" ||
          !Number.isFinite(Number(value)) ||
          (f.type === "int" && !Number.isInteger(Number(value))))
      )
        throw new Error(
          `${f.label}需要填写有效${f.type === "int" ? "整数" : "数值"}`,
        );
      updates[f.key] = ["int", "float"].includes(f.type)
        ? Number(value)
        : f.type === "list_str_multiline"
          ? String(value)
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean)
          : value;
    }
    const result = await api.request("/api/config", {
      method: "POST",
      body: { updates },
    });
    if (result.ignored?.length)
      toast.info(`以下字段未保存：${result.ignored.join("、")}`);
    toast.success(result.note || "配置已保存");
    await load();
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
const helperResult = ref(""),
  helperOpen = ref(false);
const {
  status: updateStatus,
  loading: updateLoading,
  applying: updateApplying,
  refresh: refreshUpdate,
  checkNow: checkUpdateNow,
  applyUpdate: applyUpdateNow,
} = useUpdateStatus();
const updating = ref(false);
const updateBadge = computed(() => {
  const s = updateStatus.value;
  if (s.checking || updateLoading.value) return { kind: "badge-info", label: "检查中…" };
  if (!s.checked_at) return { kind: "badge-neutral", label: "尚未检查" };
  if (s.ok === false) return { kind: "badge-danger", label: "检查失败" };
  if (s.update_available)
    return {
      kind: "badge-warning",
      label:
        typeof s.behind === "number" && s.behind > 0
          ? `有 ${s.behind} 个新提交`
          : "有新版本",
    };
  return { kind: "badge-success", label: "已是最新" };
});
async function checkUpdate() {
  try {
    await checkUpdateNow();
    const s = updateStatus.value;
    if (s.update_available) toast.info(`${updateBadge.value.label}，可在下方一键更新`);
    else if (s.ok === false) toast.error(s.error || "检查更新失败");
    else toast.success("当前已是最新版本");
  } catch (e: any) {
    toast.error(e.message);
  }
}
async function runUpdate() {
  const s = updateStatus.value;
  const source = `${s.source?.remote || "origin"}/${s.source?.branch || "main"}`;
  if (
    !window.confirm(
      `将执行 git fetch + git merge --ff-only（${source}）。\n` +
        "只允许快进合并，不会丢弃本地改动。确认更新？",
    )
  )
    return;
  updating.value = true;
  try {
    const result = await applyUpdateNow();
    toast.success(result.message || "更新完成");
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    updating.value = false;
  }
}
async function helper(action: string) {
  if (
    changed.value.length &&
    !window.confirm("此操作会使用当前配置并可能保存 CloudMail 配置，是否继续？")
  )
    return;
  busy.value = true;
  try {
    const result = await api.request(
      action === "roxy" ? "/api/roxy/workspaces" : `/api/cloudmail/${action}`,
      action === "roxy"
        ? {}
        : {
            method: "POST",
            body: {
              api_base: values.CLOUDMAIL_API_BASE,
              email: values.CLOUDMAIL_ADMIN_EMAIL,
              password: values.CLOUDMAIL_PASSWORD,
              token: values.CLOUDMAIL_AUTH_TOKEN,
              path: values.CLOUDMAIL_TOKEN_PATH,
            },
          },
    );
    if (action === "roxy") {
      helperResult.value = JSON.stringify(result, null, 2);
      helperOpen.value = true;
    } else {
      toast.success(result.message || "操作成功");
      await load();
    }
  } catch (e: any) {
    toast.error(e.message);
  } finally {
    busy.value = false;
  }
}
function preventLoss(event: BeforeUnloadEvent) {
  if (changed.value.length) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(() => {
  void load();
  void refreshUpdate();
  window.addEventListener("beforeunload", preventLoss);
});
onBeforeUnmount(() => window.removeEventListener("beforeunload", preventLoss));
onBeforeRouteLeave(
  () => !changed.value.length || window.confirm("还有未保存的配置，确定离开？"),
);
</script>
<template>
  <div>
    <div class="page-header">
      <div>
        <div class="eyebrow">PREFERENCES & INTEGRATIONS</div>
        <h1 class="page-title">系统配置</h1>
        <p class="page-description">
          管理注册方式、邮箱服务与第三方集成。配置按分组组织，保存后应用到新任务。
        </p>
      </div>
      <button
        class="btn btn-primary"
        :disabled="!changed.length || busy"
        @click="save"
      >
        <UiIcon name="check" />{{
          busy
            ? "正在保存…"
            : `保存更改${changed.length ? `（${changed.length}）` : ""}`
        }}
      </button>
    </div>
    <div v-if="error" class="alert alert-error" role="alert">
      {{ error }} <button class="btn btn-sm" @click="load">重新加载</button>
    </div>
    <section class="card update-card">
      <div class="card-header">
        <h2>版本与更新</h2>
        <span class="badge" :class="updateBadge.kind"
          ><span class="badge-dot" />{{ updateBadge.label }}</span
        >
      </div>
      <div class="card-body">
        <div class="update-grid">
          <div class="update-cell">
            <span class="muted">当前版本</span>
            <strong class="mono">{{
              updateStatus.local?.short || "未知版本"
            }}</strong>
            <p>{{ updateStatus.local?.subject || "未获取到本地提交信息" }}</p>
            <small class="muted"
              >{{ updateStatus.local?.branch || "-" }} ·
              {{ updateStatus.local?.date || "-" }}</small
            >
          </div>
          <div class="update-cell">
            <span class="muted">远端最新</span>
            <strong class="mono">{{
              updateStatus.remote?.short || "未检查"
            }}</strong>
            <p>{{ updateStatus.remote?.subject || "点击「立即检查」获取远端提交" }}</p>
            <small class="muted"
              >{{ updateStatus.source?.repo || "-" }}@{{
                updateStatus.source?.branch || "-"
              }}
              <template v-if="updateStatus.release?.tag">
                · 最新发布 {{ updateStatus.release.tag }}</template
              ></small
            >
          </div>
        </div>
        <p v-if="updateStatus.ok === false" class="alert alert-error" role="alert">
          {{ updateStatus.error }}
        </p>
        <details
          v-if="updateStatus.update_available && updateStatus.commits?.length"
          class="update-commits"
          open
        >
          <summary>
            待更新提交（{{ updateStatus.commits.length
            }}<template v-if="updateStatus.commits_approx">，近似列表</template>）
          </summary>
          <ul>
            <li v-for="commit in updateStatus.commits" :key="commit.sha">
              <code>{{ commit.short }}</code
              ><span>{{ commit.subject }}</span
              ><small class="muted">{{ commit.author }} · {{ commit.date }}</small>
            </li>
          </ul>
        </details>
        <p v-if="updateStatus.local?.dirty" class="muted update-note">
          工作区有 {{ updateStatus.local.dirty }} 个未提交改动，一键更新已禁用；请先提交或暂存。
        </p>        <div class="inline update-actions">
          <button class="btn" :disabled="updateLoading" @click="checkUpdate">
            <UiIcon name="refresh" />{{
              updateStatus.checking || updateLoading ? "正在检查…" : "立即检查"
            }}
          </button>
          <button
            v-if="updateStatus.update_available"
            class="btn btn-primary"
            :disabled="updating || !updateStatus.apply?.allowed"
            :title="updateStatus.apply?.allowed ? '' : updateStatus.apply?.reason"
            @click="runUpdate"
          >
            <UiIcon name="download" />{{ updating ? "正在更新…" : "一键更新" }}
          </button>
          <a
            v-if="updateStatus.source?.html_url"
            class="btn btn-ghost"
            :href="updateStatus.source.html_url"
            target="_blank"
            rel="noopener"
            ><UiIcon name="link" />在 GitHub 查看</a
          >
          <span class="muted update-meta"
            >上次检查：{{ updateStatus.checked_at_text || "尚未检查" }} · 自动检查{{
              updateStatus.enabled ? `每 ${updateStatus.interval_hours} 小时` : "已关闭"
            }}</span
          >
        </div>
      </div>
    </section>
    <div class="settings-layout">
      <aside class="card settings-nav">
        <div class="settings-search">
          <input
            v-model="search"
            class="input"
            placeholder="搜索配置项…"
            aria-label="搜索配置项"
          />
        </div>
        <nav aria-label="配置分组">
          <button
            v-for="group in groups"
            :key="group"
            :class="{ active: activeGroup === group && !search }"
            @click="
              activeGroup = group;
              search = '';
            "
          >
            {{ group
            }}<span>{{ fields.filter((f) => f.group === group).length }}</span>
          </button>
        </nav>
      </aside>
      <section class="card settings-panel">
        <div class="card-header">
          <h2>
            {{ search ? `搜索结果 · ${shown.length}` : activeGroup || "配置" }}
          </h2>
          <span class="muted">{{
            changed.length ? "有未保存的更改" : "已与服务器同步"
          }}</span>
        </div>
        <UiEmpty v-if="loading" title="正在读取配置…" description="" /><UiEmpty
          v-else-if="!shown.length"
          title="未找到配置项"
          description="试试其他名称或关键字。"
        />
        <form v-else class="settings-fields" @submit.prevent="save">
          <label v-for="field in shown" :key="field.key" class="setting-field"
            ><div>
              <strong>{{ field.label }}</strong>
              <p>{{ field.help }}</p>
              <code>{{ field.key }}</code>
            </div>
            <input
              v-if="field.type === 'bool'"
              v-model="values[field.key]"
              type="checkbox"
              :aria-label="field.label" /><select
              v-else-if="field.choices?.length"
              v-model="values[field.key]"
              class="select"
              :aria-label="field.label"
            >
              <option
                v-for="choice in field.choices"
                :key="choice.value"
                :value="choice.value"
              >
                {{ choice.label }}
              </option></select
            ><textarea
              v-else-if="
                field.type === 'list_str_multiline' ||
                /DOMAINS|POOL|PATTERNS/.test(field.key)
              "
              v-model="values[field.key]"
              class="textarea"
              :aria-label="field.label"
              rows="3" /><input
              v-else
              v-model="values[field.key]"
              class="input"
              :type="
                isSecret(field)
                  ? 'password'
                  : ['int', 'float'].includes(field.type)
                    ? 'number'
                    : 'text'
              "
              :step="field.type === 'float' ? 'any' : '1'"
              :aria-label="field.label"
              autocomplete="off"
          /></label>
          <div v-if="activeGroup === 'RoxyBrowser' && !search" class="inline">
            <button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('roxy')"
            >
              获取团队与项目
            </button>
          </div>
          <div v-if="activeGroup === 'CloudMail' && !search" class="inline">
            <button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('gen-token')"
            >
              生成授权 Token</button
            ><button
              class="btn"
              type="button"
              :disabled="busy"
              @click="helper('domains')"
            >
              获取可用域名
            </button>
          </div>
        </form>
      </section>
    </div>
    <UiModal v-model:open="helperOpen" title="团队与项目">
      <pre class="log">{{ helperResult }}</pre>
    </UiModal>
  </div>
</template>
<style scoped>
.update-card {
  margin-bottom: 22px;
}
.update-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 18px;
}
.update-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
  border: 1px solid #eff2f5;
  border-radius: 6px;
  background: #fbfcfd;
}
.update-cell strong {
  font-size: 13px;
}
.update-cell p {
  font-size: 11px;
  line-height: 1.7;
  color: #7a8797;
  overflow-wrap: anywhere;
}
.update-cell small {
  font-size: 10px;
}
.update-commits {
  margin-top: 16px;
  font-size: 11px;
}
.update-commits summary {
  cursor: pointer;
  color: #7a8797;
}
.update-commits ul {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  max-height: 240px;
  overflow: auto;
}
.update-commits li {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: baseline;
  padding: 7px 0;
  border-bottom: 1px solid #f2f5f7;
}
.update-commits li:last-child {
  border-bottom: 0;
}
.update-commits code {
  font-size: 10px;
  color: #8592a1;
}
.update-commits span {
  overflow-wrap: anywhere;
}
.update-commits small {
  font-size: 10px;
  white-space: nowrap;
}
.update-note {
  margin-top: 14px;
  font-size: 11px;
}
.update-actions {
  margin-top: 18px;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.update-meta {
  font-size: 11px;
}
@media (max-width: 760px) {
  .update-commits li {
    grid-template-columns: 62px minmax(0, 1fr);
  }
  .update-commits small {
    grid-column: 2;
  }
}
.settings-layout {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.settings-nav {
  position: sticky;
  top: 20px;
  box-shadow: none;
}
.settings-search {
  padding: 14px;
}
.settings-nav nav {
  padding: 0 8px 12px;
  max-height: 65vh;
  overflow: auto;
}
.settings-nav button {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  background: none;
  border: 0;
  border-radius: 5px;
  padding: 10px;
  color: #7a8797;
  text-align: left;
  font-size: 12px;
}
.settings-nav button.active {
  background: var(--accent-light);
  color: var(--accent);
}
.settings-nav button span {
  font-size: 10px;
  color: #a3adb7;
}
.settings-panel {
  margin: 0 !important;
}
.settings-fields {
  padding: 0 25px 24px;
}
.setting-field {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(190px, 42%);
  align-items: center;
  gap: 30px;
  padding: 24px 0;
  border-bottom: 1px solid #eff2f5;
}
.setting-field:last-child {
  border-bottom: 0;
}
.setting-field strong {
  font-size: 12px;
  font-weight: 500;
}
.setting-field p {
  font-size: 11px;
  line-height: 1.8;
  color: #96a0ae;
  margin-top: 5px;
  overflow-wrap: anywhere;
}
.setting-field code {
  font-size: 9px;
  color: #b2bac3;
  overflow-wrap: anywhere;
}
.setting-field input[type="checkbox"] {
  justify-self: end;
  width: 18px;
  height: 18px;
}
.settings-fields > .inline {
  padding-top: 20px;
}
@media (max-width: 1000px) {
  .settings-layout {
    grid-template-columns: 170px minmax(0, 1fr);
  }
  .setting-field {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .setting-field input[type="checkbox"] {
    justify-self: start;
  }
}
@media (max-width: 760px) {
  .settings-layout {
    grid-template-columns: 1fr;
  }
  .settings-nav {
    position: static;
  }
  .settings-nav nav {
    display: flex;
    max-height: none;
    overflow: auto;
    padding: 0 10px 10px;
  }
  .settings-nav button {
    white-space: nowrap;
    gap: 15px;
    width: auto;
  }
  .settings-fields {
    padding-inline: 18px;
  }
}
</style>
