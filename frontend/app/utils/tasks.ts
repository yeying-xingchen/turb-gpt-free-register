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

// 任务中心筛选：纯函数，便于 Node 单测直接复用。
export type TaskFilterState = {
  job_type?: string;
  status?: string;
  q?: string;
};

export type TaskFilterOption = { value: string; label: string };

/** 只把非空筛选项拼进查询串，空值不产生 ?job_type=&status= 这类噪声。 */
export function taskFilterQuery(
  filters: TaskFilterState | null | undefined,
): Record<string, string> {
  const query: Record<string, string> = {};
  const jobType = String(filters?.job_type ?? "").trim();
  const status = String(filters?.status ?? "").trim();
  const keyword = String(filters?.q ?? "").trim();
  if (jobType) query.job_type = jobType;
  if (status) query.status = status;
  if (keyword) query.q = keyword;
  return query;
}

export function taskFilterActive(
  filters: TaskFilterState | null | undefined,
): boolean {
  return Object.keys(taskFilterQuery(filters)).length > 0;
}

/** 后端下发的筛选下拉项；缺失时返回空数组，界面回退到默认文案。 */
export function taskFilterOptions(payload: PoolRow | null | undefined): {
  jobTypes: TaskFilterOption[];
  statuses: TaskFilterOption[];
} {
  const options = payload?.filters;
  return {
    jobTypes: Array.isArray(options?.job_types) ? options.job_types : [],
    statuses: Array.isArray(options?.statuses) ? options.statuses : [],
  };
}
