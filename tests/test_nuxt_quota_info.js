// Run: node --test tests/test_nuxt_quota_info.js
// 账号「额度」「银行重置」与「5h/周用量」列（Nuxt 版）的展示规则。
const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const modulePromise = import(
  path.join(__dirname, '../frontend/app/utils/accounts.ts')
);

async function quota(account) {
  const { accountQuotaInfo } = await modulePromise;
  return accountQuotaInfo(account);
}

async function usage(account) {
  const { accountUsageInfo } = await modulePromise;
  return accountUsageInfo(account);
}

test('balance keeps the server string and currency', async () => {
  const info = await quota({
    id: 1,
    quota_check_status: 'success',
    quota_balance: '12.34',
    quota_balance_amount: 12.34,
    quota_currency: 'USD',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
  });
  assert.equal(info.balanceLabel, '12.34 USD');
  assert.equal(info.balanceKind, 'value');
  assert.match(info.checkedLabel, /^查询于 /);
});

test('zero balance and zero resets read as 没有剩余, not as 未使用', async () => {
  const zero = await quota({
    id: 2,
    quota_check_status: 'success',
    quota_balance: '0.00',
    quota_balance_amount: 0,
    quota_currency: 'USD',
    reset_credits_available: 0,
    reset_credits_applicable: 0,
  });
  assert.equal(zero.balanceLabel, '0.00 USD');
  // 0 是「没有剩余」（用完或本来没有额度），不能按有可用额度的绿色展示。
  assert.equal(zero.balanceKind, 'empty');
  assert.equal(zero.resetLabel, '0 张');
  assert.equal(zero.resetKind, 'empty');
  assert.match(zero.tooltip, /可能已经用完，也可能该账号没有这类额度/);
  assert.match(zero.tooltip, /没有可用重置券/);

  const never = await quota({ id: 3 });
  assert.equal(never.balanceLabel, '未查询');
  assert.equal(never.balanceKind, 'unknown');
  assert.equal(never.resetLabel, '未查询');
  assert.equal(never.resetKind, 'unknown');
});

test('queued and running checks show 查询中 instead of a stale value', async () => {
  for (const status of ['queued', 'running']) {
    const info = await quota({
      id: 4,
      quota_check_status: status,
      quota_balance: '1.00',
      reset_credits_available: 3,
    });
    assert.equal(info.balanceLabel, '查询中…');
    assert.equal(info.balanceKind, 'busy');
    assert.equal(info.resetLabel, '查询中…');
    assert.equal(info.resetKind, 'busy');
  }
});

test('each endpoint fails independently and keeps the other column usable', async () => {
  const info = await quota({
    id: 5,
    quota_check_status: 'success',
    quota_error: 'HTTP 500',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
    reset_credits_available: 2,
    reset_credits_applicable: 2,
    reset_credits_expires_at: '2026-07-17T17:38:38+00:00',
  });
  assert.equal(info.balanceLabel, '查询失败');
  assert.equal(info.balanceKind, 'failed');
  assert.equal(info.resetLabel, '2 张');
  assert.match(info.resetExpiryLabel, /^最近到期 /);
  assert.match(info.tooltip, /额度查询失败：HTTP 500/);
});

test('unlimited balance and applicable resets are spelled out', async () => {
  const info = await quota({
    id: 6,
    quota_check_status: 'success',
    quota_unlimited: true,
    reset_credits_available: 3,
    reset_credits_applicable: 1,
    reset_credits_detail: [
      { id: 'c1', status: 'available', expires_at: '2026-07-17T17:38:38Z' },
      { id: 'c2', status: 'redeemed', expires_at: '2026-06-01T00:00:00Z' },
    ],
  });
  assert.equal(info.balanceLabel, '不限量');
  assert.match(info.tooltip, /当前可应用 1 张/);
  assert.match(info.tooltip, /重置券到期：2026-07-17T17:38:38Z（available）/);
});

test('a checked account without a recognisable value says 无数据', async () => {
  const info = await quota({
    id: 8,
    quota_check_status: 'success',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
    reset_credits_checked_at: '2026-10-06T10:00:00+00:00',
  });
  assert.equal(info.balanceLabel, '无数据');
  assert.equal(info.balanceKind, 'unknown');
  assert.equal(info.resetLabel, '无数据');
  assert.match(info.checkedLabel, /^查询于 /);
});

test('a failed check without endpoint detail still reads as 查询失败', async () => {
  const info = await quota({ id: 7, quota_check_status: 'failed' });
  assert.equal(info.balanceLabel, '查询失败');
  assert.equal(info.resetLabel, '查询失败');
  assert.equal(info.error, '');
});

test('credits.has_credits separates 用完 from 本来就没有额度', async () => {
  const noEntitlement = await quota({
    id: 20,
    quota_check_status: 'success',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
    quota_has_credits: false,
    quota_balance: '0.00',
    quota_balance_amount: 0,
    quota_currency: 'USD',
  });
  assert.equal(noEntitlement.balanceLabel, '无额度权益');
  assert.equal(noEntitlement.balanceKind, 'empty');
  assert.match(noEntitlement.tooltip, /该账号没有 credits 额度（不是用完了）/);

  const drained = await quota({
    id: 21,
    quota_check_status: 'success',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
    quota_has_credits: true,
    quota_balance: '0.00',
    quota_balance_amount: 0,
    quota_currency: 'USD',
  });
  assert.equal(drained.balanceLabel, '0.00 USD');
  assert.equal(drained.balanceKind, 'empty');
  assert.match(drained.tooltip, /余额已用完/);

  const fallback = await quota({
    id: 22,
    quota_check_status: 'success',
    quota_checked_at: '2026-10-06T10:00:00+00:00',
    quota_balance: '7.50',
    quota_balance_fallback: true,
  });
  assert.match(fallback.tooltip, /余额数值来自 wham\/usage 的 credits\.balance/);
});

