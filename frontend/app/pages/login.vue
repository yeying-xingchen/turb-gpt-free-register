<script setup lang="ts">
definePageMeta({ layout: false });
useHead({ title: "登录 · Registrator" });
const code = ref("");
const remember = ref(true);
const busy = ref(false);
const error = ref("");
const route = useRoute();
async function login() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await useApi().request("/api/auth/login", {
      method: "POST",
      body: { auth_code: code.value, remember: remember.value },
    });
    useState("authenticated").value = true;
    code.value = "";
    const next = typeof route.query.next === "string" ? route.query.next : "/";
    const allowed = [
      "/",
      "/accounts",
      "/tasks",
      "/mailboxes",
      "/codex",
      "/redemptions",
      "/settings",
      "/providers",
    ];
    await navigateTo(allowed.includes(next.split("?")[0]!) ? next : "/");
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="public-shell">
    <header class="public-header">
      <NuxtLink class="brand" to="/"
        ><img
          src="/brand.svg"
          width="34"
          height="34"
          alt=""
        />Registrator</NuxtLink
      ><NuxtLink to="/redeem" class="muted"
        >前往兑换 <span aria-hidden="true">↗</span></NuxtLink
      >
    </header>
    <main class="login-main">
      <div class="login-intro">
        <div class="eyebrow">YOUR WORKSPACE, CONNECTED</div>
        <h1>让管理更简单。<br /><span>从这里开始。</span></h1>
        <p>
          账号、资源与任务，尽在一个工作空间。<br />登录控制台，继续你的工作。
        </p>
        <div class="login-decoration" aria-hidden="true">
          <div class="decor-line">
            <UiIcon name="users" /><span>账号管理</span><UiIcon name="check" />
          </div>
          <div class="decor-line">
            <UiIcon name="tasks" /><span>任务与进度</span
            ><UiIcon name="check" />
          </div>
          <div class="decor-line">
            <UiIcon name="shield" /><span>安全会话</span><UiIcon name="check" />
          </div>
        </div>
      </div>
      <section class="login-card">
        <span class="login-icon"><UiIcon name="key" :size="25" /></span>
        <h2>欢迎回来</h2>
        <p class="muted">使用授权码访问你的工作空间</p>
        <form class="stack" @submit.prevent="login">
          <div v-if="error" class="alert alert-error" role="alert">
            {{ error }}
          </div>
          <label class="field"
            >授权码<input
              v-model="code"
              class="input"
              type="password"
              name="auth_code"
              autocomplete="current-password"
              placeholder="请输入 WebUI 授权码"
              required
              autofocus
              :disabled="busy" /></label
          ><label class="remember"
            ><input v-model="remember" type="checkbox" />保持登录状态</label
          ><button class="btn btn-primary" :disabled="busy || !code.trim()">
            {{ busy ? "正在验证…" : "进入工作空间" }}<UiIcon name="arrow" />
          </button>
        </form>
        <p class="login-help">
          <UiIcon name="shield" :size="13" />授权码由管理员配置或启动时生成
        </p>
      </section>
    </main>
    <footer class="public-footer">Registrator · 你的账号管理工作空间</footer>
  </div>
</template>
<style scoped>
.login-main {
  display: grid;
  grid-template-columns: 1fr 420px;
  gap: 110px;
  align-items: center;
  max-width: 1080px;
  padding: 65px 40px 100px;
  width: 100%;
  margin: auto;
}
.login-intro h1 {
  font-size: 46px;
  font-weight: 600;
  line-height: 1.45;
  letter-spacing: -1.8px;
  margin: 18px 0;
}
.login-intro h1 span {
  color: #72968a;
}
.login-intro p {
  font-size: 13px;
  color: #8a9b98;
  line-height: 2;
}
.login-decoration {
  margin-top: 34px;
  max-width: 260px;
  display: grid;
  gap: 12px;
}
.decor-line {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: #7b938b;
  padding: 13px 16px;
  border: 1px solid #e0ebe5;
  border-radius: 8px;
  background: #ffffff78;
}
.decor-line svg:last-child {
  margin-left: auto;
  color: #67a88e;
}
.login-card {
  padding: 36px;
  background: #fff;
  border: 1px solid #e6ece8;
  border-radius: 14px;
  box-shadow: 0 16px 60px #24483a08;
}
.login-icon {
  display: inline-flex;
  padding: 13px;
  background: #edf7f2;
  border-radius: 12px;
  color: #267d67;
  margin-bottom: 25px;
}
.login-card h2 {
  font-size: 24px;
  margin-bottom: 8px;
}
.login-card form {
  margin-top: 30px;
  gap: 20px;
}
.remember {
  font-size: 12px;
  color: #87918f;
  display: flex;
  align-items: center;
  gap: 8px;
}
.login-card .btn {
  padding: 13px;
}
.login-help {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 10px;
  color: #a4aeab;
  margin-top: 27px;
}
.login-card .input {
  padding: 12px;
}
.login-card .alert {
  margin-bottom: 0;
}
@media (max-width: 850px) {
  .login-main {
    grid-template-columns: 1fr;
    gap: 30px;
    max-width: 500px;
    padding: 35px 22px;
  }
  .login-intro {
    display: none;
  }
  .login-card {
    padding: 28px;
  }
}
</style>
