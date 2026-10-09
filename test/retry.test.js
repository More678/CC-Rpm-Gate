'use strict';

// 429 重试钩子单元测试：两种暂存模式、请求头覆盖、预算/上限、配置归一化、调度器插队。
const test = require('node:test');
const assert = require('node:assert/strict');

const { RetryHook, nextMinuteWaitMs, requestOverrides } = require('../src/retry');
const { loadConfig, normalizeRetry, normalizeUpstreams, DEFAULT_RETRY } = require('../src/config');
const { Scheduler } = require('../src/scheduler');
const { FakeClock, tick } = require('./fake-clock');

function hook(overrides = {}, wallNow = () => 0) {
  return new RetryHook({ ...DEFAULT_RETRY, ...overrides }, { wallNow });
}

test('nextMinuteWaitMs：对齐到下一个 :00 / :01', () => {
  assert.equal(nextMinuteWaitMs(30000, 0), 30000, ':30 → 等到下一分钟 :00');
  assert.equal(nextMinuteWaitMs(30000, 1000), 31000, ':30 → 等到下一分钟 :01');
  assert.equal(nextMinuteWaitMs(59500, 1000), 1500, ':59.5 → 1.5 秒后就是 :01');
  assert.equal(nextMinuteWaitMs(300, 1000), 700, ':00.3 → 本分钟的 :01 还没到，等它');
  assert.equal(nextMinuteWaitMs(1000, 1000), 60000, '正好 :01 → 等下一分钟的 :01，不返回 0');
  assert.equal(nextMinuteWaitMs(0, 0), 60000, '正好 :00 且 offset=0 → 等满一分钟');
  assert.equal(nextMinuteWaitMs(120000 + 45000, 1000), 16000, '跨多分钟也只看秒数');
});

test('cooldown 模式：固定等 cooldownMs，Retry-After 更大时听上游', () => {
  const h = hook({ mode: 'cooldown', cooldownMs: 5000 });
  assert.deepEqual(h.decide({ status: 429, attempt: 1 }), { action: 'retry', waitMs: 5000, mode: 'cooldown' });
  assert.equal(h.decide({ status: 429, attempt: 1, retryAfterMs: 9000 }).waitMs, 9000);
  assert.equal(h.decide({ status: 429, attempt: 1, retryAfterMs: 1000 }).waitMs, 5000, '上游给得短不缩短');

  const deaf = hook({ mode: 'cooldown', cooldownMs: 5000, respectRetryAfter: false });
  assert.equal(deaf.decide({ status: 429, attempt: 1, retryAfterMs: 9000 }).waitMs, 5000);
});

test('nextMinute 模式：按墙上时钟对齐，Retry-After 跨过边界时顺延', () => {
  let now = 30000; // :30
  const h = hook({ mode: 'nextMinute', nextMinuteOffsetMs: 1000 }, () => now);
  assert.deepEqual(h.decide({ status: 429, attempt: 1 }), { action: 'retry', waitMs: 31000, mode: 'nextMinute' });

  now = 59000; // :59，下一个 :01 只剩 2 秒，但上游要求等 5 秒
  assert.equal(h.decide({ status: 429, attempt: 1, retryAfterMs: 5000 }).waitMs, 5000 + 57000,
    '从 :04 再对齐到下一个 :01');
});

test('次数用尽 / 未启用 / 非限流状态码：透传', () => {
  const h = hook({ maxRetries: 2 });
  assert.equal(h.decide({ status: 429, attempt: 1 }).action, 'retry');
  assert.equal(h.decide({ status: 429, attempt: 2 }).action, 'retry');
  assert.deepEqual(h.decide({ status: 429, attempt: 3 }), { action: 'passthrough', reason: 'max-retries' });
  assert.equal(h.stats.exhausted, 1);

  assert.equal(hook({ enabled: false }).decide({ status: 429, attempt: 1 }).reason, 'disabled');
  assert.equal(hook({ maxRetries: 0 }).decide({ status: 429, attempt: 1 }).reason, 'retry-disabled');
  assert.equal(hook().decide({ status: 503, attempt: 1 }).reason, 'not-retryable');
  assert.equal(hook({ statuses: [503] }).decide({ status: 503, attempt: 1 }).action, 'retry',
    'statuses 可以自定义');
});

