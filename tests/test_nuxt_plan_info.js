// Run: node --test tests/test_nuxt_plan_info.js
// 账号套餐信息（Nuxt 版）与旧版控制台展示字段的一致性检查。
const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const modulePromise = import(
  path.join(__dirname, '../frontend/app/utils/accounts.ts')
);

async function plan(account) {
  const { accountPlanInfo } = await modulePromise;
  return accountPlanInfo(account);
}

test('paid plans keep billing, currency, expiry and discount from the legacy cell', async () => {
  const info = await plan({
    id: 1,
    current_plan_type: 'chatgptplusplan',
    plan_check_status: 'success',
    billing_period: 'monthly',
    billing_currency: 'USD',
    plan_expires_at: '2026-03-01T00:00:00+00:00',
    discount_amount: 20,
    discount_type: 'percentage',
    plan_checked_at: '2026-02-01T10:00:00+00:00',
    plan_check_network_route: 'proxy',
    plan_check_proxy_used: 'http://127.0.0.1:7890',
  });
  assert.equal(info.name, 'Plus');
  assert.equal(info.label, 'chatgptplusplan');
  assert.equal(info.paid, true);
  assert.equal(info.eligibilityPending, false);
  assert.deepEqual(info.lines.slice(0, 2), ['月付', 'USD']);
  assert.match(info.lines.join(' · '), /到期 /);
  assert.match(info.lines.join(' · '), /20%折扣/);
  assert.match(info.networkLabel, /^代理/);
  assert.match(info.checkedLabel, /^查询于 /);
});

test('free account with promos lists each campaign and never shows billing lines', async () => {
  const info = await plan({
    id: 2,
    current_plan_type: 'free',
    plus_trial_eligible: true,
    plan_check_status: 'success',
    billing_currency: 'USD',
    subscription_active_until: '2026-03-01T00:00:00+00:00',
    eligible_promo_campaigns: {
      plus: {
        id: 'plus-1-month-free',
        metadata: {
          plan_name: 'chatgptplusplan',
          discount: { percentage: 100 },
          duration: { num_periods: 1, period: 'month' },
        },
      },
      go: { id: 'go-discount', metadata: { promotion_type_label: '首月优惠' } },
    },
  });
  assert.equal(info.name, 'Free');
  assert.equal(info.label, 'free');
  assert.equal(info.paid, false);
  assert.equal(info.trialEligible, true);
  assert.deepEqual(info.lines, []);
  assert.match(info.activeUntilLabel, /^\d{4}/);
  assert.deepEqual(
    info.promos.map(promo => promo.summary),
    ['Plus：免费/1个月', 'Go：首月优惠']
  );
  assert.deepEqual(info.promos[0].details, ['活动 ID：plus-1-month-free']);
});

test('unknown free eligibility is flagged instead of assumed unusable', async () => {
  const pending = await plan({ id: 3, current_plan_type: 'free' });
  assert.equal(pending.eligibilityPending, true);
  assert.equal(pending.trialEligible, false);

  const eligible = await plan({
    id: 4,
    current_plan_type: 'free',
    plus_trial_eligible: true,
    plan_check_status: 'success',
  });
  assert.equal(eligible.eligibilityPending, false);

  const confirmed = await plan({
    id: 5,
    current_plan_type: 'free',
    plan_check_status: 'success',
  });
  assert.equal(confirmed.eligibilityPending, false);
});

test('queued, running and failed plan checks keep the legacy wording', async () => {
  const queued = await plan({
    id: 6,
    current_plan_type: 'free',
    plan_check_status: 'queued',
    plan_check_trigger: 'registration_auto',
  });
  assert.equal(queued.queryKind, 'pending');
  assert.equal(queued.queryLabel, '自动查询排队中');

  const running = await plan({ id: 7, plan_check_status: 'running' });
  assert.equal(running.queryKind, 'running');
  assert.equal(running.queryLabel, '套餐查询中');

  const failed = await plan({
    id: 8,
    current_plan_type: 'plus',
    plan_check_status: 'failed',
    plan_check_error: 'timeout',
    plan_last_success_at: '2026-01-01T00:00:00+00:00',
  });
  assert.equal(failed.queryKind, 'failed');
  assert.equal(failed.queryLabel, '查询失败');
  assert.equal(failed.error, 'timeout');
  assert.match(failed.lastSuccess, /^\d{4}/);

  const reasonless = await plan({ id: 8.1, plan_check_status: 'failed' });
  assert.equal(
    reasonless.error,
    '套餐查询失败，未返回具体原因',
    '失败的查询必须给出可读原因，避免空白提示'
  );
});

test('grace period and subscription errors stay visible', async () => {
  const info = await plan({
    id: 9,
    current_plan_type: 'plus',
    is_delinquent: true,
    subscription_became_delinquent_at: '2026-02-01T00:00:00+00:00',
    subscription_grace_period_end_at: '2026-02-15T00:00:00+00:00',
    subscription_error: 'HTTP 502',
  });
  assert.match(info.graceLabel, /^挽留期：开始 /);
  assert.match(info.graceLabel, /结束 /);
  assert.match(info.graceTitle, /挽留期开始：/);
  assert.equal(info.subscriptionError, 'HTTP 502');
});

test('malformed promo payloads degrade to an empty list', async () => {
  for (const value of [null, undefined, [], 'plus', 42]) {
    const info = await plan({
      id: 10,
      current_plan_type: 'free',
      eligible_promo_campaigns: value,
    });
    assert.deepEqual(info.promos, []);
  }
  const info = await plan({
    id: 11,
    current_plan_type: 'free',
    eligible_promo_campaigns: { plus: null },
  });
  assert.deepEqual(info.promos.map(promo => promo.summary), ['Plus：有优惠']);
});

test('unqueried accounts report 尚未查询 rather than a fake plan', async () => {
  const info = await plan({ id: 12 });
  assert.equal(info.name, '尚未查询');
  assert.equal(info.label, '尚未查询');
  assert.equal(info.normalized, '');
  assert.equal(info.paid, false);
  assert.equal(info.eligibilityPending, false);
  assert.deepEqual(info.lines, []);
});

test('label keeps the raw lowercase plan token used by the pill', async () => {
  assert.equal((await plan({ id: 13, plan_type: 'PLUS' })).label, 'plus');
  assert.equal((await plan({ id: 14, current_plan_type: 'Pro' })).label, 'pro');
  assert.equal(
    (await plan({ id: 15, plan_type: 'chatgptbusinessplan' })).label,
    'chatgptbusinessplan'
  );
});
