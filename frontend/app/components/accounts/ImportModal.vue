<script setup lang="ts">
import {
  accountError,
  accountResult,
  accountResultDetails,
} from "~/utils/accounts";
const emit = defineEmits<{ close: []; imported: [] }>();
const { request } = useApi();
const toast = useToast();
const text = ref("");
const busy = ref(false);
const error = ref("");
const result = ref<any>(null);
const fileName = ref("");
const open = computed({
  get: () => true,
  set: (value: boolean) => {
    if (!value && !busy.value) emit("close");
  },
});
const lines = computed(
  () => text.value.split(/\r?\n/).filter((value) => value.trim()).length,
);
async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  error.value = "";
  try {
    if (file.size > 10 * 1024 * 1024)
      throw new Error("单次导入文件不能超过 10 MiB");
    text.value = await file.text();
    fileName.value = file.name;
    result.value = null;
  } catch (cause) {
    error.value = accountError(cause);
  } finally {
    input.value = "";
  }
}
async function submit() {
  if (busy.value) return;
  error.value = "";
  result.value = null;
  try {
    if (!text.value.trim()) throw new Error("请粘贴账号内容或选择文本文件");
    if (lines.value > 5000) throw new Error("单次最多导入 5000 行");
    if (new Blob([text.value]).size > 10 * 1024 * 1024)
      throw new Error("单次导入内容不能超过 10 MiB");
    busy.value = true;
    result.value = await request("/api/accounts/import", {
      method: "POST",
      body: { text: text.value },
    });
    if (result.value.ok === false)
      throw new Error(result.value.error || "导入失败");
    toast.success(accountResult(result.value));
    text.value = "";
    fileName.value = "";
    emit("imported");
  } catch (cause) {
    error.value = accountError(cause);
    const details = (cause as any)?.data;
    if (details) result.value = details;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <UiModal v-model:open="open" title="导入已有账号">
    <form id="accounts-import" class="stack" @submit.prevent="submit">
      <p class="muted">
        每行一个账号，格式：邮箱--密码--2FA--AT；分隔符可用 --、--- 或 ----。
        四个字段均不能为空。最多
        5000 行 / 10 MiB。
      </p>
      <label class="field"
        >从文本文件导入<input
          type="file"
          accept=".txt,.csv,text/plain,text/csv"
          :disabled="busy"
          @change="readFile"
        /><small v-if="fileName" class="muted"
          >已读取 {{ fileName }}，可在下方修改。</small
        ></label
      >
      <label class="field"
        >账号内容<textarea
          v-model="text"
          class="textarea mono import-text"
          rows="9"
          :disabled="busy"
          spellcheck="false"
          autocomplete="off"
          placeholder="user@example.com--password--2FA_SECRET--access_token"
        />
      </label>
      <small class="muted"
        >{{
          lines
        }}
        行。提交成功后清空输入；关闭窗口会清除尚未提交的内容。</small
      >
      <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
      <section v-if="result" class="import-result" aria-live="polite">
        <strong>{{ accountResult(result) }}</strong>
        <p v-if="result.user_names_fetched != null" class="muted">
          已获取 {{ result.user_names_fetched }} 个用户名。
        </p>
        <ul v-if="accountResultDetails(result).length">
          <li
            v-for="(detail, index) in accountResultDetails(result)"
            :key="index"
          >
            {{ detail }}
          </li>
        </ul>
      </section>
    </form>
    <template #footer
      ><button class="btn" :disabled="busy" @click="emit('close')">关闭</button
      ><button
        class="btn btn-primary"
        form="accounts-import"
        type="submit"
        :disabled="busy || !text.trim()"
      >
        {{ busy ? "正在导入，请稍候…" : "导入账号" }}
      </button></template
    >
  </UiModal>
</template>

<style scoped>
.import-text {
  width: 100%;
  min-height: 210px;
}
.import-result {
  max-height: 240px;
  overflow: auto;
  overflow-wrap: anywhere;
}
</style>
