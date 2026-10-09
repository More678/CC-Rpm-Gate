'use strict';

// 代理层的 429 暂存/重试端到端测试：真上游 + 真闸门 + 注入的重试钩子。
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { Scheduler } = require('../src/scheduler');
const { createHandler } = require('../src/proxy');
const { RetryHook } = require('../src/retry');
const { DEFAULT_RETRY } = require('../src/config');

const silentLogger = { debug() {}, info() {}, warn() {}, error() {}, close() {} };

function listen(handler) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(handler);
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => resolve({
      server,
      port: server.address().port,
      url: `http://127.0.0.1:${server.address().port}`,
      close: () => new Promise((done) => server.close(done)),
    }));
  });
}

/** 搭一套「假上游 + 闸门 + 重试钩子」。retry 里的字段覆盖 DEFAULT_RETRY。 */
async function makeStack(upstreamHandler, { retry = {}, wallNow, ...cfgOverrides } = {}) {
  const upstream = await listen(upstreamHandler);
  const cfg = {
    host: '127.0.0.1',
    port: 0,
    upstream: upstream.url,
    rpm: 1000,
    windowMs: 60000,
    limits: [],
    effectiveLimits: [{ windowMs: 60000, max: 1000 }],
    maxConcurrent: 10,
    minGapMs: 0,
    maxQueue: 100,
    queueTimeoutMs: 5000,
    penaltyMs: 50,
    maxPenaltyMs: 500,
    bypassPaths: ['/v1/messages/count_tokens'],
    upstreams: {},
    retry: { ...DEFAULT_RETRY, cooldownMs: 60, maxHoldMs: 5000, ...retry },
    logFile: '',
    logLevel: 'silent',
    ...cfgOverrides,
  };
  const scheduler = new Scheduler(cfg, { logger: silentLogger });
  const retryHook = new RetryHook(cfg.retry, { wallNow, logger: silentLogger });
  const gate = await listen(createHandler(cfg, scheduler, silentLogger, { retryHook }));
  return {
    cfg,
    scheduler,
    retryHook,
    upstreamUrl: upstream.url,
    gateUrl: gate.url,
    async close() {
      scheduler.close();
      await gate.close();
      await upstream.close();
    },
  };
}

function request(url, { method = 'POST', headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const t = new URL(url);
    const req = http.request({
      hostname: t.hostname, port: t.port, path: t.pathname + t.search, method, headers,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8'),
      }));
      res.on('error', reject);
    });
    req.on('error', reject);
    if (body !== null) req.write(body);
    req.end();
  });
}

/** 前 n 次回 429，之后回 200。记录每次收到的请求体。 */
function limitedUpstream(n, { retryAfter, status = 429 } = {}) {
  const state = { hits: 0, bodies: [] };
  const handler = (req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      state.hits += 1;
      state.bodies.push(Buffer.concat(chunks).toString('utf8'));
      if (state.hits <= n) {
        const h = { 'content-type': 'application/json', 'x-attempt': String(state.hits) };
        if (retryAfter !== undefined) h['retry-after'] = retryAfter;
        res.writeHead(status, h);
        res.end('{"type":"error","error":{"type":"rate_limit_error"}}');
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true, attempt: state.hits }));
    });
  };
  return { state, handler };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('429 暂存重试：客户端只看到最终的 200，上游被打了 2 次', async () => {
  const up = limitedUpstream(1);
  const stack = await makeStack(up.handler, { retry: { mode: 'cooldown', cooldownMs: 60 } });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, {
      headers: { 'content-type': 'application/json' },
      body: '{"model":"claude","n":1}',
    });
    assert.equal(res.status, 200, '429 被闸门自己扛掉，客户端看不到');
    assert.equal(JSON.parse(res.body).attempt, 2);
    assert.equal(up.state.hits, 2);
    assert.deepEqual(up.state.bodies, ['{"model":"claude","n":1}', '{"model":"claude","n":1}'],
      '重试时请求体原样重放');

    const snap = stack.retryHook.snapshot();
    assert.equal(snap.stats.retried, 1);
    assert.equal(snap.stats.stashed, 1);
    assert.equal(snap.stats.recovered, 1);
    assert.equal(snap.stashed, 0, '放行后暂存计数归零');
    assert.ok(stack.scheduler.snapshot().stats.penalties >= 1, '暂存期间全局冷却也生效');
  } finally {
    await stack.close();
  }
});

