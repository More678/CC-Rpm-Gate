'use strict';

// 排队器单元测试：并发、每 X 秒 Y 次、最小间隔、429 冷却、公平性、断开、队列上限。
const test = require('node:test');
const assert = require('node:assert/strict');

const { Scheduler } = require('../src/scheduler');
const { FakeClock, tick } = require('./fake-clock');

function makeScheduler(clock, overrides = {}) {
  const cfg = {
    effectiveLimits: [{ windowMs: 60000, max: 1000 }],
    maxConcurrent: 10,
    minGapMs: 0,
    maxQueue: 100,
    queueTimeoutMs: 600000,
    penaltyMs: 1000,
    maxPenaltyMs: 60000,
    ...overrides,
  };
  return new Scheduler(cfg, clock.deps);
}

/** 发起一次 acquire 并记录结果，避免 await 卡住测试主线。 */
function track(scheduler, clock, meta = {}) {
  const rec = { state: 'pending', slot: null, error: null, at: -1 };
  const promise = scheduler.acquire(meta);
  rec.cancel = () => promise.cancel();
  promise.then(
    (slot) => { rec.state = 'ok'; rec.slot = slot; rec.at = clock.now(); },
    (err) => { rec.state = 'err'; rec.error = err; rec.at = clock.now(); },
  );
  return rec;
}

const okCount = (recs) => recs.filter((r) => r.state === 'ok').length;

test('并发上限：活跃请求数永不超过 maxConcurrent', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 2 });

  const recs = [0, 1, 2, 3, 4].map((i) => track(s, clock, { window: `w${i}` }));
  await tick();

  assert.equal(okCount(recs), 2, '一开始只能放行 2 个');
  assert.equal(s.inFlight, 2);
  assert.equal(s.snapshot().queued, 3);

  recs[0].slot.release();
  await tick();
  assert.equal(okCount(recs), 3, 'release 之后补放 1 个');
  assert.equal(s.inFlight, 2, '并发数始终不超过 2');

  recs[1].slot.release();
  recs[2].slot.release();
  await tick();
  assert.equal(okCount(recs), 5);
  assert.equal(s.inFlight, 2);

  recs[3].slot.release();
  recs[4].slot.release();
  await tick();
  assert.equal(s.inFlight, 0);
  s.close();
});

test('每 X 毫秒 Y 次：窗口打满后要等最早一次滚出去', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, {
    effectiveLimits: [{ windowMs: 300, max: 3 }],
    maxConcurrent: 10,
  });

  const recs = [0, 1, 2, 3].map(() => track(s, clock));
  await tick();
  assert.equal(okCount(recs), 3, '窗口内先放 3 个');
  assert.equal(recs[3].state, 'pending');

  await clock.advance(299);
  assert.equal(recs[3].state, 'pending', '窗口还没滚出，第 4 个继续等');

  await clock.advance(1);
  assert.equal(recs[3].state, 'ok', '窗口滚出后第 4 个放行');
  assert.equal(recs[3].at - recs[0].at, 300);
  s.close();
});

test('多条限制同时生效：每 1 秒 10 次 + 每 100 毫秒 2 次', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, {
    effectiveLimits: [{ windowMs: 100, max: 2 }, { windowMs: 1000, max: 10 }],
    maxConcurrent: 20,
  });

  const recs = Array.from({ length: 12 }, () => track(s, clock));
  await tick();
  assert.equal(okCount(recs), 2, '短窗口先卡住：100ms 内只许 2 个');

  await clock.advance(100);
  assert.equal(okCount(recs), 4, '每过 100ms 再放 2 个');

  await clock.advance(300);
  assert.equal(okCount(recs), 10, '放到 1 秒窗口的 10 个上限为止');

  await clock.advance(100);
  assert.equal(okCount(recs), 10, '长窗口打满，短窗口也无法放行');

  await clock.advance(600);
  assert.equal(okCount(recs), 12, '长窗口滚出后剩下的放完');

  const snap = s.snapshot();
  assert.deepEqual(snap.limits.map((l) => l.windowMs), [100, 1000], '快照按窗口从短到长列出');
  s.close();
});

