<script setup lang="ts">
const props = defineProps<{ open: boolean; title: string }>();
const emit = defineEmits<{ "update:open": [open: boolean] }>();
const titleId = useId();
const dialog = ref<HTMLDialogElement>();
let restoreFocus: HTMLElement | null = null;
let previousOverflow = "";
const close = () => emit("update:open", false);
function focusFirstControl() {
  const target = dialog.value?.querySelector<HTMLElement>(
    "[autofocus], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex='-1'])",
  );
  target?.focus();
}
function restorePageFocus() {
  if (typeof document !== "undefined") document.body.style.overflow = previousOverflow;
  if (restoreFocus?.isConnected) restoreFocus.focus();
  restoreFocus = null;
}
watch(
  () => props.open,
  async (open) => {
    await nextTick();
    if (!dialog.value || typeof document === "undefined") return;
    if (open) {
      restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      previousOverflow = document.body.style.overflow;
      if (!dialog.value.open) dialog.value.showModal();
      document.body.style.overflow = "hidden";
      await nextTick();
      focusFirstControl();
    } else {
      if (dialog.value.open) dialog.value.close();
      restorePageFocus();
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  if (dialog.value?.open) dialog.value.close();
  restorePageFocus();
});
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
