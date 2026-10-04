<script setup lang="ts">
import { accountError, type AccountGroup } from "~/utils/accounts";
const props = defineProps<{ groups: AccountGroup[] }>();
const emit = defineEmits<{ close: []; changed: [] }>();
const { request } = useApi();
const toast = useToast();
const open = computed({
  get: () => true,
  set: (value: boolean) => {
    if (!value && !busy.value) emit("close");
  },
});
const busy = ref(false);
const error = ref("");
const current = ref("");
const name = ref("");
const prefix = ref("");
const publicStock = ref(false);
const destination = ref("默认分组");
function choose(group?: AccountGroup) {
  current.value = group?.group_name || "";
  name.value = current.value;
  prefix.value = group?.redeem_prefix || "";
  publicStock.value = !!group?.public_stock;
  destination.value = "默认分组";
  error.value = "";
}
async function save() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    if (!name.value.trim()) throw new Error("请填写分组名");
    if (current.value && name.value.trim() !== current.value) {
      await request("/api/account-groups/rename", {
        method: "POST",
        body: { old_name: current.value, new_name: name.value.trim() },
      });
      current.value = name.value.trim();
    }
    await request("/api/account-groups/meta", {
      method: "POST",
      body: {
        group_name: name.value.trim(),
        redeem_prefix: prefix.value.trim(),
        public_stock: publicStock.value,
      },
    });
    toast.success("分组已保存");
    emit("changed");
    choose();
  } catch (cause) {
    error.value = accountError(cause);
    emit("changed");
  } finally {
    busy.value = false;
  }
}
async function remove() {
  if (
    !current.value ||
    !confirm(
      `删除分组「${current.value}」？组内账号将转入「${destination.value}」，账号不会被删除。`,
    )
  )
    return;
  busy.value = true;
  error.value = "";
  try {
    await request("/api/account-groups/delete", {
      method: "POST",
      body: { group_name: current.value, merge_to: destination.value },
    });
    toast.success("分组已合并删除");
    emit("changed");
    choose();
  } catch (cause) {
    error.value = accountError(cause);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <UiModal v-model:open="open" title="管理账号分组">
    <div class="stack">
      <div class="group-list">
        <button class="btn btn-sm" :disabled="busy" @click="choose()">
          ＋ 新建分组</button
        ><button
          v-for="group in groups"
          :key="group.group_name"
          class="btn btn-sm"
          :class="{ 'btn-primary': current === group.group_name }"
          :disabled="busy"
          @click="choose(group)"
        >
          {{ group.group_name }} · {{ group.total }}
        </button>
      </div>
      <form id="account-group-editor" class="stack" @submit.prevent="save">
        <div class="form-grid">
          <label class="field"
            >分组名<input
              v-model="name"
              class="input"
              maxlength="60"
              required
              :disabled="busy || current === '默认分组'"
          /></label>
          <label class="field"
            >兑换码前缀（可选）<input
              v-model="prefix"
              class="input"
              maxlength="16"
              :disabled="busy"
          /></label>
        </div>
        <label class="inline"
          ><input
            v-model="publicStock"
            type="checkbox"
            :disabled="busy"
          />公开展示该分组库存</label
        >
        <div v-if="current && current !== '默认分组'" class="group-delete">
          <label class="field"
            >删除分组时将账号转入<select
              v-model="destination"
              class="select"
              :disabled="busy"
            >
              <option
                v-for="group in groups.filter(
                  (item) => item.group_name !== current,
                )"
                :key="group.group_name"
                :value="group.group_name"
              >
                {{ group.group_name }}
              </option>
              <option
                v-if="!groups.some((item) => item.group_name === '默认分组')"
                value="默认分组"
              >
                默认分组
              </option>
            </select></label
          ><button
            type="button"
            class="btn btn-danger btn-sm"
            :disabled="busy"
            @click="remove"
          >
            合并并删除此分组
          </button>
        </div>
        <p v-if="error" class="alert alert-error" role="alert">{{ error }}</p>
      </form>
    </div>
    <template #footer
      ><button class="btn" :disabled="busy" @click="emit('close')">关闭</button
      ><button
        class="btn btn-primary"
        type="submit"
        form="account-group-editor"
        :disabled="busy"
      >
        {{ busy ? "保存中…" : "保存分组" }}
      </button></template
    >
  </UiModal>
</template>

<style scoped>
.group-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 180px;
  overflow: auto;
}
.group-delete {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 12px;
  margin-top: 12px;
}
</style>
