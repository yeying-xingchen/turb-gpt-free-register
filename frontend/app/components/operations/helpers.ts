export type OperationRow = Record<string, any>;
export type ActionPrompt = {
  title: string;
  description: string;
  label?: string;
  danger?: boolean;
  run: () => Promise<unknown>;
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试";
}

export function formatTime(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}

export function checkResult<T extends OperationRow>(result: T): T {
  if (result?.ok === false) throw new Error(result.error || "操作失败");
  return result;
}

export function resultMessage(result: OperationRow, fallback: string): string {
  const skipped =
    result.skipped_count ??
    (Array.isArray(result.skipped) ? result.skipped.length : 0);
  const count =
    result.deleted_count ??
    result.updated_count ??
    result.started_count ??
    result.stopped_count;
  let message =
    result.message || (count == null ? fallback : `${fallback} ${count} 项`);
  if (result.reused_count) message += `，复用 ${result.reused_count} 项`;
  if (skipped) message += `，跳过 ${skipped} 项`;
  return message;
}

export async function copyText(text: string): Promise<void> {
  if (!text) throw new Error("暂无可复制的内容");
  await navigator.clipboard.writeText(text);
}

// Download endpoints need the raw response rather than JSON request().
export async function downloadFile(
  path: string,
  filename: string,
  body?: Record<string, any>,
): Promise<void> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    ...(body
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `下载失败（${response.status}）`);
  }
  if (
    response.redirected ||
    (response.headers.get("content-type") || "").includes("text/html")
  ) {
    throw new Error("登录状态已过期，请重新登录后下载");
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = match?.[1] || filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
