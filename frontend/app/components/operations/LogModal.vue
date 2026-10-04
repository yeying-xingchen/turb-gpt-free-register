<script setup lang="ts">
import { checkResult, copyText, errorMessage } from "./helpers";
import type { OperationRow } from "./helpers";
const props = withDefaults(
  defineProps<{
    endpoint: string;
    title?: string;
    query?: Record<string, any>;
  }>(),
  { title: "任务日志" },
);
const open = defineModel<boolean>("open", { default: false });
const { request } = useApi();
const toast = useToast();
const log = ref("");
const job = ref<OperationRow | null>(null);
const loading = ref(false);
const failure = ref("");
const complete = ref(false);
const auto = ref(true);
const follow = ref(true);
const content = ref<HTMLElement | null>(null);
let revision = 0;
async function load() {
  if (!open.value || !props.endpoint) return;
  const current = ++revision;
  loading.value = true;
  try {
    const result = checkResult(
      await request(props.endpoint, { query: props.query }),
    );
    if (current !== revision || !open.value) return;
    log.value = result.log || "";
    job.value = result.job || null;
    complete.value = result.complete !== false;
    failure.value = "";
    if (follow.value) {
      await nextTick();
      content.value?.scrollTo({ top: content.value.scrollHeight });
    }
  } catch (error) {
    if (current === revision) failure.value = errorMessage(error);
  } finally {
    if (current === revision) loading.value = false;
  }
}
watch(
  () => [open.value, props.endpoint, JSON.stringify(props.query)],
  () => {
    revision++;
    log.value = "";
    job.value = null;
    complete.value = false;
    failure.value = "";
    loading.value = false;
    if (open.value) void load();
  },
);
usePolling(() => {
  if (auto.value && !loading.value) return load();
}, 5000);
onBeforeUnmount(() => {
  revision++;
});
async function copy() {
  try {
    await copyText(log.value);
    toast.success("日志已复制");
  } catch (error) {
    toast.error(errorMessage(error));
  }
}
function download() {
  if (!log.value) return;
  const blob = new Blob([log.value], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${job.value?.id || "task-log"}.log`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <UiModal v-model:open="open" :title="title">
    <div class="stack">
      <div class="toolbar log-toolbar">
        <label class="inline"
          ><input v-model="auto" type="checkbox" />自动刷新</label
        >
        <label class="inline"
          ><input v-model="follow" type="checkbox" />跟随最新日志</label
        >
        <button class="btn btn-sm" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
        <button class="btn btn-sm" :disabled="!log" @click="copy">
          复制日志
        </button>
        <button class="btn btn-sm" :disabled="!log" @click="download">
          下载完整日志
        </button>
        <span v-if="complete && log" class="muted log-complete"
          >完整日志 · {{ log.split("\n").length }} 行</span
        >
      </div>
      <div v-if="job" class="log-detail">
        <div class="inline">
          <UiBadge :value="job.display_status || job.status" /><span>{{
            job.email || "尚未分配邮箱"
          }}</span>
        </div>
        <p
          v-if="job.error_message || job.progress_message || job.stage"
          class="muted"
        >
          {{ job.error_message || job.progress_message || job.stage }}
        </p>
      </div>
      <div v-if="failure" class="alert alert-error" role="alert">
        {{ failure }}
      </div>
      <pre
        ref="content"
        class="log-content"
        tabindex="0"
        aria-label="任务日志内容"
        >{{
          log ||
          (loading ? "正在读取日志…" : "暂无日志，任务开始后会在这里显示。")
        }}</pre>
    </div>
    <template #footer
      ><button class="btn" @click="open = false">关闭</button></template
    >
  </UiModal>
</template>

<style scoped>
.log-toolbar {
  gap: 12px;
  font-size: 12px;
}
.log-complete {
  margin-left: auto;
  white-space: nowrap;
}
.log-detail {
  padding: 12px 14px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 10px;
  font-size: 13px;
}
.log-detail p {
  margin: 8px 0 0;
  overflow-wrap: anywhere;
}
.log-content {
  margin: 0;
  min-height: 260px;
  max-height: 52vh;
  overflow: auto;
  background: #0e1729;
  color: #d8e6f8;
  border-radius: 12px;
  padding: 18px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font:
    12px/1.85 ui-monospace,
    SFMono-Regular,
    Menlo,
    monospace;
}
</style>
