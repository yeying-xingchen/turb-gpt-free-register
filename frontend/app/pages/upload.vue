<script setup lang="ts">
definePageMeta({ layout: false });
useHead({
  title: "公共账号上传 · Registrator",
  meta: [{ name: "referrer", content: "no-referrer" }],
});
const api = useApi();
const key = ref("");
const text = ref("");
const fileName = ref("");
const busy = ref(false);
const error = ref("");
const result = ref<any>(null);
const lines = computed(() => text.value.split(/\r?\n/).filter((line) => line.trim()).length);

async function readFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  error.value = "";
  result.value = null;
  try {
    if (file.size > 10 * 1024 * 1024) throw new Error("单次导入文件不能超过 10 MiB");
    text.value = await file.text();
    fileName.value = file.name;
  } catch (cause: any) {
    error.value = cause?.message || "读取文件失败";
  } finally {
    input.value = "";
  }
}

async function submit() {
  if (busy.value) return;
  error.value = "";
  result.value = null;
  if (!key.value.trim()) { error.value = "请输入上传 Key"; return; }
  if (!text.value.trim()) { error.value = "请粘贴账号内容或选择文本文件"; return; }
  if (lines.value > 5000) { error.value = "单次最多导入 5000 行"; return; }
  if (new Blob([text.value]).size > 10 * 1024 * 1024) { error.value = "单次导入内容不能超过 10 MiB"; return; }
  busy.value = true;
  try {
    result.value = await api.request("/api/public/accounts/import", {
      method: "POST",
      headers: { "X-Upload-Key": key.value.trim() },
      body: { text: text.value },
    });
    if (result.value.ok === false) throw new Error(result.value.error || "上传失败");
    text.value = "";
    fileName.value = "";
  } catch (cause: any) {
    error.value = cause?.message || "上传失败，请稍后重试";
    if (cause?.data) result.value = cause.data;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="public-shell">
    <header class="public-header">
      <NuxtLink class="brand" to="/upload"><img src="/brand.svg" width="34" height="34" alt="" />Registrator</NuxtLink>
      <NuxtLink to="/login" class="muted">管理员登录 ↗</NuxtLink>
    </header>
    <main class="upload-main">
      <div class="upload-heading">
        <div class="eyebrow">PRIVATE KEY REQUIRED</div>
        <h1>上传账号到池内</h1>
        <p>使用有效 Key 将账号批量加入账号池。</p>
      </div>
      <section class="card upload-card">
        <form class="card-body stack" @submit.prevent="submit">
          <label class="field">上传 Key
            <input v-model="key" class="input" type="password" autocomplete="off" placeholder="输入公共上传 Key" :disabled="busy" required />
            <small>Key 仅用于本次请求，请勿分享给无关人员。</small>
          </label>
          <label class="field">从文本文件导入
            <input type="file" accept=".txt,.csv,text/plain,text/csv" :disabled="busy" @change="readFile" />
            <small v-if="fileName" class="muted">已读取 {{ fileName }}，可在下方修改。</small>
          </label>
          <label class="field">账号内容
            <textarea v-model="text" class="textarea mono upload-text" rows="12" :disabled="busy" spellcheck="false" autocomplete="off" placeholder="user@example.com--password--2FA_SECRET--access_token" />
          </label>
          <small class="muted">{{ lines }} 行。格式：邮箱--密码--2FA（可追加 --AT）；不带 AT 的新账号导入后自动查活。最多 5000 行 / 10 MiB。</small>
          <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
          <section v-if="result" class="import-result" aria-live="polite">
            <strong v-if="result.ok">上传完成：已解析 {{ result.parsed }} 个，新增 {{ result.inserted }} 个，跳过 {{ result.skipped }} 个。</strong>
            <ul v-if="result.errors?.length"><li v-for="(item, index) in result.errors" :key="index">第 {{ item.line }} 行：{{ item.reason }}</li></ul>
            <ul v-if="result.skipped_details?.length"><li v-for="item in result.skipped_details" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
            <p v-if="result.user_names_fetched != null" class="muted">已获取 {{ result.user_names_fetched }} 个用户名。</p>
            <p v-if="result.live_checks_queued != null" class="muted">已加入自动查活队列 {{ result.live_checks_queued }} 个。</p>
            <ul v-if="result.live_check_warnings?.length"><li v-for="item in result.live_check_warnings" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
            <ul v-if="result.user_name_warnings?.length"><li v-for="item in result.user_name_warnings" :key="item.email">{{ item.email }}：{{ item.reason }}</li></ul>
          </section>
          <button class="btn btn-primary" type="submit" :disabled="busy || !key.trim() || !text.trim()">{{ busy ? "正在上传，请稍候…" : "验证 Key 并上传" }}</button>
        </form>
      </section>
    </main>
    <footer class="public-footer">Registrator · 公共账号上传</footer>
  </div>
</template>

<style scoped>
.upload-main { width: 100%; max-width: 760px; margin: 35px auto 50px; padding: 0 24px; }
.upload-heading { text-align: center; margin: 15px 0 38px; }
.upload-heading h1 { font-size: 35px; letter-spacing: -1px; font-weight: 600; }
.upload-heading p { color: #91a299; font-size: 13px; margin-top: 12px; }
.upload-card { max-width: 760px; margin: 0 auto; }
.upload-text { width: 100%; min-height: 260px; }
.import-result { padding: 13px 15px; border: 1px solid #bfe5d6; border-radius: 7px; background: #f1fbf7; overflow-wrap: anywhere; }
.import-result ul { margin: 8px 0 0; padding-left: 20px; }
@media (max-width: 680px) { .upload-heading h1 { font-size: 28px; } .upload-main { padding: 0 18px; margin-top: 20px; } }
</style>
