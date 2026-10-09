interface Toast {
  id: number;
  kind: "success" | "error" | "info";
  message: string;
}
let serial = 0;
const timers = new Map<number, ReturnType<typeof setTimeout>>();
export function useToast() {
  const toasts = useState<Toast[]>("toasts", () => []);
  function dismiss(id: number) {
    const timer = timers.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.delete(id);
    }
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }
  function add(kind: Toast["kind"], message: string) {
    const text = message.trim();
    if (!text) return;
    const existing = toasts.value.find(
      (toast) => toast.kind === kind && toast.message === text,
    );
    if (existing) dismiss(existing.id);
    const id = ++serial;
    toasts.value.push({ id, kind, message: text });
    const timer = setTimeout(() => dismiss(id), kind === "error" ? 9000 : 5000);
    timers.set(id, timer);
  }
  return {
    toasts,
    dismiss,
    success: (m: string) => add("success", m),
    error: (m: string) => add("error", m),
    info: (m: string) => add("info", m),
  };
}
