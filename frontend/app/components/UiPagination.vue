<script setup lang="ts">
const props = withDefaults(
  defineProps<{ page: number; total: number; pageSize?: number }>(),
  { pageSize: 50 },
);
const emit = defineEmits<{ "update:page": [page: number] }>();
const pages = computed(() =>
  Math.max(1, Math.ceil(props.total / props.pageSize)),
);
type PageItem = number | "ellipsis-left" | "ellipsis-right";
const pageList = computed<PageItem[]>(() => {
  const totalPages = pages.value;
  const current = props.page;
  if (totalPages <= 7)
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  if (current <= 4)
    return [1, 2, 3, 4, 5, "ellipsis-right", totalPages];
  if (current >= totalPages - 3)
    return [
      1,
      "ellipsis-left",
      totalPages - 4,
      totalPages - 3,
      totalPages - 2,
      totalPages - 1,
      totalPages,
    ];
  return [
    1,
    "ellipsis-left",
    current - 1,
    current,
    current + 1,
    "ellipsis-right",
    totalPages,
  ];
});
function goTo(target: number) {
  const next = Math.min(Math.max(1, target), pages.value);
  if (next !== props.page) emit("update:page", next);
}
</script>
<template>
  <nav class="pagination" aria-label="分页导航">
    <span class="muted pagination-summary"
      >共 {{ total.toLocaleString() }} 条记录 · 第 {{ page }} / {{ pages }} 页</span
    >
    <div class="pagination-pages">
      <button
        class="btn btn-sm"
        :disabled="page <= 1"
        aria-label="上一页"
        @click="goTo(page - 1)"
      >
        上一页
      </button>
      <div class="pagination-numbers" aria-label="页码">
        <template v-for="(item, index) in pageList" :key="`${item}-${index}`">
          <span v-if="typeof item !== 'number'" class="pagination-ellipsis" aria-hidden="true">…</span>
          <button
            v-else
            class="btn btn-sm pagination-page"
            :class="{ 'pagination-page--active': item === page }"
            :aria-current="item === page ? 'page' : undefined"
            :aria-label="`第 ${item} 页`"
            :title="`第 ${item} 页`"
            @click="goTo(item)"
          >
            {{ item }}
          </button>
        </template>
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
  </nav>
</template>
