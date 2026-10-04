interface Toast {
  id: number;
  kind: "success" | "error" | "info";
  message: string;
}
let serial = 0;
export function useToast() {
  const toasts = useState<Toast[]>("toasts", () => []);
  function dismiss(id: number) {
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }
  function add(kind: Toast["kind"], message: string) {
    const id = ++serial;
    toasts.value.push({ id, kind, message });
    setTimeout(() => dismiss(id), kind === "error" ? 9000 : 5000);
  }
  return {
    toasts,
    dismiss,
    success: (m: string) => add("success", m),
    error: (m: string) => add("error", m),
    info: (m: string) => add("info", m),
  };
}
