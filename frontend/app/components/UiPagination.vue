<script setup lang="ts">
const props = withDefaults(
  defineProps<{ page: number; total: number; pageSize?: number }>(),
  { pageSize: 50 },
);
const emit = defineEmits<{ "update:page": [page: number] }>();
const pages = computed(() =>
  Math.max(1, Math.ceil(props.total / props.pageSize)),
);
const pageList = computed(() =>
  Array.from({ length: pages.value }, (_, index) => index + 1),
);
function goTo(target: number) {
  const next = Math.min(Math.max(1, target), pages.value);
  if (next !== props.page) emit("update:page", next);
}
</script>
<template>
  <div class="pagination">
    <span class="muted">共 {{ total.toLocaleString() }} 条记录</span>
    <div class="pagination-pages">
      <button
        class="btn btn-sm"
        :disabled="page <= 1"
        aria-label="上一页"
        @click="goTo(page - 1)"
      >
        上一页
      </button>
      <div class="pagination-numbers">
        <button
          v-for="item in pageList"
          :key="item"
          class="btn btn-sm pagination-page"
          :class="{ 'pagination-page--active': item === page }"
          :aria-current="item === page ? 'page' : undefined"
          :aria-label="`第 ${item} 页`"
          :title="`第 ${item} 页`"
          @click="goTo(item)"
        >
          {{ item }}
        </button>
      </div>
      <button
        class="btn btn-sm"
        :disabled="page >= pages"
        aria-label="下一页"
        @click="goTo(page + 1)"
      >
        下一页
      </button>
    </div>
  </div>
</template>