test('重试次数用尽：把上游最后那个 429 原样透传（状态码/头/体都不改）', async () => {
  const up = limitedUpstream(99, { retryAfter: '1' });
  const stack = await makeStack(up.handler, { retry: { maxRetries: 2, cooldownMs: 40 } });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, { body: '{}' });
    assert.equal(res.status, 429);
    assert.equal(res.headers['retry-after'], '1', '上游的 Retry-After 带给客户端');
    assert.equal(res.headers['x-attempt'], '3', '回的是最后一次的响应头');
    assert.ok(res.body.includes('rate_limit_error'), '上游错误体不被改写');
    assert.equal(up.state.hits, 3, '首次 + 2 次重试');

    const snap = stack.retryHook.snapshot();
    assert.equal(snap.stats.retried, 2);
    assert.equal(snap.stats.exhausted, 1);
    assert.equal(snap.stats.recovered, 0);
  } finally {
    await stack.close();
  }
});

test('529 也走暂存重试；503 不碰', async () => {
  const up529 = limitedUpstream(1, { status: 529 });
  const s1 = await makeStack(up529.handler, { retry: { cooldownMs: 40 } });
  try {
    assert.equal((await request(`${s1.gateUrl}/v1/messages`, { body: '{}' })).status, 200);
    assert.equal(up529.state.hits, 2);
  } finally {
    await s1.close();
  }

  const up503 = limitedUpstream(1, { status: 503 });
  const s2 = await makeStack(up503.handler);
  try {
    const res = await request(`${s2.gateUrl}/v1/messages`, { body: '{}' });
    assert.equal(res.status, 503, '503 不算限流，原样透传');
    assert.equal(up503.state.hits, 1, '不重试');
    assert.equal(s2.scheduler.snapshot().stats.penalties, 0);
    assert.equal(s2.retryHook.snapshot().stats.retried, 0);
  } finally {
    await s2.close();
  }
});

test('nextMinute 模式：等到下一个 :01 再重试', async () => {
  const up = limitedUpstream(1);
  // 假墙上时钟停在 :59.90，下一个 :01 还有 1.1 秒；offset 调小让测试跑得快
  const wallNow = () => 59900;
  const stack = await makeStack(up.handler, {
    wallNow,
    retry: { mode: 'nextMinute', nextMinuteOffsetMs: 100 },
  });
  try {
    const t0 = Date.now();
    const res = await request(`${stack.gateUrl}/v1/messages`, { body: '{}' });
    const elapsed = Date.now() - t0;
    assert.equal(res.status, 200);
    assert.equal(up.state.hits, 2);
    // :59.90 → 下一个 :00.10 = 200ms
    assert.ok(elapsed >= 180, `应等约 200ms 才重试，实际 ${elapsed}ms`);
    assert.ok(elapsed < 3000, `不该等满一分钟，实际 ${elapsed}ms`);
  } finally {
    await stack.close();
  }
});

test('按请求头覆盖：x-gate-max-retries=0 时直接透传 429', async () => {
  const up = limitedUpstream(99);
  const stack = await makeStack(up.handler, { retry: { maxRetries: 3, cooldownMs: 40 } });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, {
      headers: { 'x-gate-max-retries': '0' }, body: '{}',
    });
    assert.equal(res.status, 429);
    assert.equal(up.state.hits, 1, '这个请求不重试');

    const res2 = await request(`${stack.gateUrl}/v1/messages`, {
      headers: { 'x-gate-max-retries': '1' }, body: '{}',
    });
    assert.equal(res2.status, 429);
    assert.equal(up.state.hits, 3, '第二个请求按头里的 1 次重试');
  } finally {
    await stack.close();
  }
});

test('暂存期间客户端断开：立刻放手，不再重试', async () => {
  const up = limitedUpstream(99);
  const stack = await makeStack(up.handler, { retry: { maxRetries: 5, cooldownMs: 3000 } });
  try {
    const t = new URL(`${stack.gateUrl}/v1/messages`);
    const req = http.request({
      hostname: t.hostname, port: t.port, path: t.pathname, method: 'POST',
    });
    req.on('error', () => {});
    req.end('{}');

    // 等它被 429 弹回来、进入暂存
    for (let i = 0; i < 100 && stack.retryHook.stashed === 0; i += 1) await sleep(20);
    assert.equal(stack.retryHook.stashed, 1, '已进入暂存');
    assert.equal(up.state.hits, 1);

    req.destroy();
    for (let i = 0; i < 100 && stack.retryHook.stashed !== 0; i += 1) await sleep(20);
    assert.equal(stack.retryHook.stashed, 0, '断开后立刻从暂存里出来');
    assert.equal(stack.scheduler.snapshot().queued, 0, '也不留在队列里');

    await sleep(100);
    assert.equal(up.state.hits, 1, '不会再往上游打');
  } finally {
    await stack.close();
  }
});

