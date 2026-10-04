export function usePolling(
  callback: () => unknown | Promise<unknown>,
  interval = 5000,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = true;
  let running = false;
  const run = async () => {
    if (stopped || running) return;
    running = true;
    try {
      if (!document.hidden) await callback();
    } catch {
      /* The page owns error presentation. */
    } finally {
      running = false;
      if (!stopped) timer = setTimeout(run, interval);
    }
  };
  const visible = () => {
    if (!document.hidden && !running) {
      clearTimeout(timer);
      void run();
    }
  };
  onMounted(() => {
    stopped = false;
    void run();
    document.addEventListener("visibilitychange", visible);
  });
  onBeforeUnmount(() => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visible);
  });
}
