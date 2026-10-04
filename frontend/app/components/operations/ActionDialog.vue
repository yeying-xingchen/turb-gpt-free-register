<script setup lang="ts">
import type { ActionPrompt } from "./helpers";
import { errorMessage } from "./helpers";
const action = defineModel<ActionPrompt | null>("action", { default: null });
const working = ref(false);
const failure = ref("");
const open = computed({
  get: () => !!action.value,
  set: (value) => {
    if (!value && !working.value) action.value = null;
  },
});
watch(action, () => {
  failure.value = "";
});
async function confirm() {
  if (!action.value || working.value) return;
  working.value = true;
  try {
    await action.value.run();
    action.value = null;
  } catch (error) {
    failure.value = errorMessage(error);
  } finally {
    working.value = false;
  }
}
</script>

<template>
  <UiModal v-model:open="open" :title="action?.title || '确认操作'">
    <p class="action-description">{{ action?.description }}</p>
    <div v-if="failure" class="alert alert-error" role="alert">
      {{ failure }}
    </div>
    <template #footer>
      <button class="btn" :disabled="working" @click="open = false">
        取消
      </button>
      <button
        class="btn"
        :class="action?.danger ? 'btn-danger' : 'btn-primary'"
        :disabled="working"
        @click="confirm"
      >
        {{ working ? "正在处理…" : action?.label || "确认" }}
      </button>
    </template>
  </UiModal>
</template>

<style scoped>
.action-description {
  white-space: pre-line;
  line-height: 1.8;
  margin: 0 0 16px;
  color: var(--text-secondary, #64748b);
  overflow-wrap: anywhere;
}
</style>
