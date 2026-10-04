export interface ApiOptions {
  method?: string;
  body?: any;
  query?: Record<string, any>;
  signal?: AbortSignal;
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
        Accept: "application/json",
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      body:
        options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
    const data = await response
      .json()
      .catch(() => ({ error: `服务响应异常（${response.status}）` }));
    if (!response.ok || data?.ok === false) {
      if (
        response.status === 401 &&
        !["/login", "/redeem"].includes(window.location.pathname)
      ) {
        useState<boolean>("authenticated").value = false;
        await navigateTo({
          path: "/login",
          query: { next: window.location.pathname + window.location.search },
        });
      }
      throw new ApiError(
        data.error || data.message || "请求失败，请稍后重试",
        response.status,
        data,
      );
    }
    return data as T;
  };
  return { request };
}