test('暂存预算与暂存上限：超了就透传', () => {
  const h = hook({ cooldownMs: 10000, maxHoldMs: 25000, maxRetries: 5 });
  assert.equal(h.decide({ status: 429, attempt: 1, heldMs: 0 }).action, 'retry');
  assert.equal(h.decide({ status: 429, attempt: 2, heldMs: 10000 }).action, 'retry');
  assert.equal(h.decide({ status: 429, attempt: 3, heldMs: 20000 }).reason, 'hold-budget',
    '20s + 10s 超过 25s 预算');

  const full = hook({ maxStashed: 1 });
  const at = full.enter(1000);
  assert.equal(full.decide({ status: 429, attempt: 1 }).reason, 'stash-full');
  full.leave(at, 1000);
  assert.equal(full.decide({ status: 429, attempt: 1 }).action, 'retry', '腾出位置后又能暂存');
});

test('请求头覆盖：x-gate-max-retries / x-gate-retry-mode', () => {
  assert.deepEqual(requestOverrides({ 'x-gate-max-retries': '5', 'x-gate-retry-mode': 'NEXTMINUTE' }),
    { maxRetries: 5, mode: 'nextMinute' });
  assert.deepEqual(requestOverrides({ 'x-gate-max-retries': '-1', 'x-gate-retry-mode': 'bogus' }), {},
    '非法值忽略');
  assert.deepEqual(requestOverrides({}), {});

  const h = hook({ maxRetries: 1, mode: 'cooldown', cooldownMs: 100 }, () => 30000);
  const ov = { maxRetries: 3, mode: 'nextMinute' };
  assert.equal(h.decide({ status: 429, attempt: 2, overrides: ov }).action, 'retry', '按请求放宽次数');
  assert.equal(h.decide({ status: 429, attempt: 1, overrides: ov }).mode, 'nextMinute');
  assert.equal(h.decide({ status: 429, attempt: 1, overrides: { maxRetries: 0 } }).reason, 'retry-disabled',
    '单个请求也能关掉重试');
});

test('快照：暂存数、下一次放行倒计时、统计', () => {
  let now = 1000;
  const h = hook({}, () => now);
  const a = h.enter(5000);
  h.enter(8000);
  let snap = h.snapshot();
  assert.equal(snap.stashed, 2);
  assert.equal(snap.nextReleaseInMs, 5000);
  assert.equal(snap.stats.peakStashed, 2);

  now = 3000;
  h.leave(a, 2000);
  snap = h.snapshot();
  assert.equal(snap.stashed, 1);
  assert.equal(snap.nextReleaseInMs, 6000, '剩下那个 9000 放行，还差 6000');
  assert.equal(snap.stats.heldMsTotal, 2000);
});

test('配置：retry 默认值、简写、环境变量覆盖、非法值报错', () => {
  const cfg = loadConfig({ file: null, env: {} });
  assert.deepEqual(cfg.retry, DEFAULT_RETRY);

  assert.equal(normalizeRetry(false).enabled, false, '写 false 等于关闭');
  assert.equal(normalizeRetry({ cooldownSeconds: 30 }).cooldownMs, 30000);
  assert.equal(normalizeRetry({ cooldown: '2m' }).cooldownMs, 120000);
  assert.deepEqual(normalizeRetry({ statuses: '429, 503' }).statuses, [429, 503]);
  assert.ok(!('typo' in normalizeRetry({ typo: 1 })), '未知字段不保留');

  const env = loadConfig({
    file: null,
    env: {
      GATE_MAX_RETRIES: '4',
      GATE_RETRY_MODE: 'nextMinute',
      GATE_RETRY_COOLDOWN_SECONDS: '20',
      GATE_RETRY_ENABLED: 'yes',
      GATE_RETRY_NEXT_MINUTE_OFFSET_MS: '0',
    },
  });
  assert.equal(env.retry.maxRetries, 4);
  assert.equal(env.retry.mode, 'nextMinute');
  assert.equal(env.retry.cooldownMs, 20000);
  assert.equal(env.retry.enabled, true);
  assert.equal(env.retry.nextMinuteOffsetMs, 0, '0 = 对齐到 :00');

  assert.throws(() => normalizeRetry({ mode: 'sometimes' }), /retry\.mode/);
  assert.throws(() => normalizeRetry({ maxRetries: -1 }), /retry\.maxRetries/);
  assert.throws(() => normalizeRetry({ statuses: [] }), /retry\.statuses/);
  assert.throws(() => normalizeRetry({ statuses: [42] }), /状态码不合法/);
  assert.throws(() => normalizeRetry({ nextMinuteOffsetMs: 60000 }), /小于 60000/);
  assert.throws(() => loadConfig({ file: null, env: { GATE_RETRY_ENABLED: 'maybe' } }), /true\/false/);
});

