// Run: node --test tests/test_nuxt_account_selection.js
// Nuxt 账号页：全选（筛选结果 / 所有账号）与批量操作自动分批提交。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..');
const modulePromise = import(path.join(root, 'frontend/app/utils/accounts.ts'));
const page = fs.readFileSync(path.join(root, 'frontend/app/pages/accounts.vue'), 'utf8');
const modal = fs.readFileSync(
  path.join(root, 'frontend/app/components/accounts/OperationsModal.vue'),
  'utf8',
);

test('批量接口上限与后端校验保持一致', async () => {
  const { accountBatchLimit } = await modulePromise;
  assert.equal(accountBatchLimit('live'), 500);
  assert.equal(accountBatchLimit('extract'), 500);
  assert.equal(accountBatchLimit('note'), 5000);
  assert.equal(accountBatchLimit('archive'), 5000);
  assert.equal(accountBatchLimit('export'), 5000);
  assert.equal(accountBatchLimit('secret'), 5000);
  assert.equal(accountBatchLimit('delete'), 5000);
  assert.equal(accountBatchLimit('download'), 1000);
  // 未知操作退回最保守的 500，避免超过后端上限。
  assert.equal(accountBatchLimit('mystery'), 500);
  assert.equal(accountBatchLimit(''), 500);
});

test('chunkAccountIds 按上限切片并过滤非法 ID', async () => {
  const { chunkAccountIds } = await modulePromise;
  assert.deepEqual(chunkAccountIds([], 500), []);
  assert.deepEqual(chunkAccountIds([1, 2, 3], 3), [[1, 2, 3]]);
  assert.deepEqual(chunkAccountIds([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
  assert.deepEqual(chunkAccountIds([0, -1, 2, 3.5, 4], 500), [[2, 4]]);
  // 非法批次大小按 1 处理，绝不生成超大请求。
  assert.deepEqual(chunkAccountIds([1, 2], 0), [[1], [2]]);
  assert.deepEqual(chunkAccountIds([1, 2], -5), [[1], [2]]);
  // 支持 Set 输入（页面用 Set 保存选中 ID）。
  assert.deepEqual(chunkAccountIds(new Set([7, 8, 9]), 2), [[7, 8], [9]]);
  const many = Array.from({ length: 12001 }, (_, index) => index + 1);
  const chunks = chunkAccountIds(many, 5000);
  assert.equal(chunks.length, 3);
  assert.ok(chunks.every((chunk) => chunk.length <= 5000));
  assert.equal(chunks.flat().length, 12001);
});

test('mergeAccountResults 合并分批响应并重算计数', async () => {
  const { mergeAccountResults } = await modulePromise;
  const merged = mergeAccountResults([
    { ok: true, started: [{ id: 1 }, { id: 2 }], started_count: 2, skipped: [], skipped_count: 0 },
    { ok: true, started: [{ id: 3 }], started_count: 1, skipped: [{ id: 4, reason: '缺少 access_token' }], skipped_count: 1 },
  ]);
  assert.equal(merged.started.length, 3);
  assert.equal(merged.started_count, 3);
  assert.equal(merged.skipped.length, 1);
  assert.equal(merged.skipped_count, 1);
  assert.equal(merged.ok, true);
  // 单批时保持原样（引用可以不同，内容一致即可）。
  assert.deepEqual(mergeAccountResults([{ updated: [1], updated_count: 1 }]), {
    updated: [1],
    updated_count: 1,
  });
  assert.deepEqual(mergeAccountResults([]), {});
  // 标量保留首个出现的值，避免后面的批次覆盖 idempotency_key 等参数。
  assert.equal(
    mergeAccountResults([
      { ok: true, idempotency_key: 'batch-1' },
      { ok: true, idempotency_key: 'batch-2' },
    ]).idempotency_key,
    'batch-1',
  );
  // 标识与分页字段不能累加：多批返回的 account_id 不能被拼成一个错误 ID。
  assert.deepEqual(
    mergeAccountResults([
      { ok: true, account_id: 12, page: 1, page_size: 5000, total: 3 },
      { ok: true, account_id: 13, page: 1, page_size: 5000, total: 4 },
    ]),
    { ok: true, account_id: 12, page: 1, page_size: 5000, total: 3 },
  );
  assert.equal(
    mergeAccountResults([{ created_count: 2 }, { created_count: 3 }]).created_count,
    5,
  );
});

test('账号页提供两种全选且不再有 5000 上限', () => {
  assert.match(page, /selectAllAccounts\('filtered'\)/);
  assert.match(page, /selectAllAccounts\('all'\)/);
  assert.match(page, /选择全部筛选结果/);
  assert.match(page, /全选所有账号/);
  assert.match(page, /scope:/);
  assert.match(page, /"\/api\/accounts\/ids"/);
  assert.match(page, /scope === "all" \? \{\} : \{ \.\.\.applied\.value \}/);
  // 选中状态改为 ID 集合 + 行缓存，不再把整库账号对象塞进内存。
  assert.match(page, /const selectedIds = ref\(new Set<number>\(\)\)/);
  assert.match(page, /const selectedRows = ref\(new Map<number, Account>\(\)\)/);
  assert.doesNotMatch(page, /最多选择 5000 个账号/);
  assert.doesNotMatch(page, /筛选结果超过 5000/);
  assert.doesNotMatch(page, /selectedIds\.value\.size < 5000/);
});

test('账号页删除操作按后端上限分批提交', () => {
  assert.match(page, /chunkAccountIds\(target, accountBatchLimit\("delete"\)\)/);
  assert.match(page, /mergeAccountResults\(results\)/);
});

test('操作弹窗接收全量 ID 并按操作上限分批提交', () => {
  assert.match(modal, /accountIds: number\[\]/);
  assert.match(modal, /accountCount: number/);
  assert.match(modal, /const actionLimit = computed\(\(\) => accountBatchLimit\(mode\.value\)\)/);
  assert.match(modal, /chunkAccountIds\(ids\.value, actionLimit\.value\)/);
  assert.match(modal, /mergeAccountResults\(results\)/);
  // 旧的“超过上限就报错”的硬限制已经移除，改为自动分批。
  assert.doesNotMatch(modal, /本操作单次最多处理/);
  assert.doesNotMatch(modal, /accounts\.length > actionLimit/);
  // ZIP 下载无法合并，保留明确的 1000 上限提示。
  assert.match(modal, /ZIP 下载单次最多 1000 个账号/);
});

test('账号页把全量 ID 传给操作弹窗', () => {
  assert.match(page, /:account-ids="operationSeed\?\.ids \|\| \[\]"/);
  assert.match(page, /:account-count="operationSeed\?\.count \|\| 0"/);
  assert.match(page, /:key="`\$\{action\}:\$\{operationRevision\}`"/);
});