test('最小间隔：相邻两次放行至少隔 minGapMs', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { minGapMs: 50, maxConcurrent: 10 });

  const recs = [0, 1, 2, 3].map(() => track(s, clock));
  await tick();
  assert.equal(okCount(recs), 1, '第一个立刻放，其余要等间隔');

  await clock.advance(200);
  assert.equal(okCount(recs), 4);

  for (let i = 1; i < recs.length; i += 1) {
    const gap = recs[i].at - recs[i - 1].at;
    assert.ok(gap >= 50, `第 ${i} 次放行间隔 ${gap}ms，应 ≥ 50ms`);
  }
  s.close();
});

test('429 冷却：penalize 之后所有窗口一起等', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { penaltyMs: 100, maxPenaltyMs: 5000 });

  const waitMs = s.penalize(200);
  assert.equal(waitMs, 200, 'Retry-After 比退避基数大时听上游的');
  assert.equal(s.nextDelayMs(), 200);

  const a = track(s, clock, { window: 'w1' });
  const b = track(s, clock, { window: 'w2' });
  await clock.advance(199);
  assert.equal(a.state, 'pending');
  assert.equal(b.state, 'pending', '冷却是全局的，换个窗口也躲不过');

  await clock.advance(1);
  assert.equal(a.state, 'ok');
  assert.equal(b.state, 'ok');

  // 连续限流指数退避：第二次 100*2=200，第三次 100*4=400
  assert.equal(s.penalize(NaN), 200);
  assert.equal(s.penalize(NaN), 400);
  s.onSuccess();
  assert.equal(s.penaltyStreak, 0, '成功一次就重置退避');
  assert.equal(s.snapshot().stats.penalties, 3);
  s.close();
});

test('冷却时间不超过 maxPenaltyMs', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { penaltyMs: 1000, maxPenaltyMs: 3000 });
  assert.equal(s.penalize(99999), 3000, '上游给的 Retry-After 再大也被削到上限');
  s.close();
});

test('公平调度：FIFO，排在后面的窗口不会被饿死', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1 });

  const a1 = track(s, clock, { window: 'A' });
  const a2 = track(s, clock, { window: 'A' });
  const a3 = track(s, clock, { window: 'A' });
  await tick();
  const b1 = track(s, clock, { window: 'B' });  // B 只排 1 个，排在 A 的 3 个后面
  await tick();
  assert.equal(a1.state, 'ok');
  assert.equal(b1.state, 'pending');

  // A 又追加一个，不能插到 B 前面
  const a4 = track(s, clock, { window: 'A' });
  await tick();

  a1.slot.release();
  await tick();
  assert.equal(a2.state, 'ok');

  a2.slot.release();
  await tick();
  assert.equal(a3.state, 'ok');

  a3.slot.release();
  await tick();
  assert.equal(b1.state, 'ok', '轮到 B，不被 A 的后续请求挤掉');
  assert.equal(a4.state, 'pending', '后入队的 A4 排在 B 之后');

  b1.slot.release();
  await tick();
  assert.equal(a4.state, 'ok');
  a4.slot.release();
  s.close();
});

test('客户端断开：排队中取消立即出队，不占名额', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1 });

  const a = track(s, clock, { window: 'A' });
  const b = track(s, clock, { window: 'B' });
  const c = track(s, clock, { window: 'C' });
  await tick();
  assert.equal(a.state, 'ok');
  assert.equal(s.snapshot().queued, 2);

  b.cancel();
  await tick();
  assert.equal(b.state, 'err');
  assert.equal(b.error.code, 'CLIENT_ABORTED');
  assert.equal(b.error.status, 499);
  assert.equal(s.snapshot().queued, 1, 'B 立刻出队');
  assert.equal(s.inFlight, 1, '取消不占用并发名额');

  a.slot.release();
  await tick();
  assert.equal(c.state, 'ok', '名额直接给 C');
  assert.equal(s.snapshot().stats.aborted, 1);

  b.cancel(); // 幂等：重复取消不应改变计数
  await tick();
  assert.equal(s.snapshot().stats.aborted, 1);
  c.slot.release();
  s.close();
});

