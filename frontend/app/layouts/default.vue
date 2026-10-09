<script setup lang="ts">
const route = useRoute();
const open = ref(false);
let previousBodyOverflow = "";
const links = [
  { to: "/", label: "工作台", icon: "grid", caption: "注册与概览" },
  { to: "/accounts", label: "账号管理", icon: "users", caption: "账号与订阅" },
  { to: "/tasks", label: "任务中心", icon: "tasks", caption: "进度与日志" },
  { to: "/codex", label: "Codex 授权", icon: "key", caption: "授权与凭证" },
  { to: "/mailboxes", label: "邮箱资源", icon: "mail", caption: "邮箱池管理" },
  {
    to: "/redemptions",
    label: "兑换管理",
    icon: "gift",
    caption: "兑换码与库存",
  },
  {
    to: "/providers",
    label: "提链服务",
    icon: "link",
    caption: "供应商与额度",
  },
  {
    to: "/settings",
    label: "系统配置",
    icon: "settings",
    caption: "偏好与集成",
  },
];
const frontendOptions = [
  { href: "/", label: "Nuxt 新前端", key: "nuxt" },
  { href: "/?ui=modern", label: "旧版侧边栏", key: "modern" },
  { href: "/?ui=legacy", label: "历史版顶部导航", key: "legacy" },
];
const currentFrontend = "nuxt";
const current = computed(
  () => links.find((link) => link.to === route.path) || links[0]!,
);
watch(
  () => route.path,
  () => {
    open.value = false;
  },
);
watch(
  open,
  (isOpen, wasOpen) => {
    if (typeof document === "undefined") return;
    if (isOpen) {
      previousBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    } else if (wasOpen) {
      document.body.style.overflow = previousBodyOverflow;
    }
  },
);
function handleKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && open.value) open.value = false;
}
onMounted(() => window.addEventListener("keydown", handleKeydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleKeydown);
  if (typeof document !== "undefined" && open.value)
    document.body.style.overflow = previousBodyOverflow;
});
const loggingOut = ref(false);
const {
  status: updateStatus,
  updateAvailable,
  refresh: refreshUpdate,
} = useUpdateStatus();
// 只读后台缓存结论，开销很低；真正的网络检查由服务端按配置间隔执行。
usePolling(() => refreshUpdate(), 600000);
const updateLabel = computed(() => {
  const behind = updateStatus.value.behind;
  return typeof behind === "number" && behind > 0
    ? `${behind} 个新提交`
    : "有新版本可用";
});
async function logout() {
  loggingOut.value = true;
  try {
    await useApi().request("/api/auth/logout", { method: "POST" });
    useState("authenticated").value = false;
    await navigateTo("/login");
  } catch (e: any) {
    useToast().error(e.message);
  } finally {
    loggingOut.value = false;
  }
}
</script>
<template>
  <div class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <button
      v-if="open"
      class="sidebar-scrim"
      aria-label="关闭导航"
      @click="open = false"
    />
    <aside
      id="primary-navigation"
      class="sidebar"
      :class="{ 'is-open': open }"
      aria-label="主导航"
    >
      <NuxtLink to="/" class="brand"
        ><img src="/brand.svg" alt="" width="34" height="34" /><span
          >Registrator<small>CONTROL CENTER</small></span
        ></NuxtLink
      >
      <div class="workspace-label">
        <span class="status-dot" />本地工作空间<UiIcon
          name="chevron"
          :size="13"
        />
      </div>
      <div class="nav-section-label">工作空间</div>
      <nav aria-label="主导航">
        <NuxtLink
          v-for="link in links"
          :key="link.to"
          :to="link.to"
          class="nav-link"
          :class="{ active: route.path === link.to }"
          :aria-current="route.path === link.to ? 'page' : undefined"
          ><UiIcon :name="link.icon" /><span>{{ link.label }}</span
          ><span v-if="route.path === link.to" class="nav-active-dot"
        /></NuxtLink>
      </nav>
      <div class="sidebar-bottom">
        <NuxtLink v-if="updateAvailable" to="/settings" class="update-pill">
          <UiIcon name="download" :size="14" />
          <span>发现新版本</span>
          <small>{{ updateLabel }}</small>
        </NuxtLink>
        <div class="frontend-switcher" aria-label="切换前端">
          <div class="frontend-switcher-title">切换前端</div>
          <a
            v-for="option in frontendOptions"
            :key="option.key"
            :href="option.href"
            class="frontend-switch-link"
            :class="{ active: option.key === currentFrontend }"
            :aria-current="option.key === currentFrontend ? 'page' : undefined"
          >
            <span class="frontend-switch-dot" />{{ option.label }}
          </a>
        </div>
        <NuxtLink to="/redeem" class="public-link"
          ><UiIcon name="gift" /><span>公开兑换页面</span
          ><UiIcon name="arrow" :size="15"
        /></NuxtLink>
        <div class="profile">
          <span class="avatar">AD</span>
          <div><strong>管理员</strong><small>工作空间管理</small></div>
          <button
            class="icon-btn"
            :disabled="loggingOut"
            aria-label="退出登录"
            title="退出登录"
            @click="logout"
          >
            <UiIcon name="logout" />
          </button>
        </div>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="inline">
          <button
            class="icon-btn mobile-menu"
            aria-label="打开导航"
             :aria-expanded="open"
             aria-controls="primary-navigation"
            @click="open = true"
          >
            <UiIcon name="menu" /></button
          ><span class="muted">工作空间</span
          ><UiIcon name="chevron" :size="13" /><strong>{{
            current.label
          }}</strong>
        </div>
        <span class="topbar-status"><span class="status-dot" />管理控制台</span>
      </header>
      <main id="main-content" class="main-content" tabindex="-1"><slot /></main>
      <footer class="workspace-footer">
        <span>Registrator</span><span>让每一步管理都井然有序</span>
      </footer>
    </div>
  </div>
</template>
