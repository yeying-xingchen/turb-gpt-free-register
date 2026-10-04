<script setup lang="ts">
const props = defineProps<{ open: boolean; title: string }>();
const emit = defineEmits<{ "update:open": [open: boolean] }>();
const titleId = useId();
const dialog = ref<HTMLDialogElement>();
const close = () => emit("update:open", false);
watch(
  () => props.open,
  async (open) => {
    await nextTick();
    if (open && !dialog.value?.open) dialog.value?.showModal();
    else if (!open && dialog.value?.open) dialog.value?.close();
  },
  { immediate: true },
);
onBeforeUnmount(() => dialog.value?.close());
</script>
<template>
  <Teleport to="body"
    ><dialog
      ref="dialog"
      class="modal"
      :aria-labelledby="titleId"
      @cancel.prevent="close"
      @click="$event.target === dialog && close()"
    >
      <div class="modal-header">
        <h2 :id="titleId">{{ title }}</h2>
        <button class="icon-btn" aria-label="关闭对话框" @click="close">
          <UiIcon name="x" />
        </button>
      </div>
      <div class="modal-body"><slot /></div>
      <div v-if="$slots.footer" class="modal-footer">
        <slot name="footer" />
      </div></dialog
  ></Teleport>
</template>
