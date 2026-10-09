<template>
  <NuxtLayout><NuxtPage /></NuxtLayout>
  <div class="toast-stack" aria-live="polite" aria-relevant="additions text">
    <div
      v-for="item in toasts"
      :key="item.id"
      class="toast"
      :class="`toast-${item.kind}`"
      :role="item.kind === 'error' ? 'alert' : 'status'"
      aria-atomic="true"
    >
      <UiIcon
        :name="item.kind === 'error' ? 'alert' : item.kind === 'info' ? 'info' : 'check'"
      />
      <span>{{ item.message }}</span>
      <button
        class="icon-btn"
        type="button"
        aria-label="关闭提示"
        @click="dismiss(item.id)"
      >
        <UiIcon name="x" />
      </button>
    </div>
  </div>
</template>
<script setup lang="ts">
const { toasts, dismiss } = useToast();
</script>
