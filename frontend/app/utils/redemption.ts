import { sha256 } from "@noble/hashes/sha2.js";

export const normalizeRedeemCode = (value: string) =>
  value
    .replace(
      /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/g,
      "",
    )
    .toUpperCase();
export interface Recovery {
  request_id: string;
  completed: boolean;
}
export function recoveryKey(code: string) {
  const digest = sha256(new TextEncoder().encode(normalizeRedeemCode(code)));
  return (
    "redeem_recovery_v2:" +
    Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("")
  );
}
export function readRecovery(key: string): Recovery | null {
  try {
    const raw =
      sessionStorage.getItem(key) ??
      sessionStorage.getItem("redeem_recovery_v1");
    if (raw === null) return null;
    const state = JSON.parse(raw);
    if (
      !state ||
      !/^[A-Za-z0-9_-]{32,128}$/.test(state.request_id) ||
      typeof state.completed !== "boolean"
    )
      throw new Error();
    return state;
  } catch {
    throw new Error(
      "无法读取本标签页的恢复信息。请允许标签页存储；若已提交兑换，请联系管理员核对。",
    );
  }
}
export function saveRecovery(key: string, state: Recovery) {
  try {
    const raw = JSON.stringify(state);
    sessionStorage.setItem(key, raw);
    if (sessionStorage.getItem(key) !== raw) throw new Error();
  } catch {
    throw new Error("无法保存兑换恢复信息，请允许本站使用标签页存储后重试。");
  }
}
export function newRecovery(): Recovery {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return {
    request_id: Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
      "",
    ),
    completed: false,
  };
}
