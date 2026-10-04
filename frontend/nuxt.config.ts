import process from "node:process";

const apiTarget = process.env.NUXT_API_PROXY || "http://127.0.0.1:5000";

export default defineNuxtConfig({
  compatibilityDate: "2026-01-01",
  ssr: false,
  devtools: { enabled: false },
  css: ["~/assets/css/main.css"],
  app: {
    head: {
      title: "Registrator · 工作台",
      htmlAttrs: { lang: "zh-CN" },
      meta: [
        { name: "description", content: "账号、任务与资源的统一管理工作台" },
      ],
      link: [{ rel: "icon", type: "image/svg+xml", href: "/brand.svg" }],
    },
  },
  nitro: {
    devProxy: {
      "/api": { target: `${apiTarget}/api`, changeOrigin: true },
    },
    prerender: {
      routes: [
        "/",
        "/login",
        "/redeem",
        "/accounts",
        "/tasks",
        "/mailboxes",
        "/codex",
        "/redemptions",
        "/settings",
        "/providers",
      ],
    },
  },
  typescript: { strict: true },
});
