<template>
  <NuxtLayout><NuxtPage /></NuxtLayout>
  <div class="toast-stack" aria-live="polite">
    <div
      v-for="item in toasts"
      :key="item.id"
      class="toast"
      :class="`toast-${item.kind}`"
      role="status"
    >
      <UiIcon :name="item.kind === 'error' ? 'alert' : 'check'" />
      <span>{{ item.message }}</span>
      <button class="icon-btn" aria-label="关闭提示" @click="dismiss(item.id)">
        <UiIcon name="x" />
      </button>
    </div>
  </div>
</template>
<script setup lang="ts">
const { toasts, dismiss } = useToast();
</script>
