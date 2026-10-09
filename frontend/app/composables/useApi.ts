export interface ApiOptions {
  method?: string;
  body?: any;
  query?: Record<string, any>;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public data: any,
  ) {
    super(message);
  }
}

export function useApi() {
  const request = async <T = any>(
    path: string,
    options: ApiOptions = {},
  ): Promise<T> => {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(options.query || {})) {
      if (value !== undefined && value !== null && value !== "")
        url.searchParams.set(key, String(value));
    }
    const response = await fetch(url, {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        ...options.headers,
        Accept: "application/json",
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      body:
        options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
    let data: any;
    let invalidJson = false;
    try {
      data = await response.json();
    } catch (cause) {
      if (options.signal?.aborted) throw cause;
      invalidJson = true;
    }
    const loginRedirect =
      response.redirected &&
      new URL(response.url, url).pathname.replace(/\/$/, "") === "/login";
    if (
      (response.status === 401 || loginRedirect) &&
      !["/login", "/redeem", "/upload"].includes(window.location.pathname.replace(/\/$/, ""))
    ) {
      useState<boolean>("authenticated").value = false;
      await navigateTo({
        path: "/login",
        query: { next: window.location.pathname + window.location.search },
      });
    }
    if (loginRedirect)
      throw new ApiError("登录状态已过期，请重新登录", response.status, null);
    if (invalidJson)
      throw new ApiError(
        response.status === 401
          ? "登录状态已过期，请重新登录"
          : `服务响应异常（${response.status}）：未收到有效 JSON 数据`,
        response.status,
        null,
      );
    if (!response.ok || data?.ok === false) {
      throw new ApiError(
        data?.error || data?.message || "请求失败，请稍后重试",
        response.status,
        data,
      );
    }
    return data as T;
  };
  return { request };
}