test('暂存上限：超过 maxStashed 的请求直接透传 429', async () => {
  const up = limitedUpstream(99);
  const stack = await makeStack(up.handler, {
    maxConcurrent: 4,
    retry: { maxRetries: 3, cooldownMs: 2000, maxStashed: 1 },
  });
  try {
    const all = await Promise.all([1, 2, 3].map(() => request(`${stack.gateUrl}/v1/messages`, { body: '{}' })));
    const passed = all.filter((r) => r.status === 429);
    assert.ok(passed.length >= 2, `暂存位只有 1 个，其余应透传，实际 ${all.map((r) => r.status)}`);
    assert.ok(stack.retryHook.snapshot().stats.passedThrough >= 2);
  } finally {
    await stack.close();
  }
});

test('暂存预算：maxHoldMs 用完后不再重试', async () => {
  const up = limitedUpstream(99);
  const stack = await makeStack(up.handler, {
    retry: { maxRetries: 10, cooldownMs: 100, maxHoldMs: 250 },
  });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, { body: '{}' });
    assert.equal(res.status, 429);
    // 100 + 100 = 200 还在预算内，再加 100 就超了 → 最多重试 2 次
    assert.equal(up.state.hits, 3, `应在预算用完时停手，实际打了 ${up.state.hits} 次`);
    assert.equal(stack.retryHook.snapshot().stats.passedThrough, 1);
  } finally {
    await stack.close();
  }
});

test('上游 Retry-After 超出暂存预算：不硬等，直接透传', async () => {
  // 上游要求等 7 秒，但预算只有 2 秒 → 不值得占着连接干等，让客户端自己退避
  const up = limitedUpstream(99, { retryAfter: '7' });
  const stack = await makeStack(up.handler, {
    retry: { maxRetries: 3, cooldownMs: 40, maxHoldMs: 2000 },
  });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, { body: '{}' });
    assert.equal(res.status, 429);
    assert.equal(res.headers['retry-after'], '7');
    assert.equal(up.state.hits, 1, '一次都不重试');
    assert.equal(stack.retryHook.snapshot().stats.retried, 0);
  } finally {
    await stack.close();
  }
});

test('状态接口：带上 retry 配置与实时暂存数', async () => {
  const up = limitedUpstream(0);
  const stack = await makeStack(up.handler, { retry: { mode: 'nextMinute', maxRetries: 4 } });
  try {
    const res = await request(`${stack.gateUrl}/__gate/status`, { method: 'GET' });
    const snap = JSON.parse(res.body);
    assert.equal(snap.retry.enabled, true);
    assert.equal(snap.retry.mode, 'nextMinute');
    assert.equal(snap.retry.maxRetries, 4);
    assert.deepEqual(snap.retry.statuses, [429, 529]);
    assert.equal(snap.retry.stashed, 0);
    assert.equal(snap.retry.nextReleaseInMs, 0);
    assert.equal(snap.queuedPriority, 0);
    assert.equal(up.state.hits, 0, '状态接口不转发上游');
  } finally {
    await stack.close();
  }
});

test('命名上游：/__gate/u/<名字>/... 转到对应上游，共用同一套限流', async () => {
  const other = await listen((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ from: 'other', path: req.url }));
  });
  const up = limitedUpstream(0);
  const stack = await makeStack(up.handler, { upstreams: { alt: other.url } });
  try {
    const res = await request(`${stack.gateUrl}/__gate/u/alt/v1/messages?x=1`, { body: '{}' });
    assert.equal(res.status, 200);
    const payload = JSON.parse(res.body);
    assert.equal(payload.from, 'other');
    assert.equal(payload.path, '/v1/messages?x=1', '前缀被剥掉，查询串保留');
    assert.equal(up.state.hits, 0, '没打到默认上游');
    assert.equal(stack.scheduler.snapshot().stats.admitted, 1, '命名上游也走同一个队列');

    const missing = await request(`${stack.gateUrl}/__gate/u/nope/v1/messages`, { body: '{}' });
    assert.equal(missing.status, 404);
    assert.ok(missing.body.includes('nope'));

    const dflt = await request(`${stack.gateUrl}/v1/messages`, { body: '{}' });
    assert.equal(dflt.status, 200);
    assert.equal(up.state.hits, 1, '不带前缀仍走默认上游');
  } finally {
    await stack.close();
    await other.close();
  }
});

test('不排队的旁路请求不受重试影响', async () => {
  const up = limitedUpstream(99);
  const stack = await makeStack(up.handler, { retry: { maxRetries: 3, cooldownMs: 40 } });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages/count_tokens`, { body: '{}' });
    assert.equal(res.status, 429, '旁路请求原样透传，不做暂存');
    assert.equal(up.state.hits, 1);
    assert.equal(stack.retryHook.snapshot().stats.retried, 0);
  } finally {
    await stack.close();
  }
});
