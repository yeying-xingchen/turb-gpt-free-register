// 任务中心的后台并发设置：纯函数，便于 Node 单测直接复用。
export type PoolRow = Record<string, any>;

export const DEFAULT_MAX_WORKERS = 16;

export function taskPoolByName(
  pools: PoolRow[] | undefined,
  name: string,
): PoolRow | null {
  if (!Array.isArray(pools) || !name) return null;
  return pools.find((pool) => String(pool?.name) === String(name)) || null;
}

export function taskPoolWorkers(pool: PoolRow | null, fallback = 1): number {
  const value = Number(pool?.workers);
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}

export function taskPoolMax(pool: PoolRow | null): number {
  const value = Number(pool?.max_workers);
  return Number.isFinite(value) && value >= 1
    ? Math.floor(value)
    : DEFAULT_MAX_WORKERS;
}

export function taskPoolWorkersValid(
  value: unknown,
  pool: PoolRow | null,
): boolean {
  const workers = Number(value);
  return (
    Number.isInteger(workers) &&
    workers >= 1 &&
    workers <= taskPoolMax(pool)
  );
}

export function taskPoolDirty(value: unknown, pool: PoolRow | null): boolean {
  return Boolean(pool) && Number(value) !== taskPoolWorkers(pool);
}

export function taskPoolHint(pool: PoolRow | null): string {
  if (!pool) return "";
  const running = Number(pool.running) || 0;
  const pending = Number(pool.pending) || 0;
  const threads = Number(pool.threads) || 0;
  return `${running} 运行 · ${pending} 排队 · ${threads} 线程`;
}
