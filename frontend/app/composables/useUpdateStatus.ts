export interface UpdateCommit {
  sha: string;
  short: string;
  subject: string;
  author: string;
  date: string;
  url: string;
}

export interface UpdateStatus {
  checked_at?: number;
  checked_at_text?: string;
  enabled?: boolean;
  interval_hours?: number;
  checking?: boolean;
  busy?: boolean;
  ok?: boolean;
  error?: string;
  update_available?: boolean;
  behind?: number | null;
  ahead?: number | null;
  compare?: string;
  commits_approx?: boolean;
  commits?: UpdateCommit[];
  local?: {
    available: boolean;
    short: string;
    subject: string;
    date: string;
    branch: string;
    repo: string;
    dirty: number;
    error: string;
  };
  remote?: UpdateCommit | null;
  release?: {
    tag: string;
    name: string;
    url: string;
    published_at: string;
  } | null;
  source?: {
    repo: string;
    branch: string;
    remote: string;
    html_url: string;
    branch_url: string;
    token_configured: boolean;
    token_hint: string;
  };
  apply?: { allowed: boolean; reason: string };
  applied_at?: number;
  applied_to?: string;
  scheduler_running?: boolean;
}

async function fetchStatus(): Promise<UpdateStatus> {
  const data = await useApi().request<any>("/api/update/status");
  return (data || {}) as UpdateStatus;
}

export function useUpdateStatus() {
  const status = useState<UpdateStatus>("update-status", () => ({}));
  const loading = useState<boolean>("update-status-loading", () => false);
  const applying = useState<boolean>("update-status-applying", () => false);

  /** 读取后台缓存的检查结论（不触发网络检查，开销很低）。 */
  async function refresh(): Promise<UpdateStatus> {
    loading.value = true;
    try {
      status.value = await fetchStatus();
    } finally {
      loading.value = false;
    }
    return status.value;
  }

  /** 立即检查一次；检查期间后台线程持锁，重复点击安全。 */
  async function checkNow(): Promise<UpdateStatus> {
    loading.value = true;
    try {
      status.value = (await useApi().request<any>("/api/update/check", {
        method: "POST",
      })) as UpdateStatus;
    } finally {
      loading.value = false;
    }
    return status.value;
  }

  /** 一键快进更新（后端会再次校验工作区是否干净）。 */
  async function applyUpdate(): Promise<any> {
    applying.value = true;
    try {
      const result = await useApi().request<any>("/api/update/apply", {
        method: "POST",
      });
      await refresh();
      return result;
    } finally {
      applying.value = false;
    }
  }

  const updateAvailable = computed(() => status.value.update_available === true);

  return { status, loading, applying, updateAvailable, refresh, checkNow, applyUpdate };
}