test('配置：命名上游表', () => {
  assert.deepEqual(normalizeUpstreams({ a: 'https://x.example.com/', b: 'http://127.0.0.1:9/v1' }),
    { a: 'https://x.example.com', b: 'http://127.0.0.1:9/v1' });
  assert.deepEqual(normalizeUpstreams('{"a":"http://h"}'), { a: 'http://h' }, '环境变量可以给 JSON 字符串');
  assert.throws(() => normalizeUpstreams({ 'bad name': 'http://h' }), /名字/);
  assert.throws(() => normalizeUpstreams({ a: 'ftp://h' }), /http\/https/);
  const cfg = loadConfig({ file: null, env: {}, overrides: { upstreams: { k: 'http://k:1' } } });
  assert.deepEqual(cfg.upstreams, { k: 'http://k:1' });
});

test('调度器：priority 插到普通请求前面，exactMs 精确冷却', async () => {
  const clock = new FakeClock();
  const s = new Scheduler({
    effectiveLimits: [{ windowMs: 60000, max: 1000 }],
    maxConcurrent: 1, minGapMs: 0, maxQueue: 1, queueTimeoutMs: 10000,
    penaltyMs: 100, maxPenaltyMs: 500,
  }, clock.deps);

  const order = [];
  const take = (name, opts) => {
    const p = s.acquire({ name }, opts).then((slot) => { order.push(name); return slot; });
    p.catch(() => {}); // 结尾 close() 会拒掉还在排队的 B，这里吞掉
    return p;
  };

  const a = await take('A');
  take('B');                                          // 普通排队，占满 maxQueue=1
  const c = take('C', { priority: true });            // 优先：不受 maxQueue 限制
  take('D', { priority: true });                      // 优先组内部 FIFO
  await tick();
  assert.equal(s.snapshot().queuedPriority, 2);

  assert.equal(s.penalize(NaN, { exactMs: 2000 }), 2000, '不做指数退避、不被 maxPenaltyMs 削顶');
  a.release();
  await clock.advance(1999);
  assert.deepEqual(order, ['A'], '冷却期间谁都不放');
  await clock.advance(1);
  assert.deepEqual(order, ['A', 'C']);
  (await c).release();
  await tick();
  assert.deepEqual(order, ['A', 'C', 'D'], 'D 也是优先，排在 B 前面');
  s.close();
});

test('调度器：priority 请求可以单独放宽排队超时', async () => {
  const clock = new FakeClock();
  const s = new Scheduler({
    effectiveLimits: [{ windowMs: 60000, max: 1000 }],
    maxConcurrent: 1, minGapMs: 0, maxQueue: 10, queueTimeoutMs: 100,
    penaltyMs: 0, maxPenaltyMs: 0,
  }, clock.deps);
  const a = await s.acquire({});
  let state = 'pending';
  s.acquire({}, { priority: true, timeoutMs: 5000 }).then(() => { state = 'ok'; }, () => { state = 'err'; });
  await clock.advance(1000);
  assert.equal(state, 'pending', '超过默认 100ms 也没被踢掉');
  a.release();
  await tick();
  assert.equal(state, 'ok');
  s.close();
});