test('队列上限：超过 maxQueue 直接 503，不无限堆积', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1, maxQueue: 2 });

  const recs = [0, 1, 2, 3].map(() => track(s, clock));
  await tick();

  assert.equal(recs[0].state, 'ok');
  assert.equal(recs[1].state, 'pending');
  assert.equal(recs[2].state, 'pending');
  assert.equal(recs[3].state, 'err');
  assert.equal(recs[3].error.code, 'QUEUE_FULL');
  assert.equal(recs[3].error.status, 503);
  assert.equal(s.snapshot().stats.rejected, 1);
  s.close();
});

test('排队超时：超过 queueTimeoutMs 返回 504', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1, queueTimeoutMs: 500 });

  const a = track(s, clock);
  const b = track(s, clock);
  await tick();

  await clock.advance(500);
  assert.equal(b.state, 'err');
  assert.equal(b.error.code, 'QUEUE_TIMEOUT');
  assert.equal(b.error.status, 504);
  assert.equal(s.snapshot().queued, 0);
  assert.equal(s.snapshot().stats.timedOut, 1);

  a.slot.release();
  s.close();
});

test('release 幂等：重复调用不会把并发数扣穿', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1 });
  const a = track(s, clock);
  await tick();
  a.slot.release();
  a.slot.release();
  a.slot.release();
  await tick();
  assert.equal(s.inFlight, 0);
  s.close();
});

test('关闭闸门：排队中的请求全部收到 503', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, { maxConcurrent: 1 });
  const a = track(s, clock);
  const b = track(s, clock);
  await tick();

  s.close();
  await tick();
  assert.equal(b.state, 'err');
  assert.equal(b.error.code, 'GATE_CLOSED');

  const after = track(s, clock);
  await tick();
  assert.equal(after.state, 'err', '关闭后不再受理新请求');
  assert.equal(after.error.code, 'GATE_CLOSED');
  a.slot.release();
});

test('兼容旧配置：只给 rpm/windowMs 也能正常限流', async () => {
  const clock = new FakeClock();
  const s = new Scheduler({
    rpm: 2,
    windowMs: 200,
    maxConcurrent: 10,
    minGapMs: 0,
    maxQueue: 10,
    queueTimeoutMs: 10000,
    penaltyMs: 0,
    maxPenaltyMs: 0,
  }, clock.deps);

  const recs = [0, 1, 2].map(() => track(s, clock));
  await tick();
  assert.equal(okCount(recs), 2);
  await clock.advance(200);
  assert.equal(okCount(recs), 3);
  s.close();
});

test('快照：报告窗口用量与下一个名额的等待时间', async () => {
  const clock = new FakeClock();
  const s = makeScheduler(clock, {
    effectiveLimits: [{ windowMs: 1000, max: 2 }],
    maxConcurrent: 5,
  });

  const recs = [0, 1, 2].map(() => track(s, clock));
  await tick();

  const snap = s.snapshot();
  assert.equal(snap.limits[0].used, 2);
  assert.equal(snap.limits[0].max, 2);
  assert.equal(snap.limits[0].resetInMs, 1000);
  assert.equal(snap.queued, 1);
  assert.equal(snap.inFlight, 2);
  assert.equal(snap.stats.admitted, 2);

  recs[0].slot.release();
  recs[1].slot.release();
  await clock.advance(1000);
  assert.equal(recs[2].state, 'ok');
  assert.equal(s.snapshot().limits[0].used, 1, '旧的放行记录滚出窗口后不再计数');
  s.close();
});
