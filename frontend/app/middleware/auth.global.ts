export default defineNuxtRouteMiddleware(async (to) => {
  if (["/login", "/redeem"].includes(to.path)) return;
  const authenticated = useState<boolean>("authenticated", () => false);
  if (authenticated.value) return;
  try {
    const session = await $fetch<{ authenticated: boolean }>(
      "/api/auth/session",
      { credentials: "same-origin" },
    );
    if (session.authenticated) {
      authenticated.value = true;
      return;
    }
  } catch {
    /* Login page displays connection errors. */
  }
  return navigateTo({ path: "/login", query: { next: to.fullPath } });
});
