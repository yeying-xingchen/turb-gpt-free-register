<script setup lang="ts">
withDefaults(defineProps<{ title?: string; description?: string }>(), {
  title: "暂无数据",
  description: "数据会在这里显示。",
});
</script>
<template>
  <div class="empty-state">
    <span class="empty-icon"><UiIcon name="grid" :size="26" /></span>
    <h3>{{ title }}</h3>
    <p>{{ description }}</p>
    <slot />
  </div>
</template>