test('usage windows expose progress bar values and a compact countdown', async () => {
  const resetAt = new Date(Date.now() + 4 * 3600 * 1000).toISOString();
  const info = await usage({
    id: 40,
    quota_check_status: 'success',
    usage_checked_at: new Date().toISOString(),
    usage_5h_percent: 22,
    usage_5h_window_seconds: 18000,
    usage_5h_reset_at: resetAt,
    usage_5h_started: true,
    // 上游异常值也要截断，避免进度条溢出。
    usage_week_percent: 130,
    usage_week_window_seconds: 2592000,
    usage_week_started: true,
  });
  assert.equal(info.fiveHour.shortLabel, '5h');
  assert.equal(info.fiveHour.barPercent, 22);
  assert.equal(info.fiveHour.percentLabel, '22%');
  assert.match(info.fiveHour.countdownLabel, /^4 小时后重置$/);
  assert.equal(info.week.shortLabel, '月');
  assert.equal(info.week.barPercent, 100);
  assert.equal(info.week.percentLabel, '已用尽');
  assert.equal(info.week.countdownLabel, '');
});

test('missing usage data leaves the bar empty instead of faking 0%', async () => {
  const info = await usage({
    id: 41,
    quota_check_status: 'success',
    usage_checked_at: '2026-10-06T10:00:00+00:00',
  });
  assert.equal(info.fiveHour.barPercent, null);
  assert.equal(info.fiveHour.percentLabel, '无数据');
  assert.equal(info.fiveHour.countdownLabel, '');
  assert.equal(info.week.barPercent, null);
});

test('5h and weekly usage windows show used percent with reset time', async () => {
  const info = await usage({
    id: 30,
    quota_check_status: 'success',
    usage_checked_at: '2026-10-06T10:00:00+00:00',
    usage_plan_type: 'plus',
    usage_5h_percent: 22,
    usage_5h_window_seconds: 18000,
    usage_5h_reset_at: '2026-10-06T15:00:00+00:00',
    usage_5h_started: true,
    usage_week_percent: 94,
    usage_week_window_seconds: 604800,
    usage_week_reset_at: '2026-10-11T00:00:00+00:00',
    usage_week_started: true,
  });
  assert.equal(info.fiveHour.percentLabel, '22%');
  assert.equal(info.fiveHour.kind, 'ok');
  assert.equal(info.fiveHour.windowLabel, '5 小时');
  assert.match(info.fiveHour.resetLabel, /^重置 /);
  assert.match(info.fiveHour.tooltip, /5 小时窗口已用 22%，剩 78%/);
  assert.equal(info.week.percentLabel, '94%');
  assert.equal(info.week.kind, 'warn');
  assert.equal(info.week.windowLabel, '周');
});

test('a monthly long window is labelled 月 and 100% reads as 已用尽', async () => {
  const info = await usage({
    id: 31,
    quota_check_status: 'success',
    usage_checked_at: '2026-10-06T10:00:00+00:00',
    usage_week_percent: 100,
    usage_week_window_seconds: 2592000,
    usage_week_reset_after_seconds: 1209600,
    usage_week_started: true,
    usage_limit_reached: true,
    usage_limit_reached_type: 'secondary',
  });
  assert.equal(info.week.percentLabel, '已用尽');
  assert.equal(info.week.kind, 'limit');
  assert.equal(info.week.windowLabel, '月');
  assert.equal(info.limitReached, true);
  assert.match(info.week.tooltip, /账号当前已被限流/);
  // reset_after_seconds 以 usage_checked_at 为锚点换算成绝对时间。
  assert.match(info.week.resetLabel, /^重置 /);
});

test('an untouched window shows 0% as 未使用 while a missing window shows 无数据', async () => {
  const info = await usage({
    id: 32,
    quota_check_status: 'success',
    usage_checked_at: '2026-10-06T10:00:00+00:00',
    usage_5h_percent: 0,
    usage_5h_window_seconds: 18000,
    usage_5h_reset_after_seconds: 18000,
    usage_5h_started: false,
  });
  assert.equal(info.fiveHour.percentLabel, '0%');
  assert.equal(info.fiveHour.kind, 'idle');
  assert.match(info.fiveHour.tooltip, /本窗口尚未开始使用/);
  assert.equal(info.week.percentLabel, '无数据');
  assert.equal(info.week.kind, 'unknown');
});

test('usage failures and in-flight checks keep their own wording', async () => {
  const failed = await usage({
    id: 33,
    quota_check_status: 'failed',
    usage_error: 'HTTP 500',
    usage_checked_at: '2026-10-06T10:00:00+00:00',
  });
  assert.equal(failed.fiveHour.percentLabel, '查询失败');
  assert.equal(failed.fiveHour.kind, 'failed');
  assert.match(failed.fiveHour.tooltip, /HTTP 500/);

  const checking = await usage({ id: 34, quota_check_status: 'running' });
  assert.equal(checking.fiveHour.percentLabel, '查询中…');
  assert.equal(checking.fiveHour.kind, 'busy');
  assert.equal(checking.week.kind, 'busy');
});
